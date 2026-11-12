"""Local web UI server for SocialPulse NLQ Phase 2.

Standard library only: no Streamlit, no CDN, no external JavaScript.
Run: python scripts/app.py [--port 8000] [--data data/phase1]
Then open http://localhost:8000 in Codespace (port forwarding) or locally.
"""
import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from urllib.parse import parse_qs, urlparse

from socialpulse_nlq.planner import plan, run_plan, build_trace, metric_definition, token_meter, GALLERY_SCENARIOS
from socialpulse_nlq.sqlviz import build_visualization
from socialpulse_nlq.weather import compute_weather
from socialpulse_nlq.chat import ChatSession
from socialpulse_nlq.questions import load_all_questions, save_questions
from socialpulse_nlq.usage import client_ip, record_event, usage_snapshot
from socialpulse_nlq.llm_planner import (
    MAX_OUTPUT_TOKENS, MODEL_ID, PROMPT_VERSION,
    UrllibGeminiTransport, build_routing_prompt_template,
)
import uuid

CHAT_SESSIONS = {}

# Phase 0: health payload is static process state, NOT a data check.
# /api/health answers from this dict alone and never opens DuckDB, so it
# stays fast even while the 500K data is still cold. It is a liveness
# check ("is the server up?"), not a data-integrity check ("is the data
# right?") — that is what the validation suite is for.
HEALTH_PAYLOAD = {
    "status": "ok",
    "service": "socialpulse-nlq",
    "users": 500000,
    "seed": 42,
    "window": "2026-01-01..2026-03-31",
}


def parse_experiments():
    text = (ROOT / "catalog/experiments.yaml").read_text()
    out = []
    for block in re.split(r"(?m)^\s*-\s*id:\s*", text):
        if "name:" not in block:
            continue
        def get(key):
            m = re.search(rf"(?m)^\s*{key}:\s*(.+)$", block)
            return m.group(1).strip().strip('"') if m else ""
        out.append({
            "id": block.split("\n")[0].strip(),
            "name": get("name"), "app": get("app"), "status": get("status"),
            "start_date": get("start_date"), "end_date": get("end_date"),
            "primary_metric": get("primary_metric"),
        })
    return out


# Chunk B, slice 2 — owner-key LLM wiring. The server reads
# GEMINI_API_KEY from the environment (set by the repo owner as a
# Render env var, never committed) and hands it to each ChatSession.
# The session's variant stays "baseline": the deterministic turns keep
# planning deterministically, and the key is used ONLY by the LLM
# fallback turn for otherwise-unmapped questions. No key -> the demo
# behaves exactly as before (deterministic-only). The key is never
# logged, never returned in any response, and never written to a
# trace; request logging is a no-op (log_message below).
def _llm_config():
    key = os.environ.get("GEMINI_API_KEY") or None
    tier = os.environ.get("GEMINI_TIER", "free")
    if tier not in ("free", "paid"):
        tier = "free"
    return key, tier


def _new_chat_session(data_dir):
    api_key, tier = _llm_config()
    return ChatSession(data_dir, llm_api_key=api_key, llm_tier=tier)


def _shell_llm_status():
    """Honest shell engine status: whether a server-side LLM key is
    configured. No key material, ever — only the fact of it."""
    api_key, _tier = _llm_config()
    return {"configured": bool(api_key),
            "model": MODEL_ID if api_key else None,
            "mode": "owner-key" if api_key else "none",
            "prompt_version": PROMPT_VERSION if api_key else None}


class Handler(BaseHTTPRequestHandler):
    data_dir = ROOT / "data/phase1"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # Phase 0: strip the query string before route matching. Shared
        # links carry tracking parameters (/?v=..., /shell?utm_source=...)
        # and the old exact-match on self.path returned "not found" for
        # all of them. The original path is kept for /api/weather below,
        # which reads its own query. Unknown paths still 404.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (ROOT / "app/index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            # Usage tracking: one page_load line per page served. The
            # log carries country + salted daily hash only — never the
            # raw IP (see socialpulse_nlq/usage.py, docs/usage-tracking.md).
            record_event("page_load",
                         client_ip(self.headers, self.client_address[0]))
        elif path in ("/shell", "/shell.html"):
            # Unified Muse-style shell (slice 1): a NEW page only. The
            # existing "/" dashboard + docked chat routes are untouched,
            # so the live demo keeps working while the shell is tested.
            html = (ROOT / "app/shell.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            record_event("page_load",
                         client_ip(self.headers, self.client_address[0]))
        elif path == "/api/health":
            # Liveness only: static payload, no DuckDB, no data dir.
            self._send_json(HEALTH_PAYLOAD)
        elif path == "/api/usage":
            # Live usage counters since this process started. Counts
            # only — no IPs, no hashes, no per-visitor rows. Resets
            # when the instance sleeps/restarts (Render free tier).
            self._send_json(usage_snapshot())
        elif path == "/api/shell-config":
            # Copilot backend origin for the shell. Env override (used in
            # deployment) wins; empty means the shell falls back to its
            # location heuristic (localhost -> :8001, else Render URL).
            # "llm" is the honest engine status for the Analytics side:
            # whether a server-side key is configured (never the key).
            self._send_json({"copilot_base": os.environ.get("COPILOT_BASE_URL", ""),
                             "llm": _shell_llm_status()})
        elif path == "/api/llm-routing-prompt":
            # Visitor-BYOK routing prompt (chunk B, slice 2): the pinned
            # prompt with {question} and {context} left for the browser
            # to fill. No key is involved in this endpoint — the browser
            # calls the provider directly with the visitor's own key.
            # The generation params are the pinned ones, so the browser
            # uses exactly the contract the server-side path uses.
            prompt, prompt_sha, inventory_sha = build_routing_prompt_template()
            self._send_json({
                "model": MODEL_ID, "prompt_version": PROMPT_VERSION,
                "prompt_sha256": prompt_sha,
                "inventory_sha256": inventory_sha,
                "prompt": prompt,
                "generation": {"temperature": 0,
                               "maxOutputTokens": MAX_OUTPUT_TOKENS,
                               "responseMimeType": "application/json"},
                "endpoint": UrllibGeminiTransport().url,
            })
        elif path == "/api/gallery":
            self._send_json({"scenarios": GALLERY_SCENARIOS})
        elif path == "/api/metrics":
            text = (ROOT / "catalog/measures.yaml").read_text()
            ids = re.findall(r"-\s*id:\s*(\S+)", text)
            self._send_json({"metrics": ids})
        elif path == "/api/experiments":
            self._send_json({"experiments": parse_experiments()})
        elif path == "/api/questions":
            # Both-in-one editor read: flow (chat/questions.json) +
            # single-shot evals + conversation evals, from their files.
            self._send_json(load_all_questions(ROOT))
        elif path == "/api/weather":
            try:
                # Query is read from the ORIGINAL path (parse_qs above
                # the stripped path), so /api/weather?app=instagram
                # still works exactly as before the routing fix.
                app = parse_qs(urlparse(self.path).query).get("app", [None])[0]
                self._send_json(compute_weather(self.data_dir, app=app))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/chat":
            # Phase 3: one ChatSession per client session id, kept in
            # memory on this server process. Restarting app.py resets
            # chat history — that is intentional for a local demo.
            # Chunk B, slice 2: the session is built with the owner key
            # when one is configured (see _new_chat_session); without
            # one it is exactly today's deterministic session.
            sid = body.get("session_id") or str(uuid.uuid4())
            if sid not in CHAT_SESSIONS:
                CHAT_SESSIONS[sid] = _new_chat_session(self.data_dir)
                # Usage tracking: a new chat session starts. Country +
                # salted daily hash only — never the raw IP.
                record_event("session_start",
                             client_ip(self.headers, self.client_address[0]))
            if "llm_choice" in body:
                # Visitor BYOK: the browser called the provider directly
                # with the visitor's key and posts the parsed choice.
                # The key itself is never in this request — if it were,
                # that would be a bug, not a feature.
                reply = CHAT_SESSIONS[sid].respond_with_llm_choice(
                    str(body.get("message", "")), body.get("llm_choice"),
                    body.get("llm_tokens"))
            else:
                reply = CHAT_SESSIONS[sid].respond(str(body.get("message", "")))
            reply = dict(reply)
            reply["session_id"] = sid
            # Weather/drill-down evidence can be large; the UI renders
            # text + options, so strip the full weather dict from the
            # chat reply and keep only the headline for reference.
            if reply.get("weather"):
                reply["weather"] = {"headline": reply["weather"]["headline"],
                                    "app": reply["weather"]["app"]}
            self._send_json(reply)
            return
        if self.path == "/api/questions":
            # Editor save: validated, atomic, allowlisted files only
            # (flow, benchmark, conversation). A bad save is rejected
            # with its reason and writes nothing. Edited flow takes
            # effect for a NEW chat session, not existing ones.
            try:
                path = save_questions(ROOT, body.get("kind"), body.get("data"))
                self._send_json({"ok": True, "saved": str(path.relative_to(ROOT))})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        if self.path != "/api/ask":
            self._send_json({"error": "not found"}, status=404)
            return
        p = plan(str(body.get("question", "")))
        cols, rows, ms = [], [], 0
        weather = None
        if p["type"] == "query":
            cols, rows, ms = run_plan(p, self.data_dir)
        elif p["type"] == "weather_report":
            try:
                weather = compute_weather(self.data_dir, app=p.get("app"))
            except Exception as e:
                weather = {"error": str(e)}
        first_row = [str(v) for v in rows[0]] if rows else None
        trace = build_trace(p, len(rows), ms, first_row=first_row)
        meter = token_meter(p, self.data_dir, rows, ms) if p["type"] == "query" else {
            "available": False, "reason": "No query was run for this answer, so there is no token comparison to show."}
        if meter.get("available") and meter.get("routed"):
            trace = trace + [{"step": 6, "title": "Token meter (estimated)",
                              "text": meter["note"] + " " + meter["method"]}]
        definition = metric_definition(p["metric"]) if p.get("metric") else None
        # Visualization uses only the SQL that actually ran. For a
        # routed plan that is the aggregate SQL; the raw SQL stays
        # labelled NOT executed and is never diagrammed or charted.
        visualization = (build_visualization(p["sql"], cols, rows)
                         if p["type"] == "query" and p.get("sql") else None)
        working = {
            "sql": p.get("sql"), "raw_sql": p.get("raw_sql"),
            "visualization": visualization,
            "tables": p.get("tables", []), "raw_tables": p.get("raw_tables", []),
            "grain": p.get("grain", ""),
            "metric_definition": ({"definition": definition[0], "grain": definition[1],
                                   "denominator": definition[2]} if definition else None),
            "routed": bool(p.get("routed")), "can_list_rows": p.get("can_list_rows", True),
            "routing_explanation": p.get("routing_explanation"),
            "alternative": p.get("alternative"),
            "deterministic_note": ("Deterministic working trace: every step below is produced by the "
                                   "planner from allowlisted templates and the executed result. No live LLM, "
                                   "no API key, no external call."),
        }
        self._send_json({
            "weather": weather,
            "type": p["type"], "metric": p.get("metric"), "sql": p.get("sql"),
            "raw_sql": p.get("raw_sql"), "tables": p.get("tables", []), "grain": p.get("grain", ""),
            "filters": p.get("filters", {}), "breakdown": p.get("breakdown"),
            "explanation": p.get("explanation", ""), "clarification": p.get("clarification"),
            "columns": cols, "rows": [[str(v) for v in r] for r in rows[:200]],
            "row_count": len(rows), "runtime_ms": ms,
            "trace": trace, "working": working, "token_meter": meter,
        })

    def log_message(self, *args):  # keep the terminal readable
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    args = ap.parse_args()
    Handler.data_dir = args.data
    print(f"SocialPulse NLQ UI at http://localhost:{args.port}  (data: {args.data})")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
