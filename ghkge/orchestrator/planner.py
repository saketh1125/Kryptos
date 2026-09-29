"""Strategy Planner — Stage 1 rule-based, yield-aware.

Reads the open gap queue and ranks each entity type's YAML-declared
strategies using the doc 08 §3 scoring formula:

    score = historical_yield x tier_weight x recency_decay - normalized_cost

Strategies are never hardcoded here: the vocabulary and the seed sources come
from the domain YAML.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.models import GapQueue, StrategyYieldLog
from ghkge.models.schemas import DomainConfig, SourceTier
from ghkge.orchestrator.domain import STRATEGY_ENGINES

logger = structlog.get_logger()

# A strategy untouched for this long starts losing score.
DECAY_GRACE_DAYS = 30.0
# Relative LLM cost per harvested capture, by the tier of source it usually yields.
# Officially-sourced portals are structured and cheap to synthesize; social and
# general web crawls yield messier text that costs more per fact.
TIER_WEIGHTS: dict[int, float] = {
    int(SourceTier.OFFICIAL): 1.0,
    int(SourceTier.CURATED): 0.9,
    int(SourceTier.SOCIAL_VERIFIED): 0.7,
    int(SourceTier.SOCIAL_GENERAL): 0.5,
}
NORMALIZED_COST: dict[str, float] = {
    "osm_api": 0.0,
    "api_query": 0.05,
    "official_portals": 0.10,
    "specialized_wikis": 0.15,
    "business_directories": 0.20,
    "news_feeds": 0.25,
    "web_crawls": 0.30,
    "social_media_crawls": 0.40,
    "media_transcripts": 0.60,
    "doc_parse": 0.50,
}
# Score assigned to a strategy with no history yet, so exploration still happens.
EXPLORATION_SCORE = 1.0


@dataclass(frozen=True)
class StrategyStats:
    """Aggregated yield history for one strategy/entity-type pair."""

    entities: float = 0.0
    calls: float = 0.0
    novel: float = 0.0
    avg_tier: float = float(SourceTier.SOCIAL_GENERAL)
    last_used: datetime | None = None

    @property
    def entities_per_call(self) -> float:
        return self.entities / self.calls if self.calls > 0 else 0.0

    @property
    def novelty_rate(self) -> float:
        return self.novel / self.entities if self.entities > 0 else 0.0


def _tier_weight(tier: float) -> float:
    """Weight of a source tier; unknown tiers score conservatively."""
    nearest = min(TIER_WEIGHTS, key=lambda t: abs(t - tier))
    return TIER_WEIGHTS[nearest]


def recency_decay(last_used: datetime | None, now: datetime | None = None) -> float:
    """e^(-lambda t) after a 30-day grace period; 1.0 while inside the grace."""
    if last_used is None:
        return 1.0
    now = now or datetime.now(UTC)
    age_days = (now - last_used).total_seconds() / 86400.0
    if age_days <= DECAY_GRACE_DAYS:
        return 1.0
    return math.exp(-(age_days - DECAY_GRACE_DAYS) / DECAY_GRACE_DAYS)


def strategy_score(
    strategy: str, stats: StrategyStats | None, now: datetime | None = None
) -> float:
    """Doc 08 §3 score for one strategy. Higher is better."""
    if stats is None or stats.calls <= 0:
        return EXPLORATION_SCORE
    score = (
        stats.entities_per_call
        * _tier_weight(stats.avg_tier)
        * recency_decay(stats.last_used, now)
    )
    return score - NORMALIZED_COST.get(strategy, 0.25)


async def load_strategy_stats(session: AsyncSession) -> dict[str, StrategyStats]:
    """Aggregate yield history keyed by 'strategy:entity_type'."""
    stmt = select(StrategyYieldLog).order_by(StrategyYieldLog.logged_at.desc()).limit(1000)
    logs = (await session.execute(stmt)).scalars().all()

    totals: dict[str, dict[str, float]] = {}
    newest: dict[str, datetime] = {}
    for row in logs:
        key = f"{row.strategy_name}:{row.entity_type}"
        entry = totals.setdefault(
            key, {"entities": 0.0, "calls": 0.0, "novel": 0.0, "tier_sum": 0.0, "n": 0.0}
        )
        entry["entities"] += float(row.entities_found)
        entry["calls"] += float(row.calls_made)
        entry["novel"] += float(row.novel_entities)
        entry["tier_sum"] += float(row.avg_source_tier)
        entry["n"] += 1.0
        if key not in newest or row.logged_at > newest[key]:
            newest[key] = row.logged_at

    return {
        key: StrategyStats(
            entities=e["entities"],
            calls=e["calls"],
            novel=e["novel"],
            avg_tier=e["tier_sum"] / e["n"] if e["n"] else float(SourceTier.SOCIAL_GENERAL),
            last_used=newest.get(key),
        )
        for key, e in totals.items()
    }


def rank_strategies(
    strategies: list[str],
    entity_type: str,
    stats: dict[str, StrategyStats],
    now: datetime | None = None,
) -> list[str]:
    """Order an entity type's strategies by doc 08 §3 score, best first."""
    return sorted(
        strategies,
        key=lambda s: strategy_score(s, stats.get(f"{s}:{entity_type}"), now),
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

    stats = await load_strategy_stats(session)
    config_by_type = {et.name: et for et in config.entity_types or []}

    tasks: list[dict[str, str]] = []
    for gap in gaps:
        if entity_types and gap.entity_type not in entity_types:
            continue

        # "unknown" anomaly gaps probe broadly: use a landmark-based baseline.
        effective_type = "landmark" if gap.entity_type == "unknown" else gap.entity_type
        et_config = config_by_type.get(effective_type)
        declared = [
            s for s in (et_config.strategies if et_config else []) if s in STRATEGY_ENGINES
        ]
        if not declared:
            declared = ["osm_api", "web_crawls"]

        best_strategy = rank_strategies(declared, effective_type, stats)[0]

        tasks.append(
            {
                "gap_id": str(gap.id),
                "grid_cell": gap.grid_cell,
                "entity_type": effective_type,
                "strategy": best_strategy,
            }
        )
        gap.status = "in_progress"

    await session.flush()
    logger.info("planner.tasks_created", count=len(tasks), domain=domain)
    return tasks
