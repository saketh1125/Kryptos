from __future__ import annotations

import uuid

import httpx
import structlog

from ghkge.harvesters.base import BaseHarvester
from ghkge.models.schemas import RawCaptureData

logger = structlog.get_logger()


class APIHarvester(BaseHarvester):
    """Structured API harvester for OSM Overpass, Reddit, gov portals."""

    OVERPASS_URL = "https://overpass-api.de/api/interpreter"

    async def fetch(self, url: str, run_id: uuid.UUID, **kwargs: object) -> RawCaptureData | None:
        strategy = kwargs.get("strategy", "api_query")
        entity_type = kwargs.get("entity_type", "")
        compliance_result = await self._check_compliance(
            url, strategy=strategy, entity_type=entity_type
        )
        if not compliance_result.allowed:
            logger.info("api_harvester.blocked", url=url, reason=compliance_result.reason)
            return None

        try:
            content = await self._fetch_api(url, **kwargs)
            if content is None:
                return None

            return RawCaptureData(
                source_url=url,
                source_type="api_json",
                domain=self.extract_domain(url),
                raw_content=content,
                content_hash=self.compute_content_hash(content),
                strategy_used=kwargs.get("strategy", "api_query"),
                run_id=run_id,
            )
        except Exception:
            logger.error("api_harvester.fetch_error", url=url, exc_info=True)
            return None

    async def _fetch_api(self, url: str, **kwargs: object) -> str | None:
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
        """Query Overpass API directly with aql query."""
        compliance_result = await self._check_compliance(
            self.OVERPASS_URL, strategy="osm_api", entity_type=entity_type
        )
        if not compliance_result.allowed:
            return None

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.OVERPASS_URL,
                    data={"data": query},
                    headers={"User-Agent": "ghkge/1.0"},
                )
                if resp.status_code == 200:
                    content = resp.text
                    return RawCaptureData(
                        source_url=self.OVERPASS_URL,
                        source_type="api_json",
                        domain="openstreetmap.org",
                        raw_content=content,
                        content_hash=self.compute_content_hash(content),
                        strategy_used="osm_api",
                        run_id=run_id,
                    )
                logger.warning("overpass.query_error", status=resp.status_code)
                return None
        except Exception:
            logger.error("overpass.fetch_error", exc_info=True)
            return None
