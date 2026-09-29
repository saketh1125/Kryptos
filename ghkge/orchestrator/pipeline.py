"""Run lifecycle: submit, finalize, and aggregate strategy yield.

A run is created, a single ``plan`` task is enqueued, and the worker agents
chase it through harvest -> extract -> consolidate. When the last task for a
run settles, :func:`finalize_run_if_done` stamps counters, writes
``strategy_yield_log`` rows the planner scores on next cycle, and re-opens
gaps whose chains failed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from ghkge.database.connection import session_factory
from ghkge.database.models import AcquisitionRun, GapQueue, StrategyYieldLog, TaskQueue
from ghkge.orchestrator import bus

logger = structlog.get_logger()

TERMINAL_STATUSES = ("done", "failed")


async def submit_run(
    domain: str,
    entity_types: list[str] | None = None,
    trigger: str = "manual",
) -> AcquisitionRun:
    """Create an acquisition run and enqueue its plan task."""
    async with session_factory()() as session:
        run = AcquisitionRun(
            domain=domain,
            trigger=trigger,
            status="queued",
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()

        await bus.enqueue(
            session,
            bus.TASK_PLAN,
            bus.AGENT_PLANNER,
            {"domain": domain, "entity_types": entity_types or [], "run_id": str(run.id)},
            source_agent="api",
            run_id=run.id,
        )
        await session.commit()
        await session.refresh(run)
        logger.info("run.submitted", run_id=str(run.id), domain=domain, trigger=trigger)
        return run


async def _aggregate_yield(session: Any, run_id: uuid.UUID) -> None:
    """Write strategy_yield_log rows from this run's task results."""
    from sqlalchemy import select

    tasks = (
        (await session.execute(select(TaskQueue).where(TaskQueue.run_id == run_id)))
        .scalars()
        .all()
    )

    stats: dict[tuple[str, str], dict[str, float]] = {}
    for task in tasks:
        payload = task.payload or {}
        strategy = payload.get("strategy", "")
        entity_type = payload.get("entity_type", "")
        if not strategy or not entity_type:
            continue
        key = (strategy, entity_type)
        entry = stats.setdefault(
            key,
            {"calls": 0.0, "entities": 0.0, "novel": 0.0, "confidence_sum": 0.0,
             "confidence_n": 0.0, "tier_sum": 0.0, "tier_n": 0.0},
        )
        result = task.result or {}
        if task.task_type == bus.TASK_HARVEST and result.get("fetched"):
            entry["calls"] += 1
        elif task.task_type == bus.TASK_EXTRACT:
            tier = result.get("source_tier")
            if tier:
                entry["tier_sum"] += float(tier)
                entry["tier_n"] += 1
        elif task.task_type == bus.TASK_CONSOLIDATE:
            entry["entities"] += float(result.get("consolidated", 0) or 0)
            entry["novel"] += float(result.get("created", 0) or 0)

    for (strategy, entity_type), s in stats.items():
        session.add(
            StrategyYieldLog(
                strategy_name=strategy,
                entity_type=entity_type,
                run_id=run_id,
                calls_made=int(s["calls"]),
                entities_found=int(s["entities"]),
                novel_entities=int(s["novel"]),
                avg_source_tier=int(
                    round(s["tier_sum"] / s["tier_n"]) if s["tier_n"] else 4
                ),
            )
        )


async def finalize_run_if_done(run_id: uuid.UUID) -> bool:
    """Stamp a run complete once no active tasks remain. Returns True if finalized."""
    from sqlalchemy import select

    async with session_factory()() as session:
        active = await bus.count_active_tasks(run_id)
        if active > 0:
            return False

        run = await session.get(AcquisitionRun, run_id)
        if run is None or run.status in ("completed", "failed", "partial"):
            return False

        tasks = (
            (await session.execute(select(TaskQueue).where(TaskQueue.run_id == run_id)))
            .scalars()
            .all()
        )

        facts = sum(
            int((t.result or {}).get("facts", 0) or 0)
            for t in tasks
            if t.task_type == bus.TASK_EXTRACT
        )
        entities = sum(
            int((t.result or {}).get("consolidated", 0) or 0)
            for t in tasks
            if t.task_type == bus.TASK_CONSOLIDATE
        )
        errors = [t.last_error for t in tasks if t.status == "failed" and t.last_error]

        await _aggregate_yield(session, run_id)
        await bus.reset_run_gaps_to_open(session, run_id)

        run.facts_extracted = facts
        run.entities_written = entities
        run.errors = errors
        run.completed_at = datetime.now(UTC)
        run.status = "partial" if errors else "completed"
        await session.commit()

        logger.info(
            "run.finalized",
            run_id=str(run_id),
            status=run.status,
            facts_extracted=facts,
            entities_written=entities,
            errors=len(errors),
        )
        return True


async def reap_gap(gap_id: uuid.UUID, resolution: str) -> GapQueue:
    """Resolve a gap: 'skip' closes it, 'retry' re-opens it for the next planner pass."""
    async with session_factory()() as session:
        gap = await session.get(GapQueue, gap_id)
        if gap is None:
            raise LookupError(gap_id)
        gap.status = "open" if resolution == "retry" else "resolved"
        await session.commit()
        await session.refresh(gap)
        return gap


async def run_status(run_id: uuid.UUID) -> AcquisitionRun | None:
    async with session_factory()() as session:
        return await session.get(AcquisitionRun, run_id)
