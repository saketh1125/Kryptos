# Kryptos — GHKGE

**Generalized Hyperlocal Knowledge Graph Engine**

A domain-agnostic, strategy-adaptive, multi-agent system that acquires, validates, and consolidates hyperlocal knowledge from the open web into a queryable knowledge graph — designed to answer questions like *"Which gali is empty at 6am?"* or *"What changed in this ward's regulations this week?"*.

Kryptos is the data-intelligence layer behind [Saar](https://github.com/saketh1125/Saar) (Kashi Nav): Saar is the consumer app, Kryptos is the engine that keeps its knowledge fresh, complete, and truthful.

> **Design-first build.** The system was implemented against a locked, versioned doc set — PRD, system architecture, API contracts, data schema, compliance, extraction strategy, data quality, refresh policy, UI, observability, agent instructions, and a multi-agent protocol. Each section below links to its governing document.

---

## 1. System Architecture

Kryptos is split into a **stateless application layer** and a **Trinity Storage Layer** of managed databases:

| Store | Role |
|-------|------|
| **PostgreSQL (Supabase)** | Structured transactional state: acquisition runs, raw captures, gap queue, rate-limit state, strategy yield logs |
| **pgvector** | Semantic embeddings for narrative chunks and search queries |
| **Neo4j** | Entity linkages, path traversal, and relationship graphs |

```mermaid
flowchart TB
    subgraph App["Stateless Application Layer"]
        API[FastAPI Gateway]
        ORCH[Orchestrator]
        GE[Gap Evaluator]
        PL[Planner]
        HV[Harveters<br/>web / media / doc / API]
        EX[Extractor]
        CO[Consolidator]
        RES[Entity Resolver]
        SYN[Synthesizer<br/>chunker + refiner]
    end

    subgraph Store["Trinity Storage"]
        PG[(PostgreSQL<br/>state + raw cache)]
        VEC[(pgvector<br/>embeddings)]
        NEO[(Neo4j<br/>knowledge graph)]
    end

    E --> ORCH
    ORCH --> PL --> HV --> EX --> CO --> RES
    EVEN --> PL
    RES --> NEO
    EX --> PG
    CO --> VEC
    RES --> VEC
    ORCH --> PG
```

**Governing docs:** [02_system_architecture.md](docs/02_system_architecture.md) · [SysDes-v6.md](docs/SysDes-v6.md)

---

## 2. Acquisition Pipeline

Every acquisition campaign follows the same lifecycle, tracked end-to-end as an `AcquisitionRun`:

```
Gap Evaluator ──▶ Planner ──▶ Harvesters ──▶ Extraction ──▶ Consolidation ──▶ Knowledge Graph
      ▲                                                                          │
      └──────────────────────── GapQueue ◀───────────────────────────────────────┘
```

1. **Gap evaluation** — a scheduled job scans entity density and staleness per entity type, writing severity-scored entries to the `GapQueue`. Nothing is crawled for its own sake; the queue is the only trigger.
2. **Planning** — the planner reads open gaps and strategy-yield history, then assigns the cheapest ranked strategy set per entity type (declared in the domain config).
3. **Harvesting** — pluggable sources behind one interface: `web` (crawl4ai), `media` (yt-dlp + faster-whisper), `document` (docling), `api_client` (structured APIs).
4. **Extraction** — raw captures are converted to clean text, cached in Postgres, and synthesized into structured facts via an LLM (instructor-validated schemas).
5. **Consolidation** — fuzzy entity resolution (rapidfuzz) merges multi-source sightings into single graph nodes; safety-relevant facts require corroboration from ≥2 independent sources.

**Governing docs:** [06_extraction_strategy.md](docs/06_extraction_strategy.md) · [12_multi_agent_protocol.md](docs/12_multi_agent_protocol.md)

---

## 3. Agentic Architecture

The engine is designed as five autonomous roles synchronized over a Postgres-backed task event bus:

| Role | Responsibility | Key module |
|------|----------------|------------|
| **Planner** | Reads gap queue + yield history; ranks and assigns strategies per entity type | `ghkge/orchestrator/planner.py` |
| **Harvester** | Multi-modal acquisition behind one interface (web/media/document/API) | `ghkge/harvesters/` |
| **Gap Evaluator** | Scans entity density + staleness; writes severity-ranked gaps | `ghkge/gap_evaluator/evaluator.py` |
| **Consolidator** | Entity resolution, conflict resolution, merge/sync | `ghkge/consolidation/` |
| **Synthesizer** | Sliding-window chunking + refinement into query-ready narrative units | `ghkge/synthesis/` |

**Governing docs:** [12_multi_agent_protocol.md](docs/12_multi_agent_protocol.md) · [11_agent_instructions.md](docs/11_agent_instructions.md)

---

## 4. Data Model

Nine core tables across the Trinity Storage:

| Table | Purpose |
|-------|---------|
| `acquisition_runs` | Full run lifecycle (plan → harvest → extract → consolidate) |
| `raw_captures` | Cached raw content per source |
| `entities` | Resolved entities with grid-cell locality |
| `extracted_facts` | Structured facts with source tier + provenance |
| `narrative_chunks` | Semantic chunks + embeddings (pgvector) |
| `gap_queue` | Open coverage gaps driving the planner |
| `strategy_yield_log` | Per-strategy yield/novelty history for planner scoring |
| `domain_rate_limit_state` | Postgres-backed rate limiting |
| `source_tier` | Source trust hierarchy: OFFICIAL → CURATED → SOCIAL_VERIFIED → SOCIAL_GENERAL |

**Governing docs:** [04_data_schema.md](docs/04_data_schema.md) · [07_data_quality_and_validation.md](docs/07_data_quality_and_validation.md)

---

## 5. API Surface

Two boundaries, both FastAPI + JSON:

| Boundary | Base | Endpoints |
|----------|------|-----------|
| **Knowledge API** (public, for consumer apps) | `/api` | `GET /search` · `GET /{entity_id}` · `GET /{entity_id}/nearby` · `GET /{entity_id}/rules` · `POST /feedback` |
| **Orchestration API** (internal) | `/admin` | `POST /runs` (202) · `GET /runs/{run_id}` · `GET /gaps` · `POST /gaps/{gap_id}/resolve` · `GET /strategies/yield` |

**Governing doc:** [03_api_contracts.md](docs/03_api_contracts.md)

---

## 6. Domain Configuration

Kryptos is domain-agnostic: a new city or domain is a YAML file, not a new scraper. `config/default_domain.yaml`:

```yaml
domain: "example_hyperlocal_domain"
geography_bbox: [25.268, 82.980, 25.350, 83.030]
coverage_target: 0.8
staleness_cutoff_days:
  regulatory_rule: 7
  public_event: 1
  local_business: 30
  landmark: 90
entity_types:
  - name: "regulatory_rule"
    strategies: ["official_portals", "news_feeds"]
    safety_relevant: true
```

**Governing docs:** [01_prd.md](docs/01_prd.md) · [08_refresh_policy.md](docs/08_refresh_policy.md)

---

## 7. Compliance & Safety

Acquisition is legally constrained by design, not by convention:

- **Public-access-only:** indexes only what a human could legally view without login or access-control bypass
- **No private API reverse engineering** — zero interception of private/mobile endpoints
- **No deanonymization** — personal identity of creators/citizens is never stored or correlated
- **Robots-aware:** `robots.txt` enforcement (24h cache) + global denied patterns (`/admin`, `/login`, private API paths, credential params)
- **Blocked platforms:** LinkedIn, Instagram, Facebook, TikTok — official APIs only
- **Rate limiting:** Postgres-backed limiter (2s default interval) per domain

**Governing docs:** [05_compliance_and_legal.md](docs/05_compliance_and_legal.md) · [11_agent_instructions.md](docs/11_agent_instructions.md)

---

## 8. Observability

Structured JSON logging (structlog) throughout — every pipeline stage emits machine-parseable events (`planner.no_gaps`, `pipeline.run_not_found`, …), ready for log aggregators.

**Governing doc:** [10_monitoring_and_observability.md](docs/10_monitoring_and_observability.md)

---

## 9. Documentation Index

| Doc | Covers |
|-----|--------|
| [01_prd.md](docs/01_prd.md) | Product requirements, objectives, scope |
| [02_system_architecture.md](docs/02_system_architecture.md) | Topology, Trinity Storage, deployment |
| [03_api_contracts.md](docs/03_api_contracts.md) | API boundaries, payloads, contracts |
| [04_data_schema.md](docs/04_data_schema.md) | Storage topology, tables, indexes |
| [05_compliance_and_legal.md](docs/05_compliance_and_legal.md) | Legal boundaries, scraping guarantees |
| [06_extraction_strategy.md](docs/06_extraction_strategy.md) | Multi-modal ingestion flow |
| [07_data_quality_and_validation.md](docs/07_data_quality_and_validation.md) | Five data-quality pillars |
| [08_refresh_policy.md](docs/08_refresh_policy.md) | Staleness thresholds, refresh priorities |
| [09_ui_design.md](docs/09_ui_design.md) | Admin console design system |
| [10_monitoring_and_observability.md](docs/10_monitoring_and_observability.md) | Structured logging, metrics |
| [11_agent_instructions.md](docs/11_agent_instructions.md) | Coding-agent implementation rules |
| [12_multi_agent_protocol.md](docs/12_multi_agent_protocol.md) | Multi-agent roles, event bus |
| [SysDes-v6.md](docs/SysDes-v6.md) | Full engineering reference v6 |

---

## 10. Project Structure

```
Kryptos/
├── main.py                     # FastAPI entrypoint
├── pyproject.toml               # deps, ruff, mypy, pytest config
├── config/
│   ├── settings.py              # GHKGE_ env-prefixed settings
│   └── default_domain.yaml      # domain ontology + strategies
├── ghkge/
│   ├── api/                     # FastAPI routes (knowledge + orchestration)
│   ├── orchestrator/            # planner, pipeline
│   ├── harvesters/              # web, media, document, api_client
│   ├── gap_evaluator/           # coverage/staleness scanning
│   ├── consolidation/           # entity resolution, sync
│   ├── synthesis/               # chunker, refiner
│   ├── compliance/              # robots engine, rate limiter
│   ├── database/                # SQLAlchemy async models, connection
│   ├── models/                  # pydantic schemas
│   ├── monitoring/              # structlog setup
│   └── config/                  # settings + domain YAML
├── sql/schema.sql               # DDL reference
├── docs/                        # 13 design documents
└── tests/                       # pytest suite
```

---

## 11. Quick Start

```bash
pip install -e ".[all]"
cp .env.example .env   # fill in keys; engine also runs with partial config
python main.py         # serves API on configured host:port
```

Domain configs live in `config/default_domain.yaml` (domain, geography bbox, entity types, coverage target).

---

## 12. Status

Under active development — orchestration core, harvesters, compliance layer, and the full doc set are in place; see `docs/` for the planned surface.