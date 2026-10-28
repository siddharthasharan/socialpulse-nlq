# Phase 1 validation report

Seed: 42  |  Window: 2026-01-01 to 2026-03-31

## Row counts

- dim_users: 50,000
- dim_devices: 64,884
- dim_content: 172,752
- dim_social_edges: 473,943
- fact_sessions: 1,355,587
- fact_content_impressions: 3,204,996
- fact_content_engagements: 511,652
- fact_video_plays: 937,205
- fact_messages: 462,920
- fact_content_creation: 172,752
- agg_user_daily: 800,762
- agg_surface_daily: 15,197

## Canonical metrics (20)

- dau_mean_daily: 8897.355555555556
- wau_mean_weekly: 26651.5
- mau_march: 46818
- dau_mau_stickiness: 0.21534934867896138
- new_users_in_window: 25071
- d7_exact_retention: 0.22053042967514572
- d7_retention_by_acquisition: {'organic': 0.2224227385517708, 'paid': 0.20041629497472493, 'referral': 0.22829464448912448}
- sessions_per_dau: 1.6928712900961833
- median_session_duration_min: 12.0
- daily_creators_mean: 1630.5111111111112
- creator_penetration: 0.19894330976035846
- posts_per_creator_90d: 22.667891352840833
- orphan_post_rate: 0.24587848476428636
- top1_creator_impression_share: 0.09534676486335708
- reels_completion_rate: 0.3504750828260626
- reels_watch_time_per_viewer_day: 0.9044263437326675
- messaging_dau_mean: 3820.5714285714284
- two_way_conversation_rate: 0.6678793030174246
- commenter_penetration: 0.09146228860583062
- engagement_rate_per_impression: 0.15964200891358366

## Reconciliation

- agg_user_daily_sessions_eq_fact_sessions: actual=1355587 expected=1355587 pass=True
- agg_user_daily_impressions_eq_fact_impressions: actual=3204996.0 expected=3204996 pass=True
- agg_surface_daily_impressions_eq_fact_impressions: actual=3204996.0 expected=3204996 pass=True
- agg_surface_daily_engagements_eq_fact_engagements: actual=511652.0 expected=511652 pass=True
- dim_content_eq_fact_content_creation: actual=172752 expected=172752 pass=True
- sessions_user_fk_violations: actual=0 expected=0 pass=True
- impressions_viewer_fk_violations: actual=0 expected=0 pass=True
- impressions_content_fk_violations: actual=0 expected=0 pass=True
- messages_sender_fk_violations: actual=0 expected=0 pass=True
- video_plays_non_video: actual=0 expected=0 pass=True
- max_daily_dau_diff_agg_vs_sessions: actual=0 expected=0 pass=True
