from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger()


class MetricsCollector:
    """Collects and logs pipeline metrics in structured JSON format."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._timers: dict[str, float] = {}

    def increment(self, name: str, value: int = 1, **tags: str) -> None:
        key = self._make_key(name, tags)
        self._counters[key] = self._counters.get(key, 0) + value

    def gauge(self, name: str, value: float, **tags: str) -> None:
        key = self._make_key(name, tags)
        self._gauges[key] = value

    def timer_start(self, name: str) -> float:
        return time.monotonic()

    def timer_end(self, name: str, start: float, **tags: str) -> float:
        elapsed = time.monotonic() - start
        key = self._make_key(name, tags)
        self._timers[key] = elapsed
        return elapsed

    def flush(self) -> dict[str, Any]:
        metrics = {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "timers": dict(self._timers),
        }
        logger.info("metrics.flush", **metrics)
        return metrics

    @staticmethod
    def _make_key(name: str, tags: dict[str, str]) -> str:
        if tags:
            tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
            return f"{name}{{{tag_str}}}"
        return name


metrics = MetricsCollector()


class StructuredLogger:
    """Structured JSON logger for pipeline events."""

    @staticmethod
    def log_compliance_check(
        url: str,
        allowed: bool,
        reason: str = "",
        domain: str = "",
    ) -> None:
        logger.info(
            "compliance.check",
            url=url,
            allowed=allowed,
            reason=reason,
            domain=domain,
        )

    @staticmethod
    def log_extraction_metrics(
        source_url: str,
        chunks: int,
        facts: int,
        macro_filtered: int,
    ) -> None:
        logger.info(
            "extraction.metrics",
            source_url=source_url,
            chunks_processed=chunks,
            facts_extracted=facts,
            macro_filtered=macro_filtered,
        )

    @staticmethod
    def log_entity_resolution(
        incoming_name: str,
        matched: bool,
        matched_name: str = "",
        score: float = 0.0,
    ) -> None:
        logger.info(
            "entity.resolution",
            incoming=incoming_name,
            matched=matched,
            matched_name=matched_name,
            score=score,
        )

    @staticmethod
    def log_run_status(
        run_id: str,
        status: str,
        facts: int = 0,
        entities: int = 0,
    ) -> None:
        logger.info(
            "run.status",
            run_id=run_id,
            status=status,
            facts_extracted=facts,
            entities_written=entities,
        )
