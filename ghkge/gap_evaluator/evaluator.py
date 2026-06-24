from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.connection import async_session_factory
from ghkge.database.models import Entity, GapQueue

logger = structlog.get_logger()

# Default staleness thresholds in days
DEFAULT_STALENESS: dict[str, int] = {
    "regulatory_rule": 7,
    "public_event": 1,
    "local_business": 30,
    "landmark": 90,
}


async def evaluate_coverage(session: AsyncSession, domain: str) -> list[GapQueue]:
    """
    Evaluate coverage gaps by scanning entity density and staleness.
    Runs as a separate scheduled job, not inline with extraction.
    """
    gaps: list[GapQueue] = []

    # 1. Check for stale entities
    now = datetime.now(UTC)
    entity_types = list(DEFAULT_STALENESS.keys())

    for entity_type in entity_types:
        max_age_days = DEFAULT_STALENESS.get(entity_type, 90)
        cutoff = datetime(
            now.year, now.month, now.day, tzinfo=UTC
        ).timestamp() - (max_age_days * 86400)

        stmt = select(Entity).where(
            Entity.entity_type == entity_type,
            Entity.last_verified.timestamp() < cutoff,
        )
        result = await session.execute(stmt)
        stale_entities = result.scalars().all()

        for entity in stale_entities:
            gap = GapQueue(
                grid_cell=entity.grid_cell,
                entity_type=entity_type,
                kind="reinforce",
                severity=2.0,
                status="open",
            )
            gaps.append(gap)

    # 2. Check for density anomalies (cells with fewer entities than median)
    stmt = (
        select(Entity.grid_cell, func.count(Entity.id).label("count"))
        .group_by(Entity.grid_cell)
    )
    result = await session.execute(stmt)
    cell_counts = result.all()

    if cell_counts:
        counts = [r[1] for r in cell_counts]
        median_count = sorted(counts)[len(counts) // 2]
        threshold = median_count * 0.3

        for grid_cell, count in cell_counts:
            if count < threshold:
                gap = GapQueue(
                    grid_cell=grid_cell,
                    entity_type="unknown",
                    kind="anomaly",
                    severity=median_count - count,
                    status="open",
                )
                gaps.append(gap)

    # Write gaps to database
    for gap in gaps:
        session.add(gap)
    await session.commit()

    logger.info("gap_evaluator.complete", gaps_found=len(gaps), domain=domain)
    return gaps


async def run_gap_evaluation(domain: str = "default") -> None:
    """Entry point for scheduled gap evaluation."""
    async with async_session_factory() as session:
        gaps = await evaluate_coverage(session, domain)
        logger.info("gap_evaluator.scheduled_run", domain=domain, gaps=len(gaps))
