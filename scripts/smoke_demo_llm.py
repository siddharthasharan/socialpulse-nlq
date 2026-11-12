"""Live smoke test for the wired demo (chunk B, slice 2).

Starts the real server with the owner key from the environment and
checks the slice-2 wiring end to end, owner-key path first, then the
visitor-BYOK path (simulating the browser: fetch the routing prompt,
call Gemini directly, post the choice):

  1. GET /api/shell-config reports llm.configured=true.
  2. GET /api/llm-routing-prompt returns the pinned prompt + params.
  3. POST /api/chat "What's the WAU for Instagram?" (fresh session)
     answers through the owner-key LLM fallback turn.
  4. The BYOK simulation of the same question answers through the
     visitor-key path.

A case only counts as a live pass when the MODEL ran it (real tokens
in the receipt, no fallback). A fallback is BLOCKED, not a pass: the
model did not run. The WAU answer itself may honestly be a
clarification (the model following prompt rule 7 — WAU is not defined
per app), a weather report (whose WAU card is the real Instagram WAU,
147,138.5), or a query — but a query answer that presents the all-apps
mean as Instagram's WAU (no all-apps note) is a live FAILURE, the h07
trap in production.

Requires GEMINI_API_KEY in the environment. WITHOUT a key this script
prints LIVE VERIFICATION PENDING and exits 2: it never invents live
results. The key is never printed.

Usage:
    python scripts/smoke_demo_llm.py [--data data/phase1] [--port 8123]
Exit code: 0 all live passes; 1 a live failure; 2 no key (pending);
3 blocked (the model did not run).
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WAU_Q = "What's the WAU for Instagram?"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(method, url, payload=None, headers=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_health(base, tries=30):
    for _ in range(tries):
        try:
            payload = _req("GET", base + "/api/health", timeout=5)
            if payload.get("status") == "ok":
                return True
        except Exception:  # noqa: BLE001 - server still starting
            time.sleep(2)
    return False


def _llm_line(llm):
    if not llm:
        return "llm: (no receipt)"
    if llm.get("fallback"):
        return (f"llm: FALLBACK {llm['fallback']} — "
                f"{llm.get('fallback_reason')}")
    return (f"llm: model={llm.get('model')} "
            f"prompt={llm.get('prompt_tokens')} "
            f"completion={llm.get('completion_tokens')} "
            f"latency={llm.get('latency_ms')}ms")


def _parse_model_choice(data):
    cands = (data or {}).get("candidates") or []
    if not cands:
        raise ValueError("model response has no candidates")
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _format_context(ctx):
    if not ctx:
        return "No conversation context — this is the first question."
    lines = []
    if ctx.get("app"):
        lines.append(f"- app: {ctx['app']}")
    if ctx.get("stage"):
        lines.append(f"- stage: {ctx['stage']}")
    mc = ctx.get("mau_context")
    if mc:
        lines.append(f"- MAU context: app {mc.get('app')}, March 2026 "
                      f"({mc.get('start')}..{mc.get('end')})")
    sw = ctx.get("series_window")
    if sw:
        lines.append(f"- last daily series window: {sw[0]}..{sw[1]}")
    return "\n".join(lines) if lines else (
        "No conversation context — this is the first question.")


def _check_wau_reply(reply, label):
    """(ok, blocked, note) for a WAU reply. ok = the model ran and the
    answer is one of the honest shapes. blocked = a fallback (the model
    did not run). A query without the all-apps note is a failure."""
    working = reply.get("working") or {}
    llm = working.get("llm") or {}
    if llm.get("fallback"):
        return False, True, f"{label}: {_llm_line(llm)}"
    if llm.get("prompt_tokens") is None:
        return False, True, f"{label}: no real tokens in receipt"
    note = f"{label}: type={reply.get('type')} engine={reply.get('engine')} | {_llm_line(llm)}"
    if reply.get("type") == "query":
        if "all apps" not in (reply.get("text") or ""):
            return False, False, (note + " — LIVE FAILURE: query answer "
                                  "does not say all-apps (h07 trap)")
        return True, False, note
    if reply.get("type") == "weather":
        wau = [c for c in (reply.get("weather") or {}).get("cards", [])
               if c.get("id") == "wau"]
        if not wau or wau[0].get("current") != 147138.5:
            return False, False, note + " — LIVE FAILURE: no Instagram WAU card"
        return True, False, note + " (Instagram WAU 147,138.5)"
    if reply.get("type") == "clarification":
        return True, False, note + " (model clarified per rule 7)"
    return False, False, note + " — LIVE FAILURE: unexpected reply type"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/phase1")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("LIVE VERIFICATION PENDING: no GEMINI_API_KEY in the "
              "environment. Set the owner key (Render env var for the "
              "live demo) and re-run. Nothing was checked.")
        return 2
    port = args.port or _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/app.py"),
         "--port", str(port), "--data", str(args.data)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_health(base):
            print("BLOCKED: server did not come up on", base)
            return 3
        config = _req("GET", base + "/api/shell-config", timeout=10)
        llm_status = config.get("llm") or {}
        print("shell-config llm:", json.dumps(llm_status))
        if not llm_status.get("configured"):
            print("LIVE FAILURE: server started with a key in the "
                  "environment but /api/shell-config says no key.")
            return 1
        prompt_doc = _req("GET", base + "/api/llm-routing-prompt", timeout=10)
        print("routing prompt:", prompt_doc.get("model"),
              prompt_doc.get("prompt_version"),
              "placeholders:",
              "{question}" in prompt_doc.get("prompt", ""),
              "{context}" in prompt_doc.get("prompt", ""))
        if prompt_doc.get("prompt_version") != "llm-prompt-v1":
            print("LIVE FAILURE: wrong prompt version in routing prompt.")
            return 1

        # Owner-key path.
        reply = _req("POST", base + "/api/chat",
                     {"session_id": "smoke-demo-llm-owner",
                      "message": WAU_Q}, timeout=300)
        ok, blocked, note = _check_wau_reply(reply, "owner-key")
        print(note)
        if blocked:
            print("BLOCKED: the model did not run on the owner-key path.")
            return 3
        if not ok:
            return 1

        # BYOK path, simulating the browser.
        byok_reply = _req("POST", base + "/api/chat",
                          {"session_id": "smoke-demo-llm-byok",
                           "message": WAU_Q}, timeout=300)
        prompt = prompt_doc["prompt"].replace(
            "{context}", _format_context(byok_reply.get("context"))).replace(
            "{question}", WAU_Q)
        try:
            data = _req("POST", prompt_doc["endpoint"],
                        {"contents": [{"role": "user",
                                      "parts": [{"text": prompt}]}],
                         "generationConfig": prompt_doc["generation"]},
                        headers={"x-goog-api-key": key}, timeout=300)
        except Exception as e:  # noqa: BLE001 - model failure is BLOCKED
            print(f"BLOCKED: direct Gemini call failed: {e}")
            return 3
        choice = _parse_model_choice(data)
        usage = data.get("usageMetadata") or {}
        reply = _req("POST", base + "/api/chat",
                     {"session_id": "smoke-demo-llm-byok", "message": WAU_Q,
                      "llm_choice": choice,
                      "llm_tokens": {
                          "prompt": usage.get("promptTokenCount"),
                          "completion": usage.get("candidatesTokenCount"),
                          "total": usage.get("totalTokenCount")}},
                     timeout=300)
        ok, blocked, note = _check_wau_reply(reply, "byok")
        print(note)
        if blocked:
            print("BLOCKED: the model did not run on the BYOK path.")
            return 3
        if not ok:
            return 1
        print("SMOKE OK: wired demo answers WAU live on both paths.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
