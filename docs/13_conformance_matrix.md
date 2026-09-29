# Conformance Matrix — GHKGE

| Field | Value |
|---|---|
| **Document ID** | KRY-CONF-001 |
| **Revision** | 1.1 |
| **Supersedes** | KRY-CONF-001 r1.0 |
| **Status** | Implemented |
| **Supersedes** | — |
| **Last updated** | 2026-09-29 |
| **Owner** | Kryptos maintainers |
| **Verification** | `ruff` clean · `mypy --strict` clean (41 modules) · 221 tests passing |

**Purpose.** Every requirement in the design set, mapped to its implementation
and its test evidence. This document exists because intent and reality diverge;
recording the divergence precisely is more useful than either document alone.

**r1.1 summary.** Eight of the twelve defects recorded in r1.0 are now closed:
D-01, D-02, D-03, D-04, D-06, D-07, D-08 and D-09. The three release-blocking
defects are all resolved, and a migrations mechanism now exists (D-10). D-05,
D-11 and D-12 remain open; D-11 and D-12 are low-severity or by design.

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
| FR-2 | Compliance gate on every request | IMPLEMENTED | `compliance/engine.py:113` | `test_compliance.py:148` ⚠ — see §4.1 |
| FR-3 | Multi-modal harvesting (HTML/PDF/audio/API) | PARTIAL | `harvesters/*` | — ⚠ no harvester executes in any test |
| FR-4 | Schema-constrained extraction with provenance | PARTIAL | `synthesis/refiner.py:36` | — ⚠ unverified end-to-end |
| FR-5 | Fuzzy entity resolution ≥85% | IMPLEMENTED | `consolidation/resolver.py:23` | `test_pipeline_integration.py:481` |
| FR-6 | Tier conflict resolution + safety gate | PARTIAL (gate fixed) | `workers.py:350`, `resolver.py:110` | `test_planner_evaluator.py:223` |
| FR-7 | Gap/coverage evaluator (P1) | IMPLEMENTED | `gap_evaluator/evaluator.py` | `test_gap_evaluator.py` (13) |
| FR-8 | Knowledge API | PARTIAL | `api/routes/knowledge.py` | `test_api_routes.py` (23) |
| FR-9 | User correction flywheel (P1) | PARTIAL | `knowledge.py:307` | intake only; no consumer |
| NFR-1 | Query latency <300 ms | UNVERIFIED (queries bounded) | `knowledge.py:81,123` | see §4.4 |
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
| Alembic / migration strategy | IMPLEMENTED | `migrations/` | offline SQL verified |
| DDL ↔ ORM agreement | PARTIAL | `sql/schema.sql:121` hardcodes 768 | — ⚠ nothing validates this |

### 2.4 Data quality (KRY-DQV-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| RapidFuzz ≥85% merge + alias | IMPLEMENTED | `resolver.py:23` | `test_pipeline_integration.py:481` |
| Source tier precedence | IMPLEMENTED | `workers.py:275` | `test_planner_evaluator.py` (20) |
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
| Gap lifecycle reaches a terminal state | IMPLEMENTED | `planner.py:201` | see §4.2 |

### 2.6 Compliance (KRY-CMP-001)

| Requirement | Status | Implementation | Verified by |
|---|---|---|---|
| `robots.txt` enforcement, fail-closed | IMPLEMENTED | `engine.py:57` | `test_compliance.py:75-110` |
| Global denylist + blocked platforms | IMPLEMENTED | `engine.py:88` | `test_compliance.py:48` |
| Per-domain rate limit ≥2 s, Postgres-backed | IMPLEMENTED | `rate_limiter.py:22` | see §4.1 |
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

### 4.1 D-01 — Cypher injection on an unauthenticated endpoint — **CLOSED**

`GET /api/v1/entities/{id}/nearby?relation=<input>` interpolated the `relation`
query parameter directly into a Cypher relationship pattern, on the public
unauthenticated surface.

- **Closed:** `relation` is constrained to the five relationship types in
  KRY-SCH-001 §5 before interpolation; anything else returns 400. Validation
  runs before the entity lookup. Entity ids remain bound parameters.
- **Verified:** 15 tests including real injection payloads
  (`tests/test_api_routes.py::TestNearbyRelationAllowList`)

### 4.2 D-02 — Rate limiter double-acquires; non-Overpass fetch fails — **CLOSED**

The worker consumed the per-domain slot via `approve()`, then `fetch()` re-ran
the whole gate milliseconds later; with a 2 s interval the second acquisition
was denied, so every web/PDF/media harvest returned `fetched: false`.

- **Closed:** `ApprovedTarget` carries `validated_at`. A harvester re-check
  within `compliance_revalidate_window_s` (5 s) still re-runs the stateless
  rules — platform policy, denylist, robots.txt — but does not re-charge the
  rate slot. Those rules are the ones that protect the crawl; the interval is
  not a safety property.
- **Verified:** `tests/test_compliance_integration.py` wires the **real**
  ComplianceEngine, robots parser, PostgresRateLimiter and WebHarvester
  together, with only the socket intercepted. Confirmed it fails 4 tests with
  the fix reverted — which is what the mocked suite could never show.

### 4.3 D-03 — Gap lifecycle leak — **CLOSED**

Gaps entered `in_progress` and only left via successful consolidation, so
blocked, fetch-failed, duplicate, no-seed and zero-fact paths left gaps stranded
and coverage silently stopped advancing.

- **Closed:** every non-consolidating exit calls `_reopen_gap`. Duplicates and
  zero-fact captures are included deliberately — both "succeed" but produce no
  new knowledge, so the gap is genuinely still unmet. For crashes between
  claiming and finishing, `sweep_stranded_gaps()` returns `in_progress` gaps
  older than `gap_stale_after_minutes` to the queue, run first by the
  scheduled evaluation.
- **Verified:** 7 lifecycle tests; confirmed 6 fail with `_reopen_gap`
  disabled.

### 4.4 D-04 — Search N+1 defeats the latency NFR — **CLOSED**

Search scanned 500 entities and issued a `SELECT` per entity for its snippet,
slicing to the limit only at the end: 500 round-trips for a 20-result page.

- **Closed:** three statements total — the scan, one best-insight-per-entity
  fetch (windowed `row_number`, portable across Postgres and SQLite), and one
  similarity fetch. Two correctness bugs fixed with it: the semantic ranking
  ignored the `type` and `near`/`radius_m` filters entirely, ranking globally
  then intersecting in Python; and an entity with many chunks took whichever
  row the LIMIT returned.
- **Verified:** a test asserts the query count is identical for 5 and 50
  matching entities.
- **Residual:** absolute latency is still unmeasured — see §1.

### 4.5 D-05 — Auditability gaps (Medium) — **OPEN**

- Compliance decisions are logged, never persisted — KRY-CMP-001 §4 requires
  a queryable audit trail; no table exists.
- Neo4j nodes carry no `raw_capture_id`, so NFR-3 ("every node links to a raw
  capture") holds for facts but not graph nodes.

### 4.6 D-06 — Source tier map contradicts KRY-DQV-001 — **CLOSED**

A hostname-suffix heuristic classed OSM as tier 2, so the corroboration gate
held every OSM safety fact for review — and, since the gate refuses to create an
entity for an uncorroborated safety fact, that left the safety surface nearly
empty. Press fell through to tier 4 and tier 3 was unreachable.

- **Closed:** trust is now `source_tiers` in the domain YAML, matched on the
  registrable domain with an empty-list catch-all, so it is correctable per
  deployment without a code change. OSM and `.gov`/`.nic` are tier 1,
  encyclopaedia and press tier 2, moderated community tier 3, rest tier 4.
- **Verified:** 20 tests over tier resolution, including subdomain and
  `www` handling, and a test that a deployment can override trust in config.

### 4.7 D-07 — Unauthenticated admin write surface — **CLOSED**

Every `/admin/v1` route mutates state the public API then serves.

- **Closed:** a shared-secret gate (`X-Admin-Key`) applied as a
  **router-level** dependency, so a route added later cannot be left open by
  omission. Constant-time comparison. CORS moved from `["*"]` with
  `allow_credentials=True` — a combination browsers reject — to an explicit
  `GHKGE_CORS_ALLOW_ORIGINS` allowlist, with credentials enabled only when it
  is non-empty.
- **Unset key behaviour:** the check is skipped, so local development and the
  test suite still work; the lifespan logs a warning saying the admin API is
  unprotected.
- **Verified:** 8 tests. The Knowledge API is confirmed to stay public.

### 4.8 D-08 — Silent degradation — **PARTIALLY CLOSED**

The lifespan swallowed worker and scheduler startup failures, so a missing
schema or a dead scheduler produced an app that answered `/health` while doing
nothing.

- **Closed:** startup probes for the schema, records problems in
  `app.state.startup_problems`, logs each at error level, and reports them on
  `/health` as `{status, ready, problems}`. A degraded app still returns 200
  so it stays reachable for diagnosis; **anything monitoring it should gate on
  `ready`, not the status code** — that distinction is new API surface.
- **Still open:** the finalize (`workers.py`) and gap-resolution hooks remain
  best-effort by design, since neither can justify failing a completed task.

### 4.9 D-09 — Connection pool exceeded the free tier — **CLOSED (unverified live)**

The defaults allowed up to 20 connections (SQLAlchemy 5+10, plus a separate
asyncpg pool of 2–5) against Supabase's 15-connection free tier.

- **Closed:** both pools are bounded by settings, defaulting to 5+5 and 1–3
  — 13 total. The rate-limiter pool is sized separately because it is owned by
  the workers rather than the request path.
- **Still unverified:** the real limit depends on the provider's plan.

### 4.10 D-10 — Fresh database failed silently — **CLOSED**

A fresh database yielded an app that booted, started four workers, and failed
every query with `relation "task_queue" does not exist`, visible only as a
repeating `worker.claim_failed` line.

- **Closed two ways:** Alembic is now the schema mechanism
  (`alembic upgrade head`), and the lifespan probes for the schema at startup
  and reports the result on `/health` (see D-08).

### 4.11 D-11 — Retry delay is inverted (Low) — **OPEN**

`bus.fail_task` computes `created_at = now - delay`, which pushes retries to the
*front* of the queue, contradicting its own comment. Harmless only because the
delay is never passed.

- **Location:** `orchestrator/bus.py:146`
- **Note:** left open deliberately; it is inert and the fix is a one-liner.

### 4.12 D-12 — Data model ceiling (Design, not a bug)

Entities carry a geohash cell and no coordinates, so `near` / `radius_m` measure
from a cell centre (~1.2 km) and a hyperlocal "nearest water station" query
answers with ±600 m of error. Fixing this requires a schema migration, not a
display change. Facts also inherit the grid cell of the gap that produced them,
so entities straddling a cell boundary cannot merge.

---

## 5. Recommended Sequence

**The MVP is now operable in principle.** D-01 through D-04, D-06 through
D-10 are closed; migrations exist; the domain config is real; and NEAR edges
make `/nearby` answer from the graph.

What remains, in order:

1. **A live smoke test.** Every external path — real robots.txt, Overpass, the
   LLM, embeddings, Neo4j, and `SKIP LOCKED` — is still unverified. Nothing
   else on this list can be confirmed without it.
2. **D-05** — persist the compliance audit trail, and add `raw_capture_id` to
   Neo4j nodes so NFR-3 holds for graph nodes and not only facts.
3. **Conflict resolution** (KRY-DQV-001 §3): `resolve_conflict` returns four
   outcomes and the consolidator consumes one.
4. **Auth proper** — the shared secret is a gate, not a user model. A moderator
   identity is needed for the review queue to be auditable.
5. **Tombstoning** (KRY-RFP-001 §4), **metrics** (KRY-OBS-001 §2), **backoff**
   (KRY-ENG-001 §3), **retention jobs**, and a container image + CI.

**Production hardening:** containerisation for the target host, CI, Alembic,
tombstoning, metrics, backoff, compliance audit persistence, retention jobs,
and an operations runbook ([KRY-OPS-001](14_operations.md)).

---

## 6. Revision History

| Rev | Date | Change |
|---|---|---|
| 1.0 | 2026-09-29 | Initial conformance matrix. Recorded 12 open defects; three were release-blocking. |
| 1.1 | 2026-09-29 | Closed D-01, D-02, D-03, D-04, D-06, D-07, D-08, D-09, D-10. D-05, D-11, D-12 remain open. Added Alembic migrations. |
