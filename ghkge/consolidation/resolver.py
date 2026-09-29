from __future__ import annotations

from enum import Enum

import structlog
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.config.settings import settings
from ghkge.database.models import Entity

logger = structlog.get_logger()


class Resolution(Enum):
    OVERRIDE = "override"
    APPEND_AS_ALTERNATIVE = "append_as_alternative"
    APPEND_LOW_CONFIDENCE = "append_low_confidence"
    HOLD_FOR_REVIEW = "hold_for_review"


async def find_matching_entity(
    session: AsyncSession,
    entity_name: str,
    entity_type: str,
    grid_cell: str,
) -> Entity | None:
    """Search for a matching entity in the same grid cell using RapidFuzz."""
    stmt = select(Entity).where(
        Entity.entity_type == entity_type,
        Entity.grid_cell == grid_cell,
    )
    result = await session.execute(stmt)
    candidates = result.scalars().all()

    name_lower = entity_name.lower().strip()

    best_match: Entity | None = None
    best_score = 0.0

    for candidate in candidates:
        # Check canonical name
        score = fuzz.token_sort_ratio(name_lower, candidate.canonical_name.lower())
        if score > best_score:
            best_score = score
            best_match = candidate

        # Check aliases
        for alias in (candidate.aliases or []):
            score = fuzz.token_sort_ratio(name_lower, alias.lower())
            if score > best_score:
                best_score = score
                best_match = candidate

    if best_score >= settings.entity_match_threshold:
        logger.debug(
            "entity_resolution.match_found",
            incoming=entity_name,
            matched=best_match.canonical_name if best_match else None,
            score=best_score,
        )
        return best_match

    return None


async def upsert_entity(
    session: AsyncSession,
    entity_name: str,
    entity_type: str,
    grid_cell: str,
    source_tier: int,
    confidence: float,
) -> Entity:
    """
    Find or create a canonical entity and return it.

    On a match, appends the incoming name as an alias (when new), upgrades the
    tier if the incoming source is more trusted, and bumps corroboration.
    """
    existing = await find_matching_entity(session, entity_name, entity_type, grid_cell)

    if existing is not None:
        name_title = entity_name.strip().title()
        if name_title != existing.canonical_name and name_title not in (existing.aliases or []):
            existing.aliases = list(existing.aliases or []) + [name_title]

        if source_tier < existing.best_tier:
            existing.best_tier = source_tier

        existing.corroboration_count += 1
        await session.flush()
        logger.debug("entity_resolution.merged", entity_id=str(existing.id), name=entity_name)
        return existing

    new_entity = Entity(
        canonical_name=entity_name.strip().title(),
        entity_type=entity_type,
        grid_cell=grid_cell,
        best_tier=source_tier,
        corroboration_count=1,
    )
    session.add(new_entity)
    await session.flush()
    logger.debug("entity_resolution.created", entity_id=str(new_entity.id), name=entity_name)
    return new_entity


def resolve_conflict(
    existing_tier: int,
    existing_confidence: float,
    existing_corroboration: int,
    incoming_tier: int,
    incoming_confidence: float,
    is_safety_relevant: bool,
) -> Resolution:
    """
    Resolve conflicts between existing entity data and incoming fact.
    Implements the precedence hierarchy from the docs.
    """
    if incoming_tier < existing_tier:
        return Resolution.OVERRIDE

    if incoming_tier == existing_tier:
        if incoming_confidence > existing_confidence + 0.15:
            return Resolution.OVERRIDE
        return Resolution.APPEND_AS_ALTERNATIVE

    if is_safety_relevant and existing_corroboration < settings.safety_corroboration_min:
        return Resolution.HOLD_FOR_REVIEW

    return Resolution.APPEND_LOW_CONFIDENCE
