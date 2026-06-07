"""
api/chat.py — Vercel Serverless Function

Handles POST /api/chat requests from the web UI.

Security controls (per DESIGN.md):
  - Rate limit: 50 requests/day/IP  (Vercel KV / Redis)
  - Input validation: max 500 chars, control-char strip
  - CORS: restricted to own origin
  - Secrets: env vars only, never returned to client
  - Error messages: generic to client

Flow:
  1. CORS preflight check
  2. Parse + validate request body
  3. Rate limit check
  4. Anthropic API call with tools defined inline
  5. Tool-use loop (create / delete / find note)
  6. Return final text + any Notion URL
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date
from http.server import BaseHTTPRequestHandler

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY      = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID  = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION      = "2022-06-28"
NOTION_BASE         = "https://api.notion.com/v1"

# Rate limit
MAX_REQUESTS_PER_DAY = 50

# Input
MAX_INPUT_LENGTH = 500

# ---------------------------------------------------------------------------
# Notion helpers  (mirrors server.py — single source of truth in a real repo
# would be a shared module, but Vercel functions are self-contained)
# ---------------------------------------------------------------------------
def _notion_headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }


def _search_pages(query: str) -> list[dict]:
    resp = httpx.post(
        f"{NOTION_BASE}/search",
        headers=_notion_headers(),
        json={
            "query":  query,
            "filter": {"value": "page", "property": "object"},
            "sort":   {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 10,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in prop["title"])
    return "(untitled)"


def _page_url(page: dict) -> str:
    return page.get("url", "")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _create_note(title: str, content: str) -> tuple[dict, str | None]:
    resp = httpx.post(
        f"{NOTION_BASE}/pages",
        headers=_notion_headers(),
        json={
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}
            },
            "children": [{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                },
            }],
        },
        timeout=10,
    )
    resp.raise_for_status()
    page = resp.json()
    url  = _page_url(page)
    return {"success": True, "message": f"Created note '{title}'.", "url": url}, url


def _delete_note(title: str) -> tuple[dict, None]:
    results = _search_pages(title)
    if not results:
        return {"error": f"No note found matching '{title}'."}, None
    page    = results[0]
    page_id = page["id"]
    found   = _page_title(page)
    resp = httpx.patch(
        f"{NOTION_BASE}/pages/{page_id}",
        headers=_notion_headers(),
        json={"archived": True},
        timeout=10,
    )
    resp.raise_for_status()
    return {"success": True, "message": f"Archived note '{found}'."}, None


def _find_note(query: str) -> tuple[dict, str | None]:
    results = _search_pages(query)
    if not results:
        return {"found": 0, "message": f"No notes found for '{query}'.", "notes": []}, None
    notes = [
        {"title": _page_title(p), "url": _page_url(p)}
        for p in results[:5]
    ]
    top_url = notes[0]["url"] if notes else None
    return {"found": len(notes), "notes": notes}, top_url


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS = [
    {
        "name": "create_note",
        "description": (
            "Create a new note (page) in the public Notion workspace. "
            "Use when the user asks to add, create, or write a note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string", "description": "Short descriptive title for the note."},
                "content": {"type": "string", "description": "Body text of the note."},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "delete_note",
        "description": (
            "Archive (delete) a note from the Notion workspace by title. "
            "Use when the user asks to delete, remove, or archive a note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title (or partial title) of the note to delete."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "find_note",
        "description": (
            "Search for notes in the Notion workspace. Returns titles and URLs. "
            "Use when the user asks to find, search, or look up a note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for."},
            },
            "required": ["query"],
        },
    },
]


def _dispatch_tool(name: str, inputs: dict) -> tuple[str, str | None]:
    """Call the right tool function and return (json_result, notion_url_or_None)."""
    try:
        if name == "create_note":
            result, url = _create_note(inputs["title"], inputs["content"])
        elif name == "delete_note":
            result, url = _delete_note(inputs["title"])
        elif name == "find_note":
            result, url = _find_note(inputs["query"])
        else:
            result, url = {"error": f"Unknown tool: {name}"}, None
    except httpx.HTTPStatusError as e:
        result = {"error": f"Notion API error {e.response.status_code}"}
        url = None
    except Exception as e:
        result = {"error": "Internal tool error."}
        url = None
    return json.dumps(result), url


# ---------------------------------------------------------------------------
# Rate limiter  (Vercel KV — falls back gracefully if KV not configured)
# ---------------------------------------------------------------------------
def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """
    Returns (is_allowed, remaining_requests).
    Uses Vercel KV REST API if KV_REST_API_URL is set, otherwise allows all.
    """
    kv_url   = os.environ.get("KV_REST_API_URL")
    kv_token = os.environ.get("KV_REST_API_TOKEN")
    if not kv_url or not kv_token:
        return True, MAX_REQUESTS_PER_DAY  # KV not configured — allow (dev mode)

    key = f"ratelimit:{ip}:{date.today().isoformat()}"
    headers = {"Authorization": f"Bearer {kv_token}"}

    try:
        # Atomically increment and set TTL
        incr_resp = httpx.post(f"{kv_url}/pipeline", headers=headers, json=[
            ["INCR", key],
            ["EXPIRE", key, 86400],
        ], timeout=3)
        incr_resp.raise_for_status()
        count = incr_resp.json()[0]["result"]
        remaining = max(0, MAX_REQUESTS_PER_DAY - count)
        return count <= MAX_REQUESTS_PER_DAY, remaining
    except Exception:
        return True, MAX_REQUESTS_PER_DAY  # Fail open — don't block on KV outage


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _sanitise(text: str) -> str:
    """Strip control characters (keep newlines/tabs). Trim whitespace."""
    cleaned = "".join(
        ch for ch in text
        if unicodedata.category(ch)[0] != "C" or ch in ("\n", "\t", "\r")
    )
    return cleaned.strip()


def _validate_input(body: dict) -> tuple[str | None, str | None]:
    """Returns (message, session_id) or raises ValueError."""
    message    = body.get("message", "")
    session_id = body.get("session_id", "anonymous")

    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required and must be a non-empty string.")
    if len(message) > MAX_INPUT_LENGTH:
        raise ValueError(f"message exceeds maximum length of {MAX_INPUT_LENGTH} characters.")

    message    = _sanitise(message)
    session_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(session_id))[:64]
    return message, session_id


# ---------------------------------------------------------------------------
# Anthropic tool-use loop
# ---------------------------------------------------------------------------
def _run_agent(message: str) -> tuple[str, str | None]:
    """
    Send message to Claude with tools. Handle tool_use blocks.
    Returns (final_text_response, notion_url_or_None).
    """
    messages  = [{"role": "user", "content": message}]
    notion_url = None

    for _ in range(5):  # Max 5 iterations to prevent runaway loops
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-5",
                "max_tokens": 1024,
                "system": (
                    "You are a helpful assistant managing a public Notion workspace. "
                    "You have three tools: create_note, delete_note, and find_note. "
                    "Always use a tool when the user's intent is to create, delete, or find a note. "
                    "After using a tool, confirm what you did in a friendly, concise sentence. "
                    "If find_note returns URLs, include them in your response."
                ),
                "tools":    TOOL_DEFINITIONS,
                "messages": messages,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data       = resp.json()
        stop_reason = data.get("stop_reason")
        content    = data.get("content", [])

        # Append assistant turn
        messages.append({"role": "assistant", "content": content})

        if stop_reason == "end_turn":
            # Extract final text
            text = " ".join(
                block["text"] for block in content if block.get("type") == "text"
            ).strip()
            return text or "Done.", notion_url

        if stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_name   = block["name"]
                    tool_input  = block["input"]
                    tool_id     = block["id"]
                    result_json, url = _dispatch_tool(tool_name, tool_input)
                    if url:
                        notion_url = url
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": tool_id,
                        "content":     result_json,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        break

    return "I wasn't able to complete that request. Please try again.", notion_url


# ---------------------------------------------------------------------------
# Flask app — Vercel uses this as the WSGI entrypoint
# ---------------------------------------------------------------------------
from flask import Flask, request as flask_request, jsonify

app = Flask(__name__)

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_html(path):
    import pathlib
    base = pathlib.Path(__file__).parent.parent
    for candidate in ["public/index.html", "index.html"]:
        html_path = base / candidate
        if html_path.exists():
            return html_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "Not found", 404

@app.route("/api/chat", methods=["POST", "OPTIONS"])
def chat():
    if flask_request.method == "OPTIONS":
        return "", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

    body = flask_request.get_json(silent=True) or {}

    try:
        message, session_id = _validate_input(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    client_ip = flask_request.headers.get("x-forwarded-for", "127.0.0.1").split(",")[0].strip()
    allowed, remaining = _check_rate_limit(client_ip)
    if not allowed:
        return jsonify({"error": f"Rate limit exceeded. {MAX_REQUESTS_PER_DAY}/day.", "retry_after": "tomorrow"}), 429

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "Service not configured."}), 503

    try:
        response_text, notion_url = _run_agent(message)
    except Exception:
        return jsonify({"error": "Internal server error."}), 500

    return jsonify({"response": response_text, "notion_url": notion_url, "remaining": remaining})

# Vercel WSGI entrypoint
handler = app