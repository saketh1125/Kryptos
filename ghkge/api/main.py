from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ghkge.api.routes import knowledge, orchestration
from ghkge.database.connection import close_db, init_db
from ghkge.orchestrator.scheduler import start_background, stop_background

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: DB, background workers, scheduler."""
    logger.info("app.starting")
    await init_db()
    with contextlib.suppress(Exception):
        await start_background()
    logger.info("app.started")
    yield
    with contextlib.suppress(Exception):
        await stop_background()
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(knowledge.router)
app.include_router(orchestration.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
