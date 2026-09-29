"""Tests for planner scoring (doc 08 §3), evaluator thresholds, safety gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ghkge.gap_evaluator.evaluator import (
    DEFAULT_STALENESS,
    MAX_NEW_GAPS_PER_PASS,
    staleness_cutoffs,
)
from ghkge.models.schemas import DomainConfig, EntityTypeConfig
from ghkge.orchestrator.planner import (
    DECAY_GRACE_DAYS,
    EXPLORATION_SCORE,
    NORMALIZED_COST,
    TIER_WEIGHTS,
    StrategyStats,
    rank_strategies,
    recency_decay,
    strategy_score,
)
from ghkge.orchestrator.workers import CATEGORY_TO_TYPE, _fact_status, source_tier_for_domain

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def stats(**kw) -> StrategyStats:
    base = {
        "entities": 100.0,
        "calls": 10.0,
        "novel": 50.0,
        "avg_tier": 1.0,
        "last_used": NOW,
    }
    return StrategyStats(**{**base, **kw})


class TestRecencyDecay:
    def test_no_history_is_full_weight(self):
        assert recency_decay(None, now=NOW) == 1.0

    def test_inside_grace_period_is_full_weight(self):
        recent = NOW - timedelta(days=10)
        assert recency_decay(recent, now=NOW) == 1.0

    def test_exactly_at_grace_boundary_is_full_weight(self):
        edge = NOW - timedelta(days=DECAY_GRACE_DAYS)
        assert recency_decay(edge, now=NOW) == pytest.approx(1.0)

    def test_decays_after_grace(self):
        stale = NOW - timedelta(days=60)
        assert recency_decay(stale, now=NOW) < 1.0

    def test_decay_is_monotonic(self):
        values = [
            recency_decay(NOW - timedelta(days=d), now=NOW) for d in (30, 45, 60, 90, 180)
        ]
        assert values == sorted(values, reverse=True)

    def test_asymptotes_towards_zero(self):
        ancient = NOW - timedelta(days=3000)
        assert recency_decay(ancient, now=NOW) < 0.01

    def test_never_negative(self):
        ancient = NOW - timedelta(days=36500)
        assert recency_decay(ancient, now=NOW) >= 0.0


class TestStrategyScore:
    def test_untried_strategy_gets_exploration_score(self):
        assert strategy_score("web_crawls", None, now=NOW) == EXPLORATION_SCORE
        assert strategy_score("web_crawls", StrategyStats(), now=NOW) == EXPLORATION_SCORE

    def test_score_is_yield_times_tier_weight_minus_cost(self):
        s = stats(entities=100.0, calls=10.0, avg_tier=1.0, last_used=NOW)
        # 10 entities/call * 1.0 tier weight * 1.0 decay - cost
        expected = 10.0 - NORMALIZED_COST["osm_api"]
        assert strategy_score("osm_api", s, now=NOW) == pytest.approx(expected)

    def test_better_yield_scores_higher(self):
        good = stats(entities=100.0, calls=10.0)
        bad = stats(entities=10.0, calls=10.0)
        assert strategy_score("osm_api", good, now=NOW) > strategy_score(
            "osm_api", bad, now=NOW
        )

    def test_official_source_beats_social_at_equal_yield(self):
        official = stats(avg_tier=1.0)
        social = stats(avg_tier=4.0)
        assert strategy_score("official_portals", official, now=NOW) > strategy_score(
            "official_portals", social, now=NOW
        )

    def test_cheaper_strategy_wins_at_equal_yield(self):
        s = stats()
        assert strategy_score("osm_api", s, now=NOW) > strategy_score(
            "media_transcripts", s, now=NOW
        )

    def test_stale_strategy_loses_to_fresh_one(self):
        fresh = stats(last_used=NOW)
        stale = stats(last_used=NOW - timedelta(days=200))
        assert strategy_score("web_crawls", fresh, now=NOW) > strategy_score(
            "web_crawls", stale, now=NOW
        )

    def test_expensive_strategy_can_go_negative(self):
        poor = stats(entities=1.0, calls=10.0, avg_tier=4.0)
        assert strategy_score("media_transcripts", poor, now=NOW) < 0

    def test_all_strategies_have_a_cost(self):
        from ghkge.orchestrator.domain import STRATEGY_ENGINES

        assert set(STRATEGY_ENGINES) <= set(NORMALIZED_COST)

    def test_all_tiers_have_a_weight(self):
        from ghkge.models.schemas import SourceTier

        for tier in SourceTier:
            assert int(tier) in TIER_WEIGHTS


class TestRankStrategies:
    def test_untried_strategies_tie_and_keep_order(self):
        ranked = rank_strategies(["a", "b", "c"], "landmark", {}, now=NOW)
        assert sorted(ranked) == ["a", "b", "c"]

    def test_proven_strategy_rises(self):
        data = {
            "web_crawls:landmark": stats(last_used=NOW),
            "osm_api:landmark": StrategyStats(),  # never used
        }
        # osm_api costs 0 but has no history; the proven crawler should still win.
        ranked = rank_strategies(["web_crawls", "osm_api"], "landmark", data, now=NOW)
        assert ranked[0] == "web_crawls"

    def test_preserves_every_strategy(self):
        strategies = ["a", "b", "c", "d"]
        assert sorted(rank_strategies(strategies, "t", {}, now=NOW)) == strategies

    def test_empty(self):
        assert rank_strategies([], "t", {}, now=NOW) == []

    def test_type_isolation(self):
        data = {"web_crawls:landmark": stats(last_used=NOW)}
        # The same strategy asked about a different entity type is untried.
        assert strategy_score("web_crawls", data.get("web_crawls:regulatory_rule"), now=NOW) == (
            EXPLORATION_SCORE
        )


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
        assert merged["landmark"] == 5
        assert merged["custom_thing"] == 2
        assert merged["regulatory_rule"] == 7

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
