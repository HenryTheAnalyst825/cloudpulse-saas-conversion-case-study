-- =====================================================================================
-- CloudPulse Console: 5 product-analytics queries (PostgreSQL 13+)
-- Every query is annotated line by line. Dataset: generate_cloudpulse_data.py
--
-- T-SQL (SQL Server) cheat sheet:
--   DATE_TRUNC('month', x)        -> DATEFROMPARTS(YEAR(x), MONTH(x), 1)
--   x + INTERVAL '7 days'         -> DATEADD(day, 7, x)
--   EXTRACT(EPOCH FROM (a - b))/3600.0 -> DATEDIFF(second, b, a)/3600.0
--   COUNT(*) FILTER (WHERE c)     -> SUM(CASE WHEN c THEN 1 ELSE 0 END)
--   PERCENTILE_CONT(.5) WITHIN GROUP (ORDER BY x) is a WINDOW function in T-SQL:
--       PERCENTILE_CONT(.5) WITHIN GROUP (ORDER BY x) OVER (PARTITION BY g)  (use SELECT DISTINCT)
--   NOW() -> SYSDATETIME();  "timestamp" column -> [timestamp]
--
-- "Now" is pinned to the dataset date in each query's `params` CTE so results are
-- reproducible. For live data replace  TIMESTAMP '2026-11-15 12:00:00'  with  NOW().
-- =====================================================================================

-- ---------- SETUP (run once) ----------------------------------------------------------
CREATE TABLE organizations (
    org_id      VARCHAR(20) PRIMARY KEY,
    org_name    VARCHAR(100),
    plan_tier   VARCHAR(12)  CHECK (plan_tier IN ('Free','Pro','Enterprise')),
    quota_limit INT,
    created_at  TIMESTAMP,
    status      VARCHAR(12)  CHECK (status IN ('Active','Suspended','Past_Due'))
);
CREATE TABLE users (
    user_id      VARCHAR(20) PRIMARY KEY,
    org_id       VARCHAR(20) REFERENCES organizations(org_id),
    email        VARCHAR(150),
    role         VARCHAR(20) CHECK (role IN ('Admin','Developer','Billing_Viewer')),
    onboarded_at TIMESTAMP
);
CREATE TABLE api_usage_logs (
    log_id        VARCHAR(20) PRIMARY KEY,
    org_id        VARCHAR(20) REFERENCES organizations(org_id),
    endpoint_path VARCHAR(100),
    status_code   INT CHECK (status_code IN (200,401,403,429,500)),
    latency_ms    INT,
    "timestamp"   TIMESTAMP
);
CREATE TABLE subscription_events (
    event_id        VARCHAR(20) PRIMARY KEY,
    org_id          VARCHAR(20) REFERENCES organizations(org_id),
    event_type      VARCHAR(20) CHECK (event_type IN ('Trial_Started','Quota_80_Reached','Quota_100_Blocked',
                                                      'Upgrade_Initiated','Payment_Succeeded','Payment_Failed')),
    mrr_amount      DECIMAL(10,2),
    event_timestamp TIMESTAMP
);
-- psql client; load parents before children:
-- \copy organizations        FROM 'organizations.csv'        CSV HEADER
-- \copy users                FROM 'users.csv'                CSV HEADER
-- \copy api_usage_logs       FROM 'api_usage_logs.csv'       CSV HEADER
-- \copy subscription_events  FROM 'subscription_events.csv'  CSV HEADER


-- ===== QUERY 1 ========================================================================
-- ONBOARDING & ACTIVATION FUNNEL
-- Signup -> User Invited -> First API Call (<= 7 days) -> Reached 80% -> Upgraded
-- Strict sequential funnel: an org counts at step N only if it passed every earlier step.
WITH
user_counts AS (                                   -- 1 row per org: how many users exist
    SELECT org_id, COUNT(*) AS n_users             -- count every user row in the org
    FROM users
    GROUP BY org_id                                -- collapse users to one row per org
),
first_call AS (                                    -- 1 row per org: first SUCCESSFUL call
    SELECT l.org_id, MIN(l.timestamp) AS first_200_at   -- earliest timestamp...
    FROM api_usage_logs l
    WHERE l.status_code = 200                      -- ...among HTTP 200 only (401/403/429/500 are not value)
    GROUP BY l.org_id
),
quota_reach AS (                                   -- 1 row per org: first time quota pressure hit
    SELECT org_id, MIN(event_timestamp) AS reached_80_at
    FROM subscription_events
    WHERE event_type IN ('Quota_80_Reached', 'Quota_100_Blocked')  -- a 100% block implies the org passed 80%
    GROUP BY org_id                                -- (abrupt orgs never got an 80% event but did reach it)
),
upgrades AS (                                      -- 1 row per org: first real payment
    SELECT org_id, MIN(event_timestamp) AS upgraded_at
    FROM subscription_events
    WHERE event_type = 'Payment_Succeeded'
      AND mrr_amount > 0                           -- paid revenue only
    GROUP BY org_id
),
org_flags AS (                                     -- one 0/1 flag per funnel step per org
    SELECT o.org_id,
           CASE WHEN COALESCE(uc.n_users, 0) >= 2 THEN 1 ELSE 0 END AS f_invited,       -- >=2 users = someone was invited
           CASE WHEN fc.first_200_at <= o.created_at + INTERVAL '7 days'
                THEN 1 ELSE 0 END                                   AS f_first_call,    -- activation definition: <= 7 days
           CASE WHEN qr.reached_80_at IS NOT NULL THEN 1 ELSE 0 END AS f_reached_80,
           CASE WHEN up.upgraded_at   IS NOT NULL THEN 1 ELSE 0 END AS f_upgraded
    FROM organizations o
    LEFT JOIN user_counts uc ON uc.org_id = o.org_id   -- LEFT JOINs keep orgs with no matching rows
    LEFT JOIN first_call  fc ON fc.org_id = o.org_id   -- (NULL comparison above evaluates to ELSE 0)
    LEFT JOIN quota_reach qr ON qr.org_id = o.org_id
    LEFT JOIN upgrades    up ON up.org_id = o.org_id
),
funnel AS (                                        -- turn flags into 5 rows (one per step)
    SELECT 1 AS step_no, 'Signup'            AS step_name, COUNT(*)                                                      AS orgs FROM org_flags
    UNION ALL
    SELECT 2, 'User Invited',                 SUM(f_invited)                                                                     FROM org_flags
    UNION ALL
    SELECT 3, 'First API Call (<=7d)',        SUM(f_invited * f_first_call)                                                      FROM org_flags
    UNION ALL
    SELECT 4, 'Reached 80% Quota',            SUM(f_invited * f_first_call * f_reached_80)                                       FROM org_flags
    UNION ALL
    SELECT 5, 'Upgraded to Pro',              SUM(f_invited * f_first_call * f_reached_80 * f_upgraded)                          FROM org_flags
)                                                  -- multiplying 0/1 flags = logical AND => strict sequence
SELECT step_no,
       step_name,
       orgs,
       ROUND(100.0 * orgs / FIRST_VALUE(orgs) OVER (ORDER BY step_no), 1)       AS pct_of_signups,       -- window: always divide by step 1
       ROUND(100.0 * orgs / NULLIF(LAG(orgs) OVER (ORDER BY step_no), 0), 1)    AS step_conversion_pct,  -- window: vs previous step
       ROUND(100.0 - 100.0 * orgs / NULLIF(LAG(orgs) OVER (ORDER BY step_no), 0), 1) AS step_dropoff_pct -- complement of conversion
FROM funnel
ORDER BY step_no;


-- ===== QUERY 2 ========================================================================
-- TIME-TO-VALUE (TTV) COHORTS: hours from org creation to first HTTP 200
WITH
first_success AS (                                 -- first successful call per org
    SELECT l.org_id, MIN(l.timestamp) AS first_200_at
    FROM api_usage_logs l
    WHERE l.status_code = 200
    GROUP BY l.org_id
),
org_ttv AS (                                       -- one row per org with its cohort and TTV
    SELECT o.org_id,
           DATE_TRUNC('month', o.created_at)::date AS cohort_month,          -- signup month = cohort
           EXTRACT(EPOCH FROM (fs.first_200_at - o.created_at)) / 3600.0 AS ttv_hours   -- timestamp subtraction -> hours
    FROM organizations o
    LEFT JOIN first_success fs ON fs.org_id = o.org_id    -- never-activated orgs get ttv_hours = NULL
),
cohort_stats AS (                                  -- aggregate per cohort
    SELECT cohort_month,
           COUNT(*)                                          AS signups,
           COUNT(ttv_hours)                                  AS activated_orgs,      -- COUNT(col) skips NULLs
           COUNT(*) FILTER (WHERE ttv_hours <= 168)          AS activated_7d,        -- 168h = 7 days
           COUNT(*) FILTER (WHERE ttv_hours <= 24)           AS activated_24h,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ttv_hours) AS median_ttv_h,   -- robust to slow outliers
           PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ttv_hours) AS p90_ttv_h,      -- tail experience
           AVG(ttv_hours)                                    AS avg_ttv_h
    FROM org_ttv
    GROUP BY cohort_month
)
SELECT cohort_month,
       signups,
       activated_orgs,
       ROUND(100.0 * activated_7d  / signups, 1)             AS activation_rate_7d_pct,
       ROUND(100.0 * activated_24h / signups, 1)             AS activated_24h_pct,
       ROUND(median_ttv_h::numeric, 1)                       AS median_ttv_h,
       ROUND(p90_ttv_h::numeric, 1)                          AS p90_ttv_h,
       ROUND(avg_ttv_h::numeric, 1)                          AS avg_ttv_h,
       ROUND((median_ttv_h - LAG(median_ttv_h) OVER (ORDER BY cohort_month))::numeric, 1)
                                                             AS median_mom_change_h,   -- window: vs prior cohort (negative = faster)
       ROUND(AVG(median_ttv_h) OVER (ORDER BY cohort_month
             ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)::numeric, 1)
                                                             AS rolling_3m_median_h,   -- window: smooth the noise
       RANK() OVER (ORDER BY median_ttv_h)                   AS speed_rank             -- 1 = fastest cohort
FROM cohort_stats
ORDER BY cohort_month;


-- ===== QUERY 3 ========================================================================
-- PRODUCT-QUALIFIED LEADS (PQL): active Free orgs at 80-99% utilisation OR >5 HTTP 429 in 7 days
-- Utilisation is read from metering events (logs are a sample; in production replace
-- the band logic with  COUNT(200s this cycle) / quota_limit).
WITH
params AS (SELECT TIMESTAMP '2026-11-15 12:00:00' AS as_of),   -- replace with NOW() in production
cycle AS (                                         -- current billing cycle = calendar month
    SELECT as_of, DATE_TRUNC('month', as_of) AS cycle_start FROM params
),
quota_state AS (                                   -- what happened to each org this cycle
    SELECT e.org_id,
           MAX(e.event_timestamp) FILTER (WHERE e.event_type = 'Quota_80_Reached')  AS last_80_at,
           MAX(e.event_timestamp) FILTER (WHERE e.event_type = 'Quota_100_Blocked') AS last_100_at,
           BOOL_OR(e.event_type = 'Upgrade_Initiated'
                   AND e.event_timestamp > c.as_of - INTERVAL '7 days')             AS clicked_upgrade_7d  -- explicit buying intent
    FROM subscription_events e
    CROSS JOIN cycle c                             -- single-row CTE: expose its columns to every event
    WHERE e.event_timestamp >= c.cycle_start AND e.event_timestamp <= c.as_of
    GROUP BY e.org_id
),
recent_logs AS (                                   -- request behaviour, last 14 days
    SELECT l.org_id,
           COUNT(*) FILTER (WHERE l.status_code = 429
                            AND l.timestamp > c.as_of - INTERVAL '7 days')   AS n429_7d,
           COUNT(*) FILTER (WHERE l.status_code = 200)                       AS calls_ok_14d
    FROM api_usage_logs l
    CROSS JOIN cycle c
    WHERE l.timestamp > c.as_of - INTERVAL '14 days' AND l.timestamp <= c.as_of
    GROUP BY l.org_id
),
team AS (SELECT org_id, COUNT(*) AS team_size FROM users GROUP BY org_id),
candidates AS (                                    -- one row per active Free org with all signals
    SELECT o.org_id, o.org_name,
           CASE WHEN qs.last_100_at IS NOT NULL THEN '100% blocked'
                WHEN qs.last_80_at  IS NOT NULL THEN '80-99%'
                ELSE 'below 80%' END                                AS utilisation_band,
           COALESCE(rl.n429_7d, 0)                                  AS n429_7d,
           COALESCE(rl.calls_ok_14d, 0)                             AS calls_ok_14d,
           COALESCE(t.team_size, 1)                                 AS team_size,
           COALESCE(qs.clicked_upgrade_7d, FALSE)                   AS clicked_upgrade_7d
    FROM organizations o
    LEFT JOIN quota_state qs ON qs.org_id = o.org_id
    LEFT JOIN recent_logs rl ON rl.org_id = o.org_id
    LEFT JOIN team        t  ON t.org_id  = o.org_id
    WHERE o.plan_tier = 'Free' AND o.status = 'Active'
),
scored AS (                                        -- transparent additive scoring (max 100)
    SELECT c.*,
           LEAST(100,
               CASE utilisation_band WHEN '80-99%' THEN 40 WHEN '100% blocked' THEN 30 ELSE 0 END      -- quota pressure
             + CASE WHEN n429_7d > 15 THEN 30 WHEN n429_7d > 5 THEN 20 ELSE 0 END                      -- friction right now
             + CASE WHEN team_size >= 3 THEN 15 WHEN team_size = 2 THEN 8 ELSE 0 END                   -- multi-seat = budget
             + CASE WHEN calls_ok_14d >= 5 THEN 15 WHEN calls_ok_14d >= 1 THEN 8 ELSE 0 END            -- still engaged
             + CASE WHEN clicked_upgrade_7d THEN 10 ELSE 0 END) AS pql_score                           -- intent
    FROM candidates c
    WHERE utilisation_band = '80-99%' OR n429_7d > 5           -- the PQL gate from the brief
)
SELECT RANK() OVER (ORDER BY pql_score DESC, n429_7d DESC)      AS priority_rank,   -- window: sales call order
       org_id, org_name, utilisation_band, n429_7d, calls_ok_14d, team_size, clicked_upgrade_7d, pql_score,
       CASE WHEN pql_score >= 70 THEN 'Hot'  WHEN pql_score >= 45 THEN 'Warm' ELSE 'Watch' END AS pql_tier,
       CASE WHEN clicked_upgrade_7d            THEN 'Follow up on started upgrade'
            WHEN utilisation_band = '100% blocked' THEN 'Offer instant Pro unblock'
            WHEN utilisation_band = '80-99%'   THEN 'Send usage-warning + upgrade nudge'
            ELSE 'Investigate 429 pattern' END                                                    AS recommended_action
FROM scored
ORDER BY priority_rank, org_id;


-- ===== QUERY 4 ========================================================================
-- UPGRADE CONVERSION IMPACT: 80% warning vs 100% hard block
-- Unit of analysis = one org x one billing cycle (its first quota trigger that month).
-- Converted = first paid invoice lands within 30 days AND before the org's next trigger,
-- so each payment is credited to exactly one (the most recent) exposure.
WITH
trigger_events AS (
    SELECT e.org_id, e.event_type, e.event_timestamp AS trig_at,
           ROW_NUMBER() OVER (PARTITION BY e.org_id, DATE_TRUNC('month', e.event_timestamp)
                              ORDER BY e.event_timestamp) AS rn_in_cycle      -- 1 = first trigger that month
    FROM subscription_events e
    WHERE e.event_type IN ('Quota_80_Reached', 'Quota_100_Blocked')
),
cycle_exposure AS (                                -- keep only the first trigger per org-cycle
    SELECT org_id, event_type AS trigger_type, trig_at,
           LEAD(trig_at) OVER (PARTITION BY org_id ORDER BY trig_at) AS next_trig_at   -- window: next exposure of same org
    FROM trigger_events
    WHERE rn_in_cycle = 1
),
paid_ranked AS (                                   -- rank each org's payments to find the first one
    SELECT org_id, event_timestamp AS paid_at, mrr_amount,
           ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY event_timestamp) AS rn
    FROM subscription_events
    WHERE event_type = 'Payment_Succeeded'
),
first_paid AS (SELECT org_id, paid_at, mrr_amount AS first_mrr FROM paid_ranked WHERE rn = 1),
exposures AS (
    SELECT ce.*, fp.paid_at, fp.first_mrr,
           CASE WHEN fp.paid_at IS NOT NULL
                 AND fp.paid_at >= ce.trig_at
                 AND fp.paid_at <= ce.trig_at + INTERVAL '30 days'
                 AND (ce.next_trig_at IS NULL OR fp.paid_at < ce.next_trig_at)
                THEN 1 ELSE 0 END AS converted,                      -- last-touch attribution
           CASE WHEN EXISTS (SELECT 1 FROM subscription_events u
                             WHERE u.org_id = ce.org_id AND u.event_type = 'Upgrade_Initiated'
                               AND u.event_timestamp >= ce.trig_at
                               AND u.event_timestamp <= ce.trig_at + INTERVAL '30 days'
                               AND (ce.next_trig_at IS NULL OR u.event_timestamp < ce.next_trig_at))
                THEN 1 ELSE 0 END AS initiated                       -- clicked upgrade (even if never paid)
    FROM cycle_exposure ce
    LEFT JOIN first_paid fp ON fp.org_id = ce.org_id
    WHERE fp.paid_at IS NULL OR fp.paid_at >= ce.trig_at             -- drop exposures after the org already pays
),
cohort_stats AS (
    SELECT CASE trigger_type WHEN 'Quota_80_Reached' THEN '80% warning'
                             ELSE '100% hard block (no warning)' END AS cohort,
           COUNT(*)                                           AS exposures,
           COUNT(DISTINCT org_id)                             AS distinct_orgs,
           SUM(initiated)                                     AS upgrades_initiated,
           SUM(converted)                                     AS paid_conversions,
           100.0 * SUM(initiated)  / COUNT(*)                 AS initiate_rate_pct,
           100.0 * SUM(converted)  / COUNT(*)                 AS conversion_rate_pct,
           AVG(first_mrr) FILTER (WHERE converted = 1)        AS avg_mrr_per_conversion,
           100.0 * SUM(CASE WHEN converted = 1 THEN first_mrr ELSE 0 END) / COUNT(*) AS mrr_per_100_exposures,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (paid_at - trig_at)) / 86400.0)
               FILTER (WHERE converted = 1)                   AS median_days_to_convert
    FROM exposures
    GROUP BY 1
)
SELECT cohort, exposures, distinct_orgs, upgrades_initiated, paid_conversions,
       ROUND(initiate_rate_pct::numeric, 2)   AS initiate_rate_pct,
       ROUND(conversion_rate_pct::numeric, 2) AS conversion_rate_pct,
       ROUND(avg_mrr_per_conversion::numeric, 2) AS avg_mrr_per_conversion,
       ROUND(mrr_per_100_exposures::numeric, 2)  AS mrr_per_100_exposures,
       ROUND(median_days_to_convert::numeric, 1) AS median_days_to_convert,
       ROUND((conversion_rate_pct / NULLIF(MAX(conversion_rate_pct)
             FILTER (WHERE cohort LIKE '100%') OVER (), 0))::numeric, 1) AS lift_vs_hard_block   -- window across both rows
FROM cohort_stats
ORDER BY cohort DESC;


-- ===== QUERY 5 ========================================================================
-- PAYMENT FAILURE & CHURN RISK: Past_Due orgs with failed Stripe payments and high API volume
WITH
params AS (SELECT TIMESTAMP '2026-11-15 12:00:00' AS as_of),   -- replace with NOW() in production
failed AS (                                        -- failure history per org
    SELECT org_id,
           COUNT(*)             AS failed_attempts,
           MIN(event_timestamp) AS first_failed_at,
           MAX(event_timestamp) AS last_failed_at,
           MAX(mrr_amount)      AS mrr_at_risk           -- invoice amount that failed
    FROM subscription_events
    WHERE event_type = 'Payment_Failed'
    GROUP BY org_id
),
last_paid AS (SELECT org_id, MAX(event_timestamp) AS last_paid_at
              FROM subscription_events WHERE event_type = 'Payment_Succeeded' GROUP BY org_id),
volume AS (                                        -- lifetime and recent successful traffic
    SELECT l.org_id,
           COUNT(*) FILTER (WHERE l.status_code = 200)                                       AS calls_total,
           COUNT(*) FILTER (WHERE l.status_code = 200
                            AND l.timestamp > p.as_of - INTERVAL '30 days')                  AS calls_30d
    FROM api_usage_logs l CROSS JOIN params p
    GROUP BY l.org_id
),
paid_volume_rank AS (                              -- compare only against paying orgs
    SELECT o.org_id, v.calls_total, v.calls_30d,
           NTILE(4) OVER (ORDER BY v.calls_total)            AS volume_quartile,   -- 4 = heaviest 25%
           ROUND((100 * PERCENT_RANK() OVER (ORDER BY v.calls_total))::numeric, 0) AS volume_percentile
    FROM organizations o
    JOIN volume v ON v.org_id = o.org_id
    WHERE o.plan_tier IN ('Pro', 'Enterprise')
),
at_risk AS (
    SELECT o.org_id, o.org_name, o.plan_tier, o.status,
           f.failed_attempts, f.last_failed_at, f.mrr_at_risk,
           pv.calls_total, pv.calls_30d, pv.volume_quartile, pv.volume_percentile,
           ROUND((EXTRACT(EPOCH FROM (p.as_of - f.last_failed_at)) / 3600.0)::numeric, 1) AS hours_since_failure,
           ROUND((72 - EXTRACT(EPOCH FROM (p.as_of - f.last_failed_at)) / 3600.0)::numeric, 1) AS grace_hours_left  -- 3-day grace
    FROM organizations o
    JOIN failed f                 ON f.org_id  = o.org_id
    JOIN paid_volume_rank pv      ON pv.org_id = o.org_id
    LEFT JOIN last_paid lp        ON lp.org_id = o.org_id
    CROSS JOIN params p
    WHERE o.status = 'Past_Due'
      AND f.last_failed_at > COALESCE(lp.last_paid_at, TIMESTAMP '1970-01-01')  -- failure is unrecovered (no later success)
)
SELECT org_id, org_name, plan_tier, failed_attempts, mrr_at_risk,
       calls_total, calls_30d, volume_quartile, volume_percentile,
       hours_since_failure, grace_hours_left,
       CASE WHEN volume_quartile >= 3 AND grace_hours_left < 24 THEN 'Critical: call today'
            WHEN volume_quartile >= 3                          THEN 'High: heavy user, fix card'
            WHEN grace_hours_left < 24                         THEN 'Medium: grace ending'
            ELSE 'Monitor' END                                 AS churn_risk,
       SUM(mrr_at_risk) OVER ()                                AS total_mrr_at_risk,          -- window: portfolio exposure
       ROUND(100.0 * SUM(mrr_at_risk) OVER (ORDER BY mrr_at_risk DESC, calls_total DESC)
             / SUM(mrr_at_risk) OVER (), 1)                    AS cumulative_mrr_pct          -- window: running share (Pareto)
FROM at_risk
ORDER BY mrr_at_risk DESC, calls_total DESC;
