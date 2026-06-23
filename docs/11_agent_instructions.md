# Coding Agent Implementation Instructions — GHKGE
**Document Version:** 1.0  
**Status:** Approved  
**Target Audience:** Autonomous Coding Agents, Mid-Range/Frontier Code Generation Models, Solo Developers.

---

## 1. System Coding Guidelines
When implementing the code for GHKGE, any coding model or agent must strictly follow these rules. Do not take shortcuts that violate these constraints:

1.  **Strict Compliance Engine Routing:**
    *   *Rule:* No scraper or harvester module may import `requests`, `aiohttp`, `urllib`, or `playwright` directly to fetch external URLs.
    *   *Implementation:* All network fetching must request authorization from the `ComplianceEngine` module via `.check()`. Only compile fetch loops using the returned `ApprovedTarget` object.
2.  **No Schema-less LLM Extractions:**
    *   *Rule:* All extraction modules must use the `instructor` library wrapped around Pydantic BaseModel definitions.
    *   *Implementation:* Do not parse LLM output using regex patterns or manual JSON splits. If validation fails, leverage `instructor`'s built-in retry-on-validation loop.
3.  **Mandatory Checkpointing (Raw Captures first):**
    *   *Rule:* Harvesters must write the raw retrieved content string directly to the `raw_captures` Postgres table *before* trigger schema extraction or semantic embedding tasks.
    *   *Implementation:* The extraction module must load data from the `raw_captures` table. This keeps web scraping completely decoupled from LLM parsing tasks.
4.  **Enforced Memory Boundaries:**
    *   *Rule:* Do not write unbounded concurrent crawls.
    *   *Implementation:* Limit active browsers to `asyncio.Semaphore(3)`. Run heavy visual extractors (like `docling` layout analysis) inside a single-threaded CPU executor block.

---

## 2. Recommended Implementation Roadmap

An agent assigned to build the codebase should tackle the modules in the following order:

```
[Step 1: DB Schema & Models] ---> [Step 2: Compliance Engine] ---> [Step 3: Harvester Modules]
                                                                              |
[Step 6: Traversal APIs]   <--- [Step 5: Resolution & Sync] <--- [Step 4: Instructor Synthesis]
```

### Step 1: Database Setup & DDL Insertion
*   **File:** `database/connection.py`, `database/models.py`
*   **Objective:** Script the schema layout using SQL files in `docs/04_data_schema.md`. Define SQLAlchemy/SQLModel structures. Implement rate limit, raw capture, and entity tables.

### Step 2: Compliance Shield Module
*   **File:** `compliance/engine.py`, `compliance/rate_limiter.py`
*   **Objective:** Write the token-bucket rate limiter and robots.txt parsing/caching rules. Code the safety check gatekeeper class.

### Step 3: Harvesters Implementation
*   **File:** `harvesters/base.py`, `harvesters/web.py`, `harvesters/document.py`, `harvesters/media.py`
*   **Objective:** Scaffolding the primary fetch loops. Implement `crawl4ai` setup with a backup failover to `Scrapling` when blocked. Scaffolds whisper transcription logic.

### Step 4: Synthesis & LLM Refining
*   **File:** `synthesis/chunker.py`, `synthesis/refiner.py`
*   **Objective:** Build the 800-word sliding window chunker. Configure `instructor` logic with the Pydantic schemas. Write the prompts to filter out macro-knowledge and detect safety-relevant tags.

### Step 5: Entity Resolution & Multi-Store Sync
*   **File:** `consolidation/resolver.py`, `consolidation/sync.py`
*   **Objective:** Setup `RapidFuzz` logic to match names against database entries. Write transaction steps to write to Postgres, call Ollama embeddings to write to pgvector, and write unwound batched queries to Neo4j.

### Step 6: FastAPI Route Gateway
*   **File:** `api/main.py`, `api/routes/orchestration.py`, `api/routes/knowledge.py`
*   **Objective:** Scaffolds the route parameters defined in the API Contracts document. Connect routes to the orchestrator queues and DB query scripts.

---

## 3. Code Style & Architecture Expectations
*   **Type Hints:** Every function must include complete type annotations.
*   **Async/Await:** Use async constructs for database operations, crawler actions, and API request layers.
*   **Structured Logs:** Write status messages using standard structured JSON logs. Avoid generic, unstructured print statements.
*   **Retry Mechanisms:** Wrap external database queries (Neo4j/Supabase) and network calls in exponential-backoff retry wrappers to handle network timeouts.
