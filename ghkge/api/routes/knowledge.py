from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.consolidation.sync import generate_embedding
from ghkge.database.connection import get_session
from ghkge.database.models import (
    Entity,
    ExtractedFact,
    Feedback,
    NarrativeChunk,
    RawCapture,
)
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
from ghkge.utils.geohash import decode, haversine_m

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


def _cell_center(grid_cell: str) -> tuple[float, float]:
    """Center coordinate of a geohash cell (None-safe)."""
    try:
        return decode(grid_cell)
    except ValueError:
        return (0.0, 0.0)


@router.get("/entities/search", response_model=EntitySearchResponse)
async def search_entities(
    q: str | None = Query(None, description="Keyword or semantic query"),
    type: str | None = Query(None, description="Entity type filter"),
    near: str | None = Query(None, description="Comma-separated lat,lng"),
    radius_m: int | None = Query(None, description="Search radius in meters"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> EntitySearchResponse:
    """Hybrid search: structured filters + pgvector semantic ranking.

    Falls back to name/alias substring matching when embeddings are unavailable.
    """
    stmt = select(Entity).where(Entity.status != "archived")
    if type:
        stmt = stmt.where(Entity.entity_type == type)
    if near and radius_m:
        try:
            lat_str, lng_str = near.split(",")
            origin_lat, origin_lng = float(lat_str), float(lng_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="near must be 'lat,lng'") from exc
        # Grid cells are ~1.2km; widen the scan then filter precisely by distance.
        candidates = (
            (await session.execute(stmt.limit(500))).scalars().all()
        )
        filtered = [
            e
            for e in candidates
            if haversine_m(origin_lat, origin_lng, *_cell_center(e.grid_cell)) <= radius_m
        ]
        entities = filtered[:limit]
    else:
        entities = list((await session.execute(stmt.limit(500))).scalars().all())

    # Semantic ranking via pgvector when a query and embeddings are available.
    semantic: dict[uuid.UUID, float] = {}
    if q:
        vector = await generate_embedding(q)
        if vector:
            try:
                distance = NarrativeChunk.embedding.cosine_distance(vector)
                rows = (
                    await session.execute(
                        select(NarrativeChunk.entity_id, distance)
                        .where(
                            NarrativeChunk.entity_id.is_not(None),
                            NarrativeChunk.embedding.is_not(None),
                        )
                        .order_by(distance)
                        .limit(200)
                    )
                ).all()
                for entity_id, distance in rows:
                    if entity_id is not None:
                        semantic[entity_id] = max(0.0, 1.0 - float(distance))
            except Exception:
                logger.warning("knowledge.semantic_search_unavailable", exc_info=True)
                semantic = {}

    q_lower = q.lower() if q else ""
    results: list[EntitySearchResult] = []
    for entity in entities:
        relevance = semantic.get(entity.id, 0.0)
        if q_lower:
            if q_lower in entity.canonical_name.lower():
                relevance = max(relevance, 0.9)
            elif any(q_lower in alias.lower() for alias in (entity.aliases or [])):
                relevance = max(relevance, 0.8)
            elif q_lower in (entity.canonical_name or "").lower():
                relevance = max(relevance, 0.7)
        if not q and relevance == 0.0:
            relevance = 0.5

        snippet = ""
        fact_row = await session.execute(
            select(ExtractedFact.contextual_insight)
            .where(
                ExtractedFact.canonical_entity_id == entity.id,
                ExtractedFact.resolution_status == "approved",
            )
            .order_by(ExtractedFact.confidence_score.desc())
            .limit(1)
        )
        best = fact_row.scalar_one_or_none()
        if best:
            snippet = best[:280]

        results.append(
            EntitySearchResult(
                entity_id=entity.id,
                canonical_name=entity.canonical_name,
                relevance_score=round(float(relevance), 4),
                snippet=snippet,
            )
        )

    results.sort(key=lambda r: r.relevance_score, reverse=True)
    return EntitySearchResponse(results=results[:limit])


@router.get("/entities/{entity_id}", response_model=EntityDetail)
async def get_entity(
    entity_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EntityDetail:
    """Full entity detail with approved facts and provenance."""
    entity = await session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    rows = (
        await session.execute(
            select(ExtractedFact, RawCapture.source_url)
            .join(RawCapture, ExtractedFact.raw_capture_id == RawCapture.id)
            .where(
                ExtractedFact.canonical_entity_id == entity_id,
                ExtractedFact.resolution_status == "approved",
            )
            .order_by(ExtractedFact.confidence_score.desc())
        )
    ).all()

    lat, lng = _cell_center(entity.grid_cell)
    return EntityDetail(
        id=entity.id,
        canonical_name=entity.canonical_name,
        entity_type=entity.entity_type,
        aliases=entity.aliases or [],
        location=EntityLocation(lat=lat, lng=lng, grid_cell=entity.grid_cell),
        best_tier=entity.best_tier,
        corroboration_count=entity.corroboration_count,
        facts=[
            EntityFact(
                insight=fact.contextual_insight,
                confidence=fact.confidence_score,
                source_tier=fact.source_tier,
                is_safety_relevant=fact.is_safety_relevant,
                source_url=source_url,
            )
            for fact, source_url in rows
        ],
    )


# Relationship types the API will traverse. Anything else is rejected before
# it can reach a Cypher string (D-01: the parameter is interpolated).
ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {"NEAR", "ACCESSIBLE_VIA", "HAS_AMENITY", "PART_OF", "SUBJECT_TO_RULE"}
)


@router.get("/entities/{entity_id}/nearby", response_model=NearbyResponse)
async def get_nearby(
    entity_id: uuid.UUID,
    relation: str = Query("NEAR", description="Relationship type to traverse"),
    depth: int = Query(1, ge=1, le=3),
    session: AsyncSession = Depends(get_session),
) -> NearbyResponse:
    """Graph traversal from Neo4j; falls back to same-grid-cell neighbours."""
    if relation not in ALLOWED_RELATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown relation '{relation}'. Allowed: {sorted(ALLOWED_RELATIONS)}",
        )

    entity = await session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    try:
        from ghkge.consolidation.sync import _get_neo4j_driver

        driver = _get_neo4j_driver()
        async with driver.session() as neo_session:
            # relation is allow-listed above and depth is an int bounded by Query;
            # both are safe to interpolate. Entity ids remain bound parameters.
            result = await neo_session.run(
                f"""
                MATCH (src:Entity {{id: $id}})-[:{relation}*1..{depth}]-(n:Entity)
                RETURN DISTINCT n.id AS id, n.canonical_name AS name,
                       n.entity_type AS type
                LIMIT 100
                """,
                id=str(entity_id),
            )
            graph_rows = [record async for record in result]
        if graph_rows:
            nodes = [
                GraphNode(
                    id=uuid.UUID(str(r["id"])),
                    canonical_name=str(r["name"]),
                    entity_type=str(r["type"]),
                )
                for r in graph_rows
            ]
            return NearbyResponse(
                nodes=[GraphNode(id=entity.id, canonical_name=entity.canonical_name,
                                 entity_type=entity.entity_type)]
                + nodes,
                relationships=[
                    GraphRelationship(source=entity.id, target=n.id, type=relation)
                    for n in nodes
                ],
            )
    except Exception:
        logger.warning("knowledge.neo4j_traversal_unavailable", exc_info=True)

    neighbours = (
        (
            await session.execute(
                select(Entity)
                .where(
                    Entity.grid_cell == entity.grid_cell,
                    Entity.id != entity_id,
                    Entity.status != "archived",
                )
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    return NearbyResponse(
        nodes=[
            GraphNode(id=entity.id, canonical_name=entity.canonical_name,
                      entity_type=entity.entity_type)
        ]
        + [
            GraphNode(id=e.id, canonical_name=e.canonical_name, entity_type=e.entity_type)
            for e in neighbours
        ],
        relationships=[
            GraphRelationship(source=entity.id, target=e.id, type=relation) for e in neighbours
        ],
    )


@router.get("/entities/{entity_id}/rules", response_model=RulesResponse)
async def get_rules(
    entity_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RulesResponse:
    """Approved safety-relevant rules for an entity."""
    entity = await session.get(Entity, entity_id)
    facts = (
        (
            await session.execute(
                select(ExtractedFact)
                .where(
                    ExtractedFact.canonical_entity_id == entity_id,
                    ExtractedFact.is_safety_relevant == True,  # noqa: E712
                    ExtractedFact.resolution_status == "approved",
                )
                .order_by(ExtractedFact.confidence_score.desc())
            )
        )
        .scalars()
        .all()
    )
    corroboration = int(entity.corroboration_count) if entity is not None else 1
    return RulesResponse(
        rules=[
            RuleItem(
                insight=f.contextual_insight,
                source_tier=f.source_tier,
                corroboration_count=corroboration,
                last_verified=f.extracted_at,
            )
            for f in facts
        ]
    )


@router.post("/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    request: FeedbackRequest,
    session: AsyncSession = Depends(get_session),
) -> FeedbackResponse:
    """Persist a user correction for review."""
    if await session.get(Entity, request.entity_id) is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    row = Feedback(
        entity_id=request.entity_id,
        correction_text=request.correction_text,
        submitted_by_session=request.submitted_by_session,
        status="queued_for_review",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    logger.info("feedback.received", entity_id=str(request.entity_id), feedback_id=str(row.id))
    return FeedbackResponse(feedback_id=row.id, status=row.status)
