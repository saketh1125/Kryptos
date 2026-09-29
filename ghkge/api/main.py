from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ghkge.api.auth import admin_key_configured
from ghkge.api.routes import knowledge, orchestration
from ghkge.config.settings import settings
from ghkge.database.connection import close_db, init_db, schema_is_current
from ghkge.monitoring import configure_logging
from ghkge.orchestrator.scheduler import start_background, stop_background

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: DB, background workers, scheduler.

    Startup problems are surfaced rather than suppressed. Silently swallowing
    them produced an app that answered /health while doing nothing at all
    (D-08): a missing schema, or a scheduler that would not start, is exactly
    the condition an operator needs told about. The app still starts, so that
    /health and the logs remain reachable for diagnosis.
    """
    configure_logging(json_output=settings.json_logging)
    logger.info("app.starting")
    await init_db()

    problems: list[str] = []
    try:
        if not await schema_is_current():
            problems.append(
                "database schema is missing or incomplete -- run "
                "`alembic upgrade head` (or psql -f sql/schema.sql)"
            )
    except Exception as exc:  # pragma: no cover - defensive
        problems.append(f"schema probe failed: {exc}")

    try:
        await start_background()
    except Exception as exc:
        problems.append(f"workers/scheduler failed to start: {exc}")
        logger.error("app.background_start_failed", error=str(exc), exc_info=True)

    if not admin_key_configured():
        logger.warning(
            "app.admin_api_unprotected",
            detail=(
                "GHKGE_ADMIN_API_KEY is unset, so /admin/v1 is open to anyone "
                "who can reach this process. Set it for any shared deployment."
            ),
        )

    if problems:
        app.state.startup_problems = problems
        for problem in problems:
            logger.error("app.startup_degraded", problem=problem)
    else:
        app.state.startup_problems = []

    logger.info("app.started", degraded=bool(problems))
    yield

    try:
        await stop_background()
    except Exception:
        logger.error("app.background_stop_failed", exc_info=True)
    await close_db()
    logger.info("app.stopped")


app = FastAPI(
    title="GHKGE - Generalized Hyperlocal Knowledge Graph Engine",
    description=(
        "Domain-agnostic, strategy-adaptive hyperlocal knowledge"
        " acquisition and serving system."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # An explicit allowlist. Wildcard origins combined with credentials is a
    # misconfiguration: browsers reject it, and `*` would admit any site to
    # the admin API (D-07).
    allow_origins=settings.cors_allow_origins,
    allow_credentials=bool(settings.cors_allow_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(knowledge.router)
app.include_router(orchestration.router)


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    """Liveness plus startup problems.

    A degraded app still returns 200 so that the process stays reachable for
    diagnosis, but `ready` is false and `problems` says why. Anything
    monitoring this should gate on `ready`, not on the status code.
    """
    problems = getattr(request.app.state, "startup_problems", [])
    return {
        "status": "degraded" if problems else "ok",
        "ready": not problems,
        "problems": problems,
    }
