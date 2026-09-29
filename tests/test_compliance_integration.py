"""Integration test: the REAL compliance stack driving a REAL harvester.

This is the test whose absence hid D-02. Every other integration test mocks
the compliance gate wholesale, which makes a double rate-limit acquisition
unobservable: the mock has no rate limiter, so it never denies anything.

Here the actual ComplianceEngine, the actual robots.txt parser, the actual
PostgresRateLimiter (against a real table) and the actual WebHarvester are
wired together, and only the network is intercepted with an HTTP transport.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import ghkge.database.connection as connection
from ghkge.compliance.engine import ComplianceEngine
from ghkge.compliance.rate_limiter import PostgresRateLimiter
from ghkge.config.settings import settings
from ghkge.database.models import DomainRateLimitState
from ghkge.harvesters.web import WebHarvester
from ghkge.models.schemas import ApprovedTarget

ALLOW_ALL = "User-agent: *\nDisallow:\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
PAGE = "<html><body><h1>Darbhanga Ghat</h1><p>Open 4am to midnight.</p></body></html>"

DOMAIN_URL = "https://example.gov.in/tourist-place"


def _transport(robots: str = ALLOW_ALL, robots_status: int = 200):
    """Intercept robots.txt and the page itself; no socket is opened."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots)
        return httpx.Response(200, text=PAGE)

    return httpx.MockTransport(handler)


class FakeConn:
    """Minimal asyncpg connection over a real table, exercising the real SQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query: str, *args):
        domain = args[0]
        stmt = select(DomainRateLimitState).where(DomainRateLimitState.domain == domain)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return {"last_hit_at": row.last_hit_at} if row else None

    async def execute(self, query: str, *args):
        domain, now = args[0], args[1]
        stmt = select(DomainRateLimitState).where(DomainRateLimitState.domain == domain)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            self._session.add(
                DomainRateLimitState(domain=domain, last_hit_at=now, tokens_remaining=1.0)
            )
        else:
            row.last_hit_at = now
        # The real limiter runs inside conn.transaction(); commit so the state
        # survives to the next acquire() on a different session.
        await self._session.commit()


class FakePool:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def acquire(self):
        # A fresh session per call, matching asyncpg pool semantics.
        outer = self

        class _Acquire:
            async def __aenter__(self):
                self._session = outer._session_factory()
                return FakeConn(self._session)

            async def __aexit__(self, *exc):
                await self._session.close()
                return False

        return _Acquire()


@pytest_asyncio.fixture
async def rate_limiter(monkeypatch):
    """A real PostgresRateLimiter over a real table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    @compiles(PGUUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):
        return "CHAR(36)"

    from pgvector.sqlalchemy import Vector

    @compiles(Vector, "sqlite")
    def _vector_sqlite(type_, compiler, **kw):
        return "TEXT"

    async with engine.begin() as conn:
        await conn.run_sync(connection.Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    pool = FakePool(factory)
    limiter = PostgresRateLimiter(pool)  # type: ignore[arg-type]
    limiter._session_factory = factory  # type: ignore[attr-defined]
    yield limiter
    await engine.dispose()


@pytest.fixture
def interval(monkeypatch):
    """Use a long interval so the double-charge is unambiguous."""
    monkeypatch.setattr(settings, "rate_limit_interval_s", 30.0)
    monkeypatch.setattr(settings, "compliance_revalidate_window_s", 5.0)


class TestSingleRateLimitAcquisition:
    """D-02: worker approves, harvester re-checks; the slot is charged once."""

    async def test_planner_then_harvester_succeeds(self, rate_limiter, interval, monkeypatch):
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        # 1. Planner approves (this charges the domain slot).
        target = await engine.approve(DOMAIN_URL, "official_portals", "landmark")
        assert target is not None
        assert target.validated_at is not None

        # 2. Harvester re-checks and fetches. Must NOT be denied.
        capture = await harvester.fetch(target, run_id=_uuid())
        assert capture is not None, "D-02: harvester re-check must not re-charge the slot"
        assert "Darbhanga Ghat" in capture.raw_content

    async def test_two_sequential_fetches_to_same_domain_are_denied(
        self, rate_limiter, interval, monkeypatch
    ):
        """The gate still works: a genuinely second fetch is rate-limited."""
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        first = await engine.approve(DOMAIN_URL, "official_portals", "landmark")
        assert first is not None
        assert await harvester.fetch(first, run_id=_uuid()) is not None

        # A second, independently approved target hits the 30s interval.
        second = await engine.approve(
            "https://example.gov.in/other-page", "official_portals", "landmark"
        )
        assert second is None, "a real second fetch must be rate-limited"

    async def test_stale_stamp_recharges(self, rate_limiter, interval, monkeypatch):
        """A target validated too long ago pays the gate again."""
        from datetime import UTC, datetime, timedelta

        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        first = await engine.approve(DOMAIN_URL, "official_portals", "landmark")
        assert first is not None
        assert await harvester.fetch(first, run_id=_uuid()) is not None

        # Same URL, but the stamp is old enough that the slot is re-charged.
        stale = ApprovedTarget(
            url=DOMAIN_URL,
            domain="example.gov.in",
            strategy="official_portals",
            entity_type="landmark",
            validated_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        assert await harvester.fetch(stale, run_id=_uuid()) is None

    async def test_unstamped_target_is_charged(self, rate_limiter, interval, monkeypatch):
        """A hand-built target with no stamp pays the gate, so it can be denied."""
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        first = await engine.approve(DOMAIN_URL, "official_portals", "landmark")
        assert first is not None
        assert await harvester.fetch(first, run_id=_uuid()) is not None

        unstamped = ApprovedTarget(
            url=DOMAIN_URL,
            domain="example.gov.in",
            strategy="official_portals",
            entity_type="landmark",
        )
        assert await harvester.fetch(unstamped, run_id=_uuid()) is None


class TestStatelessRulesAlwaysRechecked:
    """The stamp waives only the rate slot, never a real policy check."""

    async def test_robots_still_blocks_a_freshly_stamped_target(
        self, rate_limiter, interval, monkeypatch
    ):
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport(DISALLOW_ALL)
        harvester = WebHarvester(engine, transport=_transport())

        assert await engine.approve(DOMAIN_URL, "web_crawls", "landmark") is None
        assert await harvester.fetch(
            ApprovedTarget(
                url=DOMAIN_URL,
                domain="example.gov.in",
                strategy="web_crawls",
                entity_type="landmark",
                validated_at=_now(),
            ),
            run_id=_uuid(),
        ) is None

    async def test_denylist_still_blocks_a_freshly_stamped_target(
        self, rate_limiter, interval, monkeypatch
    ):
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        blocked = "https://example.gov.in/admin/secret"
        target = ApprovedTarget(
            url=blocked,
            domain="example.gov.in",
            strategy="web_crawls",
            entity_type="landmark",
            validated_at=_now(),
        )
        assert await harvester.fetch(target, run_id=_uuid()) is None

    async def test_blocked_platform_still_blocked(self, rate_limiter, interval, monkeypatch):
        engine = ComplianceEngine(rate_limiter=rate_limiter)
        engine.robots_cache._transport = _transport()
        harvester = WebHarvester(engine, transport=_transport())

        target = ApprovedTarget(
            url="https://www.instagram.com/p/abc",
            domain="www.instagram.com",
            strategy="social_media_crawls",
            entity_type="landmark",
            validated_at=_now(),
        )
        assert await harvester.fetch(target, run_id=_uuid()) is None


class TestFreshnessWindow:
    def test_window_boundaries(self):
        from datetime import UTC, datetime, timedelta

        target = ApprovedTarget(
            url="https://x", domain="x", strategy="s", entity_type="t", validated_at=_now()
        )
        assert target.is_freshly_validated(5.0) is True

        old = ApprovedTarget(
            url="https://x",
            domain="x",
            strategy="s",
            entity_type="t",
            validated_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        assert old.is_freshly_validated(5.0) is False

        none = ApprovedTarget(url="https://x", domain="x", strategy="s", entity_type="t")
        assert none.is_freshly_validated(5.0) is False

    def test_future_stamp_is_not_fresh(self):
        from datetime import UTC, datetime, timedelta

        future = ApprovedTarget(
            url="https://x",
            domain="x",
            strategy="s",
            entity_type="t",
            validated_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        # A clock skew that lands in the future must not grant a free pass.
        assert future.is_freshly_validated(5.0) is False


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _uuid():
    import uuid

    return uuid.uuid4()
