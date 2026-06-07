"""
api/chat.py — Vercel Serverless Function (Flask/WSGI)
"""
from __future__ import annotations
import json, os, re, unicodedata
from datetime import date
import httpx
from flask import Flask, request as freq, jsonify, Response

# ── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION     = "2022-06-28"
NOTION_BASE        = "https://api.notion.com/v1"
MAX_REQUESTS_PER_DAY = 50
MAX_INPUT_LENGTH     = 500

# ── Notion helpers ───────────────────────────────────────────────────────────
def _nh():
    return {"Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

def _search_pages(q):
    r = httpx.post(f"{NOTION_BASE}/search", headers=_nh(),
        json={"query": q, "filter": {"value": "page", "property": "object"},
              "sort": {"direction": "descending", "timestamp": "last_edited_time"},
              "page_size": 10}, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])

def _title(page):
    for p in page.get("properties", {}).values():
        if p.get("type") == "title":
            return "".join(x.get("plain_text","") for x in p["title"])
    return "(untitled)"

def _url(page): return page.get("url", "")

# ── Tools ────────────────────────────────────────────────────────────────────
def _create_note(title, content):
    r = httpx.post(f"{NOTION_BASE}/pages", headers=_nh(), json={
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {"title": {"title": [{"type":"text","text":{"content":title}}]}},
        "children": [{"object":"block","type":"paragraph",
                      "paragraph":{"rich_text":[{"type":"text","text":{"content":content}}]}}]
    }, timeout=10)
    r.raise_for_status()
    page = r.json()
    u = _url(page)
    return {"success": True, "message": f"Created '{title}'.", "url": u}, u

def _delete_note(title):
    results = _search_pages(title)
    if not results: return {"error": f"No note matching '{title}'."}, None
    page = results[0]
    r = httpx.patch(f"{NOTION_BASE}/pages/{page['id']}", headers=_nh(),
                    json={"archived": True}, timeout=10)
    r.raise_for_status()
    return {"success": True, "message": f"Deleted '{_title(page)}'."}, None

def _find_note(query):
    results = _search_pages(query)
    if not results: return {"found": 0, "notes": []}, None
    notes = [{"title": _title(p), "url": _url(p)} for p in results[:5]]
    return {"found": len(notes), "notes": notes}, notes[0]["url"]

TOOLS = [
    {"name":"create_note","description":"Create a new note in the Notion workspace.",
     "input_schema":{"type":"object","properties":{
         "title":{"type":"string"},"content":{"type":"string"}},"required":["title","content"]}},
    {"name":"delete_note","description":"Delete a note by title.",
     "input_schema":{"type":"object","properties":{
         "title":{"type":"string"}},"required":["title"]}},
    {"name":"find_note","description":"Search for notes, returns titles and URLs.",
     "input_schema":{"type":"object","properties":{
         "query":{"type":"string"}},"required":["query"]}},
]

def _dispatch(name, inputs):
    try:
        if name == "create_note": result, url = _create_note(inputs["title"], inputs["content"])
        elif name == "delete_note": result, url = _delete_note(inputs["title"])
        elif name == "find_note": result, url = _find_note(inputs["query"])
        else: result, url = {"error": f"Unknown tool: {name}"}, None
    except Exception as e:
        result, url = {"error": str(e)}, None
    return json.dumps(result), url

# ── Rate limit ───────────────────────────────────────────────────────────────
def _rate_check(ip):
    kv_url = os.environ.get("KV_REST_API_URL")
    kv_tok = os.environ.get("KV_REST_API_TOKEN")
    if not kv_url or not kv_tok:
        return True, MAX_REQUESTS_PER_DAY
    key = f"ratelimit:{ip}:{date.today().isoformat()}"
    try:
        r = httpx.post(f"{kv_url}/pipeline", headers={"Authorization": f"Bearer {kv_tok}"},
            json=[["INCR", key], ["EXPIRE", key, 86400]], timeout=3)
        r.raise_for_status()
        count = r.json()[0]["result"]
        return count <= MAX_REQUESTS_PER_DAY, max(0, MAX_REQUESTS_PER_DAY - count)
    except Exception:
        return True, MAX_REQUESTS_PER_DAY

# ── Validation ───────────────────────────────────────────────────────────────
def _sanitise(text):
    return "".join(c for c in text if unicodedata.category(c)[0]!="C" or c in "\n\t\r").strip()

def _validate(body):
    msg = body.get("message","")
    sid = body.get("session_id","anonymous")
    if not isinstance(msg,str) or not msg.strip(): raise ValueError("message required.")
    if len(msg) > MAX_INPUT_LENGTH: raise ValueError(f"Max {MAX_INPUT_LENGTH} chars.")
    return _sanitise(msg), re.sub(r"[^a-zA-Z0-9_-]","",str(sid))[:64]

# ── Agent loop ───────────────────────────────────────────────────────────────
def _run_agent(message):
    messages = [{"role":"user","content":message}]
    notion_url = None
    for _ in range(5):
        r = httpx.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
            "content-type": "application/json"},
            json={"model":"claude-sonnet-4-5","max_tokens":1024,
                  "system":"You manage a public Notion workspace. Use create_note, delete_note, or find_note tools when users ask to create, delete, or find notes. Confirm what you did concisely. Include URLs when find_note returns them.",
                  "tools":TOOLS,"messages":messages}, timeout=30)
        r.raise_for_status()
        data = r.json()
        stop = data.get("stop_reason")
        content = data.get("content",[])
        messages.append({"role":"assistant","content":content})
        if stop == "end_turn":
            text = " ".join(b["text"] for b in content if b.get("type")=="text").strip()
            return text or "Done.", notion_url
        if stop == "tool_use":
            tool_results = []
            for b in content:
                if b.get("type") == "tool_use":
                    res, url = _dispatch(b["name"], b["input"])
                    if url: notion_url = url
                    tool_results.append({"type":"tool_result","tool_use_id":b["id"],"content":res})
            messages.append({"role":"user","content":tool_results})
    return "Couldn't complete that. Please try again.", notion_url

# ── HTML (embedded — no file path needed on Vercel) ──────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Notion MCP Demo</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;1,9..144,300&display=swap" rel="stylesheet" />
<style>
/* ── Reset & base ─────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:          #0c0c0d;
  --surface:     #141416;
  --surface2:    #1c1c1f;
  --border:      #2a2a2e;
  --border-dim:  #1f1f23;
  --text:        #e8e6e1;
  --text-dim:    #7a7873;
  --text-faint:  #3d3c3a;
  --accent:      #c8a96e;
  --accent-dim:  #8a7048;
  --accent-glow: rgba(200, 169, 110, 0.08);
  --red:         #e05c5c;
  --green:       #6ab187;
  --notion:      #ffffff;
  --radius:      6px;
  --mono:        "DM Mono", monospace;
  --serif:       "Fraunces", serif;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

/* ── Layout ───────────────────────────────────────────────── */
.shell {
  display: grid;
  grid-template-rows: auto 1fr auto;
  height: 100vh;
  max-width: 760px;
  margin: 0 auto;
  padding: 0 20px;
}

/* ── Header ───────────────────────────────────────────────── */
header {
  padding: 36px 0 28px;
  border-bottom: 1px solid var(--border-dim);
  animation: fadeDown 0.6s ease both;
}

.header-top {
  display: flex;
  align-items: baseline;
  gap: 16px;
  margin-bottom: 8px;
}

.wordmark {
  font-family: var(--serif);
  font-size: 26px;
  font-weight: 300;
  font-style: italic;
  color: var(--text);
  letter-spacing: -0.02em;
}

.badge {
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent);
  background: var(--accent-glow);
  border: 1px solid var(--accent-dim);
  padding: 3px 8px;
  border-radius: 100px;
}

.header-sub {
  font-size: 12px;
  color: var(--text-dim);
  letter-spacing: 0.01em;
}

.header-sub a {
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid var(--accent-dim);
  transition: color 0.2s;
}
.header-sub a:hover { color: var(--text); }

/* ── Chat area ────────────────────────────────────────────── */
.chat {
  overflow-y: auto;
  padding: 28px 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}

.chat::-webkit-scrollbar { width: 4px; }
.chat::-webkit-scrollbar-track { background: transparent; }
.chat::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

/* Empty state */
.empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 32px;
  padding: 40px 0;
  animation: fadeUp 0.5s 0.2s ease both;
}

.empty-label {
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin-bottom: 14px;
}

.examples {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.example {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--surface);
  border: 1px solid var(--border-dim);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  text-align: left;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text-dim);
}

.example:hover {
  background: var(--surface2);
  border-color: var(--border);
  color: var(--text);
}

.example-icon {
  font-size: 16px;
  flex-shrink: 0;
}

/* Messages */
.msg {
  display: flex;
  flex-direction: column;
  gap: 4px;
  animation: fadeUp 0.3s ease both;
}

.msg-label {
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-faint);
  padding-left: 2px;
}

.msg-bubble {
  padding: 14px 18px;
  border-radius: var(--radius);
  font-size: 13.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg.user .msg-bubble {
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--text);
  align-self: flex-end;
  max-width: 85%;
}

.msg.assistant .msg-bubble {
  background: var(--surface);
  border: 1px solid var(--border-dim);
  color: var(--text);
}

.msg.error .msg-bubble {
  background: rgba(224, 92, 92, 0.06);
  border: 1px solid rgba(224, 92, 92, 0.2);
  color: var(--red);
}

/* Notion link chip */
.notion-link {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 8px 12px;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: var(--radius);
  text-decoration: none;
  color: var(--text);
  font-size: 12px;
  font-weight: 500;
  transition: background 0.15s, border-color 0.15s;
}

.notion-link:hover {
  background: rgba(255,255,255,0.08);
  border-color: rgba(255,255,255,0.18);
}

.notion-icon {
  width: 16px;
  height: 16px;
  border-radius: 3px;
  background: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.notion-icon svg { display: block; }

/* Typing indicator */
.typing {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 14px 18px;
  background: var(--surface);
  border: 1px solid var(--border-dim);
  border-radius: var(--radius);
  width: fit-content;
}

.dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--text-dim);
  animation: pulse 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }

/* ── Input area ───────────────────────────────────────────── */
.input-area {
  padding: 20px 0 28px;
  border-top: 1px solid var(--border-dim);
  animation: fadeUp 0.5s 0.3s ease both;
}

.input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.input-wrap {
  flex: 1;
  position: relative;
}

textarea {
  width: 100%;
  min-height: 48px;
  max-height: 160px;
  padding: 13px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13.5px;
  line-height: 1.5;
  resize: none;
  outline: none;
  transition: border-color 0.15s;
  overflow-y: auto;
}

textarea::placeholder { color: var(--text-faint); }
textarea:focus { border-color: var(--accent-dim); }

.send-btn {
  width: 48px;
  height: 48px;
  flex-shrink: 0;
  background: var(--accent);
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, transform 0.1s;
  color: #0c0c0d;
}

.send-btn:hover { background: #d4b87a; }
.send-btn:active { transform: scale(0.96); }
.send-btn:disabled { background: var(--border); cursor: not-allowed; }

.input-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-faint);
}

.rate-counter { letter-spacing: 0.04em; }
.rate-counter.warn { color: var(--red); }

/* ── Animations ───────────────────────────────────────────── */
@keyframes fadeDown {
  from { opacity: 0; transform: translateY(-10px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes pulse {
  0%, 80%, 100% { opacity: 0.3; transform: scale(0.85); }
  40%           { opacity: 1;   transform: scale(1); }
}

/* ── Responsive ───────────────────────────────────────────── */
@media (max-width: 520px) {
  .shell { padding: 0 16px; }
  .wordmark { font-size: 22px; }
  .msg.user .msg-bubble { max-width: 100%; }
}
</style>
</head>
<body>

<div class="shell">

  <!-- Header -->
  <header>
    <div class="header-top">
      <span class="wordmark">notion&nbsp;mcp</span>
      <span class="badge">Live Demo</span>
    </div>
    <p class="header-sub">
      Claude + MCP → Notion. Type a command, watch it appear in the
      <a href="https://linen-pewter-aa7.notion.site/378262e1a920801a9d3ee6f616bcd17b?v=378262e1a92080d2a7c4000c6c4d5e7a" target="_blank" id="notion-workspace-link">public workspace ↗</a>
    </p>
  </header>

  <!-- Chat -->
  <div class="chat" id="chat">
    <div class="empty" id="empty">
      <div>
        <p class="empty-label">Try one of these</p>
        <div class="examples">
          <button class="example" onclick="fillInput(this)">
            <span class="example-icon">✏️</span>
            <span>Create a note called "Project Ideas" with a list of startup ideas</span>
          </button>
          <button class="example" onclick="fillInput(this)">
            <span class="example-icon">🔍</span>
            <span>Find any notes about meetings</span>
          </button>
          <button class="example" onclick="fillInput(this)">
            <span class="example-icon">🗑️</span>
            <span>Delete the note called "Project Ideas"</span>
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- Input -->
  <div class="input-area">
    <div class="input-row">
      <div class="input-wrap">
        <textarea
          id="input"
          placeholder="Create a note, find a note, delete a note…"
          rows="1"
          maxlength="500"
        ></textarea>
      </div>
      <button class="send-btn" id="send-btn" onclick="send()" title="Send">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <path d="M2 16L16 9L2 2V7.5L12 9L2 10.5V16Z" fill="currentColor"/>
        </svg>
      </button>
    </div>
    <div class="input-meta">
      <span class="rate-counter" id="rate-counter">50 messages remaining today</span>
      <span>↵ to send · shift+↵ for newline</span>
    </div>
  </div>

</div>

<script>
// ── State ──────────────────────────────────────────────────
const SESSION_ID = Math.random().toString(36).slice(2);
let remaining = 50;
let busy = false;

// ── DOM refs ───────────────────────────────────────────────
const chatEl   = document.getElementById('chat');
const inputEl  = document.getElementById('input');
const sendBtn  = document.getElementById('send-btn');
const rateEl   = document.getElementById('rate-counter');
const emptyEl  = document.getElementById('empty');

// ── Auto-resize textarea ───────────────────────────────────
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
});

// ── Enter to send ──────────────────────────────────────────
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

// ── Example prompts ────────────────────────────────────────
function fillInput(btn) {
  const text = btn.querySelector('span:last-child').textContent;
  inputEl.value = text;
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
  inputEl.focus();
}

// ── Add message to chat ────────────────────────────────────
function addMessage(role, text, notionUrl) {
  // Remove empty state on first message
  if (emptyEl) emptyEl.remove();

  const msg = document.createElement('div');
  msg.className = `msg ${role}`;

  const label = document.createElement('div');
  label.className = 'msg-label';
  label.textContent = role === 'user' ? 'You' : role === 'assistant' ? 'Claude' : 'Error';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = text;

  msg.appendChild(label);
  msg.appendChild(bubble);

  // Notion link chip
  if (notionUrl) {
    const link = document.createElement('a');
    link.href   = notionUrl;
    link.target = '_blank';
    link.className = 'notion-link';
    link.innerHTML = `
      <span class="notion-icon">
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
          <rect width="10" height="10" rx="2" fill="#000"/>
          <text x="2" y="8" font-size="7" font-weight="700" fill="#fff" font-family="serif">N</text>
        </svg>
      </span>
      Open in Notion ↗
    `;
    bubble.appendChild(document.createElement('br'));
    bubble.appendChild(link);
  }

  chatEl.appendChild(msg);
  chatEl.scrollTop = chatEl.scrollHeight;
  return msg;
}

// ── Typing indicator ───────────────────────────────────────
function showTyping() {
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.id = 'typing';
  wrap.innerHTML = `
    <div class="msg-label">Claude</div>
    <div class="typing">
      <div class="dot"></div>
      <div class="dot"></div>
      <div class="dot"></div>
    </div>
  `;
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

// ── Update rate counter ────────────────────────────────────
function updateRate(n) {
  remaining = n;
  rateEl.textContent = `${n} message${n !== 1 ? 's' : ''} remaining today`;
  rateEl.className = 'rate-counter' + (n <= 5 ? ' warn' : '');
}

// ── Send ───────────────────────────────────────────────────
async function send() {
  const text = inputEl.value.trim();
  if (!text || busy) return;

  busy = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  inputEl.style.height = 'auto';

  addMessage('user', text);
  showTyping();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: SESSION_ID }),
    });

    const data = await res.json();
    removeTyping();

    if (!res.ok) {
      const errMsg = res.status === 429
        ? `Rate limit reached. You can send ${50} messages per day. Come back tomorrow!`
        : (data.error || 'Something went wrong. Please try again.');
      addMessage('error', errMsg);
    } else {
      addMessage('assistant', data.response, data.notion_url);
      if (typeof data.remaining === 'number') updateRate(data.remaining);
    }

  } catch (err) {
    removeTyping();
    addMessage('error', 'Network error. Check your connection and try again.');
  } finally {
    busy = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }
}
</script>
</body>
</html>
"""

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/chat", methods=["POST","OPTIONS"])
def chat():
    if freq.method == "OPTIONS":
        return "", 204, {"Access-Control-Allow-Origin":"*",
                         "Access-Control-Allow-Methods":"POST,OPTIONS",
                         "Access-Control-Allow-Headers":"Content-Type"}
    body = freq.get_json(silent=True) or {}
    try:
        message, _ = _validate(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    ip = freq.headers.get("x-forwarded-for","127.0.0.1").split(",")[0].strip()
    allowed, remaining = _rate_check(ip)
    if not allowed:
        return jsonify({"error":f"Rate limit: {MAX_REQUESTS_PER_DAY}/day.","retry_after":"tomorrow"}), 429
    if not ANTHROPIC_API_KEY:
        return jsonify({"error":"Not configured."}), 503
    try:
        text, notion_url = _run_agent(message)
    except Exception:
        return jsonify({"error":"Server error."}), 500
    return jsonify({"response":text,"notion_url":notion_url,"remaining":remaining})

handler = app