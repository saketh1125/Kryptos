#!/usr/bin/env python3
"""GHKGE Entry Point - Run the FastAPI application."""

import uvicorn

from ghkge.config.settings import settings


def main() -> None:
    uvicorn.run(
        "ghkge.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
