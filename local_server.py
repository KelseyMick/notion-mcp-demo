"""
local_server.py — Run the Notion MCP demo locally.

Wraps api/chat.py logic in a plain Flask server.
The tool functions and Anthropic loop are identical to production.

Usage:
    py local_server.py
Then open: http://localhost:5000
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'api'))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, send_from_directory

# Import the core logic from chat.py
from api.chat import _validate_input, _run_agent, MAX_REQUESTS_PER_DAY

app = Flask(__name__, static_folder='public')

# Simple in-memory rate limiter for local use
from collections import defaultdict
from datetime import date
_counts: dict = defaultdict(lambda: {"date": None, "count": 0})

def _local_rate_check(ip: str):
    today = date.today().isoformat()
    entry = _counts[ip]
    if entry["date"] != today:
        entry["date"] = today
        entry["count"] = 0
    entry["count"] += 1
    remaining = max(0, MAX_REQUESTS_PER_DAY - entry["count"])
    return entry["count"] <= MAX_REQUESTS_PER_DAY, remaining


@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


@app.route('/api/chat', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS':
        return '', 204

    body = request.get_json(silent=True) or {}

    try:
        message, session_id = _validate_input(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ip = request.remote_addr or 'local'
    allowed, remaining = _local_rate_check(ip)
    if not allowed:
        return jsonify({
            "error": f"Rate limit reached ({MAX_REQUESTS_PER_DAY}/day).",
            "retry_after": "tomorrow"
        }), 429

    if not os.environ.get('ANTHROPIC_API_KEY'):
        return jsonify({"error": "ANTHROPIC_API_KEY not set."}), 503

    try:
        response_text, notion_url = _run_agent(message)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "response":   response_text,
        "notion_url": notion_url,
        "remaining":  remaining,
    })


if __name__ == '__main__':
    print("Notion MCP demo running at http://localhost:5000")
    print("Press Ctrl+C to stop.\n")
    app.run(port=5000, debug=False)