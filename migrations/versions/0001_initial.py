"""initial schema

Mirrors sql/schema.sql, which stays as the human-readable DDL reference. This
migration is the executable artifact: `alembic upgrade head` brings a database
to the current state, and `alembic revision --autogenerate` produces the
incremental changes from here on.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector must exist before narrative_chunks. Neither extension is
    # bundled with the managed Postgres image, so both are best-effort.
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "acquisition_runs",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status", sa.String(50), nullable=False, server_default="running"),
        sa.Column("trigger", sa.String(50), nullable=False, server_default="scheduled"),
        sa.Column("last_canonical_id_processed", sa.UUID()),
        sa.Column("facts_extracted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entities_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_index("idx_acquisition_runs_domain", "acquisition_runs", ["domain"])

    op.create_table(
        "raw_captures",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("raw_content", sa.Text()),
        sa.Column("raw_blob_url", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("strategy_used", sa.String(100), nullable=False),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("acquisition_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("idx_raw_captures_hash", "raw_captures", ["content_hash"])
    op.create_index("idx_raw_captures_domain", "raw_captures", ["domain"])

    op.create_table(
        "entities",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("canonical_name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("grid_cell", sa.String(50), nullable=False),
        sa.Column("aliases", sa.ARRAY(sa.Text()), server_default="{}"),
        sa.Column("best_tier", sa.Integer(), nullable=False),
        sa.Column("corroboration_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_verified", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("neo4j_node_id", sa.String(255)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.create_index("idx_entities_type_cell", "entities", ["entity_type", "grid_cell"])
    op.create_index("idx_entities_canonical_name", "entities", ["canonical_name"])

    op.create_table(
        "extracted_facts",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "raw_capture_id",
            sa.UUID(),
            sa.ForeignKey("raw_captures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "canonical_entity_id",
            sa.UUID(),
            sa.ForeignKey("entities.id", ondelete="SET NULL"),
        ),
        sa.Column("entity_name_raw", sa.String(255), nullable=False),
        sa.Column("entity_category", sa.String(100), nullable=False),
        sa.Column("contextual_insight", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("source_tier", sa.Integer(), nullable=False),
        sa.Column("is_safety_relevant", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("is_macro_knowledge", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("extracted_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("resolution_status", sa.String(50), nullable=False, server_default="pending"),
    )
    op.create_index("idx_extracted_facts_entity", "extracted_facts", ["canonical_entity_id"])
    op.create_index(
        "idx_extracted_facts_safety",
        "extracted_facts",
        ["is_safety_relevant", "resolution_status"],
    )

    op.create_table(
        "strategy_yield_log",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("acquisition_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("calls_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entities_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("novel_entities", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_confidence", sa.Float()),
        sa.Column("avg_source_tier", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("cost_estimate_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("logged_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "gap_queue",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("grid_cell", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("severity", sa.Float(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="open"),
        sa.Column("domain", sa.String(255)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_gap_queue_status_severity",
        "gap_queue",
        ["status", sa.text("severity DESC")],
    )

    op.create_table(
        "domain_rate_limit_state",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("last_hit_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("tokens_remaining", sa.Float(), nullable=False),
    )

    op.create_table(
        "narrative_chunks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "entity_id", sa.UUID(), sa.ForeignKey("entities.id", ondelete="CASCADE")
        ),
        sa.Column(
            "raw_capture_id", sa.UUID(), sa.ForeignKey("raw_captures.id", ondelete="CASCADE")
        ),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768)),
        sa.Column("source_tier", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    # IVFFlat wants a substantial table before it beats a sequential scan, so
    # it is created here but will only be chosen by the planner on enough rows.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_narrative_embedding ON narrative_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        "task_queue",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("source_agent", sa.String(50), nullable=False, server_default=""),
        sa.Column("target_agent", sa.String(50), nullable=False, server_default=""),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", sa.dialects.postgresql.JSONB()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("acquisition_runs.id", ondelete="CASCADE"),
        ),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "idx_task_queue_claim", "task_queue", ["status", "task_type", "created_at"]
    )
    op.create_index("idx_task_queue_run", "task_queue", ["run_id"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "entity_id",
            sa.UUID(),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("correction_text", sa.Text(), nullable=False),
        sa.Column("submitted_by_session", sa.String(255)),
        sa.Column("status", sa.String(50), nullable=False, server_default="queued_for_review"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_feedback_entity", "feedback", ["entity_id"])
    op.create_index("idx_feedback_status", "feedback", ["status"])


def downgrade() -> None:
    # Raw captures, facts and entities are the audit trail; dropping them is
    # not reversible by re-running this migration, so it is explicit here and
    # nothing invokes it automatically.
    for table in (
        "feedback",
        "task_queue",
        "narrative_chunks",
        "domain_rate_limit_state",
        "gap_queue",
        "strategy_yield_log",
        "extracted_facts",
        "entities",
        "raw_captures",
        "acquisition_runs",
    ):
        op.drop_table(table)
