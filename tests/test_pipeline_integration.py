"""Pipeline integration test: full chain over a real in-memory SQLite DB.

Worker handlers, the bus, the planner, entity resolution and the safety gate
are all exercised end to end. Only the true externals are faked: the
compliance gate (approved), the LLM extractor, embeddings, and Neo4j.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import ghkge.database.connection as connection
from ghkge.database.models import (
    AcquisitionRun,
    Entity,
    ExtractedFact,
    GapQueue,
    NarrativeChunk,
    RawCapture,
    StrategyYieldLog,
    TaskQueue,
)
from ghkge.models.schemas import ExtractedFactSchema
from ghkge.orchestrator import bus, workers
from ghkge.orchestrator.pipeline import finalize_run_if_done, submit_run

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def _pgvector_dialect():
    return "sqlite"


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Real relational DB; Postgres UUID/JSONB compile to SQLite equivalents."""
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.ext.compiler import compiles

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):
        return "CHAR(36)"


    async with engine.begin() as conn:
        await conn.run_sync(connection.Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    connection.set_session_factory(factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def fakes(monkeypatch):
    """Stub the true externals; keep everything else real."""

    async def fake_extract(text: str, source_url: str = "") -> list[ExtractedFactSchema]:
        return [
            ExtractedFactSchema(
                entity_name="Darbhanga Ghat",
                entity_category="LOCATION",
                is_macro_knowledge=False,
                is_safety_relevant=False,
                contextual_insight="Flood-prone ghat; open 4am-midnight.",
                confidence_score=0.9,
            )
        ]

    monkeypatch.setattr(workers, "extract_facts_from_text", fake_extract)

    import ghkge.consolidation.sync as sync_mod

    async def fake_neo4j(entity) -> str:
        return "neo-node-1"

    async def fake_embed(text: str) -> list[float] | None:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(sync_mod, "generate_embedding", fake_embed)
    monkeypatch.setattr(sync_mod, "sync_entity_to_neo4j", fake_neo4j)

    # Compliance approves everything without network I/O.
    from ghkge.models.schemas import ApprovedTarget

    async def fake_compliance():
        class _Engine:
            async def approve(self, url: str, strategy: str, entity_type: str) -> ApprovedTarget:
                from urllib.parse import urlparse

                return ApprovedTarget(
                    url=url,
                    domain=urlparse(url).netloc,
                    strategy=strategy,
                    entity_type=entity_type,
                )

        return _Engine()

    monkeypatch.setattr(workers, "get_compliance", fake_compliance)

    class FakeHarvester:
        async def fetch(self, target, run_id):
            from ghkge.harvesters.base import BaseHarvester
            from ghkge.models.schemas import RawCaptureData

            content = "# Varanasi\nDarbhanga Ghat is open 4am to midnight."
            return RawCaptureData(
                source_url=target.url,
                source_type="html",
                domain="varanasi.nic.in",
                raw_content=content,
                content_hash=BaseHarvester.compute_content_hash(content),
                strategy_used=target.strategy,
                run_id=run_id,
            )

    class FakeAPIHarvester(FakeHarvester):
        async def query_overpass(self, query, run_id, entity_type=""):
            return await self.fetch(
                type("T", (), {"url": "https://overpass-api.de/api/interpreter",
                               "strategy": "osm_api"})(),
                run_id,
            )

    async def fake_harvester(engine: str):
        return FakeAPIHarvester() if engine == "api_overpass" else FakeHarvester()

    monkeypatch.setattr(workers, "get_harvester", fake_harvester)
    return None


async def _drain(task_type: str, limit: int = 50) -> int:
    """Process a task type until the queue is empty for it."""
    processed = 0
    for _ in range(limit):
        tasks = await bus.claim_tasks(task_type, limit=10)
        if not tasks:
            return processed
        for task in tasks:
            handler = workers.WORKER_HANDLERS[task_type]
            try:
                await bus.complete_task(task.id, await handler(task.payload))
            except Exception as exc:  # pragma: no cover - surfaced by assert
                await bus.fail_task(task.id, str(exc), retryable=False)
                raise
            processed += 1
    return processed


DOMAIN = "example_hyperlocal_domain"


async def _seed_gap(db, grid_cell: str = "u4pruyk", entity_type: str = "landmark") -> str:
    async with db() as session:
        gap = GapQueue(
            grid_cell=grid_cell,
            entity_type=entity_type,
            kind="missing",
            severity=3.0,
            status="open",
            domain=DOMAIN,
        )
        session.add(gap)
        await session.commit()
        return str(gap.id)


class TestPlannerWorker:
    async def test_plan_task_enqueues_harvest(self, db, fakes):
        await _seed_gap(db)
        await submit_run(DOMAIN, trigger="manual")

        # Drain the real plan task the worker loop would pick up.
        tasks = await bus.claim_tasks(bus.TASK_PLAN, limit=5)
        assert len(tasks) == 1
        result = await workers.WORKER_HANDLERS[bus.TASK_PLAN](tasks[0].payload)
        await bus.complete_task(tasks[0].id, result)

        assert result["planned"] == 1
        assert result["harvest_tasks"] >= 1

        async with db() as session:
            gap = (await session.execute(select(GapQueue))).scalar_one()
            assert gap.status == "in_progress"
            assert (await session.execute(select(AcquisitionRun))).scalar_one().status == "running"

    async def test_plan_with_no_gaps_is_noop(self, db, fakes):
        await submit_run(DOMAIN, trigger="manual")
        tasks = await bus.claim_tasks(bus.TASK_PLAN, limit=5)
        result = await workers.WORKER_HANDLERS[bus.TASK_PLAN](tasks[0].payload)
        await bus.complete_task(tasks[0].id, result)
        assert result["planned"] == 0
        assert result["harvest_tasks"] == 0


class TestFullPipeline:
    async def test_end_to_end_acquisition(self, db, fakes):
        await _seed_gap(db)
        run = await submit_run(DOMAIN, trigger="manual")
        assert run.status == "queued"

        # Drive the real worker chain: plan -> harvest -> extract -> consolidate.
        assert await _drain(bus.TASK_PLAN) == 1
        assert await _drain(bus.TASK_HARVEST) >= 1
        await _drain(bus.TASK_EXTRACT)
        await _drain(bus.TASK_CONSOLIDATE)

        async with db() as session:
            capture = (await session.execute(select(RawCapture))).scalar_one()
            assert capture.run_id == run.id

            facts = (await session.execute(select(ExtractedFact))).scalars().all()
            assert len(facts) == 1
            assert facts[0].entity_name_raw == "Darbhanga Ghat"
            assert facts[0].source_tier == 1  # .nic.in is OFFICIAL
            assert facts[0].resolution_status == "approved"

            entity = (await session.execute(select(Entity))).scalar_one()
            assert entity.canonical_name == "Darbhanga Ghat"
            assert entity.corroboration_count >= 1
            assert entity.grid_cell == "u4pruyk"

            assert (await session.execute(select(NarrativeChunk))).scalar_one() is not None

            assert await finalize_run_if_done(run.id) is True

            refreshed = await session.get(AcquisitionRun, run.id)
            assert refreshed.status == "completed"
            assert refreshed.facts_extracted == 1
            assert refreshed.entities_written == 1
            assert refreshed.completed_at is not None

            yield_rows = (await session.execute(select(StrategyYieldLog))).scalars().all()
            assert yield_rows, "strategy yield must be logged for the planner"
            assert sum(r.entities_found for r in yield_rows) >= 1

            gap = (await session.execute(select(GapQueue))).scalar_one()
            assert gap.status == "resolved"

    async def test_duplicate_capture_is_skipped(self, db, fakes):
        run = await submit_run(DOMAIN, trigger="manual")
        payload = {
            "url": "https://varanasi.nic.in/tourist-place",
            "engine": "web",
            "strategy": "official_portals",
            "entity_type": "landmark",
            "grid_cell": "u4pruyk",
            "gap_id": None,
            "run_id": str(run.id),
            "domain": DOMAIN,
        }
        async with db() as session:
            await bus.enqueue(
                session, bus.TASK_HARVEST, bus.AGENT_HARVESTER, payload, run_id=run.id
            )
            await session.commit()
        assert await _drain(bus.TASK_HARVEST) == 1

        async with db() as session:
            await bus.enqueue(
                session, bus.TASK_HARVEST, bus.AGENT_HARVESTER, payload, run_id=run.id
            )
            await session.commit()
        assert await _drain(bus.TASK_HARVEST) == 1

        async with db() as session:
            assert len((await session.execute(select(RawCapture))).scalars().all()) == 1

    async def test_failed_task_retries_then_gives_up(self, db, fakes, monkeypatch):
        run = await submit_run(DOMAIN, trigger="manual")

        async def boom(payload):
            raise RuntimeError("network on fire")

        monkeypatch.setitem(workers.WORKER_HANDLERS, bus.TASK_HARVEST, boom)
        payload = {
            "url": "https://varanasi.nic.in/x",
            "engine": "web",
            "strategy": "web_crawls",
            "entity_type": "landmark",
            "grid_cell": "u4pruyk",
            "gap_id": None,
            "run_id": str(run.id),
            "domain": DOMAIN,
        }
        async with db() as session:
            await bus.enqueue(
                session, bus.TASK_HARVEST, bus.AGENT_HARVESTER, payload, run_id=run.id
            )
            await session.commit()
        # Retire the plan task so the harvest task is the only work in flight.
        plan_tasks = await bus.claim_tasks(bus.TASK_PLAN, limit=5)
        for t in plan_tasks:
            await bus.complete_task(t.id, {"planned": 0, "harvest_tasks": 0})

        task_id: uuid.UUID | None = None
        for _ in range(4):
            tasks = await bus.claim_tasks(bus.TASK_HARVEST, limit=5)
            if not tasks:
                break
            for t in tasks:
                task_id = t.id
                await bus.fail_task(t.id, "network on fire", retryable=True)

        async with db() as session:
            task = await session.get(TaskQueue, task_id)
            assert task is not None
            assert task.status == "failed"
            assert task.attempts >= task.max_attempts
            assert "network on fire" in task.last_error

    async def test_safety_fact_from_social_held_for_review(self, db, fakes, monkeypatch):
        """A safety fact from a non-official source must not auto-approve."""
        from ghkge.models.schemas import ExtractedFactSchema

        async def fake_extract(text: str, source_url: str = "") -> list[ExtractedFactSchema]:
            return [
                ExtractedFactSchema(
                    entity_name="Ganga Ghats",
                    entity_category="RULE",
                    is_macro_knowledge=False,
                    is_safety_relevant=True,
                    contextual_insight="Swimming is prohibited in the Ganga.",
                    confidence_score=0.8,
                )
            ]

        monkeypatch.setattr(workers, "extract_facts_from_text", fake_extract)
        # Non-official source => tier 4
        assert workers.source_tier_for_domain("randomblog.com") == 4

        run = await submit_run(DOMAIN, trigger="manual")
        async with db() as session:
            capture = RawCapture(
                source_url="https://randomblog.com/ganga",
                source_type="html",
                domain="randomblog.com",
                raw_content="Swimming prohibited in the Ganga.",
                content_hash="abc123",
                strategy_used="web_crawls",
                run_id=run.id,
            )
            session.add(capture)
            await session.commit()
            capture_id = capture.id

        payload = {
            "raw_capture_id": str(capture_id),
            "strategy": "web_crawls",
            "entity_type": "landmark",
            "grid_cell": "u4pruyk",
            "gap_id": None,
            "run_id": str(run.id),
        }
        async with db() as session:
            await bus.enqueue(
                session, bus.TASK_EXTRACT, bus.AGENT_SYNTHESIS, payload, run_id=run.id
            )
            await session.commit()
        # Retire the plan task so only the pipeline under test is in flight.
        for t in await bus.claim_tasks(bus.TASK_PLAN, limit=5):
            await bus.complete_task(t.id, {"planned": 0, "harvest_tasks": 0})

        assert await _drain(bus.TASK_EXTRACT) == 1
        assert await _drain(bus.TASK_CONSOLIDATE) == 1

        async with db() as session:
            fact = (await session.execute(select(ExtractedFact))).scalar_one()
            assert fact.source_tier == 4
            assert fact.is_safety_relevant is True
            assert fact.resolution_status == "held_for_review"

    async def test_run_marks_partial_on_errors(self, db, fakes):
        run = await submit_run(DOMAIN, trigger="manual")
        # Retire the plan task, then fail the harvest task terminally.
        for t in await bus.claim_tasks(bus.TASK_PLAN, limit=5):
            await bus.complete_task(t.id, {"planned": 0, "harvest_tasks": 0})

        async with db() as session:
            task = bus.new_task(bus.TASK_HARVEST, bus.AGENT_HARVESTER, {}, run_id=run.id)
            session.add(task)
            await session.commit()
            task_id = task.id
        await bus.fail_task(task_id, "permanent failure", retryable=False)

        assert await finalize_run_if_done(run.id) is True
        async with db() as session:
            refreshed = await session.get(AcquisitionRun, run.id)
            assert refreshed.status == "partial"
            assert any("permanent failure" in e for e in refreshed.errors)

    async def test_finalize_is_idempotent(self, db, fakes):
        run = await submit_run(DOMAIN, trigger="manual")
        for t in await bus.claim_tasks(bus.TASK_PLAN, limit=5):
            await bus.complete_task(t.id, {"planned": 0, "harvest_tasks": 0})
        assert await finalize_run_if_done(run.id) is True
        assert await finalize_run_if_done(run.id) is False

    async def test_finalize_waits_for_active_tasks(self, db, fakes):
        run = await submit_run(DOMAIN, trigger="manual")
        # The plan task is still pending => run must not be finalized.
        assert await finalize_run_if_done(run.id) is False
        async with db() as session:
            assert (await session.get(AcquisitionRun, run.id)).status == "queued"
