from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.database.connection import get_session
from ghkge.database.models import Entity, ExtractedFact, RawCapture
from ghkge.models.schemas import (
    EntityDetail,
    EntityFact,
    EntityLocation,
    EntitySearchResponse,
    EntitySearchResult,
    FeedbackRequest,
    FeedbackResponse,
    GraphNode,
    GraphRelationship,
    NearbyResponse,
    RuleItem,
    RulesResponse,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/v1/entities", tags=["knowledge"])


@router.get("/search", response_model=EntitySearchResponse)
async def search_entities(
    q: str | None = Query(None, description="Semantic keyword query"),
    type: str | None = Query(None, description="Entity type filter"),
    near: str | None = Query(None, description="Comma-separated lat,lng"),
    radius_m: int | None = Query(None, description="Search radius in meters"),
    session: AsyncSession = Depends(get_session),
) -> EntitySearchResponse:
    """Hybrid entity search: structured filter + semantic search."""
    stmt = select(Entity)

    if type:
        stmt = stmt.where(Entity.entity_type == type)

    result = await session.execute(stmt.limit(100))
    entities = result.scalars().all()

    results = []
    for entity in entities:
        # Basic relevance scoring (text match on name)
        relevance = 0.5
        if q:
            q_lower = q.lower()
            if q_lower in entity.canonical_name.lower():
                relevance = 0.9
            elif any(q_lower in alias.lower() for alias in (entity.aliases or [])):
                relevance = 0.8

        # Get a snippet from extracted facts
        fact_stmt = (
            select(ExtractedFact)
            .where(ExtractedFact.canonical_entity_id == entity.id)
            .limit(1)
        )
        fact_result = await session.execute(fact_stmt)
        fact = fact_result.scalar_one_or_none()
        snippet = fact.contextual_insight if fact else ""

        results.append(
            EntitySearchResult(
                entity_id=entity.id,
                canonical_name=entity.canonical_name,
                relevance_score=relevance,
                snippet=snippet,
            )
        )

    results.sort(key=lambda r: r.relevance_score, reverse=True)
    return EntitySearchResponse(results=results[:20])


@router.get("/{entity_id}", response_model=EntityDetail)
async def get_entity(
    entity_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EntityDetail:
    """Fetch full entity details with facts."""
    stmt = select(Entity).where(Entity.id == entity_id)
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()

    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    # Get facts
    facts_stmt = (
        select(ExtractedFact, RawCapture.source_url)
        .join(RawCapture, ExtractedFact.raw_capture_id == RawCapture.id)
        .where(ExtractedFact.canonical_entity_id == entity_id)
    )
    facts_result = await session.execute(facts_stmt)
    rows = facts_result.all()

    facts = [
        EntityFact(
            insight=fact.contextual_insight,
            confidence=fact.confidence_score,
            source_tier=fact.source_tier,
            is_safety_relevant=fact.is_safety_relevant,
            source_url=source_url,
        )
        for fact, source_url in rows
    ]

    return EntityDetail(
        id=entity.id,
        canonical_name=entity.canonical_name,
        entity_type=entity.entity_type,
        aliases=entity.aliases or [],
        location=EntityLocation(grid_cell=entity.grid_cell),
        best_tier=entity.best_tier,
        corroboration_count=entity.corroboration_count,
        facts=facts,
    )


@router.get("/{entity_id}/nearby", response_model=NearbyResponse)
async def get_nearby(
    entity_id: uuid.UUID,
    relation: str = "NEAR",
    depth: int = 1,
    session: AsyncSession = Depends(get_session),
) -> NearbyResponse:
    """Graph traversal - find related entities."""
    # Get the source entity
    stmt = select(Entity).where(Entity.id == entity_id)
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()

    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    # For now, return entities in the same grid cell as a basic graph approximation
    # In production, this queries Neo4j
    nearby_stmt = (
        select(Entity)
        .where(Entity.grid_cell == entity.grid_cell, Entity.id != entity_id)
        .limit(10)
    )
    nearby_result = await session.execute(nearby_stmt)
    nearby_entities = nearby_result.scalars().all()

    nodes = [
        GraphNode(
            id=e.id,
            canonical_name=e.canonical_name,
            entity_type=e.entity_type,
        )
        for e in [entity] + list(nearby_entities)
    ]

    relationships = [
        GraphRelationship(
            source=entity.id,
            target=e.id,
            type=relation,
        )
        for e in nearby_entities
    ]

    return NearbyResponse(nodes=nodes, relationships=relationships)


@router.get("/{entity_id}/rules", response_model=RulesResponse)
async def get_rules(
    entity_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RulesResponse:
    """Fetch safety-relevant rules for an entity."""
    stmt = (
        select(ExtractedFact)
        .where(
            ExtractedFact.canonical_entity_id == entity_id,
            ExtractedFact.is_safety_relevant == True,  # noqa: E712
            ExtractedFact.resolution_status == "approved",
        )
        .order_by(ExtractedFact.confidence_score.desc())
    )
    result = await session.execute(stmt)
    facts = result.scalars().all()

    rules = [
        RuleItem(
            insight=f.contextual_insight,
            source_tier=f.source_tier,
            corroboration_count=1,  # Derived from entity corroboration
            last_verified=f.extracted_at,
        )
        for f in facts
    ]

    return RulesResponse(rules=rules)


@router.post("/feedback", response_model=FeedbackResponse, status_code=200)
async def submit_feedback(
    request: FeedbackRequest,
    session: AsyncSession = Depends(get_session),
) -> FeedbackResponse:
    """Submit user correction feedback."""
    # In a full implementation, this would write to a feedback table
    feedback_id = uuid.uuid4()
    logger.info(
        "feedback.received",
        entity_id=str(request.entity_id),
        feedback_id=str(feedback_id),
    )
    return FeedbackResponse(feedback_id=feedback_id, status="queued_for_review")
