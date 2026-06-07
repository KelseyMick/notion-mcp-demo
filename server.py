"""
server.py — Notion MCP Server

Canonical tool definitions for the Notion demo.

Usage (local / Claude Desktop):
    py server.py

Environment variables required:
    NOTION_API_KEY       — Notion integration secret
    NOTION_DATABASE_ID   — ID of the database to write to

Tools:
    create_note   — Add a new page to the Notion workspace
    delete_note   — Archive (soft-delete) a page by title or ID
    find_note     — Search pages by keyword, return title + public URL
"""

import os
import sys
import json
import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NOTION_API_KEY      = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID  = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION      = "2022-06-28"
NOTION_BASE         = "https://api.notion.com/v1"

mcp = FastMCP(
    "notion-demo",
    instructions=(
        "You help users manage a public Notion workspace. "
        "You can create notes, delete notes, and find notes. "
        "Always confirm what you did and provide the Notion URL when available."
    ),
)

# ---------------------------------------------------------------------------
# Shared Notion HTTP helpers
# ---------------------------------------------------------------------------
def _notion_headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }


def _search_pages(query: str) -> list[dict]:
    """Return matching Notion pages for a query string."""
    resp = httpx.post(
        f"{NOTION_BASE}/search",
        headers=_notion_headers(),
        json={
            "query": query,
            "filter": {"value": "page", "property": "object"},
            "sort":   {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 10,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _page_title(page: dict) -> str:
    """Extract plain-text title from a Notion page object."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop["title"]
            return "".join(p.get("plain_text", "") for p in parts)
    return "(untitled)"


def _page_url(page: dict) -> str:
    return page.get("url", "")


# ---------------------------------------------------------------------------
# Tool 1 — create_note
# ---------------------------------------------------------------------------
@mcp.tool()
def create_note(title: str, content: str) -> str:
    """
    Create a new note (Notion page) in the shared workspace.

    Args:
        title:   The title of the note. Keep it concise and descriptive.
        content: The body text of the note.

    Returns a confirmation message and the URL of the new Notion page.

    Notion API: POST /v1/pages
    """
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        return json.dumps({"error": "Server not configured - NOTION_API_KEY or NOTION_DATABASE_ID missing."})

    try:
        resp = httpx.post(
            f"{NOTION_BASE}/pages",
            headers=_notion_headers(),
            json={
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": {
                    "title": {
                        "title": [{"type": "text", "text": {"content": title}}]
                    }
                },
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": content}}]
                        },
                    }
                ],
            },
            timeout=10,
        )
        resp.raise_for_status()
        page = resp.json()
        url  = _page_url(page)
        return json.dumps({
            "success": True,
            "message": f"Created note '{title}'.",
            "url": url,
            "page_id": page["id"],
        })
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Notion API error {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool 2 — delete_note
# ---------------------------------------------------------------------------
@mcp.tool()
def delete_note(title: str) -> str:
    """
    Archive (delete) a note from the Notion workspace by its title.

    Notion does not permanently delete pages via API — it archives them,
    which removes them from the workspace view. This is the standard
    Notion API behaviour and is equivalent to deletion for demo purposes.

    Args:
        title: The title (or partial title) of the note to delete.
               If multiple pages match, the most recently edited one is archived.

    Returns a confirmation message.

    Notion API: POST /v1/search  →  PATCH /v1/pages/{id}
    """
    if not NOTION_API_KEY:
        return json.dumps({"error": "Server not configured."})

    try:
        results = _search_pages(title)
        if not results:
            return json.dumps({"error": f"No note found matching '{title}'."})

        # Take the first (most recent) match
        page    = results[0]
        page_id = page["id"]
        found_title = _page_title(page)

        resp = httpx.patch(
            f"{NOTION_BASE}/pages/{page_id}",
            headers=_notion_headers(),
            json={"archived": True},
            timeout=10,
        )
        resp.raise_for_status()
        return json.dumps({
            "success": True,
            "message": f"Archived note '{found_title}'.",
            "page_id": page_id,
        })
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Notion API error {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool 3 — find_note
# ---------------------------------------------------------------------------
@mcp.tool()
def find_note(query: str) -> str:
    """
    Search for notes in the Notion workspace by keyword.

    Returns up to 5 matching pages with their titles and public URLs.
    Use this when the user asks to find, search for, or look up a note.

    Args:
        query: A keyword or phrase to search for in page titles and content.

    Returns a list of matching notes with titles and URLs.

    Notion API: POST /v1/search
    """
    if not NOTION_API_KEY:
        return json.dumps({"error": "Server not configured."})

    try:
        results = _search_pages(query)
        if not results:
            return json.dumps({"found": 0, "message": f"No notes found matching '{query}'.", "notes": []})

        notes = [
            {"title": _page_title(p), "url": _page_url(p), "page_id": p["id"]}
            for p in results[:5]
        ]
        return json.dumps({"found": len(notes), "notes": notes})
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Notion API error {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    configured = bool(NOTION_API_KEY and NOTION_DATABASE_ID)
    print(
        f"notion-mcp starting "
        f"({'configured' if configured else 'WARNING: env vars missing — set NOTION_API_KEY and NOTION_DATABASE_ID'})",
        file=sys.stderr,
    )
    mcp.run()
