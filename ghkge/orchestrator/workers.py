"""Agent role workers: the five doc-12 roles as polling loops over the bus.

Each worker claims its own task type (``FOR UPDATE SKIP LOCKED``) and enqueues
the next stage's task. All state is in Postgres, so restarts resume cleanly
and a crashed task is reclaimed by the stale-task sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import structlog
from sqlalchemy import select

from ghkge.compliance.engine import ComplianceEngine
from ghkge.compliance.rate_limiter import PostgresRateLimiter
from ghkge.config.settings import settings
from ghkge.consolidation.resolver import (
    Resolution,
    find_matching_entity,
    resolve_conflict,
    upsert_entity,
)
from ghkge.consolidation.sync import Neo4jUnavailableError, write_fact_to_stores
from ghkge.database.connection import session_factory
from ghkge.database.models import (
    AcquisitionRun,
    Entity,
    ExtractedFact,
    GapQueue,
    RawCapture,
)
from ghkge.harvesters.api_client import APIHarvester
from ghkge.harvesters.base import BaseHarvester
from ghkge.harvesters.document import DocumentHarvester
from ghkge.harvesters.media import MediaHarvester
from ghkge.harvesters.web import WebHarvester
from ghkge.models.schemas import DomainConfig, ExtractedFactSchema, SourceTier
from ghkge.orchestrator import bus
from ghkge.orchestrator.domain import load_domain_config, plan_targets
from ghkge.orchestrator.planner import plan_gaps
from ghkge.synthesis.refiner import extract_facts_from_text

logger = structlog.get_logger()

# CATEGORY -> ontology entity type used for entity resolution
CATEGORY_TO_TYPE = {
    "LOCATION": "landmark",
    "ORGANIZATION": "local_business",
    "EVENT": "public_event",
    "RULE": "regulatory_rule",
    "METADATA": "metadata",
}

_compliance: ComplianceEngine | None = None
_pg_pool: Any = None
_harvesters: dict[str, BaseHarvester] = {}


async def get_compliance() -> ComplianceEngine:
    """Lazily build the shared compliance engine (with rate limiting)."""
    global _compliance, _pg_pool
    if _compliance is None:
        rate_limiter = None
        try:
            import asyncpg

            db_url = settings.database_url.replace("+asyncpg", "")
            _pg_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)
            rate_limiter = PostgresRateLimiter(_pg_pool)
        except Exception:
            logger.warning("worker.rate_limiter_init_failed", exc_info=True)
            _pg_pool = None
        _compliance = ComplianceEngine(rate_limiter=rate_limiter)
    return _compliance


async def get_harvester(engine: str) -> BaseHarvester | None:
    """Harvester for a strategy engine name."""
    if not _harvesters:
        compliance = await get_compliance()
        _harvesters.update(
            {
                "web": WebHarvester(compliance),
                "media": MediaHarvester(compliance),
                "document": DocumentHarvester(compliance),
                "api": APIHarvester(compliance),
                "api_overpass": APIHarvester(compliance),
            }
        )
    return _harvesters.get(engine)


async def close_workers() -> None:
    """Release worker-held resources on shutdown."""
    global _compliance
    from ghkge.consolidation.sync import close_neo4j

    await close_neo4j()
    if _pg_pool is not None:
        with contextlib.suppress(Exception):
            await _pg_pool.close()
    _compliance = None


async def _set_run_status(run_id: uuid.UUID, status: str) -> None:
    from datetime import UTC, datetime

    async with session_factory()() as session:
        run = await session.get(AcquisitionRun, run_id)
        if run is not None and run.status in ("queued", "running"):
            run.status = status
            if status == "running":
                run.started_at = datetime.now(UTC)
            await session.commit()


# --- A1: Strategy & Compliance (planner) ---


async def handle_plan_task(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Read open gaps, rank strategies, enqueue harvest instructions."""
    domain = task_payload["domain"]
    entity_types = task_payload.get("entity_types") or []
    run_id = uuid.UUID(task_payload["run_id"])

    config: DomainConfig = load_domain_config()
    async with session_factory()() as session:
        planned = await plan_gaps(session, config, domain, entity_types)
        enqueued = 0
        for task in planned:
            target = plan_targets(config, task["entity_type"], task["grid_cell"], task["strategy"])
            if not target:
                logger.warning(
                    "planner.no_target",
                    strategy=task["strategy"],
                    entity_type=task["entity_type"],
                )
                continue
            urls = target.get("urls") or ([target["url"]] if target.get("url") else [])
            for url in urls:
                await bus.enqueue(
                    session,
                    bus.TASK_HARVEST,
                    bus.AGENT_HARVESTER,
                    {
                        "url": url,
                        "engine": target["engine"],
                        "strategy": task["strategy"],
                        "entity_type": task["entity_type"],
                        "grid_cell": task["grid_cell"],
                        "gap_id": task["gap_id"],
                        "run_id": str(run_id),
                        "domain": domain,
                    },
                    source_agent=bus.AGENT_PLANNER,
                    run_id=run_id,
                )
                enqueued += 1
            if target.get("overpass_query"):
                await bus.enqueue(
                    session,
                    bus.TASK_HARVEST,
                    bus.AGENT_HARVESTER,
                    {
                        "overpass_query": target["overpass_query"],
                        "engine": target["engine"],
                        "strategy": task["strategy"],
                        "entity_type": task["entity_type"],
                        "grid_cell": task["grid_cell"],
                        "gap_id": task["gap_id"],
                        "run_id": str(run_id),
                        "domain": domain,
                    },
                    source_agent=bus.AGENT_PLANNER,
                    run_id=run_id,
                )
                enqueued += 1
        await session.commit()

    await _set_run_status(run_id, "running")
    return {"planned": len(planned), "harvest_tasks": enqueued}


# --- A2: Multi-Modal Harvester ---


async def handle_harvest_task(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch an approved target, persist the raw capture, enqueue extraction."""
    run_id = uuid.UUID(task_payload["run_id"])
    engine = task_payload["engine"]
    strategy = task_payload["strategy"]
    entity_type = task_payload["entity_type"]
    url = task_payload.get("url", "")
    overpass_query = task_payload.get("overpass_query")
    domain = task_payload.get("domain", "")

    harvester = await get_harvester(engine)
    if harvester is None:
        raise ValueError(f"unknown engine: {engine}")

    compliance = await get_compliance()

    # Overpass is a POST query: compliance-gate the endpoint, then query.
    if overpass_query:
        if not hasattr(harvester, "query_overpass"):
            raise ValueError("overpass_query requires an API harvester")
        capture = await harvester.query_overpass(
            overpass_query, run_id=run_id, entity_type=entity_type
        )
    else:
        approved = await compliance.approve(url, strategy=strategy, entity_type=entity_type)
        if approved is None:
            logger.info("worker.harvest_blocked", url=url, strategy=strategy)
            return {"fetched": False, "reason": "compliance_blocked"}
        capture = await harvester.fetch(approved, run_id=run_id)

    if capture is None:
        return {"fetched": False, "reason": "fetch_failed"}

    # Guardrail #2: raw capture persisted BEFORE any extraction.
    async with session_factory()() as session:
        existing = await session.execute(
            select(RawCapture.id).where(
                RawCapture.content_hash == capture.content_hash,
                RawCapture.domain == capture.domain,
            )
        )
        if existing.first() is not None:
            logger.info("worker.capture_duplicate", url=capture.source_url)
            return {"fetched": True, "duplicate": True, "raw_capture_id": None}

        row = RawCapture(
            source_url=capture.source_url,
            source_type=capture.source_type,
            domain=capture.domain or domain,
            raw_content=capture.raw_content,
            content_hash=capture.content_hash,
            strategy_used=capture.strategy_used,
            run_id=run_id,
        )
        session.add(row)
        await session.flush()

        await bus.enqueue(
            session,
            bus.TASK_EXTRACT,
            bus.AGENT_SYNTHESIS,
            {
                "raw_capture_id": str(row.id),
                "strategy": capture.strategy_used,
                "entity_type": entity_type,
                "grid_cell": task_payload["grid_cell"],
                "gap_id": task_payload["gap_id"],
                "run_id": str(run_id),
            },
            source_agent=bus.AGENT_HARVESTER,
            run_id=run_id,
        )
        await session.commit()
        capture_id = str(row.id)

    results: dict[str, Any] = {"fetched": True, "raw_capture_id": capture_id}
    return results


# --- A3: Synthesis Refiner ---


def source_tier_for_domain(source_domain: str) -> int:
    """Map a source domain to a trust tier (OFFICIAL > CURATED > SOCIAL)."""
    d = source_domain.lower()
    if d.endswith(".gov.in") or d.endswith(".nic.in") or d.endswith(".gov"):
        return int(SourceTier.OFFICIAL)
    if d.endswith(".org"):
        return int(SourceTier.CURATED)
    if any(s in d for s in ("wikipedia", "openstreetmap")):
        return int(SourceTier.CURATED)
    return int(SourceTier.SOCIAL_GENERAL)


async def handle_extract_task(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Chunk a raw capture, extract hyperlocal facts, enqueue consolidation."""
    run_id = uuid.UUID(task_payload["run_id"])
    capture_id = uuid.UUID(task_payload["raw_capture_id"])
    entity_type = task_payload.get("entity_type", "")

    async with session_factory()() as session:
        capture = await session.get(RawCapture, capture_id)
        if capture is None or not capture.raw_content:
            logger.warning("worker.capture_missing", raw_capture_id=str(capture_id))
            return {"facts": 0, "reason": "capture_missing"}

        tier = source_tier_for_domain(capture.domain)
        content = capture.raw_content

    facts: list[ExtractedFactSchema] = await extract_facts_from_text(
        content, source_url=capture.source_url
    )

    async with session_factory()() as session:
        written = 0
        for i, fact in enumerate(facts):
            row = ExtractedFact(
                raw_capture_id=capture_id,
                entity_name_raw=fact.entity_name,
                entity_category=fact.entity_category,
                contextual_insight=fact.contextual_insight,
                confidence_score=fact.confidence_score,
                source_tier=tier,
                is_safety_relevant=fact.is_safety_relevant,
                is_macro_knowledge=fact.is_macro_knowledge,
                chunk_index=i,
                resolution_status="pending",
            )
            session.add(row)
            written += 1
        await session.flush()

        if written:
            await bus.enqueue(
                session,
                bus.TASK_CONSOLIDATE,
                bus.AGENT_CONSOLIDATOR,
                {
                    "raw_capture_id": str(capture_id),
                    "entity_type": entity_type,
                    "grid_cell": task_payload["grid_cell"],
                    "gap_id": task_payload["gap_id"],
                    "strategy": task_payload.get("strategy", ""),
                    "run_id": str(run_id),
                },
                source_agent=bus.AGENT_SYNTHESIS,
                run_id=run_id,
            )
        await session.commit()

    return {"facts": written}


# --- A4: Graph Consolidator ---


def _fact_status(entity: Entity, fact: ExtractedFact) -> str:
    """Safety gate (doc 07 §4): safety facts need OFFICIAL tier or 2+ sources."""
    needs_corroboration = (
        fact.is_safety_relevant
        and fact.source_tier != int(SourceTier.OFFICIAL)
        and entity.corroboration_count < settings.safety_corroboration_min
    )
    return "held_for_review" if needs_corroboration else "approved"


async def handle_consolidate_task(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve entities, apply the safety gate, write the Trinity stores."""
    capture_id = uuid.UUID(task_payload["raw_capture_id"])
    grid_cell = task_payload["grid_cell"]

    async with session_factory()() as write_session:
        facts = (
            (
                await write_session.execute(
                    select(ExtractedFact)
                    .where(
                        ExtractedFact.raw_capture_id == capture_id,
                        ExtractedFact.resolution_status == "pending",
                    )
                    .order_by(ExtractedFact.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        if not facts:
            return {"consolidated": 0, "reason": "no_pending_facts"}

        # Single session throughout: the fact rows are bound to it, so their
        # status updates must commit on this same transaction.
        created = 0
        merged = 0
        for fact in facts:
            entity_type = CATEGORY_TO_TYPE.get(
                fact.entity_category, task_payload.get("entity_type") or "metadata"
            )
            existing = await find_matching_entity(
                write_session, fact.entity_name_raw, entity_type, grid_cell
            )
            if existing is not None:
                resolution = resolve_conflict(
                    existing_tier=int(existing.best_tier),
                    existing_confidence=1.0,
                    existing_corroboration=int(existing.corroboration_count),
                    incoming_tier=int(fact.source_tier),
                    incoming_confidence=float(fact.confidence_score),
                    is_safety_relevant=bool(fact.is_safety_relevant),
                )
                if resolution is Resolution.HOLD_FOR_REVIEW:
                    fact.resolution_status = "held_for_review"
                    await write_session.commit()
                    continue

            entity_id = await upsert_entity(
                write_session,
                fact.entity_name_raw,
                entity_type,
                grid_cell,
                source_tier=int(fact.source_tier),
                confidence=float(fact.confidence_score),
            )
            if existing is not None:
                merged += 1
            else:
                created += 1

            entity = await write_session.get(Entity, entity_id)
            assert entity is not None
            entity.last_verified = _utcnow()
            fact.canonical_entity_id = entity_id
            fact.resolution_status = _fact_status(entity, fact)

            try:
                await write_fact_to_stores(
                    write_session,
                    entity=entity,
                    fact=fact,
                    raw_capture_id=capture_id,
                    chunk_text=fact.contextual_insight,
                    source_tier=int(fact.source_tier),
                )
            except Neo4jUnavailableError:
                await write_session.rollback()
                logger.error(
                    "worker.neo4j_write_failed_rolling_back", raw_capture_id=str(capture_id)
                )
                raise
            await write_session.commit()

    summary = {"consolidated": created + merged, "created": created, "merged": merged}
    await _resolve_gap_if_served(task_payload.get("gap_id"))
    return summary


def _utcnow() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _resolve_gap_if_served(gap_id: str | None) -> None:
    """Close a gap once consolidation produced entities for it."""
    if not gap_id:
        return
    with contextlib.suppress(Exception):
        async with session_factory()() as session:
            gap = await session.get(GapQueue, uuid.UUID(gap_id))
            if gap is not None and gap.status == "in_progress":
                gap.status = "resolved"
                await session.commit()


WORKER_HANDLERS = {
    bus.TASK_PLAN: handle_plan_task,
    bus.TASK_HARVEST: handle_harvest_task,
    bus.TASK_EXTRACT: handle_extract_task,
    bus.TASK_CONSOLIDATE: handle_consolidate_task,
}

HANDLER_TASK_TYPES = {
    bus.AGENT_PLANNER: bus.TASK_PLAN,
    bus.AGENT_HARVESTER: bus.TASK_HARVEST,
    bus.AGENT_SYNTHESIS: bus.TASK_EXTRACT,
    bus.AGENT_CONSOLIDATOR: bus.TASK_CONSOLIDATE,
}


async def run_worker_loop(agent_name: str, stop_event: asyncio.Event) -> None:
    """Poll this agent's task type until stopped."""
    task_type = HANDLER_TASK_TYPES[agent_name]
    handler = WORKER_HANDLERS[task_type]
    logger.info("worker.started", agent=agent_name, task_type=task_type)

    last_reclaim = 0.0
    loop = asyncio.get_running_loop()

    while not stop_event.is_set():
        try:
            tasks = await bus.claim_tasks(task_type, limit=settings.worker_batch_size)
        except Exception:
            logger.error("worker.claim_failed", agent=agent_name, exc_info=True)
            await asyncio.sleep(settings.worker_poll_interval_s)
            continue

        for task in tasks:
            try:
                result = await handler(task.payload)
                await bus.complete_task(task.id, result)
            except Exception as exc:
                logger.error(
                    "worker.task_error", agent=agent_name, task_id=str(task.id), error=str(exc)
                )
                await bus.fail_task(task.id, str(exc), retryable=True)
                await _maybe_finalize(task.run_id)

        if loop.time() - last_reclaim > 60:
            last_reclaim = loop.time()
            with contextlib.suppress(Exception):
                await bus.reclaim_stale_tasks(settings.stale_task_takeover_minutes)

        for task in tasks:
            await _maybe_finalize(task.run_id)

        if not tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.worker_poll_interval_s
                )

    logger.info("worker.stopped", agent=agent_name)


async def _maybe_finalize(run_id: uuid.UUID | None) -> None:
    from ghkge.orchestrator.pipeline import finalize_run_if_done

    if run_id is None:
        return
    with contextlib.suppress(Exception):
        await finalize_run_if_done(run_id)
