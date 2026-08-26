from __future__ import annotations

import uuid

import httpx
import structlog

from ghkge.config.settings import settings
from ghkge.harvesters.base import BaseHarvester
from ghkge.models.schemas import ApprovedTarget, RawCaptureData

logger = structlog.get_logger()


class APIHarvester(BaseHarvester):
    """Structured API harvester for OSM Overpass, gov portals, open APIs."""

    OVERPASS_URL = "https://overpass-api.de/api/interpreter"

    async def fetch(self, target: ApprovedTarget, run_id: uuid.UUID) -> RawCaptureData | None:
        compliance_result = await self._check_compliance(target)
        if not compliance_result.allowed:
            logger.info(
                "api_harvester.blocked", url=target.url, reason=compliance_result.reason
            )
            return None

        try:
            content = await self._fetch_api(target.url)
            if content is None:
                return None

            return RawCaptureData(
                source_url=target.url,
                source_type="api_json",
                domain=self.extract_domain(target.url),
                raw_content=content,
                content_hash=self.compute_content_hash(content),
                strategy_used=target.strategy,
                run_id=run_id,
            )
        except Exception:
            logger.error("api_harvester.fetch_error", url=target.url, exc_info=True)
            return None

    async def _fetch_api(self, url: str) -> str | None:
        """Generic API fetch."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.text
                logger.warning("api_harvester.http_error", url=url, status=resp.status_code)
                return None
        except Exception:
            logger.error("api_harvester.request_error", url=url, exc_info=True)
            return None

    async def query_overpass(
        self,
        query: str,
        run_id: uuid.UUID,
        entity_type: str = "",
    ) -> RawCaptureData | None:
        """Query Overpass API with an Overpass QL query."""
        approved = await self.compliance.approve(
            self.OVERPASS_URL, strategy="osm_api", entity_type=entity_type
        )
        if approved is None:
            logger.info("overpass.blocked_by_compliance")
            return None

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.OVERPASS_URL,
                    data={"data": query},
                    headers={"User-Agent": settings.user_agent},
                )
                if resp.status_code == 200:
                    content = resp.text
                    return RawCaptureData(
                        source_url=self.OVERPASS_URL,
                        source_type="api_json",
                        domain="openstreetmap.org",
                        raw_content=content,
                        content_hash=self.compute_content_hash(content),
                        strategy_used=approved.strategy,
                        run_id=run_id,
                    )
                logger.warning("overpass.query_error", status=resp.status_code)
                return None
        except Exception:
            logger.error("overpass.fetch_error", exc_info=True)
            return None
