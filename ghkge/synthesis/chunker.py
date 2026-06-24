from __future__ import annotations

import structlog

from ghkge.config.settings import settings

logger = structlog.get_logger()


def sliding_window_chunker(
    text: str,
    chunk_size: int = settings.chunk_size_words,
    overlap: int = settings.chunk_overlap_words,
) -> list[str]:
    """
    Split text into overlapping word-level windows.

    Args:
        text: Input text to chunk.
        chunk_size: Number of words per chunk.
        overlap: Number of overlapping words between chunks.

    Returns:
        List of text chunks.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text.strip()]

    chunks: list[str] = []
    start = 0

    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))

        if end >= len(words):
            break

        start += chunk_size - overlap

    logger.debug("chunker.completed", total_words=len(words), chunks=len(chunks))
    return chunks
