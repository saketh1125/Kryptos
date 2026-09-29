from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from ghkge.config.settings import settings
from ghkge.gap_evaluator.evaluator import run_gap_evaluation
from ghkge.orchestrator import bus, workers
from ghkge.orchestrator.domain import load_domain_config
from ghkge.orchestrator.pipeline import submit_run

logger = structlog.get_logger()

_scheduler: Any = None
_worker_tasks: list[asyncio.Task[None]] = []
_stop_event: asyncio.Event | None = None

AGENTS = (
    bus.AGENT_PLANNER,
    bus.AGENT_HARVESTER,
    bus.AGENT_SYNTHESIS,
    bus.AGENT_CONSOLIDATOR,
)


def _domain_name() -> str:
    if settings.scheduler_default_domain:
        return settings.scheduler_default_domain
    with contextlib.suppress(Exception):
        return load_domain_config().domain
    return "default"


async def _scheduled_gap_evaluation() -> None:
    with contextlib.suppress(Exception):
        await run_gap_evaluation(_domain_name())


async def _scheduled_acquisition() -> None:
    with contextlib.suppress(Exception):
        await submit_run(_domain_name(), trigger="scheduled")


async def start_background() -> None:
    """Start worker loops and the scheduler (idempotent)."""
    global _scheduler, _stop_event

    if _stop_event is not None:
        return
    _stop_event = asyncio.Event()

    if settings.workers_enabled:
        for agent in AGENTS:
            _worker_tasks.append(asyncio.create_task(workers.run_worker_loop(agent, _stop_event)))
        logger.info("scheduler.workers_started", count=len(_worker_tasks))

    if settings.scheduler_enabled:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        _scheduler = AsyncIOScheduler(timezone="UTC")
        _scheduler.add_job(
            _scheduled_gap_evaluation,
            "interval",
            hours=settings.gap_eval_interval_hours,
            id="gap_evaluation",
        )
        if settings.scheduled_acquisition_enabled:
            _scheduler.add_job(
                _scheduled_acquisition,
                "interval",
                hours=settings.acquisition_interval_hours,
                id="acquisition",
            )
        _scheduler.start()
        logger.info(
            "scheduler.started",
            gap_eval_hours=settings.gap_eval_interval_hours,
            acquisition_enabled=settings.scheduled_acquisition_enabled,
        )


async def stop_background() -> None:
    """Stop workers and the scheduler, then release resources."""
    global _scheduler, _stop_event

    if _stop_event is not None:
        _stop_event.set()
    for task in _worker_tasks:
        task.cancel()
    for task in _worker_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _worker_tasks.clear()

    if _scheduler is not None:
        with contextlib.suppress(Exception):
            _scheduler.shutdown(wait=False)
        _scheduler = None

    await workers.close_workers()
    _stop_event = None
    logger.info("scheduler.stopped")
