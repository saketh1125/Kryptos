from __future__ import annotations

import json
import uuid

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.config.settings import settings
from ghkge.database.models import Entity, ExtractedFact, NarrativeChunk

logger = structlog.get_logger()

# Neo4j driver - initialized lazily
_neo4j_driver = None


def _get_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import AsyncGraphDatabase

        _neo4j_driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _neo4j_driver


async def sync_entity_to_neo4j(entity: Entity) -> str | None:
    """Write or update an entity node in Neo4j. Returns the Neo4j node ID."""
    driver = _get_neo4j_driver()
    try:
        async with driver.session() as session:
            await session.run(
                """
                MERGE (e:Entity {id: $id})
                SET e.canonical_name = $name,
                    e.entity_type = $type,
                    e.grid_cell = $grid_cell,
                    e.best_tier = $tier,
                    e.corroboration_count = $corroboration
                RETURN id(e) AS node_id
                """,
                id=str(entity.id),
                name=entity.canonical_name,
                type=entity.entity_type,
                grid_cell=entity.grid_cell,
                tier=entity.best_tier,
                corroboration=entity.corroboration_count,
            )
            record = await session.run(
                """
                MATCH (e:Entity {id: $id})
                RETURN id(e) AS node_id
                """,
                id=str(entity.id),
            )
            async for r in record:
                return str(r["node_id"])
        return None
    except Exception:
        logger.error("neo4j.sync_error", entity_id=str(entity.id), exc_info=True)
        return None


async def write_relationships_batch(relationships: list[dict]) -> None:
    """Write relationships to Neo4j in batches of 50 using UNWIND."""
    if not relationships:
        return

    driver = _get_neo4j_driver()
    batch_size = 50

    try:
        async with driver.session() as session:
            for i in range(0, len(relationships), batch_size):
                batch = relationships[i : i + batch_size]
                await session.run(
                    """
                    UNWIND $rels AS rel
                    MATCH (a:Entity {id: rel.source_id})
                    MATCH (b:Entity {id: rel.target_id})
                    MERGE (a)-[r:NEAR]->(b)
                    SET r.distance_m = rel.distance_m,
                        r.source = rel.source
                    """,
                    rels=batch,
                )
        logger.debug("neo4j.relationships_written", count=len(relationships))
    except Exception:
        logger.error("neo4j.relationships_write_error", exc_info=True)


async def write_narrative_chunk(
    session: AsyncSession,
    entity_id: uuid.UUID,
    raw_capture_id: uuid.UUID,
    chunk_text: str,
    embedding: list[float] | None,
    source_tier: int,
) -> None:
    """Write a narrative chunk with optional embedding to pgvector."""
    embedding_str = None
    if embedding is not None:
        embedding_str = json.dumps(embedding)

    chunk = NarrativeChunk(
        entity_id=entity_id,
        raw_capture_id=raw_capture_id,
        chunk_text=chunk_text,
        embedding=embedding_str,
        source_tier=source_tier,
    )
    session.add(chunk)
    await session.flush()


async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding using Ollama. Returns None if Ollama is unavailable."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/embeddings",
                json={"model": settings.embedding_model, "prompt": text},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("embedding")
        return None
    except Exception:
        logger.warning("embedding generation failed", exc_info=True)
        return None


async def write_fact_to_stores(
    session: AsyncSession,
    fact: ExtractedFact,
    entity_id: uuid.UUID,
    raw_capture_id: uuid.UUID,
    chunk_text: str,
    source_tier: int,
) -> None:
    """
    Transactional sync: Postgres fact -> pgvector chunk -> Neo4j node.
    Order: Postgres first, then pgvector, then Neo4j.
    """
    # 1. Update fact with resolved entity
    fact.canonical_entity_id = entity_id
    fact.resolution_status = "approved"
    await session.flush()

    # 2. Generate and write embedding to pgvector
    embedding = await generate_embedding(chunk_text[:1000])  # Limit text length for embedding
    await write_narrative_chunk(
        session=session,
        entity_id=entity_id,
        raw_capture_id=raw_capture_id,
        chunk_text=chunk_text,
        embedding=embedding,
        source_tier=source_tier,
    )

    # 3. Sync entity to Neo4j (best-effort, non-blocking)
    stmt = select(Entity).where(Entity.id == entity_id)
    result = await session.execute(stmt)
    entity = result.scalar_one_or_none()
    if entity is not None:
        neo4j_id = await sync_entity_to_neo4j(entity)
        if neo4j_id:
            entity.neo4j_node_id = neo4j_id
            await session.flush()
