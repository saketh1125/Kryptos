"""Tests for the event bus and strategy registry (mocked sessions)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ghkge.models.schemas import DomainConfig, EntityTypeConfig
from ghkge.orchestrator import bus
from ghkge.orchestrator.domain import (
    STRATEGY_ENGINES,
    DomainConfigNotFoundError,
    build_overpass_query,
    cell_for_point,
    load_domain_config,
    plan_targets,
    reset_domain_cache,
)


class TestDomainConfig:
    def test_loads_shipped_config(self):
        reset_domain_cache()
        config = load_domain_config()
        assert config.domain
        assert len(config.geography_bbox) == 4
        assert config.entity_types
        names = {et.name for et in config.entity_types}
        assert "landmark" in names

    def test_missing_config_names_the_paths_tried(self):
        reset_domain_cache()
        with pytest.raises(DomainConfigNotFoundError) as exc:
            load_domain_config("/nope/missing.yaml")
        assert "/nope/missing.yaml" in str(exc.value)
        assert "GHKGE_DOMAIN_CONFIG_PATH" in str(exc.value)

    def test_loads_explicit_path(self, tmp_path):
        reset_domain_cache()
        path = tmp_path / "custom.yaml"
        path.write_text(
            "domain: kashi\n"
            "geography_bbox: [25.0, 83.0, 25.5, 83.5]\n"
            "entity_types:\n"
            "  - name: landmark\n"
            "    strategies: [osm_api]\n"
        )
        config = load_domain_config(str(path))
        assert config.domain == "kashi"
        assert config.entity_types[0].strategies == ["osm_api"]

    def test_cache_keys_on_resolved_path(self, tmp_path):
        reset_domain_cache()
        path = tmp_path / "custom.yaml"
        path.write_text(
            "domain: kashi\n"
            "geography_bbox: [25.0, 83.0, 25.5, 83.5]\n"
            "entity_types: []\n"
        )
        first = load_domain_config(str(path))
        second = load_domain_config(str(path))
        assert first is second

    def test_caches_by_path(self):
        reset_domain_cache()
        a = load_domain_config()
        b = load_domain_config()
        assert a is b

    def test_registry_covers_config_strategies(self):
        """Every strategy named in the YAML must map to a real engine."""
        config = load_domain_config()
        for et in config.entity_types:
            for strategy in et.strategies:
                assert strategy in STRATEGY_ENGINES, (
                    f"{et.name} declares unknown strategy '{strategy}'"
                )

    def test_safety_relevant_flag_preserved(self):
        config = load_domain_config()
        rules = {et.name: et.safety_relevant for et in config.entity_types}
        assert rules.get("regulatory_rule") is True


class TestOverpass:
    def test_builds_query_for_known_type(self):
        q = build_overpass_query("landmark", "25.3,83.0,25.4,83.1")
        assert q is not None
        assert "tourism" in q
        assert "25.3,83.0,25.4,83.1" in q
        assert q.startswith("[out:json]")

    def test_returns_none_for_unsupported_type(self):
        assert build_overpass_query("regulatory_rule", "25.3,83.0,25.4,83.1") is None

    def test_business_tags(self):
        q = build_overpass_query("local_business", "1,2,3,4")
        assert q is not None and "shop" in q


class TestPlanTargets:
    def test_osm_strategy_yields_overpass_query(self):
        config = DomainConfig(
            domain="t",
            geography_bbox=[25.3, 83.0, 25.31, 83.01],
            entity_types=[EntityTypeConfig(name="landmark", strategies=["osm_api"])],
        )
        cell = cell_for_point(25.305, 83.005)
        target = plan_targets(config, "landmark", cell, "osm_api")
        assert target["engine"] == "api_overpass"
        assert "overpass_query" in target

    def test_web_strategy_yields_seed_urls(self):
        config = DomainConfig(
            domain="t",
            geography_bbox=[25.3, 83.0, 25.31, 83.01],
            entity_types=[EntityTypeConfig(name="landmark", strategies=["web_crawls"])],
            strategy_seeds={"web_crawls": ["https://example.org/a", "https://example.org/b"]},
        )
        cell = cell_for_point(25.305, 83.005)
        target = plan_targets(config, "landmark", cell, "web_crawls")
        assert target["engine"] == "web"
        assert target["urls"] == ["https://example.org/a", "https://example.org/b"]

    def test_web_strategy_without_seeds_is_empty(self):
        config = DomainConfig(
            domain="t",
            geography_bbox=[25.3, 83.0, 25.31, 83.01],
            entity_types=[EntityTypeConfig(name="landmark", strategies=["web_crawls"])],
        )
        result = plan_targets(config, "landmark", cell_for_point(25.3, 83.0), "web_crawls")
        assert result.get("urls", []) == []

    def test_unknown_strategy_is_empty(self):
        config = DomainConfig(
            domain="t", geography_bbox=[25.3, 83.0, 25.31, 83.01], entity_types=[]
        )
        assert plan_targets(config, "landmark", "u4pru", "nope") == {}


class TestTaskConstruction:
    def test_new_task_defaults(self):
        task = bus.new_task(
            bus.TASK_HARVEST, bus.AGENT_HARVESTER, {"url": "https://x"}, run_id=None
        )
        assert task.status == "pending"
        assert task.target_agent == bus.AGENT_HARVESTER
        assert task.payload == {"url": "https://x"}

    def test_enqueue_adds_and_flushes(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()

        asyncio_result = None
        import asyncio

        async def run():
            return await bus.enqueue(
                session, bus.TASK_EXTRACT, bus.AGENT_SYNTHESIS, {"raw_capture_id": "x"}
            )

        asyncio_result = asyncio.run(run())
        session.add.assert_called_once()
        session.flush.assert_awaited_once()
        assert asyncio_result.payload == {"raw_capture_id": "x"}

    def test_all_task_types_distinct(self):
        types = [
            bus.TASK_PLAN,
            bus.TASK_HARVEST,
            bus.TASK_EXTRACT,
            bus.TASK_CONSOLIDATE,
        ]
        assert len(set(types)) == 4

    def test_active_statuses_cover_lifecycle(self):
        assert "pending" in bus.ACTIVE_STATUSES
        assert "in_progress" in bus.ACTIVE_STATUSES


class TestWorkerTaskTypes:
    def test_every_agent_has_a_task_type(self):
        from ghkge.orchestrator.workers import HANDLER_TASK_TYPES, WORKER_HANDLERS

        for agent in (
            bus.AGENT_PLANNER,
            bus.AGENT_HARVESTER,
            bus.AGENT_SYNTHESIS,
            bus.AGENT_CONSOLIDATOR,
        ):
            assert agent in HANDLER_TASK_TYPES
            assert HANDLER_TASK_TYPES[agent] in WORKER_HANDLERS

    def test_handlers_are_distinct_callables(self):
        from ghkge.orchestrator.workers import WORKER_HANDLERS

        handlers = list(WORKER_HANDLERS.values())
        assert len({id(h) for h in handlers}) == len(handlers)
