# GHKGE — Agent Instructions

## Quick Commands
```bash
python main.py                          # Run FastAPI server (port 8000)
python -m pytest tests/ -v              # Run tests
python -m ruff check ghkge/ tests/      # Lint
python -m ruff check ghkge/ --fix       # Auto-fix lint
```

## Architecture

Agents are five polling roles over a Postgres-backed task bus (`task_queue`).
No broker, no in-memory queues — all state survives restarts.

```
GAP EVALUATOR (scheduled)     writes open gaps to gap_queue
        │
PLANNER worker  ──plan task──▶   claims gaps, ranks strategies by yield history,
        │                       derives concrete targets from the domain YAML
        ▼
HARVESTER worker ──harvest task──▶  ComplianceEngine.check() → ApprovedTarget
        │                            fetch → raw_captures (persisted first)
        ▼
SYNTHESIS worker ──extract task──▶   chunk (800w/150o) → Instructor+LLM →
        │                            extracted_facts (pending)
        ▼
CONSOLIDATOR worker ─consolidate task─▶ fuzzy entity resolution → safety gate →
                                     Postgres fact → pgvector chunk → Neo4j node
```

Task types: `plan` → `harvest` → `extract` → `consolidate`.
Claiming uses `FOR UPDATE SKIP LOCKED`; crashed tasks are reclaimed after
`stale_task_takeover_minutes`.

Entry point: `main.py` → `ghkge.api.main:app`

## Non-Negotiable Guardrails

1. **No direct network fetches in harvesters.** Harvesters accept an
   `ApprovedTarget`, never a raw URL, and re-verify through
   `ComplianceEngine.check()` immediately before any I/O. Rate limiting happens
   there, at fetch time — never at planning time.
2. **Raw captures before LLM.** Always write `raw_captures` to Postgres before
   running extraction. This decouples scraping from LLM processing.
3. **Trinity write order:** pgvector chunk (same txn) → Neo4j node → commit
   Postgres. A Neo4j failure raises `Neo4jUnavailableError`; the caller rolls
   the Postgres txn back and the task is requeued. Never orphan a fact.
4. **Concurrency caps:** `asyncio.Semaphore(3)` for browsers,
   `ThreadPoolExecutor(max_workers=1)` for docling/whisper.

## Data Model Notes

- `entities.grid_cell` is a **geohash-6** string (~1.2km × 0.6km). Use
  `ghkge.utils.geohash` to encode/decode/enumerate; never hand-roll.
- Fact entity types come from `CATEGORY_TO_TYPE` in `workers.py`, mapping
  extraction categories (LOCATION/ORGANIZATION/EVENT/RULE/METADATA) onto the
  ontology. Facts inherit the grid cell of the gap that produced them.
- Safety gate: a safety-relevant fact is `approved` only if the source is
  `OFFICIAL` (tier 1) or the entity has ≥ `safety_corroboration_min` sources.
  Otherwise it is `held_for_review` and hidden from the Knowledge API.

## Testing

Tests run against SQLite (aiosqlite) with Postgres-only types compiled down —
`models.StringArray` and `models.EmbeddingVector` provide the dialect variants.
Set the factory with `connection.set_session_factory(...)`. Only true externals
are faked: compliance, the LLM, embeddings, Neo4j.

- `tests/test_pipeline_integration.py` — full plan→consolidate chain, retry,
  duplicate, safety-gate behaviour, run finalization.
- `tests/test_api_routes.py` — both API surfaces, including that unapproved
  facts never leak.

## Lint & Type Notes

- Ruff ignores `B008` (FastAPI `Depends()`) and `E501` for
  `database/models.py` + `synthesis/refiner.py`.
- `asyncio_mode = "auto"` — async tests need no decorator.
- All env vars prefixed `GHKGE_` via pydantic-settings.
- `robots.txt` parsing uses stdlib `urllib.robotparser` (fail-closed).

## DB Schema

Run `sql/schema.sql` against Supabase Postgres to initialize all 10 tables
(`acquisition_runs`, `raw_captures`, `entities`, `extracted_facts`,
`strategy_yield_log`, `gap_queue`, `domain_rate_limit_state`,
`narrative_chunks`, `task_queue`, `feedback`) + the `vector` extension.
Models in `ghkge/database/models.py`.

## Key Constants

| Setting | Default | Location |
|---------|---------|----------|
| Chunk size | 800 words, 150 overlap | `settings.chunk_size_words` |
| Entity match threshold | 85 (RapidFuzz token_sort_ratio) | `settings.entity_match_threshold` |
| Safety corroboration min | 2 | `settings.safety_corroboration_min` |
| Browser concurrency | 3 | `settings.max_concurrent_browsers` |
| Rate limit | 2s per domain | `settings.rate_limit_interval_s` |
| Grid precision | 6 (geohash) | `settings.grid_precision` |
| Source tiers | OFFICIAL(1) > CURATED(2) > SOCIAL_VERIFIED(3) > SOCIAL_GENERAL(4) | `models/schemas.py` |
