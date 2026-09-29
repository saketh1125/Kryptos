from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime

import asyncpg
import structlog

logger = structlog.get_logger()


class PostgresRateLimiter:
    """Minimum-interval gate backed by Postgres to survive container restarts.

    Enforces at least ``limit_interval_s`` seconds between fetches per domain.
    State lives in ``domain_rate_limit_state`` so restarts don't reset politeness.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.pool = db_pool

    async def acquire(self, url: str, limit_interval_s: float = 2.0) -> bool:
        domain = urllib.parse.urlparse(url).netloc
        now = datetime.now(UTC)

        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    SELECT last_hit_at
                    FROM domain_rate_limit_state
                    WHERE domain = $1
                    FOR UPDATE
                    """,
                domain,
            )

            if row:
                last_hit = row["last_hit_at"]
                # Some drivers hand back naive datetimes; normalise so the
                # subtraction below cannot raise.
                if last_hit.tzinfo is None:
                    last_hit = last_hit.replace(tzinfo=UTC)
                time_since_last = (now - last_hit).total_seconds()
                if time_since_last < limit_interval_s:
                    logger.debug(
                        "rate_limiter.rejected",
                        domain=domain,
                        wait_s=limit_interval_s - time_since_last,
                    )
                    return False

            await conn.execute(
                """
                    INSERT INTO domain_rate_limit_state (domain, last_hit_at, tokens_remaining)
                    VALUES ($1, $2, 1.0)
                    ON CONFLICT (domain)
                    DO UPDATE SET last_hit_at = $2
                    """,
                domain,
                now,
            )
            logger.debug("rate_limiter.acquired", domain=domain)
            return True
