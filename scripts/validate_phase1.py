"""Validate Phase 1: run the 20 canonical metric queries and check that
daily aggregates reconcile to the underlying events. Exits non-zero if
any reconciliation check fails or any metric is null / out of range.
"""
import argparse
import json
import re
from pathlib import Path

import duckdb

TABLES = [
    "dim_users", "dim_devices", "dim_content", "dim_social_edges",
    "fact_sessions", "fact_content_impressions", "fact_content_engagements",
    "fact_video_plays", "fact_messages", "fact_content_creation",
    "agg_user_daily", "agg_surface_daily",
]
RATE_METRICS = {
    "dau_mau_stickiness", "d7_exact_retention", "creator_penetration",
    "orphan_post_rate", "top1_creator_impression_share",
    "reels_completion_rate", "two_way_conversation_rate",
    "commenter_penetration", "engagement_rate_per_impression",
}


def parse_metrics(sql_path: Path):
    text = sql_path.read_text()
    parts = re.split(r"-- metric:\s*(\S+)\s*\n", text)
    # parts[0] is empty, then id, sql, id, sql, ...
    out = {}
    for i in range(1, len(parts), 2):
        out[parts[i]] = parts[i + 1].strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("data/phase1"))
    ap.add_argument("--sql", type=Path, default=Path("sql/canonical_metrics.sql"))
    args = ap.parse_args()
    con = duckdb.connect()
    for t in TABLES:
        con.execute(
            f"CREATE OR REPLACE VIEW {t} AS SELECT * FROM read_parquet('{args.data}/{t}.parquet')"
        )
    manifest = json.loads((args.data / "manifest.json").read_text())

    metrics = {}
    for mid, sql in parse_metrics(args.sql).items():
        rows = con.execute(sql).fetchall()
        if len(rows) == 1 and len(rows[0]) == 1:
            metrics[mid] = rows[0][0]
        else:
            metrics[mid] = {str(r[0]): r[1] for r in rows}
        print(f"{mid}: {metrics[mid]}")

    def one(q):
        return con.execute(q).fetchone()[0]

    checks = {}

    def check(name, actual, expected):
        ok = actual == expected
        checks[name] = {"actual": actual, "expected": expected, "pass": bool(ok)}
        print(f"CHECK {name}: actual={actual} expected={expected} pass={ok}")

    # Aggregates must reconcile to events (exact, by construction).
    check("agg_user_daily_sessions_eq_fact_sessions",
          one("SELECT SUM(sessions) FROM agg_user_daily"),
          one("SELECT COUNT(*) FROM fact_sessions"))
    check("agg_user_daily_impressions_eq_fact_impressions",
          one("SELECT SUM(impressions) FROM agg_user_daily"),
          one("SELECT COUNT(*) FROM fact_content_impressions"))
    check("agg_surface_daily_impressions_eq_fact_impressions",
          one("SELECT SUM(impressions) FROM agg_surface_daily"),
          one("SELECT COUNT(*) FROM fact_content_impressions"))
    check("agg_surface_daily_engagements_eq_fact_engagements",
          one("SELECT SUM(engagements) FROM agg_surface_daily"),
          one("SELECT COUNT(*) FROM fact_content_engagements"))
    check("dim_content_eq_fact_content_creation",
          one("SELECT COUNT(*) FROM dim_content"),
          one("SELECT COUNT(*) FROM fact_content_creation"))
    # Referential integrity: anti-joins must be empty.
    check("sessions_user_fk_violations",
          one("SELECT COUNT(*) FROM fact_sessions s LEFT JOIN dim_users u ON u.user_id = s.user_id WHERE u.user_id IS NULL"), 0)
    check("impressions_viewer_fk_violations",
          one("SELECT COUNT(*) FROM fact_content_impressions i LEFT JOIN dim_users u ON u.user_id = i.viewer_id WHERE u.user_id IS NULL"), 0)
    check("impressions_content_fk_violations",
          one("SELECT COUNT(*) FROM fact_content_impressions i LEFT JOIN dim_content c ON c.content_id = i.content_id WHERE c.content_id IS NULL"), 0)
    check("messages_sender_fk_violations",
          one("SELECT COUNT(*) FROM fact_messages m LEFT JOIN dim_users u ON u.user_id = m.sender_id WHERE u.user_id IS NULL"), 0)
    check("video_plays_non_video",
          one("SELECT COUNT(*) FROM fact_video_plays v JOIN dim_content c ON c.content_id = v.content_id WHERE c.content_type <> 'video'"), 0)
    # DAU per day: agg_user_daily rows per day must equal distinct session users per day.
    check("max_daily_dau_diff_agg_vs_sessions",
          one("""
            SELECT MAX(ABS(a.dau - s.dau)) FROM
              (SELECT date, COUNT(*) AS dau FROM agg_user_daily GROUP BY date) a
              JOIN (SELECT CAST(start_time AS DATE) AS date, COUNT(DISTINCT user_id) AS dau
                    FROM fact_sessions GROUP BY date) s USING (date)
          """), 0)

    # Metric sanity: rates in [0,1], counts positive, no nulls.
    sanity = {}
    for mid, val in metrics.items():
        vals = list(val.values()) if isinstance(val, dict) else [val]
        ok = all(v is not None and v == v for v in vals)
        if mid in RATE_METRICS:
            ok = ok and all(0.0 <= float(v) <= 1.0 for v in vals)
        else:
            ok = ok and all(float(v) > 0 for v in vals)
        sanity[mid] = bool(ok)
        if not ok:
            print(f"SANITY FAIL {mid}: {val}")

    report = {
        "manifest": manifest,
        "metrics": {k: (v if not isinstance(v, dict) else v) for k, v in metrics.items()},
        "reconciliation": checks,
        "metric_sanity": sanity,
        "all_reconciliation_pass": all(c["pass"] for c in checks.values()),
        "all_metrics_sane": all(sanity.values()),
    }
    (args.data / "validation_report.json").write_text(json.dumps(report, indent=2, default=str))
    lines = ["# Phase 1 validation report", "",
             f"Seed: {manifest['seed']}  |  Window: {manifest['window'][0]} to {manifest['window'][1]}", "",
             "## Row counts", ""]
    for t, c in manifest["tables"].items():
        lines.append(f"- {t}: {c:,}")
    lines += ["", "## Canonical metrics (20)", ""]
    for k, v in metrics.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Reconciliation", ""]
    for k, c in checks.items():
        lines.append(f"- {k}: actual={c['actual']} expected={c['expected']} pass={c['pass']}")
    (args.data / "validation_report.md").write_text("\n".join(lines) + "\n")
    print(f"all_reconciliation_pass={report['all_reconciliation_pass']} all_metrics_sane={report['all_metrics_sane']}")
    if not (report["all_reconciliation_pass"] and report["all_metrics_sane"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
