from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod

import structlog

from ghkge.compliance.engine import ComplianceEngine
from ghkge.models.schemas import ApprovedTarget, ComplianceResult, RawCaptureData

logger = structlog.get_logger()


class BaseHarvester(ABC):
    """Base class for all harvesters.

    Guardrail (AGENTS.md #1): harvesters never accept raw URL strings.
    All network access is keyed off a compliance-issued ApprovedTarget and
    re-verified through ComplianceEngine immediately before any I/O.
    """

    def __init__(self, compliance: ComplianceEngine) -> None:
        self.compliance = compliance

    @abstractmethod
    async def fetch(self, target: ApprovedTarget, run_id: uuid.UUID) -> RawCaptureData | None:
        """Fetch approved target content. Returns None if blocked or fetch fails."""
        ...

    @staticmethod
    def compute_content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def extract_domain(url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    async def _check_compliance(self, target: ApprovedTarget) -> ComplianceResult:
        return await self.compliance.check(
            target.url, strategy=target.strategy, entity_type=target.entity_type
        )
