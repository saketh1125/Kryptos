from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.connection import get_session
from ghkge.database.models import AcquisitionRun, GapQueue, StrategyYieldLog
from ghkge.models.schemas import (
    GapEntry,
    GapListResponse,
    GapResolveRequest,
    GapResolveResponse,
    RunCreateRequest,
    RunCreateResponse,
    RunStatusResponse,
    StrategyYield,
    StrategyYieldResponse,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/v1", tags=["orchestration"])


@router.post("/runs", response_model=RunCreateResponse, status_code=202)
async def trigger_run(
    request: RunCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> RunCreateResponse:
    """Start a new acquisition run."""
    run = AcquisitionRun(
        domain=request.domain,
        trigger=request.trigger,
        status="queued",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    # Schedule the run in background
    from ghkge.orchestrator.pipeline import execute_run

    background_tasks.add_task(execute_run, str(run.id), request.domain, request.entity_types)

    logger.info("run.triggered", run_id=str(run.id), domain=request.domain)

    return RunCreateResponse(
        run_id=run.id,
        status="queued",
        submitted_at=run.started_at or datetime.now(UTC),
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RunStatusResponse:
    """Get the status of an acquisition run."""
    stmt = select(AcquisitionRun).where(AcquisitionRun.id == run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()

    if run is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Run not found")

    return RunStatusResponse(
        run_id=run.id,
        domain=run.domain,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.get("/gaps", response_model=GapListResponse)
async def list_gaps(
    domain: str,
    status: str = "open",
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> GapListResponse:
    """List coverage gaps for a domain."""
    stmt = (
        select(GapQueue)
        .where(GapQueue.status == status)
        .order_by(GapQueue.severity.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    gaps = result.scalars().all()

    return GapListResponse(
        gaps=[
            GapEntry(
                id=g.id,
                grid_cell=g.grid_cell,
                entity_type=g.entity_type,
                kind=g.kind,
                severity=g.severity,
                status=g.status,
                created_at=g.created_at,
            )
            for g in gaps
        ]
    )


@router.post("/gaps/{gap_id}/resolve", response_model=GapResolveResponse)
async def resolve_gap(
    gap_id: uuid.UUID,
    request: GapResolveRequest,
    session: AsyncSession = Depends(get_session),
) -> GapResolveResponse:
    """Resolve or skip a coverage gap."""
    stmt = select(GapQueue).where(GapQueue.id == gap_id)
    result = await session.execute(stmt)
    gap = result.scalar_one_or_none()

    if gap is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Gap not found")

    gap.status = "resolved"
    await session.commit()

    return GapResolveResponse(
        gap_id=gap.id,
        status="resolved",
        resolution=request.resolution,
    )


@router.get("/strategies/yield", response_model=StrategyYieldResponse)
async def get_strategy_yield(
    entity_type: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> StrategyYieldResponse:
    """Get strategy yield metrics."""
    stmt = select(StrategyYieldLog).order_by(StrategyYieldLog.logged_at.desc()).limit(50)
    if entity_type:
        stmt = stmt.where(StrategyYieldLog.entity_type == entity_type)
    result = await session.execute(stmt)
    logs = result.scalars().all()

    strategies = []
    seen: set[str] = set()
    for log in logs:
        if log.strategy_name in seen:
            continue
        seen.add(log.strategy_name)
        entities_per_call = log.entities_found / max(log.calls_made, 1)
        novelty_rate = log.novel_entities / max(log.entities_found, 1)
        strategies.append(
            StrategyYield(
                strategy_name=log.strategy_name,
                entity_type=log.entity_type,
                entities_per_call=entities_per_call,
                novelty_rate=novelty_rate,
                last_run=log.logged_at,
            )
        )

    return StrategyYieldResponse(strategies=strategies)
