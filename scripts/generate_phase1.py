"""Phase 1 synthetic generator: 50K users, 12 tables, 90 days, seed 42.

Streaming design (refactored 2026-11-03 so 500K fits in a 7.7GB
workspace): every base table is written to parquet as soon as it is
generated and its in-memory frame is freed before the next large step.
Aggregates are computed by DuckDB SQL over the parquet files, never by
holding all tables plus full copies (e.g. imp_surf) in pandas at once.
Engagements and video plays are built in chunked passes over the
impressions parquet, and messages are flushed to parquet in chunks,
instead of accumulating multi-million-row Python lists/frames.

RNG order is preserved exactly: every np.random call happens in the
same order and with the same sizes as the original monolithic
generator, so 50K seed-42 output is identical (up to float summation
order in the DuckDB aggregates and parquet row order in the aggregate
tables, which are keyed by user/date or date/app/surface/country/
platform). The only deliberate non-RNG change is precomputing the
long-tail fade mask once instead of rebuilding a pandas Series per
day inside the user-days loop; the mask values are identical.

Default stays N_USERS = 50_000. 500K is available only via an explicit
--n-users 500000 flag, so CI/default builds stay safe.

Order matters: users -> social graph -> content -> user-days -> sessions
-> impressions -> engagements/video plays -> messages -> aggregates.
Planted relationships are documented in docs/synthetic-assumptions.md.
Nothing here describes real users of any real product.
"""
import argparse
import gc
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

START = pd.Timestamp("2026-01-01")
DAYS = 90
N_USERS = 50_000
_BATCH = 500_000  # rows per chunked parquet pass


def _write(df: pd.DataFrame, out: Path, name: str) -> int:
    df.to_parquet(out / f"{name}.parquet", index=False)
    print(f"{name}: {len(df):,} rows", flush=True)
    return int(len(df))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("data/phase1"))
    ap.add_argument("--n-users", type=int, default=N_USERS)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range(START, periods=DAYS, freq="D")
    n = args.n_users

    # ---- dim_users ------------------------------------------------------
    # 50% signed up before the window (established), 50% in Jan-Feb 2026
    # so D7/D30 cohorts are observable inside the window.
    pre = rng.integers(0, 214, size=n)
    during = rng.integers(0, 59, size=n)
    use_pre = rng.random(n) < 0.5
    signup = np.where(
        use_pre,
        pd.Timestamp("2025-06-01").value + pre * 86_400_000_000_000,
        START.value + during * 86_400_000_000_000,
    )
    signup = pd.to_datetime(signup)
    latent = rng.beta(2, 5, size=n)  # mean ~0.29, right-skewed
    acquisition = rng.choice(
        ["organic", "referral", "paid"], size=n, p=[0.60, 0.25, 0.15]
    )
    # Paid users start slightly lower on latent engagement (confounding, by design).
    latent = np.clip(latent - np.where(acquisition == "paid", 0.04, 0.0), 0.01, 0.99)
    degree = np.where(
        rng.random(n) < 0.30,
        rng.poisson(20, size=n),
        rng.poisson(5, size=n),
    ).astype(int)
    degree_norm = np.clip(degree / 40.0, 0, 1)
    is_creator = rng.random(n) < np.clip(0.02 + 0.35 * latent + degree / 250.0, 0.02, 0.6)
    users = pd.DataFrame(
        {
            "user_id": [f"U{i:06d}" for i in range(n)],
            "signup_date": signup.date,
            "country": rng.choice(
                ["US", "IN", "BR", "ID", "MX", "GB", "DE"], size=n,
                p=[0.30, 0.25, 0.15, 0.10, 0.08, 0.06, 0.06],
            ),
            "primary_app": rng.choice(
                ["instagram", "whatsapp", "facebook"], size=n, p=[0.45, 0.30, 0.25]
            ),
            "platform": rng.choice(
                ["android", "ios", "web"], size=n, p=[0.55, 0.35, 0.10]
            ),
            "acquisition_source": acquisition,
            "is_creator": is_creator,
        }
    )
    users["_latent"] = latent
    users["_degree_norm"] = degree_norm
    user_id = users["user_id"].to_numpy()
    signup_np = pd.to_datetime(users["signup_date"]).to_numpy()
    _write(users.drop(columns=["_latent", "_degree_norm"]), out, "dim_users")
    del pre, during, use_pre, signup
    gc.collect()

    # ---- dim_devices ----------------------------------------------------
    dev_rows = []
    for i, plat in enumerate(users["platform"]):
        k = 1 if rng.random() < 0.7 else 2
        for j in range(k):
            dev_rows.append(
                {
                    "device_id": f"D{i:06d}_{j}",
                    "user_id": user_id[i],
                    "os": plat,
                    "device_type": "desktop" if plat == "web" else "mobile",
                    "app_version": rng.choice(["4.2", "4.3", "4.4"], p=[0.2, 0.35, 0.45]),
                }
            )
    devices = pd.DataFrame(dev_rows)
    _write(devices, out, "dim_devices")
    del dev_rows, devices
    gc.collect()

    # ---- dim_social_edges ----------------------------------------------
    edge_src, edge_tgt = [], []
    for i in range(n):
        d = int(degree[i])
        if d == 0:
            continue
        tgts = rng.choice(n, size=d, replace=False)
        tgts = tgts[tgts != i]
        edge_src.extend([i] * len(tgts))
        edge_tgt.extend(tgts.tolist())
    edges = pd.DataFrame(
        {
            "source_user_id": user_id[np.array(edge_src)],
            "target_user_id": user_id[np.array(edge_tgt)],
            "follow_date": pd.to_datetime(
                np.maximum(
                    signup_np[np.array(edge_src)], signup_np[np.array(edge_tgt)]
                )
            ).date,
            "status": rng.choice(["active", "inactive"], size=len(edge_src), p=[0.95, 0.05]),
        }
    )
    edges = edges.drop_duplicates(["source_user_id", "target_user_id"]).reset_index(drop=True)
    _write(edges, out, "dim_social_edges")
    # Follow map for messages, built from the frame before freeing it.
    follow_map = {}
    for s, t in zip(edges["source_user_id"], edges["target_user_id"]):
        follow_map.setdefault(s, []).append(t)
    del edge_src, edge_tgt, edges
    gc.collect()

    # ---- dim_content -----------------------------------------------------
    creator_idx = np.where(is_creator)[0]
    tier = rng.choice(["top", "mid", "long_tail"], size=len(creator_idx), p=[0.05, 0.25, 0.70])
    tier_mean = {"top": 50, "mid": 30, "long_tail": 18}
    tier_weight = {"top": 10.0, "mid": 3.0, "long_tail": 1.0}
    creator_tier = dict(zip(creator_idx.tolist(), tier.tolist()))
    # Full-length tier array, used for the fade mask below (identical
    # values to the per-day Series construction in the original code).
    tier_full = np.full(n, "", dtype=object)
    tier_full[creator_idx] = tier
    fade_base = is_creator & (tier_full == "long_tail")
    del tier_full
    content_rows = []
    cid = 0
    for ci, t in zip(creator_idx, tier):
        k = int(rng.poisson(tier_mean[t]))
        for _ in range(k):
            # Long-tail creators fade in the second half (planted persistence effect).
            if t == "long_tail" and rng.random() < 0.70:
                day = int(rng.integers(0, 45))
            else:
                day = int(rng.integers(0, DAYS))
            app = users["primary_app"].iloc[ci]
            if app == "instagram":
                surface = rng.choice(["reels", "feed", "stories"], p=[0.45, 0.35, 0.20])
            elif app == "facebook":
                surface = rng.choice(["feed", "reels", "stories"], p=[0.60, 0.25, 0.15])
            else:
                surface = rng.choice(["stories", "feed"], p=[0.70, 0.30])
            ctype = "video" if surface == "reels" else rng.choice(
                ["photo", "video", "text"], p=[0.5, 0.3, 0.2]
            )
            content_rows.append(
                {
                    "content_id": f"C{cid:07d}",
                    "creator_id": user_id[ci],
                    "creator_tier": t,
                    "app": app,
                    "surface": surface,
                    "content_type": ctype,
                    "created_at": dates[day] + pd.Timedelta(minutes=int(rng.integers(0, 1440))),
                    "duration_sec": float(rng.integers(15, 91)) if ctype == "video" else None,
                }
            )
            cid += 1
    content = pd.DataFrame(content_rows).sort_values("created_at").reset_index(drop=True)
    content["content_id"] = [f"C{i:07d}" for i in range(len(content))]
    _write(content.drop(columns=["creator_tier"]), out, "dim_content")
    # Numpy arrays the impressions step needs; the frame itself is freed.
    content_created = pd.to_datetime(content["created_at"]).to_numpy()
    content_weight = content["creator_tier"].map(tier_weight).to_numpy(dtype=float)
    content_ids = content["content_id"].to_numpy()
    content_apps = content["app"].to_numpy()
    content_surfaces = content["surface"].to_numpy()
    del content_rows, content
    gc.collect()

    # ---- user-days, sessions ---------------------------------------------
    active_users, active_dates = [], []
    yesterday = np.zeros(n, dtype=bool)
    for di, day in enumerate(dates):
        eligible = signup_np <= np.datetime64(day)
        prob = latent * (0.55 + 0.45 * degree_norm)
        prob = prob * (1.15 if day.weekday() >= 5 else 1.0)  # weekend lift
        prob = prob * (1.0 - 0.10 * di / DAYS)  # mild downward trend
        prob = np.where(yesterday, np.clip(prob + 0.18, 0, 0.9), prob)  # habit
        # Long-tail creators go quieter in the second half.
        fade = fade_base & (di >= 45)
        prob = np.where(fade, prob * 0.6, prob)
        prob = np.clip(prob, 0.01, 0.90)
        active = eligible & (rng.random(n) < prob)
        idx = np.where(active)[0]
        active_users.append(idx)
        active_dates.extend([day] * len(idx))
        yesterday = active
    au = np.concatenate(active_users)
    ad = np.array(active_dates, dtype="datetime64[ns]")
    del active_users, active_dates, yesterday
    n_sessions = np.clip(1 + rng.poisson(0.7, size=len(au)), 1, 4)
    sess_user = np.repeat(au, n_sessions)
    sess_date = np.repeat(ad, n_sessions)
    sess_start = sess_date + pd.to_timedelta(
        rng.integers(0, 1440, size=len(sess_user)), unit="m"
    )
    sess_dur = np.clip(
        rng.lognormal(np.log(12), 0.6, size=len(sess_user)), 1, 120
    ).round(1)
    # Chunked write: one 13.6M-row sessions frame plus its id strings
    # peaked at ~2.7GB in the first 500K run, so rows go out in slices
    # of the same global arrays (row order and values unchanged).
    sess_app = users["primary_app"].to_numpy()
    writer = None
    n_sess = len(sess_user)
    for lo in range(0, n_sess, 1_000_000):
        hi = min(lo + 1_000_000, n_sess)
        chunk = pd.DataFrame(
            {
                "session_id": [f"S{i:08d}" for i in range(lo, hi)],
                "user_id": user_id[sess_user[lo:hi]],
                "app": sess_app[sess_user[lo:hi]],
                "start_time": sess_start[lo:hi],
                "duration_minutes": sess_dur[lo:hi],
            }
        )
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(out / "fact_sessions.parquet", table.schema)
        writer.write_table(table)
        del chunk, table
    writer.close()
    print(f"fact_sessions: {n_sess:,} rows", flush=True)
    del n_sessions, sess_user, sess_date, sess_start, sess_dur, sess_app, writer
    gc.collect()

    # ---- impressions (sampled, per active day, content up to that day) ---
    # Two phases, same RNG order as the original: phase A runs the
    # per-day choice draws and keeps only compact numpy chunk arrays
    # (never Python lists of millions of ints, never one giant frame);
    # the global viewed-at/dwell draws follow, then phase B assembles
    # one day-chunk at a time and streams it to parquet. A single
    # impressions frame plus its 32M id strings peaked past 5GB in the
    # first 500K run and was OOM-killed; this peaks near 2GB.
    n_imps_day = rng.poisson(4, size=len(au))
    ad_days = ad.astype("datetime64[D]")
    imp_chunks = []  # (day, chosen array, viewers array), in day order
    for day in dates:
        d64 = np.datetime64(day, "D")
        lo = int(np.searchsorted(ad_days, d64))
        hi = int(np.searchsorted(ad_days, d64 + np.timedelta64(1, "D")))
        if lo == hi:
            continue
        day_users = au[lo:hi]
        k = int(n_imps_day[lo:hi].sum())
        if k == 0:
            continue
        cutoff = int(np.searchsorted(content_created, np.datetime64(day) + np.timedelta64(1, "D")))
        if cutoff == 0:
            continue
        w = content_weight[:cutoff]
        w = w / w.sum()
        chosen = rng.choice(cutoff, size=k, replace=True, p=w)
        viewers = rng.choice(day_users, size=k, replace=True)
        imp_chunks.append((day, chosen, viewers))
    del ad_days, n_imps_day
    gc.collect()
    n_imp = int(sum(len(c) for _, c, _ in imp_chunks))
    view_min = rng.integers(0, 1440, size=n_imp)
    dwell = np.clip(
        rng.lognormal(np.log(8000), 0.8, size=n_imp), 500, 120000
    ).astype(int)
    writer = None
    pos = 0
    for day, chosen, viewers in imp_chunks:
        k = len(chosen)
        chunk = pd.DataFrame(
            {
                "impression_id": [f"I{i:08d}" for i in range(pos, pos + k)],
                "viewer_id": user_id[viewers],
                "content_id": content_ids[chosen],
                "app": content_apps[chosen],
                "surface": content_surfaces[chosen],
                "viewed_at": pd.to_datetime(np.full(k, day))
                + pd.to_timedelta(view_min[pos:pos + k], unit="m"),
                "dwell_ms": dwell[pos:pos + k],
            }
        )
        pos += k
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(
                out / "fact_content_impressions.parquet", table.schema
            )
        writer.write_table(table)
        del chunk, table
    writer.close()
    assert pos == n_imp
    print(f"fact_content_impressions: {n_imp:,} rows", flush=True)
    del imp_chunks, view_min, dwell, writer
    del content_created, content_weight, content_ids, content_apps, content_surfaces
    gc.collect()

    # ---- engagements (chunked pass over impressions parquet) ------------
    # RNG draws are global, exactly as in the original code; only the
    # row assembly is chunked, with slices of the global draws.
    engage_mask = rng.random(n_imp) < 0.16
    n_eng = int(engage_mask.sum())
    eng_action = rng.choice(
        ["like", "comment", "share", "save"], size=n_eng,
        p=[0.70, 0.15, 0.05, 0.10],
    )
    eng_offset = rng.integers(0, 60, size=n_eng)
    imp_file = pq.ParquetFile(out / "fact_content_impressions.parquet")
    writer = None
    kept = 0
    mask_pos = 0
    for batch in imp_file.iter_batches(
        batch_size=_BATCH, columns=["viewer_id", "content_id", "viewed_at"]
    ):
        b = batch.to_pandas()
        m = engage_mask[mask_pos:mask_pos + len(b)]
        mask_pos += len(b)
        if not m.any():
            continue
        sub = b[m].reset_index(drop=True)
        chunk = pd.DataFrame(
            {
                "engagement_id": [f"E{i:08d}" for i in range(kept, kept + len(sub))],
                "user_id": sub["viewer_id"],
                "content_id": sub["content_id"],
                "action_type": eng_action[kept:kept + len(sub)],
                "created_at": sub["viewed_at"]
                + pd.to_timedelta(eng_offset[kept:kept + len(sub)], unit="m"),
            }
        )
        kept += len(sub)
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(out / "fact_content_engagements.parquet", table.schema)
        writer.write_table(table)
        del b, sub, chunk, table
    if writer is not None:
        writer.close()
    assert kept == n_eng, (kept, n_eng)
    print(f"fact_content_engagements: {n_eng:,} rows", flush=True)
    del engage_mask, eng_action, eng_offset, imp_file
    gc.collect()

    # ---- video plays (two chunked passes over impressions parquet) ------
    content_dims = pd.read_parquet(
        out / "dim_content.parquet", columns=["content_id", "content_type", "duration_sec"]
    )
    video_content = content_dims[content_dims["content_type"] == "video"]
    video_ids = set(video_content["content_id"])
    dur_map = dict(zip(video_content["content_id"], video_content["duration_sec"]))
    del content_dims, video_content
    gc.collect()
    # Pass 1: count video impressions (no RNG) to size the global draws.
    imp_file = pq.ParquetFile(out / "fact_content_impressions.parquet")
    n_vid = 0
    for batch in imp_file.iter_batches(batch_size=_BATCH, columns=["content_id"]):
        n_vid += int(batch.to_pandas()["content_id"].isin(video_ids).sum())
    vid_mask = rng.random(n_vid) < 0.60
    n_play = int(vid_mask.sum())
    # 35% of plays are full watches; the rest follow a right-skewed Beta(2,3).
    # A plain Beta(2,2) made P(completion >= 0.95) ~0.7%, which looks broken.
    comp = rng.beta(2, 3, size=n_play)
    comp[rng.random(n_play) < 0.35] = 1.0
    # Pass 2: assemble plays in file order, slicing the global draws.
    writer = None
    kept = 0
    vid_pos = 0
    for batch in imp_file.iter_batches(
        batch_size=_BATCH, columns=["viewer_id", "content_id", "viewed_at"]
    ):
        b = batch.to_pandas()
        is_vid = b["content_id"].isin(video_ids).to_numpy()
        m = vid_mask[vid_pos:vid_pos + int(is_vid.sum())]
        vid_pos += int(is_vid.sum())
        if not m.any():
            continue
        sub = b[is_vid].reset_index(drop=True)
        sub = sub[m].reset_index(drop=True)
        c = comp[kept:kept + len(sub)]
        chunk = pd.DataFrame(
            {
                "play_id": [f"V{i:08d}" for i in range(kept, kept + len(sub))],
                "user_id": sub["viewer_id"],
                "content_id": sub["content_id"],
                "play_time": sub["viewed_at"],
                "watch_ms": (
                    sub["content_id"].map(dur_map).fillna(30).to_numpy() * 1000 * c
                ).astype(int),
                "completion_pct": c.round(3),
            }
        )
        kept += len(sub)
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(out / "fact_video_plays.parquet", table.schema)
        writer.write_table(table)
        del b, sub, chunk, table
    if writer is not None:
        writer.close()
    assert kept == n_play, (kept, n_play)
    print(f"fact_video_plays: {n_play:,} rows", flush=True)
    del vid_mask, comp, imp_file, video_ids, dur_map
    gc.collect()

    # ---- messages (chunk-flushed, loop and RNG unchanged) ---------------
    writer = None
    msg_rows = []
    mid = 0
    for u_idx, day in zip(au.tolist(), ad.tolist()):
        if rng.random() > 0.25:
            continue
        sender = user_id[u_idx]
        targets = follow_map.get(sender)
        if not targets:
            continue
        for _ in range(1 + int(rng.poisson(0.5))):
            recip = targets[int(rng.integers(0, len(targets)))]
            conv = f"conv_{min(sender, recip)}_{max(sender, recip)}"
            t0 = pd.Timestamp(day) + pd.Timedelta(minutes=int(rng.integers(0, 1440)))
            msg_rows.append(
                {
                    "message_id": f"M{mid:08d}", "sender_id": sender,
                    "recipient_id": recip, "conversation_id": conv,
                    "sent_at": t0,
                    "delivery_status": "delivered" if rng.random() < 0.98 else "failed",
                }
            )
            mid += 1
            if rng.random() < 0.55:  # reply, same day -> two-way conversation
                msg_rows.append(
                    {
                        "message_id": f"M{mid:08d}", "sender_id": recip,
                        "recipient_id": sender, "conversation_id": conv,
                        "sent_at": t0 + pd.Timedelta(minutes=int(rng.integers(1, 180))),
                        "delivery_status": "delivered",
                    }
                )
                mid += 1
            if len(msg_rows) >= 200_000:
                chunk = pd.DataFrame(msg_rows)
                msg_rows = []
                table = pa.Table.from_pandas(chunk, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(out / "fact_messages.parquet", table.schema)
                writer.write_table(table)
                del chunk, table
    if msg_rows:
        chunk = pd.DataFrame(msg_rows)
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(out / "fact_messages.parquet", table.schema)
        writer.write_table(table)
        del chunk, table
    if writer is not None:
        writer.close()
    n_msg = mid
    print(f"fact_messages: {n_msg:,} rows", flush=True)
    del msg_rows, follow_map
    gc.collect()

    # ---- fact_content_creation (reloaded content dims) ------------------
    content_dims = pd.read_parquet(
        out / "dim_content.parquet", columns=["creator_id", "content_id", "created_at"]
    )
    creation = pd.DataFrame(
        {
            "creation_id": [f"K{i:07d}" for i in range(len(content_dims))],
            "creator_id": content_dims["creator_id"],
            "content_id": content_dims["content_id"],
            "creation_started_at": pd.to_datetime(content_dims["created_at"])
            - pd.to_timedelta(rng.integers(5, 121, size=len(content_dims)), unit="m"),
            "published_at": content_dims["created_at"],
            "status": "published",
        }
    )
    _write(creation, out, "fact_content_creation")
    del content_dims, creation
    gc.collect()

    # ---- aggregates (DuckDB over parquet, never pandas copies) ----------
    con = duckdb.connect()
    q = lambda p: "'" + str(p).replace("'", "''") + "'"
    sess_p = q(out / "fact_sessions.parquet")
    imp_p = q(out / "fact_content_impressions.parquet")
    eng_p = q(out / "fact_content_engagements.parquet")
    msg_p = q(out / "fact_messages.parquet")
    users_p = q(out / "dim_users.parquet")
    content_p = q(out / "dim_content.parquet")

    con.execute(f"""
        COPY (
          SELECT s.user_id, s.date,
            s.sessions::BIGINT AS sessions,
            s.session_minutes::DOUBLE AS session_minutes,
            COALESCE(i.impressions, 0)::DOUBLE AS impressions,
            COALESCE(e.engagements, 0)::DOUBLE AS engagements,
            COALESCE(m.messages_sent, 0)::DOUBLE AS messages_sent,
            u.primary_app AS app, u.country, u.platform
          FROM (
            SELECT user_id, CAST(start_time AS DATE) AS date,
              COUNT(*) AS sessions, SUM(duration_minutes) AS session_minutes
            FROM read_parquet({sess_p}) GROUP BY user_id, CAST(start_time AS DATE)
          ) s
          LEFT JOIN (
            SELECT viewer_id AS user_id, CAST(viewed_at AS DATE) AS date,
              COUNT(*) AS impressions
            FROM read_parquet({imp_p}) GROUP BY viewer_id, CAST(viewed_at AS DATE)
          ) i ON i.user_id = s.user_id AND i.date = s.date
          LEFT JOIN (
            SELECT user_id, CAST(created_at AS DATE) AS date,
              COUNT(*) AS engagements
            FROM read_parquet({eng_p}) GROUP BY user_id, CAST(created_at AS DATE)
          ) e ON e.user_id = s.user_id AND e.date = s.date
          LEFT JOIN (
            SELECT sender_id AS user_id, CAST(sent_at AS DATE) AS date,
              COUNT(*) AS messages_sent
            FROM read_parquet({msg_p}) GROUP BY sender_id, CAST(sent_at AS DATE)
          ) m ON m.user_id = s.user_id AND m.date = s.date
          LEFT JOIN read_parquet({users_p}) u ON u.user_id = s.user_id
          ORDER BY s.user_id, s.date
        ) TO {q(out / 'agg_user_daily.parquet')} (FORMAT PARQUET)
    """)
    print("agg_user_daily: written via DuckDB", flush=True)

    con.execute(f"""
        COPY (
          SELECT COALESCE(i.date, e.date) AS date,
            COALESCE(i.app, e.app) AS app,
            COALESCE(i.surface, e.surface) AS surface,
            COALESCE(i.country, e.country) AS country,
            COALESCE(i.platform, e.platform) AS platform,
            COALESCE(i.dau, 0)::DOUBLE AS dau,
            COALESCE(i.impressions, 0)::DOUBLE AS impressions,
            COALESCE(i.avg_dwell_ms, 0)::DOUBLE AS avg_dwell_ms,
            COALESCE(e.engagements, 0)::DOUBLE AS engagements
          FROM (
            SELECT CAST(i.viewed_at AS DATE) AS date, i.app, i.surface,
              u.country, u.platform,
              COUNT(DISTINCT i.viewer_id)::DOUBLE AS dau,
              COUNT(*)::DOUBLE AS impressions,
              AVG(i.dwell_ms)::DOUBLE AS avg_dwell_ms
            FROM read_parquet({imp_p}) i
            JOIN read_parquet({users_p}) u ON u.user_id = i.viewer_id
            GROUP BY CAST(i.viewed_at AS DATE), i.app, i.surface, u.country, u.platform
          ) i
          FULL OUTER JOIN (
            SELECT CAST(e.created_at AS DATE) AS date, c.app, c.surface,
              u.country, u.platform, COUNT(*)::DOUBLE AS engagements
            FROM read_parquet({eng_p}) e
            JOIN read_parquet({content_p}) c ON c.content_id = e.content_id
            JOIN read_parquet({users_p}) u ON u.user_id = e.user_id
            GROUP BY CAST(e.created_at AS DATE), c.app, c.surface, u.country, u.platform
          ) e
          ON i.date = e.date AND i.app = e.app AND i.surface = e.surface
            AND i.country = e.country AND i.platform = e.platform
          ORDER BY date, app, surface, country, platform
        ) TO {q(out / 'agg_surface_daily.parquet')} (FORMAT PARQUET)
    """)
    print("agg_surface_daily: written via DuckDB", flush=True)
    con.close()
    gc.collect()

    # ---- manifest (counts read back from the files just written) --------
    con = duckdb.connect()
    manifest = {"seed": args.seed, "window": ["2026-01-01", "2026-03-31"], "tables": {}}
    for name in [
        "dim_users", "dim_devices", "dim_content", "dim_social_edges",
        "fact_sessions", "fact_content_impressions", "fact_content_engagements",
        "fact_video_plays", "fact_messages", "fact_content_creation",
        "agg_user_daily", "agg_surface_daily",
    ]:
        cnt = con.execute(
            f"SELECT COUNT(*) FROM read_parquet({q(out / (name + '.parquet'))})"
        ).fetchone()[0]
        manifest["tables"][name] = int(cnt)
        print(f"{name}: {cnt:,} rows")
    con.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
