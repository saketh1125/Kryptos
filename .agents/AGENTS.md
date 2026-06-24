# Project Rules & Agent Instructions — GHKGE Workspace
Welcome, Agent. You are operating inside the **GHKGE (Generalized Hyperlocal Knowledge Graph Engine)** workspace. To ensure stability, safety, and seamless integration, you must adhere to the following rules, architecture constraints, and approach guidelines.

---

## 1. Required Reading List
Before writing any code or proposing edits, you must read and align with the specifications located in the `docs/` folder:
*   📜 **[01_prd.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/01_prd.md):** Functional & performance scopes.
*   🛡️ **[05_compliance_and_legal.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/05_compliance_and_legal.md):** Operational legal boundaries.
*   🗄️ **[04_data_schema.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/04_data_schema.md):** Target database definitions.
*   🧬 **[06_extraction_strategy.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/06_extraction_strategy.md):** Chunker, scrapers, and synthesis parameters.
*   🤖 **[11_agent_instructions.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/11_agent_instructions.md):** Detailed step-by-step implementation sequence.
*   🔌 **[12_multi_agent_protocol.md](file:///C:/Users/saket/OneDrive/Desktop/Scraper/docs/12_multi_agent_protocol.md):** System prompt directives and payloads.

---

## 2. Core Guardrails & Constraints
You must not write code that violates these structural guardrails:
1.  **No Direct Network Fetches in Harvesters:** Every URL lookup must fetch through `ComplianceEngine.check()` to verify rate limits and robots.txt.
2.  **Statelessness:** Do not rely on local server memory queues. All task configurations, execution steps, and rate limiter tokens must be written directly to Postgres.
3.  **Strict Transaction Sequencing:** Do not write to pgvector or Neo4j without first saving raw captures and facts to Postgres. If Neo4j write loops fail, perform database transaction rollbacks in Postgres.
4.  **Resource Limits:** Keep Chromium concurrency limited to `asyncio.Semaphore(3)`. Run CPU-bound extraction libraries (`docling` and `faster-whisper`) inside thread pool executors.

---

## 3. How to Approach Code Generation
When tasked with implementing a module:
*   **Step 1:** Validate your plan against the schema files (`docs/04_data_schema.md`) and legal gates (`docs/05_compliance_and_legal.md`).
*   **Step 2:** Write small, testable modules. Implement connection utilities, Pydantic model scripts, or parsing rules independently first.
*   **Step 3:** Use async/await routines throughout. Write strict type annotations and docstrings for all functions.
*   **Step 4:** Incorporate structured JSON logging for major system actions.
*   **Step 5:** Before declaring completion, verify code against standard lint checks and double-check database indexes.

---

## 4. Model Tier Constraints (Frontier vs. Mid-Range Delegation)
To ensure system safety, compliance, and database consistency, tasks are partitioned by model capability. **Mid-Range / Lower-Tier Models** must avoid modifying or implementing the following modules, leaving them exclusively to **Frontier Models** (e.g., Gemini Pro, Claude 3.5 Sonnet, GPT-4o):

1.  **Compliance Engine & Rate Gates:**
    *   *Mid-Range Models:* May only consume compliance verification endpoints (`ComplianceEngine.check`).
    *   *Frontier Models:* Must handle all modifications to robots.txt caching, regex denylists, Postgres-backed token bucket rate limits, or IP lockout fail-safes.
2.  **Trinity Storage Transaction Synchronization:**
    *   *Mid-Range Models:* May write simple SQL queries or fetch data via the API.
    *   *Frontier Models:* Must write and modify the transaction lifecycle logic mapping Postgres writes, pgvector embedding generation, and Neo4j node/relationship pushes (including rollback handling on Neo4j connection drops).
3.  **Fuzzy Entity Resolution & Precedence Hierarchies:**
    *   *Mid-Range Models:* May parse schemas and perform metadata matching.
    *   *Frontier Models:* Must design and tweak the `RapidFuzz` merge logic, source-tier priority weight overrides, and safety corroboration threshold loops.
4.  **Task Event Loop & Dynamic Routing:**
    *   *Mid-Range Models:* May implement individual worker endpoints.
    *   *Frontier Models:* Must write the core orchestration loops, dynamic strategy scoring algorithms, and database queue polling logic.

