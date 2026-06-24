from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx
import robotparser
import structlog

from ghkge.compliance.rate_limiter import PostgresRateLimiter
from ghkge.config.settings import settings
from ghkge.models.schemas import ComplianceResult

logger = structlog.get_logger()

# Global denied regex patterns
GLOBAL_DENIED_PATTERNS = [
    r"/admin",
    r"/login",
    r"/api/v\d+/private",
    r"/wp-admin",
    r"\?.*password=",
    r"/cgi-bin",
]

# Platforms hostile to scraping (must use official API)
BLOCKED_PLATFORMS = [
    "linkedin.com",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
]


class RobotsCache:
    """Caches robots.txt parsers per domain with TTL."""

    def __init__(self, ttl_hours: int = 24) -> None:
        self.ttl_hours = ttl_hours
        self._cache: dict[str, tuple[float, robotparser.RobotFileParser]] = {}

    async def get_or_fetch(self, url: str) -> robotparser.RobotFileParser:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        now = time.time()

        if base_url in self._cache:
            cached_time, parser = self._cache[base_url]
            if now - cached_time < self.ttl_hours * 3600:
                return parser

        rp = robotparser.RobotFileParser()
        robots_url = f"{base_url}/robots.txt"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    robots_url, headers={"User-Agent": settings.user_agent}
                )
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    # If robots.txt unavailable, block by default
                    rp.parse(["User-agent: *", "Disallow: /"])
                    logger.warning(
                        "robots.fetch_failed",
                        url=robots_url,
                        status=resp.status_code,
                    )
        except Exception:
            rp.parse(["User-agent: *", "Disallow: /"])
            logger.warning("robots.fetch_error", url=robots_url, exc_info=True)

        self._cache[base_url] = (now, rp)
        return rp


class DenylistChecker:
    """Checks URLs against global and platform denylists."""

    def __init__(self) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in GLOBAL_DENIED_PATTERNS]
        self._platforms = [p.lower() for p in BLOCKED_PLATFORMS]

    def matches(self, url: str) -> bool:
        for pattern in self._patterns:
            if pattern.search(url):
                return True
        domain = urlparse(url).netloc.lower()
        return any(platform in domain for platform in self._platforms)

    def requires_official_api(self, url: str) -> bool:
        domain = urlparse(url).netloc.lower()
        return any(p in domain for p in self._platforms)


class ComplianceEngine:
    """
    Deterministic compliance gate. Every URL must pass through this before fetching.
    Returns ApprovedTarget on success, ComplianceResult with reason on failure.
    """

    def __init__(self, rate_limiter: PostgresRateLimiter | None = None) -> None:
        self.robots_cache = RobotsCache(ttl_hours=settings.robots_cache_ttl_hours)
        self.denylist = DenylistChecker()
        self.rate_limiter = rate_limiter

    async def check(
        self,
        url: str,
        strategy: str = "",
        entity_type: str = "",
    ) -> ComplianceResult:
        # 1. robots.txt check
        robots = await self.robots_cache.get_or_fetch(url)
        if not robots.can_fetch(settings.user_agent, url):
            logger.info("compliance.robots_disallow", url=url)
            return ComplianceResult(allowed=False, reason="robots_disallow")

        # 2. denylist check
        if self.denylist.matches(url):
            logger.info("compliance.denylisted", url=url)
            return ComplianceResult(allowed=False, reason="denylisted_domain")

        # 3. rate gate
        if self.rate_limiter is not None:
            allowed = await self.rate_limiter.acquire(url, settings.rate_limit_interval_s)
            if not allowed:
                logger.info("compliance.rate_limited", url=url)
                return ComplianceResult(
                    allowed=False,
                    reason="rate_limited",
                    retry_after=settings.rate_limit_interval_s,
                )

        # 4. platform policy
        if self.denylist.requires_official_api(url):
            logger.info("compliance.api_only_platform", url=url)
            return ComplianceResult(allowed=False, reason="api_only_platform")

        logger.debug("compliance.approved", url=url, strategy=strategy)
        return ComplianceResult(allowed=True)
