from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import structlog

from ghkge.config.settings import settings
from ghkge.harvesters.base import BaseHarvester
from ghkge.models.schemas import ApprovedTarget, RawCaptureData

logger = structlog.get_logger()

_doc_executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_doc_parsers)


class DocumentHarvester(BaseHarvester):
    """Document harvester for PDF/DOCX using docling."""

    async def fetch(self, target: ApprovedTarget, run_id: uuid.UUID) -> RawCaptureData | None:
        compliance_result = await self._check_compliance(target)
        if not compliance_result.allowed:
            logger.info(
                "doc_harvester.blocked", url=target.url, reason=compliance_result.reason
            )
            return None

        try:
            content = await asyncio.get_event_loop().run_in_executor(
                _doc_executor, self._parse_document, target.url
            )
            if content is None:
                return None

            return RawCaptureData(
                source_url=target.url,
                source_type="pdf",
                domain=self.extract_domain(target.url),
                raw_content=content,
                content_hash=self.compute_content_hash(content),
                strategy_used=target.strategy,
                run_id=run_id,
            )
        except Exception:
            logger.error("doc_harvester.fetch_error", url=target.url, exc_info=True)
            return None

    def _parse_document(self, url: str) -> str | None:
        """Parse document using docling. Runs in thread pool."""
        try:
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()
            result = converter.convert(url)
            markdown: str = result.document.export_to_markdown()
            return markdown
        except ImportError:
            logger.warning("doc_harvester.docling_not_installed")
            return None
        except Exception:
            logger.error("doc_harvester.parse_error", url=url, exc_info=True)
            return None
