# notion-mcp-demo

A live demo of MCP (Model Context Protocol) in action. Talk to Claude in a
web UI — it creates, finds, and deletes notes in a real public Notion workspace.

**Live demo:** `https://your-app.vercel.app`  
**Public Notion:** `https://notion.so/your-workspace`

---

## How it works

```
You type -> Claude decides which tool to call -> MCP server hits Notion API -> Note appears
```

Three tools:

- `create_note` — adds a new page to Notion
- `delete_note` — archives a page by title
- `find_note` — searches and returns page URLs

---

## Deployment (step by step)

### Step 1 — Create a Notion integration

1. Go to **https://www.notion.so/my-integrations**
2. Click **New integration**
3. Name it `mcp-demo`, select your workspace, click Save
4. Copy the **Internal Integration Secret** — this is your `NOTION_API_KEY`

### Step 2 — Set up your Notion database

1. In Notion, create a new **full-page database** (type `/database` -> Table)
2. Name it something like `MCP Demo Notes`
3. Click the 3-dots menu -> **Add connections** -> select `mcp-demo`
4. Copy the database ID from the URL:
   ```
   https://notion.so/yourworkspace/DATABASE_ID_HERE?v=...
   ```
   The ID is the 32-character string before the `?`
5. Make the page **public** (Share -> Publish to web) so employers can see it

### Step 3 — Get an Anthropic API key

1. Go to **https://console.anthropic.com**
2. API Keys -> Create key -> copy it
3. **Important:** Go to Billing -> Usage limits -> set a **$5 monthly hard cap**
   This is your financial protection. The demo costs ~$0.003 per message.

### Step 4 — Deploy to Vercel

1. Push this repo to GitHub (create a new repo, push all files)
2. Go to **https://vercel.com** -> Add New Project -> Import your repo
3. Vercel auto-detects the config. Before deploying, click **Environment Variables** and add:

   | Name                 | Value                                                      |
   | -------------------- | ---------------------------------------------------------- |
   | `ANTHROPIC_API_KEY`  | your Anthropic key                                         |
   | `NOTION_API_KEY`     | your Notion integration secret                             |
   | `NOTION_DATABASE_ID` | the 32-char database ID from Step 2                        |
   | `ALLOWED_ORIGIN`     | `https://your-app.vercel.app` (fill in after first deploy) |

4. Click **Deploy**

### Step 5 — Enable Vercel KV (rate limiter)

1. In your Vercel project dashboard -> **Storage** tab -> **Create Database** -> **KV**
2. Choose the free tier -> Create
3. Click **Connect to Project** -> the KV env vars are automatically added
4. Redeploy (Deployments -> 3-dots menu -> Redeploy)

The rate limiter now enforces 50 requests/day/IP. Without KV it still works —
it just won't rate-limit (fine for local testing).

### Step 6 — Update the Notion link in the UI

In `web/index.html`, find this line and replace with your actual Notion URL:

```html
<a href="NOTION_PUBLIC_URL" ...>public workspace ↗</a>
```

Commit and push — Vercel auto-redeploys.

---

## Local development

```bash
# 1. Clone and install
git clone https://github.com/yourhandle/notion-mcp-demo
cd notion-mcp-demo
pip install httpx mcp mcp[cli]

# 2. Set env vars
set NOTION_API_KEY=your_key
set NOTION_DATABASE_ID=your_db_id
set ANTHROPIC_API_KEY=your_key   # only needed for web UI

# 3. Run MCP server (Claude Desktop)
py server/server.py

# 4. For web UI local testing, use Vercel CLI:
npm i -g vercel
vercel dev
```

### Claude Desktop config (local MCP)

```json
{
  "mcpServers": {
    "notion-demo": {
      "command": "py",
      "args": ["C:\\path\\to\\notion-mcp-demo\\server\\server.py"],
      "env": {
        "NOTION_API_KEY": "your_key",
        "NOTION_DATABASE_ID": "your_db_id"
      }
    }
  }
}
```

---

## Project structure

```
notion-mcp-demo/
├── DESIGN.md          ← Architecture + design decisions
├── README.md          ← This file (deployment guide)
├── vercel.json        ← Vercel routing + security headers
├── requirements.txt   ← Python deps for Vercel runtime
├── api/
│   └── chat.py        ← Serverless function: rate limit + Anthropic loop + Notion calls
├── server/
│   └── server.py      ← FastMCP server for local / Claude Desktop use
└── web/
    └── index.html     ← Chat UI (single file, no build step)
```

---

## Security controls

| Control          | Detail                                                |
| ---------------- | ----------------------------------------------------- |
| Rate limiting    | 50 req/day/IP via Vercel KV (Redis INCR + TTL)        |
| Input validation | Max 500 chars, control-char sanitisation              |
| Secret isolation | All API keys are server-side env vars only            |
| CORS             | Locked to your Vercel domain via `ALLOWED_ORIGIN`     |
| Notion scope     | Integration limited to one database (least privilege) |
| Security headers | nosniff, no-frame, XSS protection, referrer policy    |
| Spending cap     | $5/month hard cap on Anthropic console                |

---

## What this demonstrates

- **MCP tool design** — three tools with clean input schemas and docstrings
- **Agentic loop** — multi-turn tool_use handling (Claude decides what to call)
- **Production security** — rate limiting, input validation, secret management
- **Real API integration** — live Notion workspace, not mocked
- **Deployable** — one URL, zero setup for the person trying it

---

## License

MIT
