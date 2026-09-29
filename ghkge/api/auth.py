"""Authentication for the internal Orchestration API.

The Knowledge API (/api/v1) is public by design -- it serves a consumer app.
The Orchestration API (/admin/v1) mutates state: it queues crawls, resolves
gaps, and approves or rejects facts that the public API then serves. Leaving
it unauthenticated means anyone who can reach the process can approve a
safety-relevant fact, or drive the crawler (D-07).

KRY-PRD-001 §5 places full AuthN/AuthZ out of v1 scope, and that is respected:
this is a shared-secret gate, not a user model. It is enough to keep an
internet-reachable deployment from being writable by anyone.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import Header, HTTPException, status

from ghkge.config.settings import settings

logger = structlog.get_logger()

AUTH_HEADER = "X-Admin-Key"


def admin_key_configured() -> bool:
    """True when an admin key is set. Absent means the API is left open."""
    return bool(settings.admin_api_key)


async def require_admin_key(
    x_admin_key: str | None = Header(default=None, alias=AUTH_HEADER),
) -> None:
    """Dependency guarding every /admin/v1 route.

    When GHKGE_ADMIN_API_KEY is unset the check is skipped, so local
    development and the test suite keep working. A deployment that is
    reachable from outside the host should set it; the lifespan logs a
    warning when it does not.
    """
    expected = settings.admin_api_key
    if not expected:
        return

    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        logger.warning(
            "auth.admin_key_rejected",
            path_hint="orchestration api",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {AUTH_HEADER}",
        )
