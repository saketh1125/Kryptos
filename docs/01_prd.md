# Product Requirements Document (PRD) — GHKGE
**Document Version:** 1.0  
**Status:** Approved  
**Target Audience:** Hyperlocal guide builders, final-year project evaluators, downstream application developers.

---

## 1. Product Overview & Objectives
The **Generalized Hyperlocal Knowledge Graph Engine (GHKGE)** is a domain-agnostic, strategy-adaptive backend system. Its core goal is to crawl, ingest, process, and consolidate hyperlocal information (such as city guides, transit details, business directories, and rules) into a queryable knowledge graph.

Instead of coding custom scrapers for every new town or domain, GHKGE uses configuration schemas (ontology + geography bounding box) to dynamically discover, harvest, and resolve data. It guarantees legal compliance (respects `robots.txt` and rate limits) while maintaining a strict **$0 infrastructure spend** footprint (using free-tier managed services).

---

## 2. Target Users & Use Cases
1. **Downstream LLM/RAG Applications:** A consumer-facing chatbot that needs reliable local information (e.g., "Where is the nearest drinking water station, and is it accessible via a paved trail?").
2. **Local Operators / Curators:** Administrators managing local content who want to see coverage gaps, review flagged conflicts, and monitor scraping yields.
3. **Solo Developer / Researcher:** Deploying a custom hyperlocal directory with zero ongoing server costs.

---

## 3. Detailed Functional Requirements (FR)

| ID | Title | Description | Priority |
|---|---|---|---|
| **FR-1** | **Domain & Ontology Configuration** | The system must ingest a YAML configuration defining a geographical bounding box and a target ontology (entity types, relations). It must bootstrap without code changes. | P0 |
| **FR-2** | **Compliance Gating** | Every network request must pass through a strict policy check verifying `robots.txt`, domain rate limits, and platform-specific policies. | P0 |
| **FR-3** | **Multi-Modal Harvester Pipeline** | Must extract facts from HTML (crawls), PDFs/docs, audio/video (transcripts), and official structured APIs (OSM/Gov). | P0 |
| **FR-4** | **Faceted Extraction & Provenance** | Facts must be extracted using structured schemas (Instructor + Pydantic) containing source URL, tier, confidence score, and timestamp. | P0 |
| **FR-5** | **Entity Resolution** | Duplicate or aliased entities (e.g., "Central Station" and "Main Railway Terminus") must be merged into canonical nodes using fuzzy string matching. | P0 |
| **FR-6** | **Conflict & Quality Resolution** | Conflicting facts must be resolved using source tiers. Safety-critical facts must require higher verification standards. | P0 |
| **FR-7** | **Gap & Coverage Evaluator** | A background job must analyze the density of entities within grid cells, compare them to baselines, and queue missing/thin areas for extraction. | P1 |
| **FR-8** | **Knowledge & Traversal API** | Must serve endpoints for entity search, hybrid vector lookup, graph traversal (multi-hop neighborhood queries), and safety-rule queries. | P0 |
| **FR-9** | **User Correction Flywheel** | Must accept consumer feedback to queue entities for manual validation or re-extraction. | P1 |

---

## 4. Non-Functional Requirements (NFR)

### 4.1 Cost and Compute Envelope
*   **Infrastructure Cost:** Must run entirely on **$0 budget** (excluding optional LLM token API keys).
    *   *Web Host:* Hugging Face Spaces (free tier, 16GB RAM, 2vCPU, stateless docker container).
    *   *Relational & Vector DB:* Supabase (Free tier).
    *   *Graph DB:* Neo4j AuraDB (Free tier).
*   **Compute Scheduling:** Must run as a background task. Since free-tier containers sleep, jobs must checkpoint state frequently in the database so that runs can resume transparently upon restart.

### 4.2 Latency & Throughput
*   **Acquisition Latency:** Batch processing (non-real-time). A typical municipal-scale crawl should complete within scheduled runs over 1–2 weeks, respecting rate limits.
*   **Query Latency:** Downstream Knowledge API endpoints must respond under **300ms** to support interactive user chatbots.

### 4.3 Data Quality & Durability
*   **Auditability:** Every single node/edge in the final graph must link back to an immutable raw HTML/media capture via database foreign keys.
*   **No Data Loss:** Zero in-process memory queue reliance. All state transitions, scrape queues, and rate-limit buckets must persist directly in Postgres.

---

## 5. Scope & Constraints
*   **In-Scope:** Framework core, pipeline orchestration, compliance gate, raw capture ingestion, LLM schema parsing, fuzzy resolution, three-tier storage syncing, FastAPI service layers.
*   **Out-of-Scope (Excluded):** User authentication/session management, UI admin console visualization (handled in Next.js separately), private/mobile API reverse-engineering, bypass of paywalls.
