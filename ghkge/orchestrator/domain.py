"""Domain configuration loading + canonical strategy registry.

Single source of truth for the strategy vocabulary shared by the domain YAML,
the planner, and the harvester dispatch. A new city/domain is a YAML file.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from ghkge.config.settings import settings
from ghkge.models.schemas import DomainConfig
from ghkge.utils.geohash import BBox, decode_bbox, encode

logger = structlog.get_logger()

# Canonical strategies -> harvester engine that executes them.
STRATEGY_ENGINES: dict[str, str] = {
    "osm_api": "api_overpass",
    "web_crawls": "web",
    "official_portals": "web",
    "news_feeds": "web",
    "specialized_wikis": "web",
    "business_directories": "web",
    "social_media_crawls": "web",
    "media_transcripts": "media",
    "doc_parse": "document",
    "api_query": "api",
}

# Overpass tag filters per ontology entity type. Empty value => OSM not a fit.
OSM_TAG_FILTERS: dict[str, str] = {
    "landmark": 'node["tourism"]({bbox});node["historic"]({bbox});',
    "local_business": 'node["shop"]({bbox});node["amenity"]({bbox});',
    "transit_route": 'node["highway"~"bus_stop|tram_stop"]({bbox});node["railway"="station"]({bbox});',
}

_config_cache: dict[str, DomainConfig] = {}


def load_domain_config(path: str | None = None) -> DomainConfig:
    """Load and cache a domain YAML config."""
    resolved = str(Path(path or settings.domain_config_path))
    cached = _config_cache.get(resolved)
    if cached is not None:
        return cached

    with open(resolved) as f:
        data = yaml.safe_load(f)
    config = DomainConfig(**data)
    _config_cache[resolved] = config
    logger.info("domain.config_loaded", path=resolved, domain=config.domain)
    return config


def grid_cell_bbox(grid_cell: str) -> BBox:
    """Bounding box of a geohash grid cell."""
    return decode_bbox(grid_cell)


def build_overpass_query(entity_type: str, bbox_str: str) -> str | None:
    """Build an Overpass QL query for an entity type within a bbox string."""
    filters = OSM_TAG_FILTERS.get(entity_type)
    if not filters:
        return None
    return (
        f"[out:json][timeout:25];({filters.format(bbox=bbox_str)})" f";out body 200;"
    )


def plan_targets(
    config: DomainConfig,
    entity_type: str,
    grid_cell: str,
    strategy: str,
) -> dict[str, object]:
    """Produce the harvest instruction (engine + url/query) for one gap+strategy.

    Returns {"engine": ..., "url": ...} or {"engine": ..., "overpass_query": ...},
    or {} when no concrete target can be derived for this strategy.
    """
    engine = STRATEGY_ENGINES.get(strategy)
    if engine is None:
        logger.warning("domain.unknown_strategy", strategy=strategy)
        return {}

    if engine == "api_overpass":
        min_lat, min_lon, max_lat, max_lon = decode_bbox(grid_cell)
        query = build_overpass_query(entity_type, f"{min_lat},{min_lon},{max_lat},{max_lon}")
        if query is None:
            return {}
        return {"engine": engine, "overpass_query": query, "entity_type": entity_type}

    seeds = config.strategy_seeds.get(strategy) or []
    return {
        "engine": engine,
        "urls": list(seeds),
        "entity_type": entity_type,
    }


def cell_for_point(lat: float, lng: float, precision: int | None = None) -> str:
    """Grid cell identifier for a coordinate."""
    return encode(lat, lng, precision or settings.grid_precision)


def reset_domain_cache() -> None:
    """Clear the config cache (used by tests)."""
    _config_cache.clear()
