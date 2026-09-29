"""Tests for the compliance engine (mock-transport, no network)."""

from __future__ import annotations

import httpx
import pytest

from ghkge.compliance.engine import (
    BLOCKED_PLATFORMS,
    ComplianceEngine,
    DenylistChecker,
    RobotsCache,
)

ALLOW_ALL = "User-agent: *\nDisallow:\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def _transport(robots_text: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=robots_text)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


class TestDenylist:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/admin/panel",
            "https://example.com/login",
            "https://example.com/wp-admin/edit.php",
            "https://example.com/cgi-bin/tool",
            "https://example.com/x?y=1&password=hunter2",
            "https://example.com/api/v2/private/data",
        ],
    )
    def test_denied_patterns(self, url: str):
        assert DenylistChecker().matches(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/news/varanasi",
            "https://en.wikipedia.org/wiki/Varanasi",
            "https://overpass-api.de/api/interpreter",
        ],
    )
    def test_allowed_urls(self, url: str):
        assert DenylistChecker().matches(url) is False

    def test_blocked_platforms_denied(self):
        checker = DenylistChecker()
        for platform in BLOCKED_PLATFORMS:
            assert checker.matches(f"https://www.{platform}/user/page") is True
            assert checker.requires_official_api(f"https://www.{platform}/x") is True

    def test_requires_official_api_false_for_normal_site(self):
        assert DenylistChecker().requires_official_api("https://example.com") is False


class TestRobotsCache:
    async def test_parses_robots(self):
        cache = RobotsCache(transport=_transport(ALLOW_ALL))
        rp = await cache.get_or_fetch("https://example.com/page")
        assert rp.can_fetch("any-agent", "https://example.com/page") is True

    async def test_disallow_blocks(self):
        cache = RobotsCache(transport=_transport(DISALLOW_ALL))
        rp = await cache.get_or_fetch("https://example.com/page")
        assert rp.can_fetch("any-agent", "https://example.com/page") is False

    async def test_fail_closed_on_404(self):
        """No robots.txt => block, never assume permission."""
        cache = RobotsCache(transport=_transport("", status=404))
        rp = await cache.get_or_fetch("https://example.com/page")
        assert rp.can_fetch("any-agent", "https://example.com/page") is False

    async def test_fail_closed_on_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        cache = RobotsCache(transport=httpx.MockTransport(handler))
        rp = await cache.get_or_fetch("https://example.com/page")
        assert rp.can_fetch("any-agent", "https://example.com/page") is False

    async def test_cache_is_reused(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=ALLOW_ALL)

        cache = RobotsCache(transport=httpx.MockTransport(handler))
        await cache.get_or_fetch("https://example.com/a")
        await cache.get_or_fetch("https://example.com/b")
        assert calls == 1


class FakeRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, float]] = []

    async def acquire(self, url: str, limit_interval_s: float = 2.0) -> bool:
        self.calls.append((url, limit_interval_s))
        return self.allow


class TestComplianceEngine:
    async def _engine(self, robots: str = ALLOW_ALL, allow: bool = True):
        engine = ComplianceEngine(rate_limiter=FakeRateLimiter(allow))
        engine.robots_cache = RobotsCache(transport=_transport(robots))
        return engine

    async def test_allows_public_page(self):
        engine = await self._engine()
        result = await engine.check("https://example.com/varanasi", "web_crawls", "landmark")
        assert result.allowed is True
        assert result.reason == ""

    async def test_robots_disallow(self):
        engine = await self._engine(robots=DISALLOW_ALL)
        result = await engine.check("https://example.com/x")
        assert result.allowed is False
        assert result.reason == "robots_disallow"

    async def test_denylist(self):
        engine = await self._engine()
        result = await engine.check("https://example.com/admin/x")
        assert result.reason == "denylisted_domain"

    async def test_api_only_platform(self):
        engine = await self._engine()
        result = await engine.check("https://www.instagram.com/p/abc")
        assert result.reason == "api_only_platform"

    async def test_rate_limited(self):
        engine = await self._engine(allow=False)
        result = await engine.check("https://example.com/x")
        assert result.allowed is False
        assert result.reason == "rate_limited"
        assert result.retry_after is not None

    async def test_denied_url_never_consumes_rate_slot(self):
        """A blocked URL must not burn a rate-limit token."""
        limiter = FakeRateLimiter(allow=True)
        engine = ComplianceEngine(rate_limiter=limiter)
        engine.robots_cache = RobotsCache(transport=_transport(ALLOW_ALL))
        await engine.check("https://www.linkedin.com/in/someone")
        await engine.check("https://example.com/admin/secret")
        assert limiter.calls == []

    async def test_approved_url_consumes_rate_slot(self):
        limiter = FakeRateLimiter(allow=True)
        engine = ComplianceEngine(rate_limiter=limiter)
        engine.robots_cache = RobotsCache(transport=_transport(ALLOW_ALL))
        await engine.check("https://example.com/ok")
        assert len(limiter.calls) == 1

    async def test_approve_returns_target(self):
        engine = await self._engine()
        target = await engine.approve(
            "https://example.com/varanasi", "web_crawls", "landmark"
        )
        assert target is not None
        assert target.url == "https://example.com/varanasi"
        assert target.domain == "example.com"
        assert target.strategy == "web_crawls"
        assert target.entity_type == "landmark"

    async def test_approve_returns_none_when_blocked(self):
        engine = await self._engine(robots=DISALLOW_ALL)
        target = await engine.approve("https://example.com/x", "web_crawls", "landmark")
        assert target is None

    async def test_no_rate_limiter_is_fine(self):
        engine = ComplianceEngine(rate_limiter=None)
        engine.robots_cache = RobotsCache(transport=_transport(ALLOW_ALL))
        result = await engine.check("https://example.com/x")
        assert result.allowed is True
