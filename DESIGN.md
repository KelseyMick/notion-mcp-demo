# Design Document — Notion MCP Demo

**Version:** 1.0  
**Status:** Approved  
**Stack:** Python (FastMCP) · Vercel · Notion API · Claude API

---

## 1. Problem Statement

Employers evaluating AI engineering candidates need to see _agentic_ work, not
just code, but a system where an LLM takes real action in the world. This
project demonstrates a complete MCP (Model Context Protocol) integration:
a user talks to Claude in a web UI, Claude calls tools on a hosted MCP server,
and those actions appear immediately in a public Notion workspace.

**Success metric:** An employer visits one URL, types a natural-language
instruction, and sees a Notion page created, found, or deleted — with zero
setup on their end.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Browser                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │              Web UI  (index.html)                   │   │
│   │   Chat input -> fetch -> /api/chat                  │   │
│   └────────────────────┬────────────────────────────────┘   │
└────────────────────────│────────────────────────────────────┘
                         │ HTTPS POST /api/chat
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  Vercel (api/chat.py)                       │
│                                                             │
│   1. Rate limit check  (50 req/day per IP, Redis/KV)        │
│   2. Input validation  (max length, sanitise)               │
│   3. Call Anthropic API with tools defined inline           │
│   4. Handle tool_use blocks -> dispatch to tool functions   │
│   5. Stream final text response back                        │
└───────────────┬──────────────────────┬──────────────────────┘
                │                      │
                ▼                      ▼
     Anthropic Claude API        Notion API
     (claude-sonnet-4)           (pages/databases)
                                        │
                                        ▼
                               Public Notion Workspace
```

**Why not a separate FastMCP server on Vercel?**  
Vercel Serverless Functions are stateless and HTTP-only. FastMCP's STDIO
transport doesn't map cleanly to that model. The pragmatic production
pattern is to define tools as Python functions called directly from the
Vercel function — same tool logic, same docstrings, no STDIO friction.
The MCP server (server.py) remains the canonical definition and can be
run locally or on a persistent host (Railway/Fly) for Claude Desktop use.

---

## 3. Components

### 3.1 MCP Server (`server/server.py`)

Canonical tool definitions using FastMCP. Runnable locally and connectable
via Claude Desktop. Also deployable to Railway/Fly.io for remote STDIO/SSE use.

**Tools exposed:**

| Tool          | Description                                     | Notion API call        |
| ------------- | ----------------------------------------------- | ---------------------- |
| `create_note` | Add a new page to the workspace                 | `POST /v1/pages`       |
| `delete_note` | Archive (soft-delete) a page by title or ID     | `PATCH /v1/pages/{id}` |
| `find_note`   | Search for pages by keyword, return title + URL | `POST /v1/search`      |

### 3.2 Vercel API Route (`api/chat.py`)

Serverless function. Receives `{message, session_id}`, enforces rate limit,
calls Anthropic with tools defined inline, handles multi-turn tool_use loop,
returns `{response, notion_url}`.

### 3.3 Web UI (`web/index.html`)

Single-file chat interface. No framework, no build step — pure HTML/CSS/JS.
Deployed as Vercel static asset.

### 3.4 Rate Limiter

Uses Vercel KV (Redis). Key: `ratelimit:{ip}:{date}`. Value: request count.
TTL: 86400s (resets daily). Hard limit: 50 requests/day/IP.
Returns HTTP 429 with `Retry-After` header on breach.

---

## 4. Security

| Control           | Implementation                      | Standard        |
| ----------------- | ----------------------------------- | --------------- |
| Rate limiting     | 50 req/day/IP via Vercel KV         | OWASP API4      |
| Input validation  | Max 500 chars, strip control chars  | OWASP API1      |
| Secret management | Env vars only, never in code        | 12-Factor App   |
| CORS              | Restricted to own Vercel domain     | OWASP API7      |
| Notion scope      | Integration scoped to one DB only   | Least privilege |
| No auth exposure  | API keys server-side only           | OWASP API2      |
| Error messages    | Generic to client, detailed in logs | OWASP API3      |

---

## 5. Data Flow — Create Note (detailed)

```
1.  User types: "Add a note called Meeting Summary with content: discussed Q3 roadmap"
2.  Browser POST /api/chat  {message: "...", session_id: "abc123"}
3.  api/chat.py checks rate limit -> OK (count: 3/50)
4.  api/chat.py validates input -> OK
5.  Calls Anthropic API:
      model: claude-sonnet-4
      tools: [create_note, delete_note, find_note]
      messages: [{role: user, content: "Add a note..."}]
6.  Claude responds with tool_use block:
      {name: "create_note", input: {title: "Meeting Summary", content: "discussed Q3 roadmap"}}
7.  api/chat.py calls create_note() -> POST Notion API -> returns page URL
8.  Appends tool_result to messages, re-calls Anthropic
9.  Claude returns text: "Done! I've created 'Meeting Summary' in your Notion workspace."
10. api/chat.py returns {response: "Done!...", notion_url: "https://notion.so/..."}
11. Browser renders response + clickable Notion link
```

---

## 6. Deployment Steps

1. Create Notion integration, share one database with it
2. Get Anthropic API key
3. Fork/clone repo, push to GitHub
4. Import to Vercel, set environment variables:
   - `ANTHROPIC_API_KEY`
   - `NOTION_API_KEY`
   - `NOTION_DATABASE_ID`
5. Enable Vercel KV (free tier) -> env vars auto-populated
6. Deploy -> share URL

---

## 7. What Is and Is Not In Scope

**In scope (built):**

- create_note tool + Notion API integration
- delete_note tool (Notion archive)
- find_note tool (Notion search + URL return)
- Rate limiter (50/day/IP)
- Input validation and sanitisation
- CORS restriction
- Web chat UI
- MCP server (local/Claude Desktop use)
- This design document

**Out of scope (future):**

- User authentication
- Per-user Notion workspaces
- Edit/update existing notes
- File attachments
- Persistent conversation history across sessions
