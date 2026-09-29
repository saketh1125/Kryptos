from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field


class SourceTier(IntEnum):
    OFFICIAL = 1
    CURATED = 2
    SOCIAL_VERIFIED = 3
    SOCIAL_GENERAL = 4


class RunStatus(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    domain: str
    status: str
    trigger: str
    started_at: datetime
    completed_at: datetime | None = None
    facts_extracted: int = 0
    entities_written: int = 0
    errors: list[str] = Field(default_factory=list)


class GapEntry(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    grid_cell: str
    entity_type: str
    kind: str
    severity: float
    status: str = "open"
    domain: str | None = None
    created_at: datetime


class StrategyYield(BaseModel):
    model_config = {"from_attributes": True}

    strategy_name: str
    entity_type: str
    entities_per_call: float
    novelty_rate: float
    last_run: datetime


class ExtractedFactSchema(BaseModel):
    """Schema for LLM extraction output via Instructor."""

    entity_name: str = Field(description="Canonical local name of the entity.")
    entity_category: Literal["LOCATION", "ORGANIZATION", "EVENT", "RULE", "METADATA"]
    is_macro_knowledge: bool = Field(
        description="True if this fact is global knowledge not specific to the local region."
    )
    is_safety_relevant: bool = Field(
        description="True if this outlines laws, hazards, rules, prohibitions, or guidelines."
    )
    contextual_insight: str = Field(
        description="Crucial hyperlocal fact (e.g. 'Clean drinking water is available via tap 2.')."
    )
    confidence_score: float = Field(ge=0.0, le=1.0)

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(self, "entity_name", self.entity_name.strip().title())


class EntityTypeConfig(BaseModel):
    name: str
    strategies: list[str] = Field(
        description="Ranked list of strategy names to query for this entity type"
    )
    safety_relevant: bool = False


class DomainConfig(BaseModel):
    domain: str
    geography_bbox: list[float] = Field(description="[min_lat, min_lon, max_lat, max_lon]")
    entity_types: list[EntityTypeConfig]
    coverage_target: float = 0.8
    staleness_cutoff_days: dict[str, int] = Field(default_factory=dict)
    grid_precision: int = 6
    strategy_seeds: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Seed URLs per strategy (public-access sources only)",
    )


class ComplianceResult(BaseModel):
    allowed: bool
    reason: str = ""
    retry_after: float | None = None


class ApprovedTarget(BaseModel):
    """A compliance-validated URL ready for fetching.

    ``validated_at`` records when ComplianceEngine last cleared this target.
    A harvester re-checking immediately afterwards re-verifies the cheap,
    stateless rules (platform, denylist, robots) but skips the rate-limit
    gate, which has already been paid for. Without the stamp, a
    worker-then-harvester hand-off charges the domain twice milliseconds
    apart and the fetch is denied (D-02).
    """

    url: str
    domain: str
    strategy: str
    entity_type: str
    validated_at: datetime | None = None

    def is_freshly_validated(self, window_s: float) -> bool:
        """True if validated within ``window_s`` of now."""
        if self.validated_at is None:
            return False
        age = (datetime.now(UTC) - self.validated_at).total_seconds()
        return 0 <= age <= window_s


class RawCaptureData(BaseModel):
    """Raw capture data returned by harvesters."""

    source_url: str
    source_type: str
    domain: str
    raw_content: str
    content_hash: str
    strategy_used: str
    run_id: uuid.UUID


# --- API Contract Models ---


class RunCreateRequest(BaseModel):
    domain: str
    entity_types: list[str] = Field(default_factory=list)
    trigger: str = "manual"


class RunCreateResponse(BaseModel):
    run_id: uuid.UUID
    status: str = "queued"
    submitted_at: datetime


class RunStatusResponse(BaseModel):
    run_id: uuid.UUID
    domain: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    facts_extracted: int = 0
    entities_written: int = 0
    errors: list[str] = Field(default_factory=list)


class GapListResponse(BaseModel):
    gaps: list[GapEntry]


class GapResolveRequest(BaseModel):
    resolution: Literal["skip", "retry"]


class GapResolveResponse(BaseModel):
    gap_id: uuid.UUID
    status: str
    resolution: str


class StrategyYieldResponse(BaseModel):
    strategies: list[StrategyYield]


class EntityDetail(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    location: EntityLocation | None = None
    best_tier: int
    corroboration_count: int
    facts: list[EntityFact] = Field(default_factory=list)


class EntityLocation(BaseModel):
    lat: float | None = None
    lng: float | None = None
    grid_cell: str


class EntityFact(BaseModel):
    insight: str
    confidence: float
    source_tier: int
    is_safety_relevant: bool
    source_url: str


class EntitySearchResult(BaseModel):
    entity_id: uuid.UUID
    canonical_name: str
    relevance_score: float
    snippet: str


class EntitySearchResponse(BaseModel):
    results: list[EntitySearchResult]


class GraphNode(BaseModel):
    id: uuid.UUID
    canonical_name: str
    entity_type: str


class GraphRelationship(BaseModel):
    source: uuid.UUID
    target: uuid.UUID
    type: str
    distance_m: float | None = None
    source_url: str | None = None


class NearbyResponse(BaseModel):
    nodes: list[GraphNode]
    relationships: list[GraphRelationship]


class RuleItem(BaseModel):
    insight: str
    source_tier: int
    corroboration_count: int
    last_verified: datetime


class RulesResponse(BaseModel):
    rules: list[RuleItem]


class FeedbackRequest(BaseModel):
    entity_id: uuid.UUID
    correction_text: str
    submitted_by_session: str


class FeedbackResponse(BaseModel):
    feedback_id: uuid.UUID
    status: str = "queued_for_review"


# --- Moderation (human-in-the-loop) ---


class FactItem(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    raw_capture_id: uuid.UUID
    canonical_entity_id: uuid.UUID | None = None
    entity_name_raw: str
    entity_category: str
    contextual_insight: str
    confidence_score: float
    source_tier: int
    is_safety_relevant: bool
    resolution_status: str
    extracted_at: datetime


class FactListResponse(BaseModel):
    facts: list[FactItem]


class FactModerationRequest(BaseModel):
    action: Literal["approve", "reject"]


class FactModerationResponse(BaseModel):
    fact_id: uuid.UUID
    resolution_status: str
