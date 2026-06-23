# Data Schema Document — GHKGE
**Document Version:** 1.0  
**Status:** Approved  
**Target Audience:** Database Administrators, Backend Developers, Data Engineers.

---

## 1. Storage Topology
GHKGE uses a three-store topology ("Trinity Storage") targeting managed free tiers:
1.  **Supabase Postgres:** Structured transactional state, execution queues, rate-limit logs, and raw crawl content.
2.  **pgvector Extension:** Semantic content chunks and search query embeddings.
3.  **Neo4j Graph Database:** Entity linkages and path traversal nodes.

---

## 2. Postgres DDL (Supabase Relational State)

```sql
-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Acquisition Runs Tracker
CREATE TABLE acquisition_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain          VARCHAR(255) NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          VARCHAR(50) NOT NULL DEFAULT 'running', -- 'running', 'completed', 'failed', 'partial'
    trigger         VARCHAR(50) NOT NULL DEFAULT 'scheduled', -- 'scheduled', 'manual', 'gap_reconciliation'
    last_canonical_id_processed UUID                       -- checkpoint resume pointer
);

-- 2. Raw Capture Store (Checkpointing step before LLM)
CREATE TABLE raw_captures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url      TEXT NOT NULL,
    source_type     VARCHAR(50) NOT NULL, -- 'html' | 'pdf' | 'audio' | 'api_json'
    domain          VARCHAR(255) NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_content     TEXT,                  -- Markdown or media transcription text
    raw_blob_url    TEXT,                  -- Optional link to cloud object storage (if binary)
    content_hash    VARCHAR(64) NOT NULL,  -- SHA-256 validation (dedup)
    strategy_used   VARCHAR(100) NOT NULL,
    run_id          UUID NOT NULL REFERENCES acquisition_runs(id) ON DELETE CASCADE
);
CREATE INDEX idx_raw_captures_hash ON raw_captures(content_hash);
CREATE INDEX idx_raw_captures_domain ON raw_captures(domain);

-- 3. Canonical Entities Table
CREATE TABLE entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  VARCHAR(255) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    grid_cell       VARCHAR(50) NOT NULL,  -- Geohash cell
    aliases         TEXT[] DEFAULT '{}',
    best_tier       SMALLINT NOT NULL,     -- Authority score (1-4, lower is better)
    corroboration_count INT NOT NULL DEFAULT 1,
    last_verified   TIMESTAMPTZ NOT NULL DEFAULT now(),
    neo4j_node_id   VARCHAR(255),          -- Reference mapping to Neo4j Node
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entities_type_cell ON entities(entity_type, grid_cell);
CREATE INDEX idx_entities_canonical_name ON entities(canonical_name);

-- 4. Extracted Facts Table (Linked to provenance)
CREATE TABLE extracted_facts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_capture_id      UUID NOT NULL REFERENCES raw_captures(id) ON DELETE CASCADE,
    canonical_entity_id UUID REFERENCES entities(id) ON DELETE SET NULL,
    entity_name_raw     VARCHAR(255) NOT NULL,
    entity_category     VARCHAR(100) NOT NULL,
    contextual_insight  TEXT NOT NULL,
    confidence_score    FLOAT NOT NULL,
    source_tier         SMALLINT NOT NULL,
    is_safety_relevant  BOOLEAN NOT NULL DEFAULT FALSE,
    is_macro_knowledge  BOOLEAN NOT NULL DEFAULT FALSE,
    chunk_index         INT NOT NULL,
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution_status   VARCHAR(50) NOT NULL DEFAULT 'pending' -- 'pending' | 'approved' | 'held_for_review' | 'rejected'
);
CREATE INDEX idx_extracted_facts_entity ON extracted_facts(canonical_entity_id);
CREATE INDEX idx_extracted_facts_safety ON extracted_facts(is_safety_relevant, resolution_status);

-- 5. Strategy Yield Log
CREATE TABLE strategy_yield_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_name   VARCHAR(100) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    run_id          UUID NOT NULL REFERENCES acquisition_runs(id) ON DELETE CASCADE,
    calls_made      INT NOT NULL DEFAULT 0,
    entities_found  INT NOT NULL DEFAULT 0,
    novel_entities  INT NOT NULL DEFAULT 0,
    avg_confidence  FLOAT,
    cost_estimate_usd FLOAT DEFAULT 0.0,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. Gap Queue
CREATE TABLE gap_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grid_cell       VARCHAR(50) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    kind            VARCHAR(50) NOT NULL,  -- 'missing' | 'anomaly' | 'reinforce'
    severity        FLOAT NOT NULL,
    status          VARCHAR(50) NOT NULL DEFAULT 'open', -- 'open' | 'in_progress' | 'resolved'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_gap_queue_status_severity ON gap_queue(status, severity DESC);

-- 7. Domain Rate Limiter State (Token Bucket)
CREATE TABLE domain_rate_limit_state (
    domain          VARCHAR(255) PRIMARY KEY,
    last_hit_at     TIMESTAMPTZ NOT NULL,
    tokens_remaining FLOAT NOT NULL
);
```

---

## 3. pgvector Schema (Supabase Semantic Embeddings)

```sql
-- Initialize vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Narrative Context Table
CREATE TABLE narrative_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID REFERENCES entities(id) ON DELETE CASCADE,
    raw_capture_id  UUID REFERENCES raw_captures(id) ON DELETE CASCADE,
    chunk_text      TEXT NOT NULL,
    embedding       VECTOR(768),            -- Designed for nomic-embed-text
    source_tier     SMALLINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cosine Distance Operator Index (IVFFlat)
CREATE INDEX idx_narrative_embedding ON narrative_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

---

## 4. Neo4j Graph Schema

### 4.1 Constraints & Indices
```cypher
// Ensure unique entity IDs
CREATE CONSTRAINT unique_entity_id IF NOT EXISTS
FOR (e:Entity) REQUIRE e.id IS UNIQUE;

// Index for search lookup
CREATE INDEX entity_name_idx IF NOT EXISTS
FOR (e:Entity) ON (e.canonical_name);

CREATE INDEX entity_cell_idx IF NOT EXISTS
FOR (e:Entity) ON (e.grid_cell);
```

### 4.2 Properties Schema
```cypher
// Entity Node
(:Entity {
  id: "uuid-string",
  canonical_name: "String",
  entity_type: "String",
  grid_cell: "String",
  best_tier: Integer,
  corroboration_count: Integer
})

// Relationships
(:Entity)-[:NEAR {distance_m: Float, source: String, confidence: Float}]->(:Entity)
(:Entity)-[:ACCESSIBLE_VIA {path_type: String, duration_min: Float}]->(:Entity)
(:Entity)-[:HAS_AMENITY {source: String}]->(:Entity)
(:Entity)-[:SUBJECT_TO_RULE {is_safety_relevant: Boolean}]->(:Rule)
(:Entity)-[:PART_OF]->(:Entity)
```

---

## 5. DB Synchronization Transaction Logic
To guarantee database consistency without expensive distributed locking, writes follow this strict order of operations:
1.  Write `raw_captures` records to Postgres (returns `raw_capture_id`).
2.  Parse chunks and write `extracted_facts` records to Postgres (marked as `pending`).
3.  Perform resolution checking; if entity resolves, execute Postgres `entities` updates.
4.  Generate embedding for chunks and write to pgvector `narrative_chunks` table linked to Postgres `entity_id`.
5.  Push Node and Edge parameters to Neo4j. In case of failure, roll back Postgres entity transaction and flag fact resolution as `failed` for cleanup.
