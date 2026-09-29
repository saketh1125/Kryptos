"""Tests for the gap evaluator: bootstrap coverage, staleness, dedup, self-heal."""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import ghkge.database.connection as connection
from ghkge.database.models import Entity, GapQueue
from ghkge.gap_evaluator.evaluator import (
    _anomaly_gaps,
    _bootstrap_coverage_gaps,
    _staleness_gaps,
    evaluate_coverage,
    run_gap_evaluation,
)
from ghkge.models.schemas import DomainConfig, EntityTypeConfig
from ghkge.utils.geohash import encode, enumerate_cells

DOMAIN = "test_domain"
BBOX = [25.30, 83.00, 25.31, 83.01]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    @compiles(PGUUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):
        return "CHAR(36)"

    async with engine.begin() as conn:
        await conn.run_sync(connection.Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    connection.set_session_factory(factory)
    yield factory
    connection.set_session_factory(None)
    await engine.dispose()


def config(**overrides) -> DomainConfig:
    data = {
        "domain": DOMAIN,
        "geography_bbox": BBOX,
        "grid_precision": 6,
        "entity_types": [
            EntityTypeConfig(name="landmark", strategies=["osm_api"]),
            EntityTypeConfig(name="regulatory_rule", strategies=["official_portals"]),
        ],
    }
    data.update(overrides)
    return DomainConfig(**data)


async def add_entities(db, count: int, entity_type: str, cell: str | None = None) -> None:
    cell = cell or encode(25.305, 83.005, 6)
    async with db() as s:
        for i in range(count):
            s.add(
                Entity(
                    canonical_name=f"{entity_type} {i}",
                    entity_type=entity_type,
                    grid_cell=cell,
                    best_tier=1,
                    corroboration_count=1,
                )
            )
        await s.commit()


class TestBootstrapCoverage:
    async def test_empty_db_creates_gaps_for_every_cell(self, db):
        async with db() as s:
            gaps = await _bootstrap_coverage_gaps(s, config(), set())
        cells = enumerate_cells(BBOX, precision=6)
        assert len(gaps) == len(cells) * 2  # 2 entity types
        assert {g.kind for g in gaps} == {"missing"}
        assert {g.status for g in gaps} == {"open"}
        assert all(g.domain == DOMAIN for g in gaps)

    async def test_populated_cells_produce_no_gaps(self, db):
        for cell in enumerate_cells(BBOX, precision=6):
            await add_entities(db, 1, "landmark", cell=cell)
            await add_entities(db, 1, "regulatory_rule", cell=cell)
        async with db() as s:
            gaps = await _bootstrap_coverage_gaps(s, config(), set())
        assert gaps == []

    async def test_existing_open_gap_deduped(self, db):
        cell = enumerate_cells(BBOX, precision=6)[0]
        async with db() as s:
            s.add(
                GapQueue(
                    grid_cell=cell,
                    entity_type="landmark",
                    kind="missing",
                    severity=3.0,
                    status="open",
                    domain=DOMAIN,
                )
            )
            await s.commit()

        async with db() as s:
            seen = {(cell, "landmark", "missing")}
            gaps = await _bootstrap_coverage_gaps(s, config(), seen)
        assert not any(g.grid_cell == cell and g.entity_type == "landmark" for g in gaps)


class TestStaleness:
    async def test_stale_entities_produce_reinforce_gaps(self, db):
        from datetime import UTC, datetime, timedelta

        stale = encode(25.305, 83.005, 6)
        async with db() as s:
            s.add(
                Entity(
                    canonical_name="Old Rule",
                    entity_type="regulatory_rule",
                    grid_cell=stale,
                    best_tier=1,
                    corroboration_count=1,
                    last_verified=datetime.now(UTC) - timedelta(days=30),
                )
            )
            await s.commit()

        async with db() as s:
            gaps = await _staleness_gaps(s, config(), set())
        reinforce = [g for g in gaps if g.entity_type == "regulatory_rule"]
        assert len(reinforce) == 1
        assert reinforce[0].kind == "reinforce"
        assert reinforce[0].grid_cell == stale

    async def test_fresh_entities_are_not_flagged(self, db):
        await add_entities(db, 1, "regulatory_rule")
        async with db() as s:
            gaps = await _staleness_gaps(s, config(), set())
        assert gaps == []

    async def test_archived_entities_are_skipped(self, db):
        from datetime import UTC, datetime, timedelta

        cell = encode(25.305, 83.005, 6)
        async with db() as s:
            s.add(
                Entity(
                    canonical_name="Dead",
                    entity_type="landmark",
                    grid_cell=cell,
                    best_tier=1,
                    corroboration_count=1,
                    last_verified=datetime.now(UTC) - timedelta(days=365),
                    status="archived",
                )
            )
            await s.commit()
        async with db() as s:
            gaps = await _staleness_gaps(s, config(), set())
        assert gaps == []

    async def test_one_gap_per_cell_not_per_entity(self, db):
        from datetime import UTC, datetime, timedelta

        cell = encode(25.305, 83.005, 6)
        async with db() as s:
            for i in range(5):
                s.add(
                    Entity(
                        canonical_name=f"Stale {i}",
                        entity_type="landmark",
                        grid_cell=cell,
                        best_tier=1,
                        corroboration_count=1,
                        last_verified=datetime.now(UTC) - timedelta(days=200),
                    )
                )
            await s.commit()
        async with db() as s:
            gaps = await _staleness_gaps(s, config(), set())
        assert len([g for g in gaps if g.entity_type == "landmark"]) == 1


class TestAnomalyDetection:
    async def test_sparse_cell_flagged_against_median(self, db):
        dense = encode(25.3005, 83.0005, 6)
        sparse = encode(25.3095, 83.0095, 6)
        await add_entities(db, 20, "landmark", cell=dense)
        await add_entities(db, 1, "landmark", cell=sparse)

        async with db() as s:
            gaps = await _anomaly_gaps(s, config(), set())
        flagged = [g for g in gaps if g.grid_cell == sparse]
        assert len(flagged) == 1
        assert flagged[0].kind == "anomaly"

    async def test_empty_db_no_anomalies(self, db):
        async with db() as s:
            assert await _anomaly_gaps(s, config(), set()) == []


class TestEvaluateCoverage:
    async def test_persists_and_is_idempotent(self, db):
        async with db() as s:
            first = await evaluate_coverage(s, DOMAIN, config())
        assert first
        async with db() as s:
            second = await evaluate_coverage(s, DOMAIN, config())
        assert second == [], "second pass must not duplicate open gaps"

    async def test_resolves_missing_gap_once_cell_is_populated(self, db):
        cell = enumerate_cells(BBOX, precision=6)[0]
        async with db() as s:
            s.add(
                GapQueue(
                    grid_cell=cell,
                    entity_type="landmark",
                    kind="missing",
                    severity=3.0,
                    status="open",
                    domain=DOMAIN,
                )
            )
            await s.commit()

        await add_entities(db, 1, "landmark", cell=cell)

        async with db() as s:
            await evaluate_coverage(s, DOMAIN, config())
            gap = (
                await s.execute(
                    select(GapQueue).where(GapQueue.grid_cell == cell, GapQueue.kind == "missing")
                )
            ).scalar_one()
        assert gap.status == "resolved"

    async def test_missing_config_returns_nothing(self, db, monkeypatch):
        from ghkge.gap_evaluator import evaluator

        def boom(path=None):
            raise FileNotFoundError("no config")

        monkeypatch.setattr(evaluator, "load_domain_config", boom)
        async with db() as s:
            assert await evaluate_coverage(s, DOMAIN) == []


class TestScheduledEntryPoint:
    async def test_run_gap_evaluation_creates_gaps(self, db, monkeypatch):
        from ghkge.gap_evaluator import evaluator

        monkeypatch.setattr(evaluator, "load_domain_config", lambda path=None: config())
        count = await run_gap_evaluation(DOMAIN)
        assert count > 0
        async with db() as s:
            gaps = (await s.execute(select(GapQueue))).scalars().all()
            assert all(g.domain == DOMAIN for g in gaps)
