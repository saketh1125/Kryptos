"""Domain configuration loading + canonical strategy registry.

Single source of truth for the strategy vocabulary shared by the domain YAML,
the planner, and the harvester dispatch. A new city/domain is a YAML file.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    "transit_route": (
        'node["highway"~"bus_stop|tram_stop"]({bbox});node["railway"="station"]({bbox});'
    ),
}

_config_cache: dict[str, DomainConfig] = {}


class DomainConfigNotFoundError(FileNotFoundError):
    """Raised when no domain YAML can be located."""


def _resolve_config_path(path: str | None) -> Path:
    """Locate a domain YAML, falling back to the repo-root config dir."""
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path(settings.domain_config_path))
        # Repo root relative to this file, for when CWD is not the project root.
        candidates.append(Path(__file__).resolve().parents[2] / "config/default_domain.yaml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise DomainConfigNotFoundError(
        "No domain config found. Looked for: "
        + ", ".join(str(c) for c in candidates)
        + ". Set GHKGE_DOMAIN_CONFIG_PATH or run from the project root."
    )


def load_domain_config(path: str | None = None) -> DomainConfig:
    """Load and cache a domain YAML config. Single source of truth per path."""
    resolved = _resolve_config_path(path)
    key = str(resolved.resolve())
    cached = _config_cache.get(key)
    if cached is not None:
        return cached

    with open(resolved) as f:
        data = yaml.safe_load(f)
    config = DomainConfig(**data)
    _config_cache[key] = config
    logger.info("domain.config_loaded", path=key, domain=config.domain)
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


@dataclass(frozen=True)
class HarvestPlan:
    """A concrete acquisition target derived from a gap + strategy."""

    engine: str
    urls: tuple[str, ...] = ()
    overpass_query: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.urls and not self.overpass_query


def plan_targets(
    config: DomainConfig,
    entity_type: str,
    grid_cell: str,
    strategy: str,
) -> HarvestPlan | None:
    """Produce the harvest instruction for one gap + strategy.

    Returns None when the strategy is unknown, has no OSM tag mapping, or has
    no configured seed URLs (nothing to fetch).
    """
    engine = STRATEGY_ENGINES.get(strategy)
    if engine is None:
        logger.warning("domain.unknown_strategy", strategy=strategy)
        return None

    if engine == "api_overpass":
        min_lat, min_lon, max_lat, max_lon = decode_bbox(grid_cell)
        query = build_overpass_query(entity_type, f"{min_lat},{min_lon},{max_lat},{max_lon}")
        if query is None:
            return None
        return HarvestPlan(engine=engine, overpass_query=query)

    seeds = tuple(config.strategy_seeds.get(strategy) or ())
    if not seeds:
        return None
    return HarvestPlan(engine=engine, urls=seeds)


def cell_for_point(lat: float, lng: float, precision: int | None = None) -> str:
    """Grid cell identifier for a coordinate."""
    return encode(lat, lng, precision or settings.grid_precision)


def reset_domain_cache() -> None:
    """Clear the config cache (used by tests)."""
    _config_cache.clear()
