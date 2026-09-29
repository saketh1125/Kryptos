"""Gap & Coverage Evaluator (agent role A5).

Runs as a scheduled batch job — never inline with extraction. Scans:

1. **Bootstrap coverage** — every geohash cell covering the domain bbox must
   have entities; empty cells produce ``missing`` gaps (the only way the
   pipeline ever bootstraps from an empty database).
2. **Density anomalies** — cells far below the median entity count produce
   ``anomaly`` gaps.
3. **Staleness** — entities older than their type's cutoff produce
   ``reinforce`` gaps (thresholds come from the domain YAML).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.config.settings import settings
from ghkge.database.connection import session_factory
from ghkge.database.models import Entity, GapQueue
from ghkge.models.schemas import DomainConfig
from ghkge.orchestrator.domain import load_domain_config
from ghkge.utils.geohash import enumerate_cells

logger = structlog.get_logger()

# Fallback staleness thresholds (days) when not overridden by domain YAML
DEFAULT_STALENESS: dict[str, int] = {
    "regulatory_rule": 7,
    "public_event": 1,
    "local_business": 30,
    "landmark": 90,
}

# Cap on newly created bootstrap gaps per evaluation pass
MAX_NEW_GAPS_PER_PASS = 200


async def _existing_open_gap_keys(session: AsyncSession, domain: str) -> set[tuple[str, str, str]]:
    stmt = select(GapQueue.grid_cell, GapQueue.entity_type, GapQueue.kind).where(
        GapQueue.status.in_(["open", "in_progress"]),
        (GapQueue.domain == domain) | (GapQueue.domain.is_(None)),
    )
    result = await session.execute(stmt)
    return {(r[0], r[1], r[2]) for r in result.all()}


async def _resolve_satisfied_missing_gaps(session: AsyncSession) -> int:
    """Close ``missing`` gaps whose cell now holds at least one active entity."""
    open_missing = await session.execute(
        select(GapQueue).where(GapQueue.status == "open", GapQueue.kind == "missing")
    )
    resolved = 0
    counts_stmt = select(Entity.grid_cell, func.count(Entity.id)).group_by(Entity.grid_cell)
    populated = {
        cell for cell, n in (await session.execute(counts_stmt)).all() if n and n > 0
    }
    for gap in open_missing.scalars():
        if gap.grid_cell in populated:
            gap.status = "resolved"
            resolved += 1
    return resolved


async def _bootstrap_coverage_gaps(
    session: AsyncSession,
    config: DomainConfig,
    seen_keys: set[tuple[str, str, str]],
) -> list[GapQueue]:
    """Emit ``missing`` gaps for every empty cell x configured entity type."""
    cells = enumerate_cells(config.geography_bbox, precision=config.grid_precision)
    counts_stmt = select(Entity.grid_cell, func.count(Entity.id)).group_by(Entity.grid_cell)
    populated = {cell for cell, _ in (await session.execute(counts_stmt)).all()}

    empty_cells = [c for c in cells if c not in populated]
    if not empty_cells:
        return []

    gaps: list[GapQueue] = []
    for cell in empty_cells:
        for et in config.entity_types:
            key = (cell, et.name, "missing")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            gaps.append(
                GapQueue(
                    grid_cell=cell,
                    entity_type=et.name,
                    kind="missing",
                    severity=3.0,
                    status="open",
                    domain=config.domain,
                )
            )
            if len(gaps) >= MAX_NEW_GAPS_PER_PASS:
                logger.warning("gap_evaluator.bootstrap_cap_reached", cap=MAX_NEW_GAPS_PER_PASS)
                return gaps
    return gaps


def staleness_cutoffs(config: DomainConfig | None = None) -> dict[str, int]:
    """Effective staleness thresholds merged from defaults + domain YAML."""
    overrides = config.staleness_cutoff_days if config else {}
    return {**DEFAULT_STALENESS, **overrides}


async def _staleness_gaps(
    session: AsyncSession,
    config: DomainConfig,
    seen_keys: set[tuple[str, str, str]],
) -> list[GapQueue]:
    """Emit ``reinforce`` gaps for entities past their staleness cutoff."""
    now = datetime.now(UTC)
    cutoffs = staleness_cutoffs(config)
    gaps: list[GapQueue] = []

    # Group stale entities per (cell, type): one gap per group, not per entity.
    for entity_type, max_age_days in cutoffs.items():
        cutoff = now - timedelta(days=max_age_days)
        stmt = (
            select(Entity.grid_cell, func.count(Entity.id))
            .where(
                Entity.entity_type == entity_type,
                Entity.last_verified < cutoff,
                Entity.status != "archived",
            )
            .group_by(Entity.grid_cell)
        )
        rows = (await session.execute(stmt)).all()
        for grid_cell, _count in rows:
            key = (grid_cell, entity_type, "reinforce")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            gaps.append(
                GapQueue(
                    grid_cell=grid_cell,
                    entity_type=entity_type,
                    kind="reinforce",
                    severity=2.0,
                    status="open",
                    domain=config.domain,
                )
            )
    return gaps


async def _anomaly_gaps(
    session: AsyncSession,
    config: DomainConfig,
    seen_keys: set[tuple[str, str, str]],
) -> list[GapQueue]:
    """Emit ``anomaly`` gaps for cells below 30% of the median density."""
    stmt = (
        select(Entity.grid_cell, func.count(Entity.id).label("count"))
        .where(Entity.status != "archived")
        .group_by(Entity.grid_cell)
    )
    cell_counts = (await session.execute(stmt)).all()
    gaps: list[GapQueue] = []
    if not cell_counts:
        return gaps

    counts = sorted(r[1] for r in cell_counts)
    median_count = counts[len(counts) // 2]
    threshold = median_count * 0.3
    if threshold <= 0:
        return gaps

    for grid_cell, count in cell_counts:
        if count >= threshold:
            continue
        key = (grid_cell, "unknown", "anomaly")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        gaps.append(
            GapQueue(
                grid_cell=grid_cell,
                entity_type="unknown",
                kind="anomaly",
                severity=float(median_count - count),
                status="open",
                domain=config.domain,
            )
        )
    return gaps


async def evaluate_coverage(
    session: AsyncSession,
    domain: str,
    config: DomainConfig | None = None,
) -> list[GapQueue]:
    """Full coverage evaluation pass; persists and returns created gaps."""
    if config is None:
        try:
            config = load_domain_config()
        except FileNotFoundError:
            logger.error("gap_evaluator.domain_config_missing", path=settings.domain_config_path)
            return []

    seen_keys = await _existing_open_gap_keys(session, domain)
    resolved = await _resolve_satisfied_missing_gaps(session)

    gaps: list[GapQueue] = []
    gaps.extend(await _staleness_gaps(session, config, seen_keys))
    gaps.extend(await _anomaly_gaps(session, config, seen_keys))
    gaps.extend(await _bootstrap_coverage_gaps(session, config, seen_keys))

    for gap in gaps:
        session.add(gap)
    await session.commit()

    logger.info(
        "gap_evaluator.complete",
        domain=domain,
        gaps_found=len(gaps),
        auto_resolved=resolved,
    )
    return gaps


async def sweep_stranded_gaps(older_than_minutes: int = 30) -> int:
    """Return long-running ``in_progress`` gaps to the queue.

    The happy paths release their own gaps, but a worker that dies between
    claiming a gap and finishing it leaves the gap in_progress with no task
    to release it. The evaluator counts in_progress as seen, so such a gap
    is never re-planned and coverage silently stops advancing (D-03).

    Returns the number of gaps recovered.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    async with session_factory()() as session:
        result = await session.execute(
            select(GapQueue).where(
                GapQueue.status == "in_progress", GapQueue.created_at < cutoff
            )
        )
        gaps = result.scalars().all()
        for gap in gaps:
            gap.status = "open"
        if gaps:
            await session.commit()
        recovered = len(gaps)
    if recovered:
        logger.warning("gap_evaluator.stranded_gaps_recovered", count=recovered)
    return recovered


async def run_gap_evaluation(domain: str | None = None) -> int:
    """Entry point for the scheduler. Returns number of gaps created."""
    domain = domain or settings.scheduler_default_domain
    config = load_domain_config()
    domain = domain or config.domain
    async with session_factory()() as session:
        gaps = await evaluate_coverage(session, domain, config)
        logger.info("gap_evaluator.scheduled_run", domain=domain, gaps=len(gaps))
        return len(gaps)
