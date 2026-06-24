from __future__ import annotations

import structlog
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.models import GapQueue, StrategyYieldLog

logger = structlog.get_logger()


class EntityTypeConfig(BaseModel):
    name: str
    strategies: list[str] = Field(
        description="Ranked list of strategy names to query for this entity type"
    )
    safety_relevant: bool = False


class DomainPlan(BaseModel):
    domain: str
    geography_bbox: list[float]
    entity_types: list[EntityTypeConfig]
    coverage_target: float = 0.8
    staleness_cutoff_days: dict[str, int] = {}


def load_domain_config(path: str) -> DomainPlan:
    """Load domain configuration from YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return DomainPlan(**data)


async def plan_strategies(
    session: AsyncSession,
    domain: str,
    entity_types: list[str] | None = None,
) -> list[dict[str, str]]:
    """
    Plan which strategies to use based on gap queue and yield history.
    Stage 1: Rule-based planner (reads gap queue, assigns strategies).
    """
    # Get open gaps
    stmt = (
        select(GapQueue)
        .where(GapQueue.status == "open")
        .order_by(GapQueue.severity.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    gaps = result.scalars().all()

    if not gaps:
        logger.info("planner.no_gaps", domain=domain)
        return []

    # Get yield history for strategy scoring
    yield_stmt = select(StrategyYieldLog).order_by(StrategyYieldLog.logged_at.desc()).limit(100)
    yield_result = await session.execute(yield_stmt)
    yield_logs = yield_result.scalars().all()

    # Build yield stats per strategy
    yield_stats: dict[str, dict] = {}
    for log in yield_logs:
        key = f"{log.strategy_name}:{log.entity_type}"
        if key not in yield_stats:
            yield_stats[key] = {
                "total_entities": 0,
                "total_calls": 0,
                "total_novel": 0,
            }
        yield_stats[key]["total_entities"] += log.entities_found
        yield_stats[key]["total_calls"] += log.calls_made
        yield_stats[key]["total_novel"] += log.novel_entities

    # Default strategy mappings per entity type
    default_strategies: dict[str, list[str]] = {
        "landmark": ["osm_api", "web_crawls"],
        "local_business": ["web_crawls", "api_query"],
        "public_event": ["web_crawls", "api_query"],
        "transit_route": ["osm_api"],
        "regulatory_rule": ["web_crawls"],
    }

    tasks: list[dict[str, str]] = []
    for gap in gaps:
        if entity_types and gap.entity_type not in entity_types:
            continue

        strategies = default_strategies.get(gap.entity_type, ["web_crawls"])

        # Score strategies based on yield
        scored: list[tuple[str, float]] = []
        for strat in strategies:
            key = f"{strat}:{gap.entity_type}"
            stats = yield_stats.get(key)
            if stats and stats["total_calls"] > 0:
                entities_per_call = stats["total_entities"] / stats["total_calls"]
                novelty_rate = stats["total_novel"] / max(stats["total_entities"], 1)
                score = entities_per_call * novelty_rate
            else:
                score = 1.0  # Default score for untested strategies
            scored.append((strat, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        best_strategy = scored[0][0] if scored else "web_crawls"

        tasks.append({
            "strategy": best_strategy,
            "entity_type": gap.entity_type,
            "grid_cell": gap.grid_cell,
            "gap_id": str(gap.id),
        })

    logger.info("planner.tasks_created", count=len(tasks), domain=domain)
    return tasks
