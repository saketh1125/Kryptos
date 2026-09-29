# API Contracts Document — GHKGE

| Field | Value |
|---|---|
| **Document ID** | KRY-API-001 |
| **Revision** | 1.1 |
| **Status** | Implemented (partial) |
| **Supersedes** | KRY-API-001 r1.0 |
| **Last updated** | 2026-09-29 |
| **Target audience** | Frontend engineers, downstream app developers, integration engineers |
|
---

## 1. Gateway Overview
All API services are built using FastAPI and expose standard JSON payloads. The system exposes two API boundaries:
1.  **Orchestration API (Internal / Port 8000/admin):** Used to queue scraping runs, manage coverage gaps, and inspect strategy metrics.
2.  **Knowledge API (Public / Port 8000/api):** Used by downstream consumer applications to traversal the knowledge graph, query facts, search, and submit corrections.

> **Conformance note (r1.1).** Endpoint *paths* in §2–§3 below are written
> without the version segment. The implementation composes the documented
> boundary with the version segment: `/admin/v1/runs`, `/admin/v1/gaps`,
> `/admin/v1/strategies/yield`, `/api/v1/entities/*`, `/api/v1/feedback`.
> The boundary, method, request shape and status code in each table are as
> specified; field-level deltas are itemised in
> [KRY-CONF-001 §3](13_conformance_matrix.md). Two endpoints exist in the
> implementation that this contract does not yet describe —
> `GET /admin/v1/facts` and `POST /admin/v1/facts/{id}/moderate`, which back the
> conflict-review queue in KRY-UI-001. They are unspecced pending r1.2.

---

## 2. Orchestration API Contracts (Admin Gateway)

### 2.1 Trigger Acquisition Run
Starts a background task scoped to a geographical domain and target entity list.
*   **Path:** `POST /v1/runs`
*   **Request Body (JSON):**
    ```json
    {
      "domain": "example_city_guide",
      "entity_types": ["landmark", "local_business"],
      "trigger": "manual"
    }
    ```
*   **Response (202 Accepted):**
    ```json
    {
      "run_id": "8fa27003-8d68-45a8-9d62-67852c002f23",
      "status": "queued",
      "submitted_at": "2026-06-22T14:40:00Z"
    }
    ```

### 2.2 Monitor Acquisition Run Status
*   **Path:** `GET /v1/runs/{run_id}`
*   **Response (200 OK):**
    ```json
    {
      "run_id": "8fa27003-8d68-45a8-9d62-67852c002f23",
      "domain": "example_city_guide",
      "status": "completed",
      "started_at": "2026-06-22T14:40:05Z",
      "completed_at": "2026-06-22T14:52:12Z",
      "facts_extracted": 142,
      "entities_written": 24,
      "errors": []
    }
    ```

### 2.3 List System Gaps
Fetches prioritized target areas requiring additional scraping.
*   **Path:** `GET /v1/gaps`
*   **Query Parameters:**
    *   `domain` (string, required)
    *   `status` (string, default: `"open"`)
    *   `limit` (integer, default: `50`)
*   **Response (200 OK):**
    ```json
    {
      "gaps": [
        {
          "gap_id": "a9081a2e-4b68-4cf2-8de9-89b5c3ff2103",
          "grid_cell": "tdr1v2",
          "entity_type": "landmark",
          "kind": "missing",
          "severity": 4.5,
          "created_at": "2026-06-22T12:00:00Z"
        }
      ]
    }
    ```

### 2.4 Resolve System Gap Override
Allows manual override of a queued gap.
*   **Path:** `POST /v1/gaps/{gap_id}/resolve`
*   **Request Body (JSON):**
    ```json
    {
      "resolution": "skip"
    }
    ```
*   **Response (200 OK):**
    ```json
    {
      "gap_id": "a9081a2e-4b68-4cf2-8de9-89b5c3ff2103",
      "status": "resolved",
      "resolution": "skipped"
    }
    ```

---

## 3. Knowledge API Contracts (Public Gateway)

### 3.1 Fetch Entity Details
*   **Path:** `GET /v1/entities/{entity_id}`
*   **Response (200 OK):**
    ```json
    {
      "id": "e302be1b-252a-43cf-bf24-a4f615437812",
      "canonical_name": "City Park Playground",
      "entity_type": "landmark",
      "aliases": ["Municipal Play Area", "Central Park Playground"],
      "location": {
        "lat": 25.3214,
        "lng": 83.0019,
        "grid_cell": "tdr1v2"
      },
      "best_tier": 1,
      "corroboration_count": 3,
      "facts": [
        {
          "insight": "Drinking fountain is situated next to the play structures.",
          "confidence": 0.95,
          "source_tier": 1,
          "is_safety_relevant": false,
          "source_url": "https://gov.city.example/parks/central"
        }
      ]
    }
    ```

### 3.2 Hybrid Entity Search
Combines exact metadata tags with vector text semantic search.
*   **Path:** `GET /v1/entities/search`
*   **Query Parameters:**
    *   `q` (string, optional - semantic keyword query)
    *   `type` (string, optional)
    *   `near` (string, optional - comma-separated `lat,lng`)
    *   `radius_m` (integer, optional)
*   **Response (200 OK):**
    ```json
    {
      "results": [
        {
          "entity_id": "e302be1b-252a-43cf-bf24-a4f615437812",
          "canonical_name": "City Park Playground",
          "relevance_score": 0.89,
          "snippet": "Drinking fountain is situated next to the play structures."
        }
      ]
    }
    ```

### 3.3 Traversal Graph Neighborhood
Triggers graph operations to traverse relationships.
*   **Path:** `GET /v1/entities/{entity_id}/nearby`
*   **Query Parameters:**
    *   `relation` (string, e.g. `"NEAR"`, `"ACCESSIBLE_VIA"`, `"HAS_AMENITY"`)
    *   `depth` (integer, default: `1`)
*   **Response (200 OK):**
    ```json
    {
      "nodes": [
        {
          "id": "e302be1b-252a-43cf-bf24-a4f615437812",
          "canonical_name": "City Park Playground",
          "entity_type": "landmark"
        },
        {
          "id": "f818cd1d-654a-47ef-ba4c-0062a420b987",
          "canonical_name": "City Park Restroom",
          "entity_type": "facility"
        }
      ],
      "relationships": [
        {
          "source": "e302be1b-252a-43cf-bf24-a4f615437812",
          "target": "f818cd1d-654a-47ef-ba4c-0062a420b987",
          "type": "NEAR",
          "distance_m": 45,
          "source_url": "https://openstreetmap.org/node/12345"
        }
      ]
    }
    ```

### 3.4 Fetch Safety-Relevant Guidelines
Returns pre-corroborated rules associated with the entity.
*   **Path:** `GET /v1/entities/{entity_id}/rules`
*   **Response (200 OK):**
    ```json
    {
      "rules": [
        {
          "insight": "Fires are strictly prohibited in the playground area.",
          "source_tier": 1,
          "corroboration_count": 2,
          "last_verified": "2026-06-22T14:40:00Z"
        }
      ]
    }
    ```

### 3.5 Submit User Correction Feedback
Feeds directly into the validation queue loop.
*   **Path:** `POST /v1/feedback`
*   **Request Body (JSON):**
    ```json
    {
      "entity_id": "e302be1b-252a-43cf-bf24-a4f615437812",
      "correction_text": "The drinking fountain is broken as of June 2026.",
      "submitted_by_session": "sess_87239"
    }
    ```
*   **Response (200 OK):**
    ```json
    {
      "feedback_id": "cd0b89f1-ef4a-476e-bb12-9c124aa7bf83",
      "status": "queued_for_review"
    }
    ```
