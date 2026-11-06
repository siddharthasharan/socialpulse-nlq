"""Usage tracking: salted daily visitor hash, /24 geo cache with
fail-open, no raw IP in any log line, /api/usage counts only.

Runs a real local server (ephemeral port, daemon thread) like
test_phase0.py. The resolver is monkeypatched in the unit tests so no
test depends on ip-api.com being reachable; the live check is done by
hand against the real endpoint (see docs/usage-tracking.md).
"""
import contextlib
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import Handler  # noqa: E402
from socialpulse_nlq import usage  # noqa: E402

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


def _get(path, headers=None):
    req = urllib.request.Request(_base() + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(path, payload, headers=None):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(_base() + path, data=data, headers=h,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class _FakeResp:
    """Minimal urlopen stand-in: context manager with .read()."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_visitor_hash_is_daily_and_stable():
    day = date(2026, 11, 5)
    h1 = usage.visitor_hash("203.0.113.7", day=day, salt="test-salt")
    h2 = usage.visitor_hash("203.0.113.7", day=day, salt="test-salt")
    assert h1 == h2, "same IP + day + salt must hash the same"
    assert len(h1) == 16 and all(c in "0123456789abcdef" for c in h1)
    assert usage.visitor_hash("203.0.113.7", day=date(2026, 11, 6),
                              salt="test-salt") != h1, "hash must change daily"
    assert usage.visitor_hash("203.0.113.8", day=day,
                              salt="test-salt") != h1, "hash must differ per IP"
    assert usage.visitor_hash("203.0.113.7", day=day,
                              salt="other-salt") != h1, "hash must differ per salt"


def test_resolver_uses_24_cache_and_fails_open():
    calls = []

    def fake_ok(url, timeout=None):
        calls.append(url)
        return _FakeResp({"status": "success", "countryCode": "US"})

    old = urllib.request.urlopen
    usage.reset_state()
    try:
        urllib.request.urlopen = fake_ok
        assert usage.resolve_country("8.8.8.8") == "US"
        assert usage.resolve_country("8.8.8.9") == "US"
        assert len(calls) == 1, f"same /24 must resolve once, got {calls}"
        assert usage.resolve_country("1.1.1.1") == "US"
        assert len(calls) == 2, "a different /24 must resolve again"

        def fake_down(url, timeout=None):
            down_calls.append(url)
            raise urllib.error.URLError("resolver down")

        down_calls = []
        urllib.request.urlopen = fake_down
        usage.reset_state()
        assert usage.resolve_country("9.9.9.9") == "unknown"
        assert usage.resolve_country("9.9.9.9") == "unknown"
        assert len(down_calls) == 1, (
            "a failed lookup is cached briefly, not retried per request")
        # A later good lookup for the same subnet works once the
        # negative cache entry is gone (modelled here by reset_state).
        urllib.request.urlopen = fake_ok
        usage.reset_state()
        assert usage.resolve_country("9.9.9.9") == "US"

        def fake_fail(url, timeout=None):
            return _FakeResp({"status": "fail", "message": "reserved range"})

        urllib.request.urlopen = fake_fail
        usage.reset_state()
        assert usage.resolve_country("9.9.9.9") == "unknown"
    finally:
        urllib.request.urlopen = old
        usage.reset_state()


def test_private_ips_never_resolve():
    calls = []

    def fake(url, timeout=None):
        calls.append(url)
        return _FakeResp({"status": "success", "countryCode": "US"})

    old = urllib.request.urlopen
    usage.reset_state()
    try:
        urllib.request.urlopen = fake
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9",
                   "::1", "not-an-ip", ""):
            assert usage.resolve_country(ip) == "unknown", ip
        assert calls == [], "no resolver call for private/unparsable IPs"
    finally:
        urllib.request.urlopen = old
        usage.reset_state()


def test_log_lines_contain_no_raw_ip():
    old = usage.resolve_country
    usage.resolve_country = lambda ip: "DE"  # noqa: ARG005 - deterministic
    usage.reset_state()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            country, vhash = usage.record_event("session_start", "203.0.113.7")
        out = buf.getvalue()
        assert country == "DE"
        assert "203.0.113.7" not in out, "raw IP must never appear in a log line"
        assert "203.0.113" not in out, "not even the /24 may appear"
        rec = json.loads(out.strip())
        assert rec["event"] == "session_start"
        assert rec["country"] == "DE"
        assert rec["visitor_hash"] == vhash == usage.visitor_hash("203.0.113.7")
        assert set(rec) == {"ts", "event", "country", "visitor_hash"}, rec
    finally:
        usage.resolve_country = old
        usage.reset_state()


def test_api_usage_returns_counts_only():
    old = usage.resolve_country
    usage.resolve_country = lambda ip: "DE"  # noqa: ARG005 - deterministic
    usage.reset_state()
    try:
        # One page load and one new chat session, same visitor.
        status, _ = _get("/", headers={"X-Forwarded-For": "203.0.113.7"})
        assert status == 200, status
        status, body = _post("/api/chat", {"message": "hi"},
                             headers={"X-Forwarded-For": "203.0.113.7"})
        assert status == 200, (status, body[:200])
        assert json.loads(body)["session_id"], body[:200]
        status, body = _get("/api/usage")
        assert status == 200, (status, body[:200])
        payload = json.loads(body)
        assert payload["sessions"] == 1, payload
        assert payload["page_loads"] == 1, payload
        assert payload["unique_visitors_today"] == 1, payload
        assert payload["sessions_by_country"] == {"DE": 1}, payload
        assert payload["since"] == "server start", payload
        # Counts only: no IP, no hash, no per-visitor rows anywhere.
        assert "203.0.113.7" not in body.decode(), body
        assert "visitor_hash" not in payload, payload
        assert "ip" not in " ".join(payload).lower(), payload
    finally:
        usage.resolve_country = old
        usage.reset_state()


def test_health_and_pages_untouched():
    status, body = _get("/api/health")
    assert status == 200, (status, body[:100])
    assert json.loads(body) == {"status": "ok", "service": "socialpulse-nlq",
                                "users": 500000, "seed": 42,
                                "window": "2026-01-01..2026-03-31"}, body
    for path in ("/", "/shell"):
        status, body = _get(path)
        assert status == 200, (path, status)
        assert len(body) > 1000, path
