from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.connection import get_session
from ghkge.database.models import ExtractedFact, GapQueue, StrategyYieldLog
from ghkge.models.schemas import (
    FactItem,
    FactListResponse,
    FactModerationRequest,
    FactModerationResponse,
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
from ghkge.orchestrator.pipeline import reap_gap, submit_run

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/v1", tags=["orchestration"])


@router.post("/runs", response_model=RunCreateResponse, status_code=202)
async def trigger_run(
    request: RunCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> RunCreateResponse:
    """Queue a new acquisition run (202: work is picked up by the planner agent)."""
    from datetime import UTC, datetime

    run = await submit_run(request.domain, request.entity_types, request.trigger)
    return RunCreateResponse(
        run_id=run.id,
        status=run.status,
        submitted_at=run.started_at or datetime.now(UTC),
    )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RunStatusResponse:
    from ghkge.database.models import AcquisitionRun

    run = await session.get(AcquisitionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunStatusResponse(
        run_id=run.id,
        domain=run.domain,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        facts_extracted=run.facts_extracted,
        entities_written=run.entities_written,
        errors=list(run.errors or []),
    )


@router.get("/gaps", response_model=GapListResponse)
async def list_gaps(
    domain: str,
    status: str = "open",
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> GapListResponse:
    """Coverage gaps for a domain, highest severity first."""
    stmt = select(GapQueue).where(GapQueue.domain == domain, GapQueue.status == status)
    if kind:
        stmt = stmt.where(GapQueue.kind == kind)
    gaps = (
        (
            await session.execute(
                stmt.order_by(GapQueue.severity.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return GapListResponse(gaps=[GapEntry.model_validate(g) for g in gaps])


@router.post("/gaps/{gap_id}/resolve", response_model=GapResolveResponse)
async def resolve_gap(
    gap_id: uuid.UUID,
    request: GapResolveRequest,
    session: AsyncSession = Depends(get_session),
) -> GapResolveResponse:
    """Close a gap ('skip') or re-open it for the next planner pass ('retry')."""
    try:
        gap = await reap_gap(gap_id, request.resolution)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Gap not found") from exc
    return GapResolveResponse(gap_id=gap.id, status=gap.status, resolution=request.resolution)


@router.get("/strategies/yield", response_model=StrategyYieldResponse)
async def get_strategy_yield(
    entity_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> StrategyYieldResponse:
    """Per-strategy yield metrics the planner scores on."""
    stmt = select(StrategyYieldLog).order_by(StrategyYieldLog.logged_at.desc()).limit(limit)
    if entity_type:
        stmt = stmt.where(StrategyYieldLog.entity_type == entity_type)
    logs = (await session.execute(stmt)).scalars().all()

    strategies: list[StrategyYield] = []
    seen: set[tuple[str, str]] = set()
    for row in logs:
        key = (row.strategy_name, row.entity_type)
        if key in seen:
            continue
        seen.add(key)
        strategies.append(
            StrategyYield(
                strategy_name=row.strategy_name,
                entity_type=row.entity_type,
                entities_per_call=row.entities_found / max(row.calls_made, 1),
                novelty_rate=row.novel_entities / max(row.entities_found, 1),
                last_run=row.logged_at,
            )
        )
    return StrategyYieldResponse(strategies=strategies)


@router.get("/facts", response_model=FactListResponse)
async def list_review_queue(
    status: str = "held_for_review",
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> FactListResponse:
    """Facts awaiting human moderation (the conflict queue)."""
    facts = (
        (
            await session.execute(
                select(ExtractedFact)
                .where(ExtractedFact.resolution_status == status)
                .order_by(ExtractedFact.extracted_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return FactListResponse(facts=[FactItem.model_validate(f) for f in facts])


@router.post("/facts/{fact_id}/moderate", response_model=FactModerationResponse)
async def moderate_fact(
    fact_id: uuid.UUID,
    request: FactModerationRequest,
    session: AsyncSession = Depends(get_session),
) -> FactModerationResponse:
    """Approve or reject a held fact."""
    fact = await session.get(ExtractedFact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    fact.resolution_status = "approved" if request.action == "approve" else "rejected"
    await session.commit()
    return FactModerationResponse(fact_id=fact.id, resolution_status=fact.resolution_status)
