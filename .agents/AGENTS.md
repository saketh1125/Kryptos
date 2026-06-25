# GHKGE — Agent Instructions

## Quick Commands
```bash
python main.py                          # Run FastAPI server (port 8000)
python -m pytest tests/ -v              # Run tests
python -m ruff check ghkge/             # Lint
python -m ruff check ghkge/ --fix       # Auto-fix lint
```

## Architecture (5 layers)

```
ghkge/
  compliance/     Gate: every URL passes ComplianceEngine.check() BEFORE any fetch
  harvesters/     Web, doc, media, API — all route through compliance
  synthesis/      Chunker (800w/150o) → Instructor+LLM extraction
  consolidation/  RapidFuzz entity resolver → Postgres→pgvector→Neo4j sync
  api/            FastAPI: Orchestration API (/v1/runs, /v1/gaps) + Knowledge API (/v1/entities)
```

Entry point: `main.py` → `ghkge.api.main:app`

## Non-Negotiable Guardrails

1. **No direct network fetches in harvesters.** All URLs go through `ComplianceEngine.check()` first. Harvesters receive `ApprovedTarget`, never raw URLs.
2. **Raw captures before LLM.** Always write `raw_captures` to Postgres before running extraction. This decouples scraping from LLM processing.
3. **Trinity write order:** Postgres facts → pgvector embeddings → Neo4j nodes. Rollback Postgres if later writes fail.
4. **Concurrency caps:** `asyncio.Semaphore(3)` for browsers. `ThreadPoolExecutor(max_workers=1)` for docling/whisper. Never run Chromium + docling + whisper concurrently.

## Lint & Type Notes

- Ruff config ignores `B008` (FastAPI `Depends()` pattern) and `E501` for `database/models.py` + `synthesis/refiner.py`
- `asyncio_mode = "auto"` in pytest — async tests just work, no decorator needed
- All env vars prefixed `GHKGE_` — loaded via pydantic-settings from `.env`

## Data Flow (happy path)

1. `POST /v1/runs` → background task `execute_run()`
2. Planner reads `gap_queue`, picks strategies by yield score
3. ComplianceEngine validates each URL (robots.txt + denylist + rate limit)
4. Harvester fetches → writes `raw_captures` row → returns content
5. Synthesis chunks → LLM extracts `ExtractedFactSchema` → filters macro knowledge
6. Resolver fuzzy-matches entity (≥85% = merge, else create new)
7. Writes: Postgres entity + fact → pgvector embedding → Neo4j node

## Key Constants

| Setting | Value | Location |
|---------|-------|----------|
| Chunk size | 800 words, 150 overlap | `settings.chunk_size_words` |
| Entity match threshold | 85% (RapidFuzz token_sort_ratio) | `settings.entity_match_threshold` |
| Safety corroboration min | 2 sources | `settings.safety_corroboration_min` |
| Browser concurrency | 3 | `settings.max_concurrent_browsers` |
| Rate limit | 2s per domain | `settings.rate_limit_interval_s` |
| Source tiers | OFFICIAL(1) > CURATED(2) > SOCIAL_VERIFIED(3) > SOCIAL_GENERAL(4) | `models/schemas.py` |

## DB Schema

Run `sql/schema.sql` against Supabase Postgres to initialize all 8 tables + pgvector extension. Models in `ghkge/database/models.py`.
