# Conformance Matrix — GHKGE

| Field | Value |
|---|---|
| **Document ID** | KRY-CONF-001 |
| **Revision** | 1.0 |
| **Status** | Implemented |
| **Supersedes** | — |
| **Last updated** | 2026-09-29 |
| **Owner** | Kryptos maintainers |
| **Verification** | `ruff` clean · `mypy --strict` clean (40 modules) · 167 tests passing |

**Purpose.** Every requirement in the design set, mapped to its implementation
and its test evidence. This document exists because intent and reality diverge;
recording the divergence precisely is more useful than either document alone.

**Reading the status column.** `IMPLEMENTED` means the requirement is built and
exercised by a test. `PARTIAL` means part of it is built; the gap is named. `MISSING`
means absent. `DEFECTIVE` means built but contradicting the spec or breaking an
invariant. `N/A (v1)` means the design set marks it out of scope.

---

## 1. Verification Basis

**All verification is local and mocked.** Every test runs against SQLite
(`aiosqlite`, in-memory) with the true externals — compliance gate, LLM,
embeddings, Neo4j — replaced by fakes. No test has ever contacted Supabase
Postgres, Neo4j, Ollama, or OpenRouter. `sql/schema.sql` has never been applied
to a real database.

**Consequently:**

- Logic, orchestration, data flow and invariants **are** verified.
- Every external I/O path, the Cypher statements, pgvector behaviour, and
  `FOR UPDATE SKIP LOCKED` concurrency are **unverified** — SQLite compiles
  Postgres types down and silently ignores row locking.
- Any requirement whose only evidence is a mocked test is marked with ⚠ in the
  "Verified by" column.

---

## 2. Requirement Conformance

### 2.1 Product requirements (KRY-PRD-001)

| ID | Requirement | Status | Implementation | Verified by |
|---|---|---|---|---|
| FR-1 | YAML domain config; new domain without code change | IMPLEMENTED | `orchestrator/domain.py:72` | `test_bus_and_domain.py:24-85` |
| FR-2 | Compliance gate on every request | **DEFECTIVE** | `compliance/engine.py:113` | `test_compliance.py:148` ⚠ — see §4.1 |
| FR-3 | Multi-modal harvesting (HTML/PDF/audio/API) | PARTIAL | `harvesters/*` | — ⚠ no harvester executes in any test |
| FR-4 | Schema-constrained extraction with provenance | PARTIAL | `synthesis/refiner.py:36` | — ⚠ unverified end-to-end |
| FR-5 | Fuzzy entity resolution ≥85% | IMPLEMENTED | `consolidation/resolver.py:23` | `test_pipeline_integration.py:481` |
| FR-6 | Tier conflict resolution + safety gate | PARTIAL | `workers.py:350`, `resolver.py:110` | `test_planner_evaluator.py:223` |
| FR-7 | Gap/coverage evaluator (P1) | IMPLEMENTED | `gap_evaluator/evaluator.py` | `test_gap_evaluator.py` (13) |
| FR-8 | Knowledge API | PARTIAL | `api/routes/knowledge.py` | `test_api_routes.py` (23) |
| FR-9 | User correction flywheel (P1) | PARTIAL | `knowledge.py:307` | intake only; no consumer |
| NFR-1 | Query latency <300 ms | **UNVERIFIED** | `knowledge.py:81,123` | see §4.4 |
| NFR-2 | Zero in-process state | IMPLEMENTED | `orchestrator/bus.py` | `test_pipeline_integration.py:281` |
| NFR-3 | Full auditability to raw capture | PARTIAL | `models.py:117` | see §4.5 |
| NFR-4 | $0 infrastructure spend | ARCHITECTURAL | free-tier targets | — |

### 2.2 Architecture (KRY-ARC-001, KRY-REF-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| Task event bus on Postgres | IMPLEMENTED | `orchestrator/bus.py` | `test_pipeline_integration.py` |
| `FOR UPDATE SKIP LOCKED` claiming | IMPLEMENTED ⚠ | `bus.py:93` | no-op on SQLite |
| Retry with attempt ceiling | IMPLEMENTED | `bus.py:123` | `test_pipeline_integration.py:281` |
| Stale-task reclaim after crash | IMPLEMENTED ⚠ | `bus.py:161` | — ⚠ untested |
| Exponential backoff on external calls | MISSING | — | KRY-ENG-001 §3 requires it |
| Cross-pool serialization (SysDes §7) | MISSING | — | three unsynchronised executors |
| Rollback on downstream write failure | IMPLEMENTED ⚠ | `workers.py:430` | — ⚠ untested |
| Statelessness via `last_canonical_id_processed` | PARTIAL | `models.py:62` | column never written |

### 2.3 Data schema (KRY-SCH-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| 8 core tables | IMPLEMENTED | `database/models.py` | `test_pipeline_integration.py:db` |
| `task_queue` (KRY-MAP-001) | IMPLEMENTED | `models.py:170` | `test_bus_and_domain.py` |
| `feedback` (FR-9) | IMPLEMENTED | `models.py:190` | `test_api_routes.py:255` |
| `pgvector` VECTOR(768) | IMPLEMENTED ⚠ | `models.py:151` | Text on SQLite |
| IVFFlat cosine index | DECLARED ⚠ | `sql/schema.sql:125` | — ⚠ never created |
| Alembic / migration strategy | MISSING | — | raw `sql/schema.sql` only |
| DDL ↔ ORM agreement | PARTIAL | `sql/schema.sql:121` hardcodes 768 | — ⚠ nothing validates this |

### 2.4 Data quality (KRY-DQV-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| RapidFuzz ≥85% merge + alias | IMPLEMENTED | `resolver.py:23` | `test_pipeline_integration.py:481` |
| Source tier precedence | PARTIAL | `workers.py:275` | see §4.6 |
| Safety gate: OFFICIAL or ≥2 sources | IMPLEMENTED | `workers.py:350` | `test_planner_evaluator.py:223` |
| Conflict matrix (4 outcomes) | **PARTIAL** | `resolver.py:110` | only `HOLD_FOR_REVIEW` consumed; the other three take one identical path |
| Macro-knowledge filter | IMPLEMENTED ⚠ | `refiner.py:64` | — ⚠ inside the LLM call |
| Tombstoning (KRY-RFP-001 §4) | MISSING | `models.py:105` read 4×, written 0× | — |

### 2.5 Refresh policy (KRY-RFP-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| §1 staleness thresholds per category | IMPLEMENTED | `evaluator.py:32` | `test_planner_evaluator.py:155` |
| §2 density anomaly scan (<30% median) | IMPLEMENTED | `evaluator.py:153` | `test_gap_evaluator.py:170` |
| §2 bootstrap coverage scan | IMPLEMENTED | `evaluator.py:69` | `test_gap_evaluator.py:113` |
| §3 decay scoring `yield × tier × e^(−λt) − cost` | IMPLEMENTED | `planner.py:91` | `test_planner_evaluator.py:41-152` |
| §3 cost term from measured spend | PARTIAL | `planner.py:33` | `cost_estimate_usd` never written; table is hardcoded |
| §4 tombstoning | MISSING | — | see §2.4 |
| Gap lifecycle reaches a terminal state | **DEFECTIVE** | `planner.py:201` | see §4.2 |

### 2.6 Compliance (KRY-CMP-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| `robots.txt` enforcement, fail-closed | IMPLEMENTED | `engine.py:57` | `test_compliance.py:75-110` |
| Global denylist + blocked platforms | IMPLEMENTED | `engine.py:88` | `test_compliance.py:48` |
| Per-domain rate limit ≥2 s, Postgres-backed | **DEFECTIVE** | `rate_limiter.py:22` | see §4.1 |
| Blocked URL consumes no rate slot | IMPLEMENTED | `engine.py:136` | `test_compliance.py:148` |
| Structural enforcement (`ApprovedTarget`) | IMPLEMENTED | `harvesters/base.py:26` | `test_compliance.py:170` |
| §4 compliance audit trail persisted | MISSING | `engine.py:121` logs only | — |
| §3.1 robots cache in Postgres | MISSING | `engine.py:50` in-process | — |

### 2.7 Observability (KRY-OBS-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| Structured logging | IMPLEMENTED | `monitoring/__init__.py` | — |
| JSON renderer for aggregators | IMPLEMENTED | `GHKGE_JSON_LOGGING` | — |
| Per-line `run_id` / `module` context | MISSING | `bind_contextvars` never called | — |
| Nine named metrics | MISSING | — | — |
| Alerting | MISSING | — | — |

### 2.8 Multi-agent protocol (KRY-MAP-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| Five agent roles | IMPLEMENTED | `orchestrator/workers.py` | `test_bus_and_domain.py:198` |
| Task bus envelope | IMPLEMENTED | `bus.py:45` | `test_bus_and_domain.py:106` |
| Polling loop per role | IMPLEMENTED ⚠ | `workers.py:513` | — ⚠ disabled in every test |
| Human-in-the-loop for tied-tier conflicts | MISSING | `resolver.py:110` | see §2.4 |

### 2.9 Out of scope (KRY-PRD-001 §5)

| Requirement | Status | Note |
|---|---|---|
| Admin console (KRY-UI-001) | Not implemented | spec approved, no frontend exists |
| LLM-router planner (KRY-REF-001 Stage 3) | Not implemented | `planner_model` setting is dead |
| Scrapling / OCR failover | Not implemented | `crawl4ai` declared, never imported |
| Authentication | Not implemented | — but see §4.7 |

---

## 3. API Contract Deltas (KRY-API-001)

Field-level comparison of the served OpenAPI against the contract.

| Contract | Served | Note |
|---|---|---|
| `POST /v1/runs` | `POST /admin/v1/runs` | boundary + version composed |
| `GET /v1/runs/{id}` | `GET /admin/v1/runs/{id}` | fields match, incl. `errors[]` |
| `GET /v1/gaps` → `gap_id` | `GET /admin/v1/gaps` → `id` | **field name** |
| `POST .../resolve` → `"skipped"` | echoes `"skip"` | value |
| `GET /entities/{id}/nearby` → `distance_m`, `source_url` | always `null` | no edges written; see §4.3 |
| `GET /entities/{id}/rules` → per-rule corroboration | entity's count reused | semantic |
| `POST /v1/feedback` → 200 | 201 | status code |
| *(not in contract)* | `/admin/v1/facts`, `/admin/v1/facts/{id}/moderate` | additive; moderation has no contract yet |

---

## 4. Open Defects and Risks

Ranked by consequence. Items 1–3 must be fixed before the system is
operationally meaningful; item 1 is a security defect.

### 4.1 D-01 — Cypher injection on an unauthenticated endpoint (Critical)

`GET /api/v1/entities/{id}/nearby?relation=<input>` interpolates the `relation`
query parameter directly into a Cypher pattern.

- **Location:** `api/routes/knowledge.py:196` (unvalidated), `:212` (f-string)
- **Exposure:** the Knowledge API is the public, unauthenticated surface
- **Fix:** constrain `relation` to an allow-list before interpolation
- **Verified:** no — the Cypher has never executed

### 4.2 D-02 — Rate limiter double-acquires; non-Overpass fetch fails (Critical)

The worker calls `compliance.approve()` (consumes the domain slot), then
`harvester.fetch()` re-runs the full gate milliseconds later. With a 2 s
interval, `time_since_last ≈ 0` and the request is denied.

- **Location:** `orchestrator/workers.py:217` → `harvesters/web.py:30` → `compliance/rate_limiter.py:40`
- **Consequence:** every web/PDF/media harvest returns `fetched: false`; only
  Overpass succeeds, because `query_overpass` re-approves exactly once
- **Root cause of the test blind spot:** every integration test mocks the
  compliance gate wholesale, so double-acquisition is unobservable
- **Fix:** let the harvester own the single gate acquisition

### 4.3 D-03 — Gap lifecycle leak (High)

Gaps enter `in_progress` and only leave via successful consolidation. Blocked,
fetch-failed, duplicate, no-seed and zero-fact paths all complete the task
`done` with the gap stuck. The evaluator counts `in_progress` as seen, so it is
never re-queued.

- **Location:** set at `planner.py:201`; leak paths at `workers.py:219,236,325`, `planner.py:176`
- **Consequence:** the pipeline stops making progress after a partial pass,
  silently
- **Fix:** re-open the gap on every non-consolidating exit, plus a sweeper for
  `in_progress` gaps older than N minutes

### 4.4 D-04 — Search N+1 defeats the latency NFR (High)

Search loads up to 500 entities, then issues one `SELECT` per entity for
snippets, and slices to `limit` only afterwards — 500 sequential round-trips for
a default 20 results. Semantic ranking also ignores `type` / `near` /
`radius_m`, ranking globally then intersecting in Python.

- **Location:** `api/routes/knowledge.py:81,123-134,146`
- **Consequence:** KRY-PRD-001 NFR-1 (<300 ms) is unreachable at any real corpus size

### 4.5 D-05 — Auditability gaps (Medium)

- Compliance decisions are logged, never persisted — KRY-CMP-001 §4 requires
  a queryable audit trail; no table exists.
- Neo4j nodes carry no `raw_capture_id`, so NFR-3 ("every node links to a raw
  capture") holds for facts but not graph nodes.

### 4.6 D-06 — Source tier map contradicts KRY-DQV-001 (Medium)

`openstreetmap.org` maps to tier 2; the spec classes OSM as tier 1 OFFICIAL.
Established news domains fall through to tier 4 rather than tier 2. Tier 3 is
unreachable. This directly affects the safety gate: OSM safety facts are held
for review because OSM is not tier 1.

- **Location:** `orchestrator/workers.py:275`
- **Fix:** make the tier map domain-configurable in the YAML rather than a
  hostname-suffix heuristic

### 4.7 D-07 — Unauthenticated admin write surface (Medium)

`POST /admin/v1/runs` and `POST /admin/v1/facts/{id}/moderate` are unauthenticated,
and CORS is `allow_origins=["*"]` with `allow_credentials=True`.

- **Location:** `api/main.py:46`
- **Note:** KRY-PRD-001 §5 declares auth out of scope, but this is an
  internet-reachable state-changing surface

### 4.8 D-08 — Silent degradation (Medium)

Startup of workers and the scheduler is wrapped in bare
`contextlib.suppress(Exception)`, as are the finalize and gap-resolution hooks.
A system that fails to start looks healthy and quietly does nothing.

- **Location:** `api/main.py:26,30`, `scheduler.py:38,43`, `workers.py:490,563`

### 4.9 D-09 — Connection pool likely exceeds free tier (Medium, unverifiable)

SQLAlchemy `pool_size=5 + max_overflow=10` alongside a separate asyncpg pool of
2–5 is up to 20 connections. Supabase free tier allows 15.

- **Location:** `database/connection.py:31`, `orchestrator/workers.py:75`

### 4.10 D-10 — Startup failure is silent on a fresh database (Medium)

A fresh database yields an app that boots, starts four workers, and fails every
query with `relation "task_queue" does not exist` — visible only as a repeating
`worker.claim_failed` log line.

### 4.11 D-11 — Retry delay is inverted (Low)

`bus.fail_task` computes `created_at = now - delay`, which pushes retries to the
*front* of the queue, contradicting its own comment. Harmless only because the
delay is never passed.

- **Location:** `orchestrator/bus.py:146`

### 4.12 D-12 — Data model ceiling (Design, not a bug)

Entities carry a geohash cell and no coordinates, so `near` / `radius_m` measure
from a cell centre (~1.2 km) and a hyperlocal "nearest water station" query
answers with ±600 m of error. Fixing this requires a schema migration, not a
display change. Facts also inherit the grid cell of the gap that produced them,
so entities straddling a cell boundary cannot merge.

---

## 5. Recommended Sequence

**Before the MVP is operable** (in order — each unblocks the next):

1. **D-01** — allow-list `relation`. Security; small diff.
2. **D-02** — single rate-limit acquisition. Without it, no HTML/PDF/media data
   is ever captured.
3. **D-03** — close the gap lifecycle. Without it, the pipeline stalls silently.
4. Self-applying schema (Alembic, or a guarded `create_all`).
5. `sslmode=require` guidance for Supabase; verify D-09 against a live project.
6. A real domain config (the shipped one is named `example_hyperlocal_domain`,
   so a run submitted for `"kashi"` matches zero gaps).
7. An integration test wiring the **real** compliance engine and rate limiter
   into a real `WebHarvester` — the test whose absence hid D-02.

**Then:** D-04, D-07, D-08, D-06, then Neo4j edge writes (which would make
`/nearby` real and satisfy the contract's `distance_m` / `source_url` fields).

**Production hardening:** containerisation for the target host, CI, Alembic,
tombstoning, metrics, backoff, compliance audit persistence, retention jobs,
and an operations runbook ([KRY-OPS-001](14_operations.md)).

---

## 6. Revision History

| Rev | Date | Change |
|---|---|---|
| 1.0 | 2026-09-29 | Initial conformance matrix. Recorded 12 open defects; three are release-blocking. |
