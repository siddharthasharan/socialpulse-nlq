"""Phase 1 synthetic generator: 50K users, 12 tables, 90 days, seed 42.

Order matters: users -> social graph -> content -> user-days -> sessions
-> impressions -> engagements / video plays -> messages -> aggregates.
Planted relationships are documented in docs/synthetic-assumptions.md.
Nothing here describes real users of any real product.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

START = pd.Timestamp("2026-01-01")
DAYS = 90
N_USERS = 50_000


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

    # ---- dim_content -----------------------------------------------------
    creator_idx = np.where(is_creator)[0]
    tier = rng.choice(["top", "mid", "long_tail"], size=len(creator_idx), p=[0.05, 0.25, 0.70])
    tier_mean = {"top": 50, "mid": 30, "long_tail": 18}
    tier_weight = {"top": 10.0, "mid": 3.0, "long_tail": 1.0}
    creator_tier = dict(zip(creator_idx.tolist(), tier.tolist()))
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
    content_created = pd.to_datetime(content["created_at"]).to_numpy()
    content_weight = content["creator_tier"].map(tier_weight).to_numpy(dtype=float)

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
        fade = (
            is_creator
            & (pd.Series(tier, index=creator_idx).reindex(range(n)).fillna("").to_numpy() == "long_tail")
            & (di >= 45)
        )
        prob = np.where(fade, prob * 0.6, prob)
        prob = np.clip(prob, 0.01, 0.90)
        active = eligible & (rng.random(n) < prob)
        idx = np.where(active)[0]
        active_users.append(idx)
        active_dates.extend([day] * len(idx))
        yesterday = active
    au = np.concatenate(active_users)
    ad = np.array(active_dates, dtype="datetime64[ns]")
    n_sessions = np.clip(1 + rng.poisson(0.7, size=len(au)), 1, 4)
    sess_user = np.repeat(au, n_sessions)
    sess_date = np.repeat(ad, n_sessions)
    sess_start = sess_date + pd.to_timedelta(
        rng.integers(0, 1440, size=len(sess_user)), unit="m"
    )
    sessions = pd.DataFrame(
        {
            "session_id": [f"S{i:08d}" for i in range(len(sess_user))],
            "user_id": user_id[sess_user],
            "app": users["primary_app"].to_numpy()[sess_user],
            "start_time": sess_start,
            "duration_minutes": np.clip(
                rng.lognormal(np.log(12), 0.6, size=len(sess_user)), 1, 120
            ).round(1),
        }
    )

    # ---- impressions (sampled, per active day, content up to that day) ---
    n_imps_day = rng.poisson(4, size=len(au))
    imp_user, imp_date = [], []
    imp_content_idx = []
    # Group active users by day for content cutoff sampling.
    au_by_day = {}
    for u, d in zip(au.tolist(), ad.tolist()):
        au_by_day.setdefault(pd.Timestamp(d), []).append(u)
    for day, day_users in au_by_day.items():
        # impressions for this day: approximate from active-day counts
        day_mask = ad == np.datetime64(day)
        k = int(n_imps_day[day_mask].sum())
        if k == 0:
            continue
        cutoff = int(np.searchsorted(content_created, np.datetime64(day) + np.timedelta64(1, "D")))
        if cutoff == 0:
            continue
        w = content_weight[:cutoff]
        w = w / w.sum()
        chosen = rng.choice(cutoff, size=k, replace=True, p=w)
        viewers = rng.choice(day_users, size=k, replace=True)
        imp_user.extend(viewers)
        imp_date.extend([day] * k)
        imp_content_idx.extend(chosen.tolist())
    imp_content_idx = np.array(imp_content_idx)
    imp_user = np.array(imp_user)
    impressions = pd.DataFrame(
        {
            "impression_id": [f"I{i:08d}" for i in range(len(imp_user))],
            "viewer_id": user_id[imp_user],
            "content_id": content["content_id"].to_numpy()[imp_content_idx],
            "app": content["app"].to_numpy()[imp_content_idx],
            "surface": content["surface"].to_numpy()[imp_content_idx],
            "viewed_at": np.array(imp_date, dtype="datetime64[ns]")
            + pd.to_timedelta(rng.integers(0, 1440, size=len(imp_user)), unit="m"),
            "dwell_ms": np.clip(
                rng.lognormal(np.log(8000), 0.8, size=len(imp_user)), 500, 120000
            ).astype(int),
        }
    )

    # ---- engagements ------------------------------------------------------
    engage_mask = rng.random(len(impressions)) < 0.16
    eng_imp = impressions[engage_mask].reset_index(drop=True)
    engagements = pd.DataFrame(
        {
            "engagement_id": [f"E{i:08d}" for i in range(len(eng_imp))],
            "user_id": eng_imp["viewer_id"],
            "content_id": eng_imp["content_id"],
            "action_type": rng.choice(
                ["like", "comment", "share", "save"], size=len(eng_imp),
                p=[0.70, 0.15, 0.05, 0.10],
            ),
            "created_at": eng_imp["viewed_at"]
            + pd.to_timedelta(rng.integers(0, 60, size=len(eng_imp)), unit="m"),
        }
    )

    # ---- video plays ------------------------------------------------------
    video_content = content[content["content_type"] == "video"]
    video_ids = set(video_content["content_id"])
    vid_imp = impressions[impressions["content_id"].isin(video_ids)].reset_index(drop=True)
    vid_imp = vid_imp[rng.random(len(vid_imp)) < 0.60].reset_index(drop=True)
    dur_map = dict(zip(video_content["content_id"], video_content["duration_sec"]))
    # 35% of plays are full watches; the rest follow a right-skewed Beta(2,3).
    # A plain Beta(2,2) made P(completion >= 0.95) ~0.7%, which looks broken.
    comp = rng.beta(2, 3, size=len(vid_imp))
    comp[rng.random(len(vid_imp)) < 0.35] = 1.0
    plays = pd.DataFrame(
        {
            "play_id": [f"V{i:08d}" for i in range(len(vid_imp))],
            "user_id": vid_imp["viewer_id"],
            "content_id": vid_imp["content_id"],
            "play_time": vid_imp["viewed_at"],
            "watch_ms": (
                vid_imp["content_id"].map(dur_map).fillna(30).to_numpy() * 1000 * comp
            ).astype(int),
            "completion_pct": comp.round(3),
        }
    )

    # ---- messages ---------------------------------------------------------
    follow_map = {}
    for s, t in zip(edges["source_user_id"], edges["target_user_id"]):
        follow_map.setdefault(s, []).append(t)
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
    messages = pd.DataFrame(msg_rows)

    creation = pd.DataFrame(
        {
            "creation_id": [f"K{i:07d}" for i in range(len(content))],
            "creator_id": content["creator_id"],
            "content_id": content["content_id"],
            "creation_started_at": pd.to_datetime(content["created_at"])
            - pd.to_timedelta(rng.integers(5, 121, size=len(content)), unit="m"),
            "published_at": content["created_at"],
            "status": "published",
        }
    )

    # ---- aggregates -------------------------------------------------------
    sess_agg = sessions.assign(d=sessions["start_time"].dt.date).groupby(
        ["user_id", "d"], as_index=False
    ).agg(sessions=("session_id", "count"), session_minutes=("duration_minutes", "sum"))
    imp_agg = impressions.assign(d=impressions["viewed_at"].dt.date).groupby(
        ["viewer_id", "d"], as_index=False
    ).agg(impressions=("impression_id", "count"))
    eng_agg = engagements.assign(d=engagements["created_at"].dt.date).groupby(
        ["user_id", "d"], as_index=False
    ).agg(engagements=("engagement_id", "count"))
    msg_agg = messages.assign(d=messages["sent_at"].dt.date).groupby(
        ["sender_id", "d"], as_index=False
    ).agg(messages_sent=("message_id", "count"))
    user_daily = sess_agg.rename(columns={"user_id": "user_id", "d": "date"})
    for agg, key in [(imp_agg, "viewer_id"), (eng_agg, "user_id"), (msg_agg, "sender_id")]:
        user_daily = user_daily.merge(
            agg.rename(columns={key: "user_id", "d": "date"}),
            on=["user_id", "date"], how="left",
        )
    user_daily = user_daily.fillna(
        {"impressions": 0, "engagements": 0, "messages_sent": 0}
    )
    user_daily = user_daily.merge(
        users[["user_id", "primary_app", "country", "platform"]],
        on="user_id", how="left",
    ).rename(columns={"primary_app": "app"})

    imp_surf = impressions.copy()
    imp_surf["date"] = imp_surf["viewed_at"].dt.date
    imp_surf = imp_surf.merge(
        users[["user_id", "country", "platform"]].rename(columns={"user_id": "viewer_id"}),
        on="viewer_id", how="left",
    )
    surf = imp_surf.groupby(
        ["date", "app", "surface", "country", "platform"], as_index=False
    ).agg(
        dau=("viewer_id", "nunique"),
        impressions=("impression_id", "count"),
        avg_dwell_ms=("dwell_ms", "mean"),
    )
    eng_surf = engagements.merge(
        content[["content_id", "app", "surface"]], on="content_id", how="left"
    )
    eng_surf["date"] = eng_surf["created_at"].dt.date
    eng_surf = eng_surf.merge(
        users[["user_id", "country", "platform"]], on="user_id", how="left"
    )
    eng_surf = eng_surf.groupby(
        ["date", "app", "surface", "country", "platform"], as_index=False
    ).agg(engagements=("engagement_id", "count"))
    # Outer join: an engagement can cross midnight into a date/surface/country
    # combination with no impressions that day. A left join from impressions
    # silently dropped 139 engagements in the first run; this must reconcile exactly.
    surface_daily = surf.merge(
        eng_surf, on=["date", "app", "surface", "country", "platform"], how="outer"
    ).fillna({"dau": 0, "impressions": 0, "engagements": 0, "avg_dwell_ms": 0})

    tables = {
        "dim_users": users.drop(columns=["_latent", "_degree_norm"]),
        "dim_devices": devices,
        "dim_content": content.drop(columns=["creator_tier"]),
        "dim_social_edges": edges,
        "fact_sessions": sessions,
        "fact_content_impressions": impressions,
        "fact_content_engagements": engagements,
        "fact_video_plays": plays,
        "fact_messages": messages,
        "fact_content_creation": creation,
        "agg_user_daily": user_daily,
        "agg_surface_daily": surface_daily,
    }
    manifest = {"seed": args.seed, "window": ["2026-01-01", "2026-03-31"], "tables": {}}
    for name, df in tables.items():
        df.to_parquet(out / f"{name}.parquet", index=False)
        manifest["tables"][name] = int(len(df))
        print(f"{name}: {len(df):,} rows")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
