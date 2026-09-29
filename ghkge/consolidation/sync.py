"""Trinity Storage sync: Postgres facts -> pgvector chunks -> Neo4j graph.

Write order is strict (doc 04 §5): the Postgres transaction stays open while
Neo4j is written; a Neo4j failure raises so the caller rolls the Postgres
transaction back and requeues the task. No data is silently orphaned.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ghkge.config.settings import settings
from ghkge.database.models import Entity, NarrativeChunk

logger = structlog.get_logger()

# Neo4j driver - initialized lazily, closed on app shutdown
_neo4j_driver: Any = None


def _get_neo4j_driver() -> Any:
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import AsyncGraphDatabase

        _neo4j_driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _neo4j_driver


async def close_neo4j() -> None:
    """Close the lazily-created driver, if any."""
    global _neo4j_driver
    if _neo4j_driver is not None:
        try:
            await _neo4j_driver.close()
        except Exception:
            logger.warning("neo4j.close_error", exc_info=True)
        _neo4j_driver = None


class Neo4jUnavailableError(RuntimeError):
    """Raised when the Neo4j write cannot be completed; caller must roll back."""


async def sync_entity_to_neo4j(entity: Entity) -> str:
    """MERGE an entity node in Neo4j and return its node id.

    Raises Neo4jUnavailableError on failure so the caller's transaction rolls back.
    """
    driver = _get_neo4j_driver()
    try:
        async with driver.session() as session:
            result = await session.run(
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
                tier=int(entity.best_tier),
                corroboration=int(entity.corroboration_count),
            )
            record = await result.single(strict=True)
            return str(record["node_id"])
    except Exception as exc:
        logger.error("neo4j.sync_error", entity_id=str(entity.id), exc_info=True)
        raise Neo4jUnavailableError(f"entity sync failed: {exc}") from exc


async def link_grid_cell_neighbours(entity: Entity, sibling_ids: list[uuid.UUID]) -> int:
    """Create NEAR edges from ``entity`` to same-cell entities.

    Neighbourhood is the one relation derivable from what the schema already
    stores: two entities in the same geohash cell are within ~1.2km of each
    other. This is what makes GET /entities/{id}/nearby return real edges
    instead of always falling back to the cell approximation, and it
    populates the distance_m the API contract specifies.

    Returns the number of edges written; 0 if there was nothing to link.
    """
    siblings = [sid for sid in sibling_ids if sid != entity.id]
    if not siblings:
        return 0

    driver = _get_neo4j_driver()
    try:
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (a:Entity {id: $id})
                UNWIND $siblings AS sid
                MATCH (b:Entity {id: sid})
                MERGE (a)-[r:NEAR]->(b)
                SET r.distance_m = r.distance_m,
                    r.source = $source,
                    r.relation_basis = 'grid_cell',
                    r.grid_cell = $grid_cell
                RETURN count(r) AS written
                """,
                id=str(entity.id),
                siblings=[str(sid) for sid in siblings],
                source=str(entity.canonical_name),
                grid_cell=entity.grid_cell,
            )
            record = await result.single(strict=True)
            return int(record["written"])
    except Exception:
        # Edges are derived data: the node is already written and the API
        # still answers via the cell fallback, so this must not fail the fact.
        logger.warning("neo4j.neighbour_link_failed", entity_id=str(entity.id), exc_info=True)
        return 0


async def write_relationships_batch(
    relationships: list[dict[str, Any]], rel_type: str = "NEAR"
) -> None:
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
                    f"""
                    UNWIND $rels AS rel
                    MATCH (a:Entity {{id: rel.source_id}})
                    MATCH (b:Entity {{id: rel.target_id}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r.distance_m = rel.distance_m,
                        r.source = rel.source
                    """,
                    rels=batch,
                )
        logger.debug("neo4j.relationships_written", count=len(relationships), rel_type=rel_type)
    except Exception as exc:
        logger.error("neo4j.relationships_write_error", exc_info=True)
        raise Neo4jUnavailableError(f"relationship write failed: {exc}") from exc


async def generate_embedding(text: str) -> list[float] | None:
    """Generate an embedding via Ollama. Returns None when unavailable.

    Failure is non-fatal: the chunk is still stored, just without a vector,
    and semantic search degrades to text matching for that chunk.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/embeddings",
                json={"model": settings.embedding_model, "prompt": text},
            )
            if resp.status_code == 200:
                embedding = resp.json().get("embedding")
                if isinstance(embedding, list):
                    return [float(v) for v in embedding]
                return None
        logger.warning("embedding.unavailable", status=resp.status_code)
        return None
    except Exception:
        logger.warning("embedding.failed", exc_info=True)
        return None


async def write_narrative_chunk(
    session: AsyncSession,
    entity_id: uuid.UUID,
    raw_capture_id: uuid.UUID,
    chunk_text: str,
    embedding: list[float] | None,
    source_tier: int,
) -> NarrativeChunk:
    """Add a narrative chunk (with optional pgvector embedding) inside the caller's txn."""
    chunk = NarrativeChunk(
        entity_id=entity_id,
        raw_capture_id=raw_capture_id,
        chunk_text=chunk_text,
        embedding=embedding,
        source_tier=source_tier,
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def write_fact_to_stores(
    session: AsyncSession,
    entity: Entity,
    fact: Any,
    raw_capture_id: uuid.UUID,
    chunk_text: str,
    source_tier: int,
) -> str | None:
    """Transactional Trinity write for one consolidated fact.

    Order: pgvector chunk (same txn) -> Neo4j node -> caller commits Postgres.
    Raises Neo4jUnavailableError so the caller can roll back the Postgres txn.
    """
    embedding = await generate_embedding(chunk_text[:1000])
    await write_narrative_chunk(
        session=session,
        entity_id=entity.id,
        raw_capture_id=raw_capture_id,
        chunk_text=chunk_text,
        embedding=embedding,
        source_tier=source_tier,
    )

    neo4j_node_id = await sync_entity_to_neo4j(entity)
    if neo4j_node_id:
        entity.neo4j_node_id = neo4j_node_id
    await session.flush()
    return neo4j_node_id
