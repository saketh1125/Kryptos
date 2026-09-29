# Operations Runbook — GHKGE

| Field | Value |
|---|---|
| **Document ID** | KRY-OPS-001 |
| **Revision** | 1.0 |
| **Status** | Implemented |
| **Supersedes** | — |
| **Last updated** | 2026-09-29 |
| **Owner** | Kryptos maintainers |
| **Audience** | Operators, on-call engineers, whoever runs the first deploy |

This is the procedure document. It covers what to do on a first deploy, what the
system is doing at any moment, and what to do when it stops.

> **Read §1 before deploying.** The system has never been run against live
> services. [KRY-CONF-001](13_conformance_matrix.md) D-01 through D-03 are
> release-blocking, and a first deploy should be expected to surface further
> issues that no local test can reach.

---

## 1. Prerequisites

| Service | Purpose | Free tier |
|---|---|---|
| PostgreSQL + pgvector | system of record, embeddings, task bus | Supabase |
| Neo4j | entity graph | AuraDB Free |
| Ollama (local or host) | embeddings, `nomic-embed-text`, 768-dim | self-hosted |
| OpenRouter or an OpenAI-compatible endpoint | fact extraction | — |

The PostgreSQL instance needs the `vector` and `uuid-ossp` extensions. They are
created by `sql/schema.sql`, which must be applied manually.

```bash
psql "$GHKGE_DATABASE_URL" -f sql/schema.sql
```

**On Supabase**, append `?sslmode=require` to the DSN — remote instances refuse
unencrypted connections, and this is not in the default value:

```bash
GHKGE_DATABASE_URL=postgresql+asyncpg://user:pass@db.xxx.supabase.co:5432/postgres?sslmode=require
```

⚠ **Check the pool budget before first deploy.** The app opens a SQLAlchemy pool
of 5 + up to 10 overflow *and* a separate asyncpg pool of 2–5 — up to 20
connections. Supabase free tier allows 15 (D-09). Reduce `pool_size` /
`max_overflow` in `database/connection.py` if the provider is free-tier.

---

## 2. First Deploy

```bash
pip install -e ".[all,dev]"
cp .env.example .env      # fill in keys
psql "$GHKGE_DATABASE_URL" -f sql/schema.sql
python main.py
```

On startup the app initialises the database, starts four worker loops
(planner, harvester, synthesis, consolidator) and the APScheduler gap-evaluation
job. `/health` should return `{"status":"ok"}` within a second.

⚠ `main.py` runs uvicorn with `reload=True`, which is correct for development
and wrong for a container. A deployment entrypoint should set `reload=False`.

### Smoke test

Do not conclude the system works from a green `/health`. Drive one run end to
end against a single grid cell:

```bash
# 1. Confirm the gap evaluator populated the queue.
curl -s "localhost:8000/admin/v1/gaps?domain=example_hyperlocal_domain&status=open" | jq '.gaps | length'

# 2. Queue a run limited to one entity type.
curl -s -X POST localhost:8000/admin/v1/runs \
  -H 'content-type: application/json' \
  -d '{"domain":"example_hyperlocal_domain","entity_types":["landmark"],"trigger":"manual"}' | jq

# 3. Poll it. Status reaches completed/partial only when every task settles.
curl -s localhost:8000/admin/v1/runs/<run_id> | jq '{status,facts_extracted,entities_written,errors}'
```

Then confirm the data actually landed:

```sql
SELECT count(*) FROM raw_captures WHERE run_id = '<run_id>';
SELECT count(*) FROM extracted_facts WHERE resolution_status = 'approved';
SELECT count(*) FROM entities;
SELECT * FROM strategy_yield_log ORDER BY logged_at DESC LIMIT 5;
```

**Expect zero `raw_captures` rows.** That is D-02: the rate limiter
double-acquires and denies every non-Overpass fetch. It is the single most
likely first-deploy failure, and it is invisible to the test suite because every
test mocks the compliance gate.

A run that reaches `completed` with `facts_extracted: 0` and no errors is the
silent-stall signature of D-03: gaps stuck in `in_progress`, work never
rescheduled.

---

## 3. What the System Is Doing

```
gap_evaluator (APScheduler, 24h)
    │  writes missing / anomaly / reinforce gaps
    ▼
gap_queue ──▶ planner worker
                 │  claims highest-severity open gaps, ranks strategies (KRY-RFP-001 §3)
                 ▼
             task_queue : harvest
                 │
                 ▼
             harvester worker ──▶ ComplianceEngine ──▶ ApprovedTarget
                 │                  robots + denylist + platform + rate limit
                 │  writes raw_captures FIRST (decouples scrape from LLM)
                 ▼
             task_queue : extract
                 │
                 ▼
             synthesis worker ──▶ chunk 800w/150o ──▶ instructor+LLM
                 │                  macro knowledge dropped
                 ▼
             task_queue : consolidate
                 │
                 ▼
             consolidator worker ──▶ fuzzy resolve (85%) ──▶ safety gate
                 │                  pgvector chunk → Neo4j node → commit
                 ▼
             finalize: counters, strategy_yield_log, gap resolution
```

Everything lives in Postgres. Container restarts lose in-flight tasks; the
stale-task sweep returns them to `pending` after
`GHKGE_STALE_TASK_TAKEOVER_MINUTES` (default 10).

Worker loops are strictly serial: at most one task per role in flight. The
`Semaphore(3)` on browsers is therefore decorative — effective concurrency is 1.
Throughput is bounded by `GHKGE_RATE_LIMIT_INTERVAL_S` (2 s) per domain, and a
full municipal bootstrap is a multi-day affair. This is expected, not a fault.

---

## 4. Reading the Logs

All output is structlog. `GHKGE_JSON_LOGGING=true` for newline-delimited JSON.

| Event | Meaning |
|---|---|
| `app.started` | workers and scheduler running |
| `planner.tasks_created` | gaps claimed; count is the plan size |
| `planner.no_gaps` | nothing to do — queue is drained or the domain mismatches |
| `planner.no_target` | strategy has no seed URLs; see `strategy_seeds` in the YAML |
| `compliance.robots_disallow` | target forbidden by robots.txt |
| `compliance.denylisted_domain` | blocked pattern or hostile platform |
| `compliance.rate_limited` | 2 s interval not elapsed |
| `worker.capture_duplicate` | content hash already stored; extraction skipped |
| `worker.harvest_blocked` | compliance refused the URL |
| `worker.task_error` | handler raised; task will retry |
| `worker.neo4j_write_failed_rolling_back` | graph write failed; Postgres rolled back, task requeued |
| `bus.task_failed` | terminal failure after `max_attempts` |
| `bus.stale_tasks_reclaimed` | crash recovery; non-zero after a restart is normal |
| `run.finalized` | run closed; compare `facts_extracted` to `entities_written` |

⚠ **A silent system logs nothing wrong.** Worker and scheduler startup, run
finalization and gap resolution are all wrapped in `contextlib.suppress`
(D-08). Absence of errors is not evidence of health — query the database.

---

## 5. Common Situations

### Gaps stuck in `in_progress`

The signature of D-03. Nothing rescues a gap whose task completed without
consolidating. To recover:

```sql
UPDATE gap_queue SET status = 'open'
WHERE status = 'in_progress' AND created_at < now() - interval '1 day';
```

Then queue a run. A permanent fix belongs in the harvest/extract exit paths.

### A run never leaves `queued`

No planner worker is claiming tasks. Check the app started, and that
`task_queue` exists — a missing table surfaces only as repeated
`worker.claim_failed` (D-10).

```sql
SELECT status, count(*) FROM task_queue GROUP BY status;
```

| Query result | Interpretation |
|---|---|
| rows in `pending` | workers are down or the loop crashed |
| rows in `in_progress`, `claimed_at` recent | working normally |
| rows in `in_progress`, `claimed_at` old | workers died; the reclaim sweep will recover them |
| no rows | nothing was enqueued; check `planner.no_gaps` in the logs |

### Entity found but the API returns it empty

Held facts are invisible by design. Check:

```sql
SELECT resolution_status, count(*) FROM extracted_facts GROUP BY 1;
```

`held_for_review` rows need a moderator:
`POST /admin/v1/facts/{id}/moderate {"action":"approve"}`. With no admin console
and no auth (D-07), this is a manual step — and the reason the safety surface
stays nearly empty by design.

### Everything is `held_for_review`

Almost all safety facts start held, because the tier map classes OSM as tier 2
rather than tier 1 OFFICIAL (D-06). Either fix the map or moderate in bulk.

### Rate limit looks wrong

`domain_rate_limit_state` is the source of truth and survives restarts:

```sql
SELECT * FROM domain_rate_limit_state ORDER BY last_hit_at DESC;
```

If `last_hit_at` never advances, no fetch is being attempted — check the planner.

### Disk or storage pressure

`raw_captures.raw_content` dominates. On free-tier storage, a two-week municipal
crawl at 800-word chunks is substantial. There is no retention job; pruning is
manual and undocumented, which is a known gap.

### Resetting for a clean re-run

```sql
TRUNCATE task_queue, strategy_yield_log, gap_queue;
UPDATE gap_queue SET status = 'open';
```

Entities, captures and facts are retained. To re-evaluate coverage, run the gap
evaluation job and queue a run.

---

## 6. Operational Gaps

Not yet built; listed so their absence is not mistaken for oversights.

| Gap | Impact |
|---|---|
| No container image | The target host (HF Spaces) has no artifact |
| No CI | ruff, mypy and pytest pass locally but nothing enforces it |
| No migrations | `sql/schema.sql` must be applied by hand and can drift from the ORM |
| No metrics or alerting | Health is inferred from logs and ad-hoc SQL |
| No backoff | External calls retry on the task bus, not with exponential delay |
| No auth on `/admin/v1/*` | State-changing endpoints are internet-reachable |
| No retention jobs | Storage grows without bound |
| Silent startup failures | A broken deploy looks healthy (D-08) |

---

## 7. Revision History

| Rev | Date | Change |
|---|---|---|
| 1.0 | 2026-09-29 | Initial runbook, written before first live deploy. |
