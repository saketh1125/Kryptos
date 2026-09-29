# Operations Runbook — GHKGE

| Field | Value |
|---|---|
| **Document ID** | KRY-OPS-001 |
| **Revision** | 1.1 |
| **Status** | Implemented |
| **Supersedes** | KRY-OPS-001 r1.0 |
| **Last updated** | 2026-09-29 |
| **Owner** | Kryptos maintainers |
| **Audience** | Operators, on-call engineers, whoever runs the first deploy |

This is the procedure document. It covers what to do on a first deploy, what the
system is doing at any moment, and what to do when it stops.

> **The release-blocking defects are closed** (D-01, D-02, D-03 — see
> [KRY-CONF-001 §4](13_conformance_matrix.md)). The system has still never run
> against live services: every external path is unverified, and a first deploy
> should be expected to surface issues no local test can reach. This document
> is what to do when that happens.

---

## 1. Prerequisites

| Service | Purpose | Free tier |
|---|---|---|
| PostgreSQL + pgvector | system of record, embeddings, task bus | Supabase |
| Neo4j | entity graph | AuraDB Free |
| Ollama (local or host) | embeddings, `nomic-embed-text`, 768-dim | self-hosted |
| OpenRouter or an OpenAI-compatible endpoint | fact extraction | — |

The schema is applied by Alembic. It creates the `vector` and `uuid-ossp`
extensions along with all 10 tables:

```bash
alembic upgrade head
```

`sql/schema.sql` remains as a readable DDL reference and still applies
cleanly, but the migration is the artifact of record — it is what supports
`upgrade`, and `--autogenerate` produces subsequent changes from it.

**On Supabase**, append `?sslmode=require` to the DSN — remote instances refuse
unencrypted connections, and this is not in the default value:

```bash
GHKGE_DATABASE_URL=postgresql+asyncpg://user:pass@db.xxx.supabase.co:5432/postgres?sslmode=require
```

Pool sizing defaults to 5+5 for SQLAlchemy and 1–3 for the rate limiter's own
asyncpg pool — 13 total, inside Supabase's 15-connection free tier. Adjust
`GHKGE_DB_POOL_SIZE`, `GHKGE_DB_MAX_OVERFLOW` and
`GHKGE_RATE_LIMITER_POOL_{MIN,MAX}` for other plans.

---

## 2. First Deploy

```bash
pip install -e ".[all,dev]"
cp .env.example .env      # fill in keys; set GHKGE_ADMIN_API_KEY
alembic upgrade head
python main.py
```

On startup the app initialises the database, probes for the schema, starts
four worker loops (planner, harvester, synthesis, consolidator) and the
APScheduler gap-evaluation job.

`/health` returns `{"status", "ready", "problems"}`. **Gate on `ready`, not the
status code** — a degraded app deliberately still returns 200 so it stays
reachable for diagnosis. If `ready` is false, `problems` says why.

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

**Expect `raw_captures` rows.** D-02, which previously made this step capture
nothing, is closed and covered by a test that drives the real compliance stack.

If a run reaches `completed` with `facts_extracted: 0` and no errors, suspect
compliance: the captures may have been blocked. Check
`compliance.robots_disallow` and `compliance.denylisted_domain` in the logs —
both are silent by design, so a blocked domain looks exactly like an empty one.

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
| `worker.gap_reopened` | a gap returned to the queue after producing nothing |
| `app.startup_degraded` | startup problem; see `/health` `problems` |
| `app.admin_api_unprotected` | `GHKGE_ADMIN_API_KEY` is unset — set it for any shared deployment |
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

Normally impossible: every non-consolidating exit re-opens its gap, and
`gap_stale_after_minutes` sweeps anything stranded by a crash. To recover
manually — for example after a database restore — :

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
| No metrics or alerting | Health is inferred from logs, `/health`, and ad-hoc SQL |
| No backoff | External calls retry on the task bus, not with exponential delay |
| Shared secret, not identities | Adequate as a gate; the review queue has no moderator identity |
| No retention jobs | Storage grows without bound |
| D-05 audit trail | Blocked requests are logged, not persisted |

---

## 7. Revision History

| Rev | Date | Change |
|---|---|---|
| 1.0 | 2026-09-29 | Initial runbook, written before first live deploy. |
| 1.1 | 2026-09-29 | Alembic replaces manual DDL; D-01/D-02/D-03 closed so the smoke test can now expect captures; `/health` readiness contract; pool sizing documented. |
