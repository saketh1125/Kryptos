# Monitoring and Observability Spec — GHKGE
**Document Version:** 1.0  
**Status:** Approved  
**Target Audience:** DevOps Engineers, Backend Engineers, System Administrators.

---

## 1. Structured Logging Principles
To enable quick debugging without terminal log tracking, GHKGE logs are standardized as structured JSON output. This ensures they can be directly parsed by log aggregators or queried in database logs:

```json
{
  "timestamp": "2026-06-22T14:40:05.123Z",
  "level": "INFO",
  "module": "compliance.engine",
  "run_id": "8fa27003-8d68-45a8-9d62-67852c002f23",
  "event": "compliance_check_passed",
  "domain": "openstreetmap.org",
  "details": {
    "url": "https://openstreetmap.org/api/0.6/map?bbox=25.268,82.980,25.350,83.030",
    "rate_limit_wait_s": 0.0,
    "robots_parsed": true
  }
}
```

---

## 2. Core Telemetry Metrics

### 2.1 Pipeline Orchestration Metrics
*   **`acquisition_run_latency_seconds`**: Measures duration of crawling loops.
*   **`run_success_rate`**: Tracked via `status` field checks in `acquisition_runs`.
*   **`active_concurrency_workers`**: Tracks current headless browser processes (target <= 3).

### 2.2 Extraction & Cost Metrics
*   **`llm_tokens_consumed_total`**: Broken down by `prompt_tokens` and `completion_tokens`. Used to calculate daily cost.
*   **`fact_extraction_yield`**: Ratio of total facts extracted over raw bytes processed.
*   **`macro_knowledge_discard_rate`**: Percentage of extracted Pydantic objects filtered out because `is_macro_knowledge = True`.

### 2.3 Compliance & Health Metrics
*   **`compliance_blocked_requests_total`**: Grouped by reason (`robots_disallow`, `denylisted_domain`, `rate_limited`).
*   **`supabase_connection_pool_size`**: Dynamic connection tracker (monitors free-tier pool exhaustion).
*   **`neo4j_batch_write_errors`**: Tracks transaction timeouts.

---

## 3. Alerts Configuration Thresholds

| Alert Rule | Metric Condition | Severity | System Action |
|---|---|---|---|
| **Rate Limit Lockout** | `rate_limited` blocks > 50 in 1 hour | Warning | Temporarily pause active crawler strategy for 30 minutes. |
| **Token Exhaustion Warning** | Daily cost exceeds $5.00 limit | Critical | Immediately halt all scheduled runs; notify admin console. |
| **Free-Tier Memory Alert** | Docker container RAM > 14.5 GB | Critical | Trigger graceful exit, flush active db transactions, restart. |
| **Database Pool Timeout** | Supabase connection timeouts > 5 | Warning | Trigger active connection recycle; throttle batch sizes. |
