"""Tests for planner scoring, evaluator thresholds, and the safety gate."""

from __future__ import annotations

import pytest

from ghkge.gap_evaluator.evaluator import (
    DEFAULT_STALENESS,
    MAX_NEW_GAPS_PER_PASS,
    staleness_cutoffs,
)
from ghkge.models.schemas import DomainConfig, EntityTypeConfig
from ghkge.orchestrator.planner import rank_strategies, score_strategy
from ghkge.orchestrator.workers import CATEGORY_TO_TYPE, _fact_status, source_tier_for_domain


class TestScoreStrategy:
    def test_untried_strategy_is_neutral(self):
        assert score_strategy("web_crawls", "landmark", {}) == 1.0

    def test_zero_call_history_is_neutral(self):
        zero = {"total_entities": 0.0, "total_calls": 0.0, "total_novel": 0.0}
        stats = {"web_crawls:landmark": zero}
        assert score_strategy("web_crawls", "landmark", stats) == 1.0

    def test_high_yield_high_novelity_scores_higher(self):
        stats = {
            "osm_api:landmark": {
                "total_entities": 100.0, "total_calls": 10.0, "total_novel": 50.0
            },
            "web_crawls:landmark": {
                "total_entities": 10.0, "total_calls": 10.0, "total_novel": 1.0
            },
        }
        assert score_strategy("osm_api", "landmark", stats) > score_strategy(
            "web_crawls", "landmark", stats
        )

    def test_score_is_entities_per_call_times_novelty(self):
        stats = {"s:t": {"total_entities": 20.0, "total_calls": 5.0, "total_novel": 10.0}}
        # 4 entities/call * 0.5 novelty = 2.0
        assert score_strategy("s", "t", stats) == pytest.approx(2.0)

    def test_other_entity_type_does_not_leak(self):
        stats = {
            "osm_api:landmark": {"total_entities": 100.0, "total_calls": 1.0, "total_novel": 100.0}
        }
        assert score_strategy("osm_api", "regulatory_rule", stats) == 1.0


class TestRankStrategies:
    def test_sorts_by_score_desc(self):
        stats = {
            "osm_api:landmark": {
                "total_entities": 100.0, "total_calls": 10.0, "total_novel": 90.0
            },
            "web_crawls:landmark": {
                "total_entities": 100.0, "total_calls": 10.0, "total_novel": 1.0
            },
        }
        ranked = rank_strategies(["web_crawls", "osm_api"], "landmark", stats)
        assert ranked[0] == "osm_api"

    def test_preserves_all_strategies(self):
        strategies = ["a", "b", "c"]
        assert sorted(rank_strategies(strategies, "t", {})) == strategies

    def test_empty_list(self):
        assert rank_strategies([], "t", {}) == []


class TestStaleness:
    def test_defaults_cover_documented_types(self):
        assert DEFAULT_STALENESS["regulatory_rule"] == 7
        assert DEFAULT_STALENESS["public_event"] == 1
        assert DEFAULT_STALENESS["local_business"] == 30
        assert DEFAULT_STALENESS["landmark"] == 90

    def test_yaml_overrides_defaults(self):
        config = DomainConfig(
            domain="t",
            geography_bbox=[0, 0, 1, 1],
            entity_types=[EntityTypeConfig(name="landmark", strategies=["osm_api"])],
            staleness_cutoff_days={"landmark": 5, "custom_thing": 2},
        )
        merged = staleness_cutoffs(config)
        assert merged["landmark"] == 5  # overridden
        assert merged["custom_thing"] == 2  # new
        assert merged["regulatory_rule"] == 7  # default preserved

    def test_no_config_returns_defaults(self):
        assert staleness_cutoffs(None) == DEFAULT_STALENESS

    def test_gap_cap_is_sane(self):
        assert 0 < MAX_NEW_GAPS_PER_PASS <= 1000


class TestSourceTierMapping:
    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("varanasi.nic.in", 1),
            ("data.gov.in", 1),
            ("city.gov", 1),
            ("en.wikipedia.org", 2),
            ("openstreetmap.org", 2),
            ("example.org", 2),
            ("randomblog.com", 4),
            ("reddit.com", 4),
        ],
    )
    def test_tiers(self, domain: str, expected: int):
        assert source_tier_for_domain(domain) == expected

    def test_tier_ordering_is_documented(self):
        assert source_tier_for_domain("x.nic.in") < source_tier_for_domain("x.org")
        assert source_tier_for_domain("x.org") < source_tier_for_domain("x.com")


class TestCategoryMapping:
    def test_all_extraction_categories_mapped(self):
        for category in ("LOCATION", "ORGANIZATION", "EVENT", "RULE", "METADATA"):
            assert category in CATEGORY_TO_TYPE

    def test_safety_rules_map_to_regulatory(self):
        assert CATEGORY_TO_TYPE["RULE"] == "regulatory_rule"


class FakeEntity:
    def __init__(self, corroboration_count: int) -> None:
        self.corroboration_count = corroboration_count


class FakeFact:
    def __init__(self, is_safety_relevant: bool, source_tier: int) -> None:
        self.is_safety_relevant = is_safety_relevant
        self.source_tier = source_tier


class TestSafetyGate:
    def test_non_safety_always_approved(self):
        assert _fact_status(FakeEntity(1), FakeFact(False, 4)) == "approved"

    def test_safety_from_official_approved(self):
        assert _fact_status(FakeEntity(1), FakeFact(True, 1)) == "approved"

    def test_safety_social_single_source_held(self):
        assert _fact_status(FakeEntity(1), FakeFact(True, 3)) == "held_for_review"

    def test_safety_social_corroborated_approved(self):
        assert _fact_status(FakeEntity(2), FakeFact(True, 3)) == "approved"

    def test_safety_social_three_sources_approved(self):
        assert _fact_status(FakeEntity(3), FakeFact(True, 4)) == "approved"
