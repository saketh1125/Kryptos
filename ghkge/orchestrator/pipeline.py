from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from ghkge.compliance.engine import ComplianceEngine
from ghkge.compliance.rate_limiter import PostgresRateLimiter
from ghkge.config.settings import settings
from ghkge.database.connection import async_session_factory
from ghkge.database.models import (
    AcquisitionRun,
    GapQueue,
)
from ghkge.harvesters.api_client import APIHarvester
from ghkge.harvesters.document import DocumentHarvester
from ghkge.harvesters.media import MediaHarvester
from ghkge.harvesters.web import WebHarvester
from ghkge.orchestrator.planner import plan_strategies

logger = structlog.get_logger()


async def execute_run(
    run_id: str,
    domain: str,
    entity_types: list[str] | None = None,
) -> None:
    """Execute a full acquisition run: plan -> harvest -> extract -> consolidate."""
    run_uuid = uuid.UUID(run_id)

    async with async_session_factory() as session:
        # Update run status
        stmt = select(AcquisitionRun).where(AcquisitionRun.id == run_uuid)
        result = await session.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            logger.error("pipeline.run_not_found", run_id=run_id)
            return

        run.status = "running"
        run.started_at = datetime.now(UTC)
        await session.commit()

        try:
            # Initialize compliance engine
            try:
                import asyncpg

                db_url = settings.database_url.replace("+asyncpg", "")
                pg_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5)
                rate_limiter = PostgresRateLimiter(pg_pool)
            except Exception:
                logger.warning("pipeline.rate_limiter_init_failed", exc_info=True)
                rate_limiter = None

            compliance = ComplianceEngine(rate_limiter=rate_limiter)

            # Initialize harvesters
            harvesters = {
                "web_crawls": WebHarvester(compliance),
                "osm_api": APIHarvester(compliance),
                "api_query": APIHarvester(compliance),
                "doc_parse": DocumentHarvester(compliance),
                "media_transcripts": MediaHarvester(compliance),
            }

            # Plan strategies
            tasks = await plan_strategies(session, domain, entity_types)
            await session.commit()

            facts_extracted = 0
            entities_written = 0

            for task in tasks:
                strategy = task["strategy"]
                entity_type = task["entity_type"]

                harvester = harvesters.get(strategy)
                if harvester is None:
                    logger.warning("pipeline.unknown_strategy", strategy=strategy)
                    continue

                # For web crawls, we need a URL - in production this comes from
                # search APIs or gap metadata. For now, log the intent.
                logger.info(
                    "pipeline.executing_task",
                    strategy=strategy,
                    entity_type=entity_type,
                    grid_cell=task["grid_cell"],
                )

                # Mark gap as in progress
                gap_id = uuid.UUID(task["gap_id"])
                gap_stmt = select(GapQueue).where(GapQueue.id == gap_id)
                gap_result = await session.execute(gap_stmt)
                gap = gap_result.scalar_one_or_none()
                if gap:
                    gap.status = "in_progress"
                    await session.commit()

            # Update run completion
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            await session.commit()

            logger.info(
                "pipeline.run_complete",
                run_id=run_id,
                facts_extracted=facts_extracted,
                entities_written=entities_written,
            )

        except Exception as e:
            run.status = "failed"
            run.completed_at = datetime.now(UTC)
            await session.commit()
            logger.error("pipeline.run_failed", run_id=run_id, error=str(e), exc_info=True)

        finally:
            if rate_limiter is not None:
                with contextlib.suppress(Exception):
                    await pg_pool.close()  # type: ignore
