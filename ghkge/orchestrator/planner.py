"""Strategy Planner — Stage 1 rule-based, yield-aware.

Reads the open gap queue, ranks strategies per entity type by historical
yield (entities/call x novelty rate), and emits harvest instructions
derived from the domain YAML (strategies, seeds) — never hardcoded here.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.models import GapQueue, StrategyYieldLog
from ghkge.models.schemas import DomainConfig
from ghkge.orchestrator.domain import STRATEGY_ENGINES

logger = structlog.get_logger()


async def load_yield_stats(session: AsyncSession) -> dict[str, dict[str, float]]:
    """Aggregate yield history keyed by 'strategy:entity_type'."""
    stmt = select(StrategyYieldLog).order_by(StrategyYieldLog.logged_at.desc()).limit(500)
    result = await session.execute(stmt)
    logs = result.scalars().all()

    stats: dict[str, dict[str, float]] = {}
    for row in logs:
        key = f"{row.strategy_name}:{row.entity_type}"
        if key not in stats:
            stats[key] = {"total_entities": 0.0, "total_calls": 0.0, "total_novel": 0.0}
        stats[key]["total_entities"] += float(row.entities_found)
        stats[key]["total_calls"] += float(row.calls_made)
        stats[key]["total_novel"] += float(row.novel_entities)
    return stats


def score_strategy(strategy: str, entity_type: str, yield_stats: dict[str, dict[str, float]]) -> float:
    """Yield score for a strategy: entities-per-call x novelty-rate.

    Untried strategies get a neutral 1.0 so exploration is not starved by
    zero-yield proven strategies settling at the bottom.
    """
    stats = yield_stats.get(f"{strategy}:{entity_type}")
    if not stats or stats["total_calls"] <= 0:
        return 1.0
    entities_per_call = stats["total_entities"] / stats["total_calls"]
    novelty_rate = stats["total_novel"] / max(stats["total_entities"], 1.0)
    return entities_per_call * novelty_rate


def rank_strategies(
    strategies: list[str],
    entity_type: str,
    yield_stats: dict[str, dict[str, float]],
) -> list[str]:
    """Order the YAML-declared strategies for one entity type by yield score."""
    return sorted(
        strategies,
        key=lambda s: score_strategy(s, entity_type, yield_stats),
        reverse=True,
    )


async def plan_gaps(
    session: AsyncSession,
    config: DomainConfig,
    domain: str,
    entity_types: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, str]]:
    """Pick open gaps and assign the best-ranked strategy per entity type.

    Returns task descriptors: {gap_id, grid_cell, entity_type, strategy}.
    """
    stmt = (
        select(GapQueue)
        .where(GapQueue.status == "open", GapQueue.domain == domain)
        .order_by(GapQueue.severity.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    gaps = list(result.scalars().all())
    if not gaps:
        logger.info("planner.no_gaps", domain=domain)
        return []

    yield_stats = await load_yield_stats(session)
    config_by_type = {et.name: et for et in config.entity_types or []}

    tasks: list[dict[str, str]] = []
    for gap in gaps:
        if entity_types and gap.entity_type not in entity_types:
            continue

        # "unknown" anomaly gaps probe broadly: use a landmark-based baseline.
        effective_type = "landmark" if gap.entity_type == "unknown" else gap.entity_type
        et_config = config_by_type.get(effective_type)
        declared = [s for s in (et_config.strategies if et_config else []) if s in STRATEGY_ENGINES]
        if not declared:
            declared = ["osm_api", "web_crawls"]

        ranked = rank_strategies(declared, effective_type, yield_stats)
        best_strategy = ranked[0]

        task = {
            "gap_id": str(gap.id),
            "grid_cell": gap.grid_cell,
            "entity_type": effective_type,
            "strategy": best_strategy,
        }
        tasks.append(task)
        gap.status = "in_progress"

    await session.flush()
    logger.info("planner.tasks_created", count=len(tasks), domain=domain)
    return tasks
