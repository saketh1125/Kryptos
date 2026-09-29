from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog

from ghkge.compliance.engine import ComplianceEngine
from ghkge.config.settings import settings
from ghkge.harvesters.base import BaseHarvester
from ghkge.models.schemas import ApprovedTarget, RawCaptureData

logger = structlog.get_logger()

# Semaphore for concurrent browser fetches
_browser_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _browser_semaphore
    if _browser_semaphore is None:
        _browser_semaphore = asyncio.Semaphore(settings.max_concurrent_browsers)
    return _browser_semaphore


class WebHarvester(BaseHarvester):
    """HTML/Web harvester using httpx (crawl4ai optional failover, future).

    ``transport`` exists for tests: it is the seam the integration suite uses
    to serve robots.txt and pages without opening a socket.
    """

    def __init__(
        self, compliance: ComplianceEngine,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(compliance)
        self._transport = transport

    async def fetch(self, target: ApprovedTarget, run_id: uuid.UUID) -> RawCaptureData | None:
        compliance_result = await self._check_compliance(target)
        if not compliance_result.allowed:
            logger.info(
                "web_harvester.blocked", url=target.url, reason=compliance_result.reason
            )
            return None

        sem = _get_semaphore()
        async with sem:
            try:
                content = await self._fetch_html(target.url)
                if content is None:
                    return None

                return RawCaptureData(
                    source_url=target.url,
                    source_type="html",
                    domain=self.extract_domain(target.url),
                    raw_content=content,
                    content_hash=self.compute_content_hash(content),
                    strategy_used=target.strategy,
                    run_id=run_id,
                )
            except Exception:
                logger.error("web_harvester.fetch_error", url=target.url, exc_info=True)
                return None

    async def _fetch_html(self, url: str) -> str | None:
        """Fetch HTML content and convert to markdown-like text."""
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": settings.user_agent},
                transport=self._transport,
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return self._html_to_markdown(resp.text)
                logger.warning("web_harvester.http_error", url=url, status=resp.status_code)
                return None
        except httpx.TimeoutException:
            logger.warning("web_harvester.timeout", url=url)
            return None

    def _html_to_markdown(self, html: str) -> str:
        """Basic HTML to markdown conversion."""
        import re

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"# \1\n", text)
        text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"## \1\n", text)
        text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"### \1\n", text)
        text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL)
        text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
