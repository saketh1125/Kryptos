from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod

import structlog

from ghkge.compliance.engine import ComplianceEngine
from ghkge.models.schemas import ComplianceResult, RawCaptureData

logger = structlog.get_logger()


class BaseHarvester(ABC):
    """Base class for all harvesters. All network access goes through ComplianceEngine."""

    def __init__(self, compliance: ComplianceEngine) -> None:
        self.compliance = compliance

    @abstractmethod
    async def fetch(self, url: str, run_id: uuid.UUID, **kwargs: object) -> RawCaptureData | None:
        """Fetch and return raw content. Returns None if compliance blocks or fetch fails."""
        ...

    @staticmethod
    def compute_content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def extract_domain(url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    async def _check_compliance(
        self, url: str, strategy: str, entity_type: str
    ) -> ComplianceResult:
        return await self.compliance.check(url, strategy=strategy, entity_type=entity_type)
