-- GHKGE Database Schema
-- Run against Supabase Postgres to initialize the schema

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Acquisition Runs Tracker
CREATE TABLE IF NOT EXISTS acquisition_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain          VARCHAR(255) NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          VARCHAR(50) NOT NULL DEFAULT 'running',
    trigger         VARCHAR(50) NOT NULL DEFAULT 'scheduled',
    last_canonical_id_processed UUID
);
CREATE INDEX IF NOT EXISTS idx_acquisition_runs_domain ON acquisition_runs(domain);

-- 2. Raw Capture Store
CREATE TABLE IF NOT EXISTS raw_captures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url      TEXT NOT NULL,
    source_type     VARCHAR(50) NOT NULL,
    domain          VARCHAR(255) NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_content     TEXT,
    raw_blob_url    TEXT,
    content_hash    VARCHAR(64) NOT NULL,
    strategy_used   VARCHAR(100) NOT NULL,
    run_id          UUID NOT NULL REFERENCES acquisition_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_raw_captures_hash ON raw_captures(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_captures_domain ON raw_captures(domain);

-- 3. Canonical Entities Table
CREATE TABLE IF NOT EXISTS entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  VARCHAR(255) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    grid_cell       VARCHAR(50) NOT NULL,
    aliases         TEXT[] DEFAULT '{}',
    best_tier       SMALLINT NOT NULL,
    corroboration_count INT NOT NULL DEFAULT 1,
    last_verified   TIMESTAMPTZ NOT NULL DEFAULT now(),
    neo4j_node_id   VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entities_type_cell ON entities(entity_type, grid_cell);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_name ON entities(canonical_name);

-- 4. Extracted Facts Table
CREATE TABLE IF NOT EXISTS extracted_facts (
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
    resolution_status   VARCHAR(50) NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_extracted_facts_entity ON extracted_facts(canonical_entity_id);
CREATE INDEX IF NOT EXISTS idx_extracted_facts_safety ON extracted_facts(is_safety_relevant, resolution_status);

-- 5. Strategy Yield Log
CREATE TABLE IF NOT EXISTS strategy_yield_log (
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
CREATE TABLE IF NOT EXISTS gap_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grid_cell       VARCHAR(50) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    kind            VARCHAR(50) NOT NULL,
    severity        FLOAT NOT NULL,
    status          VARCHAR(50) NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gap_queue_status_severity ON gap_queue(status, severity DESC);

-- 7. Domain Rate Limiter State
CREATE TABLE IF NOT EXISTS domain_rate_limit_state (
    domain          VARCHAR(255) PRIMARY KEY,
    last_hit_at     TIMESTAMPTZ NOT NULL,
    tokens_remaining FLOAT NOT NULL
);

-- 8. Narrative Chunks (pgvector)
CREATE TABLE IF NOT EXISTS narrative_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID REFERENCES entities(id) ON DELETE CASCADE,
    raw_capture_id  UUID REFERENCES raw_captures(id) ON DELETE CASCADE,
    chunk_text      TEXT NOT NULL,
    embedding       VECTOR(768),
    source_tier     SMALLINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_narrative_embedding ON narrative_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
