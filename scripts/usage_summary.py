"""Summarize SocialPulse usage log lines into a daily table.

Usage:
    python scripts/usage_summary.py [logfile]   # or pipe Render Logs on stdin
    python scripts/usage_summary.py render-logs.txt | head -30

Reads JSON lines of the form:
    {"ts": "2026-11-05T21:00:00Z", "event": "session_start",
     "country": "DE", "visitor_hash": "9f2c..."}

and prints one row per UTC date: sessions, unique visitors, page loads,
sessions by country. A session is a session_start event (a new chat
session); unique visitors are distinct salted daily hashes, so a hash
seen on two dates counts once per date.

Render Logs copy/paste often prefixes each line with Render's own
timestamp, so the JSON object is extracted from each line (first "{"
to end of line) instead of requiring the whole line to be JSON.
Non-JSON lines and other event types are skipped and counted.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict


def _extract_record(line):
    """The JSON object embedded in a log line, or None."""
    idx = line.find("{")
    if idx < 0:
        return None
    try:
        rec = json.loads(line[idx:])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def summarize(lines):
    """Aggregate parsed event lines into per-date rows.

    Returns (rows, skipped) where rows is a list of dicts sorted by
    date: date, sessions, visitors, page_loads, by_country (dict).
    """
    by_date = defaultdict(lambda: {
        "sessions": 0, "visitors": set(), "page_loads": 0,
        "by_country": Counter()})
    skipped = 0
    for line in lines:
        rec = _extract_record(line)
        if rec is None:
            skipped += 1
            continue
        event = rec.get("event")
        if event not in ("page_load", "session_start"):
            skipped += 1
            continue
        ts = rec.get("ts") or ""
        day = ts[:10] if len(ts) >= 10 else ""
        if not day:
            skipped += 1
            continue
        row = by_date[day]
        country = rec.get("country") or "unknown"
        vhash = rec.get("visitor_hash")
        if vhash:
            row["visitors"].add(vhash)
        if event == "session_start":
            row["sessions"] += 1
            row["by_country"][country] += 1
        else:
            row["page_loads"] += 1
    rows = [{
        "date": day,
        "sessions": r["sessions"],
        "visitors": len(r["visitors"]),
        "page_loads": r["page_loads"],
        "by_country": dict(sorted(r["by_country"].items(),
                                  key=lambda kv: (-kv[1], kv[0]))),
    } for day, r in sorted(by_date.items())]
    return rows, skipped


def _format_row(row):
    by_country = ", ".join(f"{c}={n}" for c, n in row["by_country"].items())
    return (f"{row['date']}  sessions={row['sessions']:>4}  "
            f"visitors={row['visitors']:>4}  "
            f"page_loads={row['page_loads']:>4}  {by_country}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Daily usage table from SocialPulse event log lines.")
    ap.add_argument("logfile", nargs="?",
                    help="log file to read (default: stdin)")
    args = ap.parse_args(argv)
    if args.logfile:
        with open(args.logfile, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()
    rows, skipped = summarize(lines)
    print("date        sessions  visitors  page_loads  sessions by country")
    for row in rows:
        print(_format_row(row))
    if not rows:
        print("(no usage events found)")
    if skipped:
        print(f"skipped {skipped} unparseable or non-usage lines")


if __name__ == "__main__":
    main()
