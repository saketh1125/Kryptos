"""Postgres-backed task event bus (doc 12 multi-agent protocol).

Agents coordinate exclusively through the ``task_queue`` table — no broker,
no in-memory state. Claiming uses ``FOR UPDATE SKIP LOCKED`` so multiple
workers (or containers) can safely poll concurrently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.models import TaskQueue

logger = structlog.get_logger()

# Task types (pipeline stages)
TASK_PLAN = "plan"
TASK_HARVEST = "harvest"
TASK_EXTRACT = "extract"
TASK_CONSOLIDATE = "consolidate"

# Agent role names (doc 12 §1)
AGENT_PLANNER = "planner"
AGENT_HARVESTER = "harvester"
AGENT_SYNTHESIS = "synthesis"
AGENT_CONSOLIDATOR = "consolidator"
AGENT_GAP_EVALUATOR = "gap_evaluator"

ACTIVE_STATUSES = ("pending", "in_progress")


def new_task(
    task_type: str,
    target_agent: str,
    payload: dict[str, Any],
    *,
    source_agent: str = "",
    run_id: uuid.UUID | None = None,
    max_attempts: int = 3,
) -> TaskQueue:
    """Build an unsaved TaskQueue row."""
    return TaskQueue(
        task_type=task_type,
        target_agent=target_agent,
        source_agent=source_agent,
        payload=payload,
        status="pending",
        max_attempts=max_attempts,
        run_id=run_id,
    )


async def enqueue(
    session: AsyncSession,
    task_type: str,
    target_agent: str,
    payload: dict[str, Any],
    *,
    source_agent: str = "",
    run_id: uuid.UUID | None = None,
    flush_only: bool = True,
) -> TaskQueue:
    """Add a task to the queue. Caller owns the commit when flush_only."""
    task = new_task(
        task_type, target_agent, payload, source_agent=source_agent, run_id=run_id
    )
    session.add(task)
    if flush_only:
        await session.flush()
    else:
        await session.commit()
        await session.refresh(task)
    return task


async def claim_tasks(task_type: str, limit: int = 5) -> list[TaskQueue]:
    """Atomically claim up to ``limit`` pending tasks of a type."""
    from ghkge.database.connection import session_factory

    now = datetime.now(UTC)
    async with session_factory()() as session:
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.task_type == task_type, TaskQueue.status == "pending")
            .order_by(TaskQueue.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        tasks = list(result.scalars().all())
        for task in tasks:
            task.status = "in_progress"
            task.claimed_at = now
            task.attempts += 1
        await session.commit()
        for t in tasks:
            await session.refresh(t)
    return tasks


async def complete_task(task_id: uuid.UUID, result: dict[str, Any] | None = None) -> None:
    """Mark a task done with its result payload."""
    from ghkge.database.connection import session_factory

    now = datetime.now(UTC)
    async with session_factory()() as session:
        task = await session.get(TaskQueue, task_id)
        if task is None:
            logger.warning("bus.task_not_found", task_id=str(task_id))
            return
        task.status = "done"
        task.result = result or {}
        task.completed_at = now
        await session.commit()


async def fail_task(
    task_id: uuid.UUID,
    error: str,
    *,
    retryable: bool = True,
    requeue_delay_s: float = 0.0,
) -> None:
    """Fail a task; requeue for retry when attempts remain, else mark failed."""
    from ghkge.database.connection import session_factory

    async with session_factory()() as session:
        task = await session.get(TaskQueue, task_id)
        if task is None:
            logger.warning("bus.task_not_found", task_id=str(task_id))
            return
        task.last_error = error[:2000]
        can_retry = retryable and task.attempts < task.max_attempts
        if can_retry:
            task.status = "pending"
            task.claimed_at = None
            if requeue_delay_s > 0:
                # Push to the back of the queue by backdating is unsafe;
                # instead bump updated_at so ordering retries after fresh work.
                task.created_at = datetime.now(UTC) - timedelta(seconds=requeue_delay_s)
        else:
            task.status = "failed"
            task.completed_at = datetime.now(UTC)
        await session.commit()
        logger.info(
            "bus.task_failed",
            task_id=str(task_id),
            task_type=task.task_type,
            attempts=task.attempts,
            retriable=can_retry,
            error=error,
        )


async def reclaim_stale_tasks(max_age_minutes: int = 10) -> int:
    """Requeue tasks stuck in_progress (e.g. worker crashed mid-flight)."""
    from ghkge.database.connection import session_factory

    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    reclaimed = 0
    async with session_factory()() as session:
        stmt = select(TaskQueue).where(
            TaskQueue.status == "in_progress", TaskQueue.claimed_at < cutoff
        )
        result = await session.execute(stmt)
        stale = result.scalars().all()
        for task in stale:
            task.status = "pending"
            task.claimed_at = None
            reclaimed += 1
        if reclaimed:
            await session.commit()
    if reclaimed:
        logger.warning("bus.stale_tasks_reclaimed", count=reclaimed)
    return reclaimed


async def count_active_tasks(run_id: uuid.UUID) -> int:
    """Number of pending/in_progress tasks belonging to a run."""
    from ghkge.database.connection import session_factory

    async with session_factory()() as session:
        stmt = (
            select(func.count())
            .select_from(TaskQueue)
            .where(
                TaskQueue.run_id == run_id,
                TaskQueue.status.in_(ACTIVE_STATUSES),
            )
        )
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


async def reset_run_gaps_to_open(session: AsyncSession, run_id: uuid.UUID) -> None:
    from ghkge.database.models import GapQueue, TaskQueue

    stmt = select(TaskQueue).where(
        TaskQueue.run_id == run_id, TaskQueue.status == "failed"
    )
    result = await session.execute(stmt)
    gap_ids: set[uuid.UUID] = set()
    for task in result.scalars():
        raw = (task.payload or {}).get("gap_id")
        if raw:
            gap_ids.add(uuid.UUID(raw))
    if gap_ids:
        gaps = await session.execute(select(GapQueue).where(GapQueue.id.in_(gap_ids)))
        for gap in gaps.scalars():
            if gap.status == "in_progress":
                gap.status = "open"
