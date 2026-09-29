"""Observability: structlog configuration.

The codebase logs structured events directly via ``structlog.get_logger()``;
this module only decides *how* those events are rendered. Called once at app
startup.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(json_output: bool = False) -> None:
    """Configure structlog for the app.

    Args:
        json_output: Emit newline-delimited JSON (for log aggregators) instead of
            the colourised console renderer. Defaults to console for local work.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
