from __future__ import annotations

import json
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, TIMESTAMP, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from ghkge.config.settings import settings
from ghkge.database.connection import Base


class EmbeddingVector(TypeDecorator):
    """pgvector embedding column that degrades to JSON text on non-PG dialects.

    Keeps the real ``VECTOR(n)`` type on Postgres while letting the test suite
    (and any SQLite-based tooling) store and read embeddings as JSON.
    """

    impl = Vector
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(Vector(settings.embedding_dim))

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite" and isinstance(value, list):
            return json.dumps(value)
        return value

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite" and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return value


# Dialect variants keep the models portable: the same ORM classes run on
# Postgres in production and on SQLite in the test suite.
StringArray = ARRAY(Text).with_variant(JSON, "sqlite")
Vector768 = EmbeddingVector()


class AcquisitionRun(Base):
    __tablename__ = "acquisition_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduled")
    last_canonical_id_processed: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    facts_extracted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entities_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        Index("idx_acquisition_runs_domain", "domain"),
    )


class RawCapture(Base):
    __tablename__ = "raw_captures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_blob_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_used: Mapped[str] = mapped_column(String(100), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("acquisition_runs.id", ondelete="CASCADE"))

    __table_args__ = (
        Index("idx_raw_captures_hash", "content_hash"),
        Index("idx_raw_captures_domain", "domain"),
    )


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    grid_cell: Mapped[str] = mapped_column(String(50), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(StringArray, default=list)
    best_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    corroboration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_verified: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    neo4j_node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    __table_args__ = (
        Index("idx_entities_type_cell", "entity_type", "grid_cell"),
        Index("idx_entities_canonical_name", "canonical_name"),
    )


class ExtractedFact(Base):
    __tablename__ = "extracted_facts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_capture_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_captures.id", ondelete="CASCADE"))
    canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True)
    entity_name_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_category: Mapped[str] = mapped_column(String(100), nullable=False)
    contextual_insight: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    source_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    is_safety_relevant: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_macro_knowledge: Mapped[bool] = mapped_column(nullable=False, default=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    resolution_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    __table_args__ = (
        Index("idx_extracted_facts_entity", "canonical_entity_id"),
        Index("idx_extracted_facts_safety", "is_safety_relevant", "resolution_status"),
    )


class StrategyYieldLog(Base):
    __tablename__ = "strategy_yield_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("acquisition_runs.id", ondelete="CASCADE"))
    calls_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entities_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novel_entities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_estimate_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    logged_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class GapQueue(Base):
    __tablename__ = "gap_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grid_cell: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_gap_queue_status_severity", "status", "severity"),
    )


class DomainRateLimitState(Base):
    __tablename__ = "domain_rate_limit_state"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_hit_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    tokens_remaining: Mapped[float] = mapped_column(Float, nullable=False)


class NarrativeChunk(Base):
    __tablename__ = "narrative_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=True)
    raw_capture_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("raw_captures.id", ondelete="CASCADE"), nullable=True)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector768, nullable=True)
    source_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class TaskQueue(Base):
    """Postgres-backed task event bus (doc 12 envelope)."""

    __tablename__ = "task_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    source_agent: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    target_agent: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("acquisition_runs.id", ondelete="CASCADE"), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_task_queue_claim", "status", "task_type", "created_at"),
        Index("idx_task_queue_run", "run_id"),
    )


class Feedback(Base):
    """User correction flywheel intake."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    correction_text: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by_session: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="queued_for_review")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_feedback_entity", "entity_id"),
        Index("idx_feedback_status", "status"),
    )
