-- metric: dau_mean_daily
SELECT AVG(dau) AS value FROM (
  SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau
  FROM fact_sessions GROUP BY d
);
-- metric: wau_mean_weekly
SELECT AVG(wau) AS value FROM (
  SELECT DATE_TRUNC('week', CAST(start_time AS DATE)) AS w, COUNT(DISTINCT user_id) AS wau
  FROM fact_sessions GROUP BY w
);
-- metric: mau_march
SELECT COUNT(DISTINCT user_id) AS value FROM fact_sessions
WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31';
-- metric: dau_mau_stickiness
SELECT AVG(dau) / NULLIF((SELECT COUNT(DISTINCT user_id) FROM fact_sessions
  WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'), 0) AS value
FROM (
  SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau
  FROM fact_sessions
  WHERE CAST(start_time AS DATE) BETWEEN DATE '2026-03-01' AND DATE '2026-03-31' GROUP BY d
);
-- metric: new_users_in_window
SELECT COUNT(*) AS value FROM dim_users
WHERE signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31';
-- metric: d7_exact_retention
SELECT AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) AS value FROM (
  SELECT u.user_id,
    EXISTS (SELECT 1 FROM fact_sessions s
      WHERE s.user_id = u.user_id AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) AS retained
  FROM dim_users u
  WHERE u.signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-21'
);
-- metric: d7_retention_by_acquisition
SELECT acquisition_source AS label, AVG(CASE WHEN retained THEN 1.0 ELSE 0.0 END) AS value FROM (
  SELECT u.user_id, u.acquisition_source,
    EXISTS (SELECT 1 FROM fact_sessions s
      WHERE s.user_id = u.user_id AND CAST(s.start_time AS DATE) = u.signup_date + INTERVAL 7 DAY) AS retained
  FROM dim_users u
  WHERE u.signup_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-21'
) GROUP BY acquisition_source ORDER BY acquisition_source;
-- metric: sessions_per_dau
SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT (user_id, CAST(start_time AS DATE))), 0) AS value
FROM fact_sessions;
-- metric: median_session_duration_min
SELECT MEDIAN(duration_minutes) AS value FROM fact_sessions;
-- metric: daily_creators_mean
SELECT AVG(creators) AS value FROM (
  SELECT CAST(published_at AS DATE) AS d, COUNT(DISTINCT creator_id) AS creators
  FROM fact_content_creation GROUP BY d
);
-- metric: creator_penetration
SELECT AVG(creators * 1.0 / NULLIF(dau, 0)) AS value FROM (
  SELECT CAST(published_at AS DATE) AS d, COUNT(DISTINCT creator_id) AS creators FROM fact_content_creation
  GROUP BY d
) c JOIN (
  SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau FROM fact_sessions GROUP BY d
) s USING (d);
-- metric: posts_per_creator_90d
SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT creator_id), 0) AS value
FROM fact_content_creation;
-- metric: orphan_post_rate
SELECT AVG(CASE WHEN engaged THEN 0.0 ELSE 1.0 END) AS value FROM (
  SELECT c.content_id,
    EXISTS (SELECT 1 FROM fact_content_engagements e WHERE e.content_id = c.content_id) AS engaged
  FROM dim_content c
);
-- metric: top1_creator_impression_share
WITH creator_imps AS (
  SELECT c.creator_id, COUNT(*) AS imps FROM fact_content_impressions i
  JOIN dim_content c ON c.content_id = i.content_id GROUP BY c.creator_id
), ranked AS (
  SELECT creator_id, imps,
    NTILE(100) OVER (ORDER BY imps DESC) AS tile FROM creator_imps
)
SELECT SUM(CASE WHEN tile = 1 THEN imps ELSE 0 END) * 1.0 / NULLIF(SUM(imps), 0) AS value FROM ranked;
-- metric: reels_completion_rate
SELECT AVG(CASE WHEN completion_pct >= 0.95 THEN 1.0 ELSE 0.0 END) AS value FROM fact_video_plays;
-- metric: reels_watch_time_per_viewer_day
SELECT SUM(watch_ms) / 60000.0 / NULLIF(COUNT(DISTINCT (user_id, CAST(play_time AS DATE))), 0) AS value
FROM fact_video_plays;
-- metric: messaging_dau_mean
SELECT AVG(senders) AS value FROM (
  SELECT CAST(sent_at AS DATE) AS d, COUNT(DISTINCT sender_id) AS senders
  FROM fact_messages GROUP BY d
);
-- metric: two_way_conversation_rate
SELECT AVG(CASE WHEN senders >= 2 THEN 1.0 ELSE 0.0 END) AS value FROM (
  SELECT conversation_id, COUNT(DISTINCT sender_id) AS senders FROM fact_messages GROUP BY conversation_id
);
-- metric: commenter_penetration
SELECT AVG(commenters * 1.0 / NULLIF(dau, 0)) AS value FROM (
  SELECT CAST(e.created_at AS DATE) AS d, COUNT(DISTINCT e.user_id) AS commenters
  FROM fact_content_engagements e WHERE e.action_type = 'comment' GROUP BY d
) c JOIN (
  SELECT CAST(start_time AS DATE) AS d, COUNT(DISTINCT user_id) AS dau FROM fact_sessions GROUP BY d
) s USING (d);
-- metric: engagement_rate_per_impression
SELECT (SELECT COUNT(*) FROM fact_content_engagements) * 1.0 /
  NULLIF((SELECT COUNT(*) FROM fact_content_impressions), 0) AS value;
