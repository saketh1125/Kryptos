"""API route tests with a real DB and a mocked service dependency."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import ghkge.database.connection as connection
from ghkge.database.models import (
    Entity,
    ExtractedFact,
    Feedback,
    GapQueue,
    RawCapture,
)
from ghkge.utils.geohash import encode
from tests.test_pipeline_integration import DOMAIN

# Cell containing the Varanasi test coordinates (center ~236m from 25.305, 83.005)
CELL = encode(25.305, 83.005, 6)


@pytest_asyncio.fixture
async def client(monkeypatch):
    """TestClient over an in-memory DB; workers/scheduler stay off."""
    from ghkge.config.settings import settings

    monkeypatch.setattr(settings, "workers_enabled", False)
    monkeypatch.setattr(settings, "scheduler_enabled", False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    @compiles(PGUUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):
        return "CHAR(36)"

    async with engine.begin() as conn:
        await conn.run_sync(connection.Base.metadata.create_all)

    # Neutralize embeddings: no Ollama in tests.
    import ghkge.consolidation.sync as sync_mod

    async def no_embed(text: str) -> list[float] | None:
        return None

    monkeypatch.setattr(sync_mod, "generate_embedding", no_embed)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with factory() as session:
            yield session

    from ghkge.api.main import app
    from ghkge.database.connection import get_session

    app.dependency_overrides[get_session] = override_get_session

    # Neutralize embeddings: no Ollama in tests.
    import ghkge.consolidation.sync as sync_mod

    async def no_embed(text: str) -> list[float] | None:
        return None

    monkeypatch.setattr(sync_mod, "generate_embedding", no_embed)

    with TestClient(app) as c:
        # Set AFTER the lifespan runs: init_db() would otherwise point the
        # module-level factory at real Postgres.
        connection.set_session_factory(factory)
        try:
            yield c, factory
        finally:
            connection.set_session_factory(None)

    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def _seed(factory):
    """One run + entity + approved safety rule + a held fact + a gap."""
    from ghkge.database.models import AcquisitionRun

    async with factory() as s:
        run = AcquisitionRun(domain=DOMAIN, trigger="manual", status="completed")
        s.add(run)
        await s.flush()  # assign run.id before referencing it

        capture = RawCapture(
            source_url="https://varanasi.nic.in/rule",
            source_type="html",
            domain="varanasi.nic.in",
            raw_content="No swimming in the Ganga.",
            content_hash="h1",
            strategy_used="official_portals",
            run_id=run.id,
        )
        # geohash-6 cell whose center is ~236m from (25.305, 83.005).
        entity = Entity(
            canonical_name="Darbhanga Ghat",
            entity_type="landmark",
            grid_cell=CELL,
            aliases=["Darbhanga"],
            best_tier=1,
            corroboration_count=3,
        )
        s.add_all([run, capture, entity])
        await s.flush()

        s.add_all(
            [
                ExtractedFact(
                    raw_capture_id=capture.id,
                    canonical_entity_id=entity.id,
                    entity_name_raw="Darbhanga Ghat",
                    entity_category="RULE",
                    contextual_insight="Swimming is prohibited here.",
                    confidence_score=0.95,
                    source_tier=1,
                    is_safety_relevant=True,
                    chunk_index=0,
                    resolution_status="approved",
                ),
                ExtractedFact(
                    raw_capture_id=capture.id,
                    canonical_entity_id=entity.id,
                    entity_name_raw="Darbhanga Ghat",
                    entity_category="LOCATION",
                    contextual_insight="Held for review, must not leak.",
                    confidence_score=0.4,
                    source_tier=4,
                    is_safety_relevant=False,
                    chunk_index=1,
                    resolution_status="held_for_review",
                ),
                GapQueue(
                    grid_cell="u4pruyk",
                    entity_type="landmark",
                    kind="missing",
                    severity=3.0,
                    status="open",
                    domain=DOMAIN,
                ),
            ]
        )
        await s.commit()
        return entity.id, capture.id


class TestHealthAndRouting:
    def test_health_reports_readiness(self, client):
        c, _ = client
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"status", "ready", "problems"}
        assert isinstance(body["ready"], bool)
        assert isinstance(body["problems"], list)
        # Status must agree with the problem list.
        assert body["ready"] is (body["status"] == "ok")
        assert body["ready"] is (not body["problems"])

    def test_health_surfaces_startup_problems(self, client):
        """D-08: a degraded app must say so instead of reporting ok."""
        c, _ = client
        from ghkge.api.main import app

        app.state.startup_problems = ["database schema is missing"]
        try:
            body = c.get("/health").json()
            assert body["ready"] is False
            assert body["status"] == "degraded"
            assert "schema" in body["problems"][0]
        finally:
            app.state.startup_problems = []

    def test_knowledge_routes_are_prefixed(self, client):
        c, _ = client
        spec = c.get("/openapi.json").json()
        paths = set(spec["paths"])
        assert "/api/v1/entities/search" in paths
        assert "/admin/v1/runs" in paths
        # Old prefixes must be gone.
        assert "/v1/entities/search" not in paths
        assert "/v1/runs" not in paths


class TestKnowledgeApi:
    async def test_search_finds_entity_by_name(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/api/v1/entities/search", params={"q": "Darbhanga"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert results
        assert results[0]["canonical_name"] == "Darbhanga Ghat"
        assert results[0]["relevance_score"] > 0

    async def test_search_type_filter(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/api/v1/entities/search", params={"type": "transit_route"})
        assert r.json()["results"] == []

    async def test_search_rejects_bad_near(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/api/v1/entities/search", params={"near": "notacoord", "radius_m": 100})
        assert r.status_code == 400

    async def test_search_radius_filters(self, client):
        c, factory = client
        await _seed(factory)
        # The cell center stands in for the entity location, so radius is
        # measured from it: 236m here, 680km from Delhi.
        inside = c.get(
            "/api/v1/entities/search",
            params={"near": "25.305,83.005", "radius_m": 1000},
        ).json()["results"]
        outside = c.get(
            "/api/v1/entities/search",
            params={"near": "28.61,77.20", "radius_m": 1000},
        ).json()["results"]
        assert len(inside) == 1
        assert outside == []

    async def test_entity_detail_hides_unapproved_facts(self, client):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.get(f"/api/v1/entities/{entity_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["canonical_name"] == "Darbhanga Ghat"
        assert body["aliases"] == ["Darbhanga"]
        assert body["corroboration_count"] == 3
        insights = [f["insight"] for f in body["facts"]]
        assert "Swimming is prohibited here." in insights
        assert "Held for review, must not leak." not in insights
        # Location is resolved from the grid cell.
        assert body["location"]["lat"] is not None
        assert body["location"]["grid_cell"] == CELL

    async def test_entity_detail_404(self, client):
        c, _ = client
        r = c.get(f"/api/v1/entities/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_rules_returns_approved_safety_only(self, client):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.get(f"/api/v1/entities/{entity_id}/rules")
        assert r.status_code == 200
        rules = r.json()["rules"]
        assert len(rules) == 1
        assert rules[0]["corroboration_count"] == 3

    async def test_nearby_falls_back_to_grid_cell(self, client):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.get(f"/api/v1/entities/{entity_id}/nearby")
        assert r.status_code == 200
        assert len(r.json()["nodes"]) == 1  # only itself in this cell

    async def test_feedback_persists(self, client):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.post(
            "/api/v1/feedback",
            json={
                "entity_id": str(entity_id),
                "correction_text": "Gate closes at 10pm, not midnight.",
                "submitted_by_session": "sess-123",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "queued_for_review"
        async with factory() as s:
            rows = (await s.execute(__import__("sqlalchemy").select(Feedback))).scalars().all()
            assert len(rows) == 1
            assert rows[0].entity_id == entity_id
            assert str(rows[0].id) == body["feedback_id"]

    async def test_feedback_unknown_entity_404(self, client):
        c, _ = client
        r = c.post(
            "/api/v1/feedback",
            json={
                "entity_id": str(uuid.uuid4()),
                "correction_text": "x",
                "submitted_by_session": "s",
            },
        )
        assert r.status_code == 404

    async def test_feedback_validation_422(self, client):
        c, _ = client
        r = c.post("/api/v1/feedback", json={"entity_id": "not-a-uuid"})
        assert r.status_code == 422


class TestOrchestrationApi:
    async def test_gaps_filtered_by_domain(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/admin/v1/gaps", params={"domain": DOMAIN})
        assert r.status_code == 200
        assert len(r.json()["gaps"]) == 1
        assert c.get("/admin/v1/gaps", params={"domain": "other"}).json()["gaps"] == []

    async def test_gap_resolve_skip_closes(self, client):
        c, factory = client
        await _seed(factory)
        gaps = c.get("/admin/v1/gaps", params={"domain": DOMAIN}).json()["gaps"]
        gap_id = gaps[0]["id"]
        r = c.post(f"/admin/v1/gaps/{gap_id}/resolve", json={"resolution": "skip"})
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"

    async def test_gap_resolve_retry_reopens(self, client):
        c, factory = client
        await _seed(factory)
        gap_id = c.get("/admin/v1/gaps", params={"domain": DOMAIN}).json()["gaps"][0]["id"]
        r = c.post(f"/admin/v1/gaps/{gap_id}/resolve", json={"resolution": "retry"})
        assert r.json()["status"] == "open"
        still = c.get("/admin/v1/gaps", params={"domain": DOMAIN}).json()["gaps"]
        assert len(still) == 1

    async def test_gap_resolve_404(self, client):
        c, _ = client
        r = c.post(f"/admin/v1/gaps/{uuid.uuid4()}/resolve", json={"resolution": "skip"})
        assert r.status_code == 404

    async def test_gap_resolve_rejects_bad_action(self, client):
        c, factory = client
        await _seed(factory)
        gap_id = c.get("/admin/v1/gaps", params={"domain": DOMAIN}).json()["gaps"][0]["id"]
        r = c.post(f"/admin/v1/gaps/{gap_id}/resolve", json={"resolution": "explode"})
        assert r.status_code == 422

    async def test_review_queue_lists_held_facts(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/admin/v1/facts", params={"status": "held_for_review"})
        assert r.status_code == 200
        facts = r.json()["facts"]
        assert len(facts) == 1
        assert facts[0]["contextual_insight"] == "Held for review, must not leak."

    async def test_moderation_approves_fact(self, client):
        c, factory = client
        await _seed(factory)
        fact_id = c.get("/admin/v1/facts").json()["facts"][0]["id"]
        r = c.post(f"/admin/v1/facts/{fact_id}/moderate", json={"action": "approve"})
        assert r.status_code == 200
        assert r.json()["resolution_status"] == "approved"
        assert c.get("/admin/v1/facts").json()["facts"] == []

    async def test_moderation_rejects_fact(self, client):
        c, factory = client
        await _seed(factory)
        fact_id = c.get("/admin/v1/facts").json()["facts"][0]["id"]
        r = c.post(f"/admin/v1/facts/{fact_id}/moderate", json={"action": "reject"})
        assert r.json()["resolution_status"] == "rejected"

    async def test_moderation_404(self, client):
        c, _ = client
        r = c.post(f"/admin/v1/facts/{uuid.uuid4()}/moderate", json={"action": "approve"})
        assert r.status_code == 404

    async def test_run_status_404(self, client):
        c, _ = client
        assert c.get(f"/admin/v1/runs/{uuid.uuid4()}").status_code == 404


class TestNearbyRelationAllowList:
    """D-01: `relation` is interpolated into Cypher, so it must be allow-listed."""

    @pytest.mark.parametrize(
        "payload",
        [
            "NEAR]-() DETACH DELETE n //",
            "NEAR` RETURN 1 //",
            "near",
            "NEAR; MATCH (a) DELETE a",
            "",
            "*",
            "NEAR-[:HAS_AMENITY",
        ],
    )
    async def test_rejects_non_allowlisted_relation(self, client, payload: str):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.get(
            f"/api/v1/entities/{entity_id}/nearby",
            params={"relation": payload},
        )
        assert r.status_code == 400, f"expected rejection for {payload!r}"

    @pytest.mark.parametrize(
        "relation", ["NEAR", "ACCESSIBLE_VIA", "HAS_AMENITY", "PART_OF", "SUBJECT_TO_RULE"]
    )
    async def test_accepts_allowlisted_relations(self, client, relation: str):
        c, factory = client
        entity_id, _ = await _seed(factory)
        r = c.get(
            f"/api/v1/entities/{entity_id}/nearby",
            params={"relation": relation},
        )
        # Neo4j is absent in tests, so the same-cell fallback answers.
        assert r.status_code == 200

    async def test_validation_precedes_entity_lookup(self, client):
        """A bad relation is a 400 even for a nonexistent entity."""
        c, _ = client
        r = c.get(
            f"/api/v1/entities/{uuid.uuid4()}/nearby",
            params={"relation": "NEAR]-(n) DETACH DELETE n //"},
        )
        assert r.status_code == 400

    async def test_depth_is_bounded(self, client):
        c, factory = client
        entity_id, _ = await _seed(factory)
        for bad in ("0", "-1", "99"):
            r = c.get(f"/api/v1/entities/{entity_id}/nearby", params={"depth": bad})
            assert r.status_code == 422, f"depth={bad} should be rejected"


class TestSearchQueryBudget:
    """D-04: search must not scale queries with the number of results."""

    async def _count_queries(self, client, factory, entities: int, **params):
        """Run a search while counting the statements it executes."""
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        counter = {"n": 0}

        def _count(conn, cursor, statement, parameters, context, executemany):
            counter["n"] += 1

        async with factory() as session:
            engine: Engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", _count)
            try:
                from ghkge.api.routes.knowledge import search_entities

                # Route defaults are Query() objects; pass them explicitly.
                args: dict = {
                    "q": None,
                    "type": None,
                    "near": None,
                    "radius_m": None,
                    "limit": 20,
                    **params,
                }
                await search_entities(session=session, **args)
            finally:
                event.remove(engine, "before_cursor_execute", _count)
        return counter["n"]

    async def test_query_count_does_not_grow_with_result_count(self, client, monkeypatch):
        c, factory = client
        import ghkge.api.routes.knowledge as kn

        async def no_embed(text: str):
            return None

        monkeypatch.setattr(kn, "generate_embedding", no_embed)

        async def seed(n: int):
            async with factory() as s:
                for i in range(n):
                    e = Entity(
                        canonical_name=f"Place {i}",
                        entity_type="landmark",
                        grid_cell=CELL,
                        best_tier=2,
                        corroboration_count=1,
                    )
                    s.add(e)
                await s.commit()

        await seed(5)
        few = await self._count_queries(client, factory, 5, q="Place", limit=20)

        await seed(45)
        many = await self._count_queries(client, factory, 50, q="Place", limit=20)

        assert many == few, (
            f"query count grew with result count ({few} -> {many}); "
            "the per-entity snippet N+1 has returned"
        )
        assert few <= 4, f"expected a fixed, small query count, got {few}"

    async def test_snippet_is_highest_confidence_approved_fact(self, client):
        c, factory = client
        await _seed(factory)
        r = c.get("/api/v1/entities/search", params={"q": "Darbhanga"})
        results = r.json()["results"]
        assert results
        # The held fact must not be chosen even if it scored higher.
        assert results[0]["snippet"] == "Swimming is prohibited here."


class TestAdminAuth:
    """D-07: /admin/v1 mutates what the public API serves, so it is gated."""

    @pytest.fixture
    def keyed(self, monkeypatch):
        from ghkge.config.settings import settings

        monkeypatch.setattr(settings, "admin_api_key", "s3cret-key")
        return "s3cret-key"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/admin/v1/gaps?domain=x"),
            ("post", "/admin/v1/runs"),
            ("get", "/admin/v1/strategies/yield"),
            ("get", "/admin/v1/facts"),
        ],
    )
    async def test_rejects_without_key(self, client, keyed, method, path):
        c, _ = client
        body = {"domain": "x"} if method == "post" else None
        r = getattr(c, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 401, f"{method.upper()} {path} should require the key"

    async def test_rejects_wrong_key(self, client, keyed):
        c, _ = client
        r = c.get("/admin/v1/gaps?domain=x", headers={"X-Admin-Key": "wrong"})
        assert r.status_code == 401

    async def test_accepts_correct_key(self, client, keyed):
        c, _ = client
        r = c.get("/admin/v1/gaps?domain=x", headers={"X-Admin-Key": keyed})
        assert r.status_code == 200

    async def test_knowledge_api_stays_public(self, client, keyed):
        """The consumer API must not be gated by the admin key."""
        c, factory = client
        await _seed(factory)
        assert c.get("/api/v1/entities/search").status_code == 200
        assert c.get("/health").status_code == 200

    async def test_open_when_no_key_configured(self, client, monkeypatch):
        from ghkge.config.settings import settings

        monkeypatch.setattr(settings, "admin_api_key", "")
        c, _ = client
        assert c.get("/admin/v1/gaps?domain=x").status_code == 200

    async def test_empty_key_header_is_rejected(self, client, keyed):
        c, _ = client
        r = c.get("/admin/v1/gaps?domain=x", headers={"X-Admin-Key": ""})
        assert r.status_code == 401
