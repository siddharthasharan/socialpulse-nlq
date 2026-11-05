"""Phase 0 — access + reliability: query-string routing, health endpoint.

Runs a real local server (ephemeral port, daemon thread) against the
500K seed-42 data. The weather test does one real DuckDB scan; the
health tests must stay fast and must not touch the data at all — one
test proves that by pointing the handler at a nonexistent data dir.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import Handler  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_SERVER = None
_BASE = None


def _base():
    global _SERVER, _BASE
    if _SERVER is None:
        _SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=_SERVER.serve_forever, daemon=True).start()
        _BASE = f"http://127.0.0.1:{_SERVER.server_address[1]}"
    return _BASE


def _get(path):
    try:
        with urllib.request.urlopen(_base() + path, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_query_strings_reach_pages():
    # The recruiter-facing bug: every one of these used to return
    # {"error": "not found"} because app.py matched self.path exactly.
    for path in ("/", "/?v=abc", "/index.html?x=1",
                 "/shell?utm_source=test", "/shell.html?x=1"):
        status, body = _get(path)
        assert status == 200, (path, status, body[:100])
        assert len(body) > 1000, path
    # /shell?utm_source=... must serve the shell page, not the dashboard.
    assert b"portfolio-shell-conversations-v1" in _get("/shell?utm_source=test")[1]


def test_api_routes_work_with_query_string():
    for path in ("/api/shell-config?utm_source=x", "/api/gallery?x=1",
                 "/api/metrics?x=1", "/api/questions?x=1"):
        status, body = _get(path)
        assert status == 200, (path, status, body[:100])


def test_health_endpoint_fields_and_speed():
    for path in ("/api/health", "/api/health?x=1"):
        t0 = time.monotonic()
        status, body = _get(path)
        dt = time.monotonic() - t0
        assert status == 200, (path, status, body[:100])
        payload = json.loads(body)
        assert payload == {"status": "ok", "service": "socialpulse-nlq",
                           "users": 500000, "seed": 42,
                           "window": "2026-01-01..2026-03-31"}, payload
        # Liveness must be fast: no DuckDB scan behind it. 2s is a very
        # generous bound for a static dict on localhost.
        assert dt < 2.0, dt


def test_health_needs_no_data_dir():
    # Strongest no-scan proof: point the handler at a data dir that
    # does not exist. Health still answers; a scan would fail.
    old = Handler.data_dir
    Handler.data_dir = ROOT / "data/does-not-exist"
    try:
        status, body = _get("/api/health")
        assert status == 200, (status, body[:100])
        assert json.loads(body)["status"] == "ok"
    finally:
        Handler.data_dir = old


def test_weather_query_still_works():
    # /api/weather reads its query from the original path; the routing
    # fix must not have broken that. One real 500K scan.
    status, body = _get("/api/weather?app=instagram")
    assert status == 200, (status, body[:200])
    payload = json.loads(body)
    assert payload["app"] == "instagram", payload.keys()
    assert "headline" in payload


def test_unknown_path_still_404():
    for path in ("/nope", "/nope?x=1", "/api/nope", "/api/nope?x=1"):
        status, body = _get(path)
        assert status == 404, (path, status)
        assert json.loads(body) == {"error": "not found"}, body
