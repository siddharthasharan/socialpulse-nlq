"""Local web UI server for SocialPulse NLQ Phase 2.

Standard library only: no Streamlit, no CDN, no external JavaScript.
Run: python scripts/app.py [--port 8000] [--data data/phase1]
Then open http://localhost:8000 in Codespace (port forwarding) or locally.
"""
import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from socialpulse_nlq.planner import plan, run_plan
from socialpulse_nlq.weather import compute_weather


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
        if self.path in ("/", "/index.html"):
            html = (ROOT / "app/index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif self.path == "/api/metrics":
            text = (ROOT / "catalog/measures.yaml").read_text()
            ids = re.findall(r"-\s*id:\s*(\S+)", text)
            self._send_json({"metrics": ids})
        elif self.path == "/api/experiments":
            self._send_json({"experiments": parse_experiments()})
        elif self.path == "/api/weather":
            try:
                self._send_json(compute_weather(self.data_dir))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        if self.path != "/api/ask":
            self._send_json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        p = plan(str(body.get("question", "")))
        cols, rows, ms = [], [], 0
        weather = None
        if p["type"] == "query":
            cols, rows, ms = run_plan(p, self.data_dir)
        elif p["type"] == "weather_report":
            try:
                weather = compute_weather(self.data_dir)
            except Exception as e:
                weather = {"error": str(e)}
        self._send_json({
            "weather": weather,
            "type": p["type"], "metric": p.get("metric"), "sql": p.get("sql"),
            "tables": p.get("tables", []), "grain": p.get("grain", ""),
            "filters": p.get("filters", {}), "breakdown": p.get("breakdown"),
            "explanation": p.get("explanation", ""), "clarification": p.get("clarification"),
            "columns": cols, "rows": [[str(v) for v in r] for r in rows[:200]],
            "row_count": len(rows), "runtime_ms": ms,
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
