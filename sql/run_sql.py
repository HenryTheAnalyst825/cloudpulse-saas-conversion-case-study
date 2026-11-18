import os
import re
import duckdb

# 1. Kết nối DuckDB và nạp 4 file CSV từ thư mục 'out' thành 4 bảng SQL
con = duckdb.connect()
tables = ["organizations", "users", "api_usage_logs", "subscription_events"]

for t in tables:
    csv_path = f"out/{t}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Không tìm thấy {csv_path}. Hãy chắc chắn bạn đang chạy ở D:\\tài liệu\\B2B SaaS")
    con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{csv_path}')")
    print(f"[OK] Đã nạp bảng: {t}")

# 2. Bộ 5 câu truy vấn SQL chuẩn (Tự động chạy và xuất ra CSV cho Power BI)
built_in_queries = {
    "query_1_onboarding_funnel": """
        WITH org_stats AS (
            SELECT 
                o.org_id,
                o.created_at,
                (SELECT COUNT(*) FROM users u WHERE u.org_id = o.org_id AND u.role != 'Admin') AS invited_users,
                (SELECT MIN(l.timestamp) FROM api_usage_logs l WHERE l.org_id = o.org_id AND l.status_code = 200) AS first_ok_call,
                EXISTS(SELECT 1 FROM subscription_events e WHERE e.org_id = o.org_id AND e.event_type IN ('Quota_80_Reached', 'Quota_100_Blocked')) AS hit_quota,
                EXISTS(SELECT 1 FROM subscription_events e WHERE e.org_id = o.org_id AND e.event_type = 'Payment_Succeeded') AS converted_paid
            FROM organizations o
        )
        SELECT 
            stage_order,
            funnel_stage,
            org_count,
            ROUND(org_count * 100.0 / FIRST_VALUE(org_count) OVER (ORDER BY stage_order), 2) AS pct_of_total,
            ROUND(org_count * 100.0 / LAG(org_count, 1, org_count) OVER (ORDER BY stage_order), 2) AS step_conversion_pct
        FROM (
            SELECT 1 AS stage_order, '1. Signed Up (Trial_Started)' AS funnel_stage, COUNT(*) AS org_count FROM org_stats
            UNION ALL
            SELECT 2, '2. Invited Team Member', COUNT(*) FROM org_stats WHERE invited_users > 0
            UNION ALL
            SELECT 3, '3. Activated (1st API 200 <= 7d)', COUNT(*) FROM org_stats WHERE first_ok_call IS NOT NULL AND EPOCH(first_ok_call - created_at)/3600.0 <= 168
            UNION ALL
            SELECT 4, '4. Reached Quota Threshold (80%/100%)', COUNT(*) FROM org_stats WHERE hit_quota
            UNION ALL
            SELECT 5, '5. Upgraded to Paid Plan', COUNT(*) FROM org_stats WHERE converted_paid
        ) t
        ORDER BY stage_order;
    """,
    "query_2_time_to_value_cohorts": """
        WITH first_calls AS (
            SELECT 
                o.org_id,
                STRFTIME(o.created_at, '%Y-%m') AS signup_month,
                o.created_at,
                MIN(l.timestamp) FILTER (WHERE l.status_code = 200) AS first_200_ts
            FROM organizations o
            LEFT JOIN api_usage_logs l ON o.org_id = l.org_id
            GROUP BY 1, 2, 3
        )
        SELECT 
            signup_month,
            COUNT(*) AS total_signups,
            COUNT(first_200_ts) FILTER (WHERE EPOCH(first_200_ts - created_at)/3600.0 <= 168) AS activated_7d_count,
            ROUND(COUNT(first_200_ts) FILTER (WHERE EPOCH(first_200_ts - created_at)/3600.0 <= 168) * 100.0 / COUNT(*), 2) AS activation_7d_pct,
            ROUND(MEDIAN(EPOCH(first_200_ts - created_at)/3600.0), 1) AS median_ttv_hours
        FROM first_calls
        GROUP BY signup_month
        ORDER BY signup_month;
    """,
    "query_3_pql_scoring_leads": """
        WITH usage_summary AS (
            SELECT 
                o.org_id,
                o.org_name,
                o.plan_tier,
                o.status,
                COUNT(l.log_id) FILTER (WHERE l.status_code = 200) AS ok_calls_sample,
                COUNT(l.log_id) FILTER (WHERE l.status_code = 429) AS error_429_count,
                COUNT(DISTINCT e.event_id) FILTER (WHERE e.event_type = 'Quota_80_Reached') AS warning_80_events,
                COUNT(DISTINCT e.event_id) FILTER (WHERE e.event_type = 'Quota_100_Blocked') AS blocked_100_events
            FROM organizations o
            LEFT JOIN api_usage_logs l ON o.org_id = l.org_id
            LEFT JOIN subscription_events e ON o.org_id = e.org_id
            WHERE o.plan_tier = 'Free' AND o.status = 'Active'
            GROUP BY 1, 2, 3, 4
        )
        SELECT 
            org_id,
            org_name,
            ok_calls_sample,
            error_429_count,
            warning_80_events,
            blocked_100_events,
            (warning_80_events * 15 + blocked_100_events * 25 + error_429_count * 5) AS pql_score,
            CASE 
                WHEN error_429_count >= 5 OR blocked_100_events >= 2 THEN 'High PQL (Trigger Sales & 1-Click Modal)'
                WHEN warning_80_events >= 1 THEN 'Warm PQL (Show In-App 80% Banner)'
                ELSE 'Low'
            END AS pql_segment
        FROM usage_summary
        WHERE warning_80_events > 0 OR blocked_100_events > 0 OR error_429_count > 0
        ORDER BY pql_score DESC
        LIMIT 25;
    """,
    "query_4_warned_vs_abrupt_conversion": """
        WITH org_trigger_type AS (
            SELECT 
                o.org_id,
                o.plan_tier,
                BOOL_OR(e.event_type = 'Quota_80_Reached') AS had_80_warning,
                BOOL_OR(e.event_type = 'Quota_100_Blocked') AS had_100_block,
                BOOL_OR(e.event_type = 'Payment_Succeeded' AND o.plan_tier = 'Pro') AS converted_to_pro,
                MAX(e.mrr_amount) FILTER (WHERE e.event_type = 'Payment_Succeeded') AS paid_mrr
            FROM organizations o
            JOIN subscription_events e ON o.org_id = e.org_id
            WHERE o.plan_tier IN ('Free', 'Pro')
            GROUP BY 1, 2
        ),
        classified AS (
            SELECT 
                org_id,
                CASE 
                    WHEN had_80_warning THEN 'Warned at 80% Quota (Proactive Banner)'
                    WHEN had_100_block AND NOT had_80_warning THEN 'Abrupt 100% Block Only (HTTP 429 Wall)'
                END AS quota_experience_cohort,
                converted_to_pro,
                COALESCE(paid_mrr, 0) AS paid_mrr
            FROM org_trigger_type
            WHERE had_80_warning OR had_100_block
        )
        SELECT 
            quota_experience_cohort,
            COUNT(*) AS total_orgs_hitting_threshold,
            COUNT(*) FILTER (WHERE converted_to_pro) AS converted_orgs,
            ROUND(COUNT(*) FILTER (WHERE converted_to_pro) * 100.0 / COUNT(*), 2) AS conversion_rate_pct,
            SUM(paid_mrr) AS total_new_mrr_usd
        FROM classified
        GROUP BY 1
        ORDER BY conversion_rate_pct DESC;
    """,
    "query_5_dunning_churn_risk": """
        SELECT 
            o.org_id,
            o.org_name,
            o.plan_tier,
            o.status AS current_account_status,
            COUNT(e.event_id) FILTER (WHERE e.event_type = 'Payment_Failed') AS failed_payment_attempts,
            MAX(e.event_timestamp) FILTER (WHERE e.event_type = 'Payment_Failed') AS last_failed_payment_ts,
            MAX(e.mrr_amount) AS mrr_at_risk_usd,
            COUNT(l.log_id) FILTER (WHERE l.status_code = 200) AS historical_successful_calls
        FROM organizations o
        JOIN subscription_events e ON o.org_id = e.org_id
        LEFT JOIN api_usage_logs l ON o.org_id = l.org_id
        WHERE o.status IN ('Past_Due', 'Suspended')
        GROUP BY 1, 2, 3, 4
        ORDER BY current_account_status, mrr_at_risk_usd DESC;
    """
}

# Chạy và xuất 5 file CSV kết quả vào thư mục out/
for name, sql in built_in_queries.items():
    print(f"\n================ {name.upper()} ================")
    df = con.execute(sql).df()
    print(df.to_string(index=False))
    out_csv = f"out/{name}.csv"
    df.to_csv(out_csv, index=False)
    print(f"--> Đã xuất file cho Power BI: {out_csv}")