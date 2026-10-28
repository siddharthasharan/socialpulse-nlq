"""CLI for SocialPulse NLQ Phase 3 chat: multi-turn, deterministic, no LLM.

Usage: python scripts/chat.py [--data data/phase1]
Then type turns, for example:
  What's happening with Instagram?
  creators
  what's the diagnosis?
  what experiment would you recommend?
  give me the PM/Eng brief
Type quit / exit / q to stop.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from socialpulse_nlq.chat import ChatSession


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/phase1"))
    args = ap.parse_args()
    session = ChatSession(args.data)
    print("SocialPulse chat (Phase 3, deterministic). Try: What's happening with Instagram?")
    print("Then pick a drill-down, ask for a diagnosis, a recommendation, or the PM/Eng brief.")
    while True:
        try:
            msg = input("\nYou: ").strip()
        except EOFError:
            break
        if msg.lower() in ("quit", "exit", "q"):
            break
        if not msg:
            continue
        r = session.respond(msg)
        print(f"\nAssistant [{r['type']}]:\n{r['text']}")
        if r["options"]:
            print("Options: " + ", ".join(f"{o['key']}) {o['label']}" for o in r["options"]))


if __name__ == "__main__":
    main()
