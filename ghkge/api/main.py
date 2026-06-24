from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ghkge.api.routes import knowledge, orchestration
from ghkge.database.connection import close_db, init_db

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    logger.info("app.starting")
    await init_db()
    logger.info("app.started")
    yield
    await close_db()
    logger.info("app.stopped")


app = FastAPI(
    title="GHKGE - Generalized Hyperlocal Knowledge Graph Engine",
    description=(
        "Domain-agnostic, strategy-adaptive hyperlocal knowledge"
        " acquisition and serving system."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(orchestration.router)
app.include_router(knowledge.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
