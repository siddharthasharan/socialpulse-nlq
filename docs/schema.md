# Schema: 24-table design, Phase 1 materialised

Phase 1 materialises 12 tables (marked P1). The rest are designed here so
Phase 3 does not require a redesign; they are not generated yet.

## Core dimensions (6)

| Table | Phase | Grain / notes |
|---|---|---|
| dim_users | P1 | one row per synthetic user; opaque id, signup_date, country, primary_app, platform, acquisition_source, latent engagement tier (not a real person attribute) |
| dim_devices | P1 | one row per device; a user may have 1-2 devices |
| dim_content | P1 | one row per published content item; creator, app, surface, type, created_at |
| dim_creators | Phase 3 | creator profile derived from dim_users in Phase 1 (is_creator flag); separate table in Phase 3 |
| dim_social_edges | P1 | directed follow edge, source -> target, follow_date, status |
| dim_calendar | derived | date, week, month, weekday; generated in SQL, not stored |

## Behavioural event facts (8)

| Table | Phase | Grain / notes |
|---|---|---|
| fact_sessions | P1 | one row per session; user, app, start_time, duration_minutes |
| fact_content_impressions | P1 | sampled impressions, one row per sampled view; viewer, content, surface, dwell_ms |
| fact_content_engagements | P1 | like / comment / share / save on an impression |
| fact_video_plays | P1 | one row per sampled video play; watch_ms, completion_pct |
| fact_messages | P1 | one row per message; sender, conversation, sent_at, delivery_status |
| fact_notifications | Phase 3 | sent / opened notifications |
| fact_searches | Phase 3 | search queries and clicks |
| fact_content_creation | P1 | one row per published item (mirrors dim_content creation event); draft abandonment is Phase 3 |

## Analytics-ready aggregates (4)

| Table | Phase | Grain |
|---|---|---|
| agg_user_daily | P1 | user x date; sessions, minutes, impressions, engagements, messages |
| agg_creator_daily | Phase 3 | creator x date; posts, views, engagements, reach |
| agg_content_daily | Phase 3 | content x date; views, reactions, comments, shares |
| agg_surface_daily | P1 | date x app x surface x country x platform; DAU, sessions, impressions |

## Revenue, safety, user experience (4) — Phase 3

fact_ad_impressions, dim_ad_campaigns, fact_content_reports,
fact_user_surveys.

## Experimentation (2) — registry in Phase 1, tables in Phase 3

dim_experiments and fact_experiment_assignments. Phase 1 ships
`catalog/experiments.yaml` as the experiment list; assignment tables arrive
with the planner in Phase 2/3 so questions can be scoped to an experiment.

## Grain rules (enforced by the catalog, not by hope)

- DAU counts users, never sessions or impressions.
- Creator metrics count creators, with the denominator stated every time.
- Impression metrics count sampled impressions; the README and catalog say
  "sampled" wherever that applies.
- A user, a session, an impression, and a content item are never mixed in
  one denominator without labelling.
