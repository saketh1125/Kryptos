from __future__ import annotations

import instructor
import openai
import structlog

from ghkge.config.settings import settings
from ghkge.models.schemas import ExtractedFactSchema
from ghkge.synthesis.chunker import sliding_window_chunker

logger = structlog.get_logger()

EXTRACTION_PROMPT = """You are a hyperlocal knowledge extraction system. Extract ONLY locally-specific facts from the following text.

RULES:
1. Extract facts that are specific to a local region, city, or neighborhood.
2. REJECT global/macro knowledge (e.g., "Water boils at 100°C", "The capital of France is Paris").
3. Each fact must be about a specific entity (place, business, event, rule, organization).
4. Mark safety-relevant facts (rules, hazards, prohibitions, guidelines) with is_safety_relevant=True.
5. Assign confidence based on how explicitly the text states the fact.
6. entity_name should be the canonical local name of the entity.

TEXT TO ANALYZE:
{chunk}
"""


def _get_client() -> instructor.Instructor:
    client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    return instructor.from_openai(client)


async def extract_facts_from_chunk(
    chunk: str,
    chunk_index: int,
    source_url: str = "",
) -> list[ExtractedFactSchema]:
    """Extract structured facts from a single text chunk using Instructor + LLM."""
    client = _get_client()

    try:
        facts = await client.chat.completions.create(
            model=settings.extraction_model,
            response_model=list[ExtractedFactSchema],
            messages=[
                {
                    "role": "system",
                    "content": "You extract hyperlocal facts. Never return macro/general knowledge.",
                },
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT.format(chunk=chunk),
                },
            ],
            max_retries=3,
        )

        # Filter out macro knowledge
        local_facts = [f for f in facts if not f.is_macro_knowledge]

        logger.debug(
            "extraction.chunk_done",
            chunk_index=chunk_index,
            raw_facts=len(facts),
            local_facts=len(local_facts),
        )
        return local_facts

    except Exception:
        logger.error("extraction.chunk_failed", chunk_index=chunk_index, exc_info=True)
        return []


async def extract_facts_from_text(
    text: str,
    source_url: str = "",
) -> list[ExtractedFactSchema]:
    """
    Full extraction pipeline: chunk text, then extract facts from each chunk.
    """
    chunks = sliding_window_chunker(text)
    all_facts: list[ExtractedFactSchema] = []

    for i, chunk in enumerate(chunks):
        facts = await extract_facts_from_chunk(chunk, chunk_index=i, source_url=source_url)
        all_facts.extend(facts)

    logger.info(
        "extraction.complete",
        source_url=source_url,
        chunks_processed=len(chunks),
        total_facts=len(all_facts),
    )
    return all_facts
