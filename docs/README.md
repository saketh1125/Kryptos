# Document Register — GHKGE

Every document in this directory is version-controlled, individually
identifiable, and referenced by a stable ID. The ID never changes; the
revision number changes when content does.

**Naming convention:** `NN_slug.md` — the numeric prefix determines read order.
The Document ID below is the canonical identifier; the filename is a convenience.

**Status values**

| Status | Meaning |
|---|---|
| `Approved` | Ratified design intent. Binding on the implementation. |
| `Implemented` | Approved **and** built, with a verification pointer. |
| `Implemented (partial)` | Built, but with a known gap against the spec. See the conformance matrix. |
| `Planned` | Written, not built. |
| `Not implemented` | Approved, deliberately out of v1 scope. |

**Conformance:** [KRY-CONF-001 — Conformance Matrix](13_conformance_matrix.md) maps
every requirement in this set to its implementation and test evidence.

---

## 1. Document Index

| ID | Rev | Document | Status | Audience |
|---|---|---|---|---|
| [KRY-PRD-001](01_prd.md) | 1.0 | [Product Requirements](01_prd.md) | Approved | Product, evaluators, app developers |
| [KRY-ARC-001](02_system_architecture.md) | 1.0 | [System Architecture](02_system_architecture.md) | Approved | Architects, backend, infra |
| [KRY-API-001](03_api_contracts.md) | 1.1 | [API Contracts](03_api_contracts.md) | Implemented (partial) | Frontend, integration |
| [KRY-SCH-001](04_data_schema.md) | 1.1 | [Data Schema](04_data_schema.md) | Implemented (partial) | DBAs, backend, data eng |
| [KRY-CMP-001](05_compliance_and_legal.md) | 1.0 | [Compliance and Legal](05_compliance_and_legal.md) | Approved | Compliance, legal, dev |
| [KRY-EXT-001](06_extraction_strategy.md) | 1.0 | [Extraction Strategy](06_extraction_strategy.md) | Implemented (partial) | Harvester, NLP, LLM eng |
| [KRY-DQV-001](07_data_quality_and_validation.md) | 1.0 | [Data Quality and Validation](07_data_quality_and_validation.md) | Implemented (partial) | Data quality, DBA, integration |
| [KRY-RFP-001](08_refresh_policy.md) | 1.0 | [Refresh Policy](08_refresh_policy.md) | Implemented (partial) | Infra ops, DBA, curators |
| [KRY-UI-001](09_ui_design.md) | 1.0 | [UI Design](09_ui_design.md) | Not implemented | Frontend, design |
| [KRY-OBS-001](10_monitoring_and_observability.md) | 1.0 | [Monitoring and Observability](10_monitoring_and_observability.md) | Implemented (partial) | DevOps, backend, sysadmin |
| [KRY-ENG-001](11_agent_instructions.md) | 1.1 | [Coding Agent Instructions](11_agent_instructions.md) | Implemented | Coding agents, solo devs |
| [KRY-MAP-001](12_multi_agent_protocol.md) | 1.0 | [Multi-Agent Protocol](12_multi_agent_protocol.md) | Implemented | Backend, SRE |
| [KRY-REF-001](SysDes-v6.md) | 6.0 | [Detailed Technical Reference v6](SysDes-v6.md) | Approved | All engineering roles |
| [KRY-CONF-001](13_conformance_matrix.md) | 1.0 | [Conformance Matrix](13_conformance_matrix.md) | Implemented | Reviewers, maintainers |
| [KRY-OPS-001](14_operations.md) | 1.0 | [Operations Runbook](14_operations.md) | Implemented | Operators, on-call |

---

## 2. Document Control

Each document carries this block directly beneath its title:

```markdown
| Field | Value |
|---|---|
| **Document ID** | KRY-XXX-NNN |
| **Revision** | N.N |
| **Status** | Approved / Implemented / Implemented (partial) / Planned / Not implemented |
| **Supersedes** | KRY-XXX-NNN rN.N |
| **Last updated** | YYYY-MM-DD |
| **Owner** | ... |
```

Revisions follow semver-lite: a major bump changes normative intent, a minor
bump adds non-breaking detail, a patch corrects prose or records conformance.

---

## 3. Precedence

When two documents conflict, the higher ID wins. In practice:

1. **[KRY-REF-001](SysDes-v6.md)** (SysDes v6) — the engineering reference. It is the
   most specific and most recently ratified of the design set.
2. **[KRY-SCH-001](04_data_schema.md)** — authoritative for DDL, per the instruction in
   [KRY-ENG-001](11_agent_instructions.md).
3. **[KRY-API-001](03_api_contracts.md)** — authoritative for endpoint shapes.
4. **[KRY-PRD-001](01_prd.md)** — authoritative for scope and success criteria.
5. The remaining documents — binding within their own domain.

`sql/schema.sql` is the executable form of KRY-SCH-001 and is the artifact of
record for the database.

### Known precedence exception

[KRY-API-001](03_api_contracts.md) §1 specifies the `/admin` and `/api` **port
boundaries**, which the implementation honours, but the doc's endpoint paths are
written without the version segment (`/v1/runs`). The implementation serves
`/admin/v1/runs` and `/api/v1/entities/*` — the boundary *and* the version
segment, composed. See [KRY-CONF-001 §3](13_conformance_matrix.md) for the
field-level deltas and this doc's revision note.

---

## 4. Adding a Document

1. Allocate the next free ID in the series for that category.
2. Add the document control block; set `Supersedes` if it replaces another.
3. Register it in §1 above, including `Status: Planned`.
4. If it specifies behaviour, add a row to
   [KRY-CONF-001](13_conformance_matrix.md) and bump its revision.
