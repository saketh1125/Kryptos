"""Basic tests for GHKGE modules."""

from ghkge.consolidation.resolver import Resolution, resolve_conflict
from ghkge.models.schemas import ExtractedFactSchema, SourceTier
from ghkge.synthesis.chunker import sliding_window_chunker


class TestChunker:
    def test_empty_text(self):
        assert sliding_window_chunker("") == []
        assert sliding_window_chunker("   ") == []

    def test_short_text(self):
        text = "This is a short text."
        chunks = sliding_window_chunker(text, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text(self):
        words = [f"word{i}" for i in range(200)]
        text = " ".join(words)
        chunks = sliding_window_chunker(text, chunk_size=80, overlap=15)
        assert len(chunks) > 1
        # Verify overlap
        for i in range(len(chunks) - 1):
            chunk_words = chunks[i].split()
            next_words = chunks[i + 1].split()
            # Last 15 words of current chunk should overlap with first 15 of next
            assert chunk_words[-15:] == next_words[:15]

    def test_exact_size(self):
        words = [f"word{i}" for i in range(80)]
        text = " ".join(words)
        chunks = sliding_window_chunker(text, chunk_size=80, overlap=15)
        assert len(chunks) == 1


class TestConflictResolution:
    def test_higher_tier_overrides(self):
        result = resolve_conflict(
            existing_tier=3,
            existing_confidence=0.8,
            existing_corroboration=1,
            incoming_tier=1,
            incoming_confidence=0.9,
            is_safety_relevant=False,
        )
        assert result == Resolution.OVERRIDE

    def test_same_tier_higher_confidence(self):
        result = resolve_conflict(
            existing_tier=2,
            existing_confidence=0.7,
            existing_corroboration=1,
            incoming_tier=2,
            incoming_confidence=0.9,
            is_safety_relevant=False,
        )
        assert result == Resolution.OVERRIDE

    def test_same_tier_similar_confidence(self):
        result = resolve_conflict(
            existing_tier=2,
            existing_confidence=0.8,
            existing_corroboration=1,
            incoming_tier=2,
            incoming_confidence=0.82,
            is_safety_relevant=False,
        )
        assert result == Resolution.APPEND_AS_ALTERNATIVE

    def test_safety_relevant_holds_for_review(self):
        result = resolve_conflict(
            existing_tier=3,
            existing_confidence=0.9,
            existing_corroboration=1,
            incoming_tier=4,
            incoming_confidence=0.5,
            is_safety_relevant=True,
        )
        assert result == Resolution.HOLD_FOR_REVIEW

    def test_low_tier_appends_low_confidence(self):
        result = resolve_conflict(
            existing_tier=1,
            existing_confidence=0.9,
            existing_corroboration=3,
            incoming_tier=4,
            incoming_confidence=0.5,
            is_safety_relevant=False,
        )
        assert result == Resolution.APPEND_LOW_CONFIDENCE


class TestSchemas:
    def test_extracted_fact_name_cleaning(self):
        fact = ExtractedFactSchema(
            entity_name="  my local park  ",
            entity_category="LOCATION",
            is_macro_knowledge=False,
            is_safety_relevant=False,
            contextual_insight="A park in the local area.",
            confidence_score=0.9,
        )
        assert fact.entity_name == "My Local Park"

    def test_source_tier_ordering(self):
        assert SourceTier.OFFICIAL < SourceTier.CURATED
        assert SourceTier.CURATED < SourceTier.SOCIAL_VERIFIED
        assert SourceTier.SOCIAL_VERIFIED < SourceTier.SOCIAL_GENERAL
