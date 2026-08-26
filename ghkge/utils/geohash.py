"""Pure-python Geohash utilities (no external dependency).

Grid cells are stored as geohash strings on entities/gaps. This module
encodes/decodes them and enumerates the full cell grid covering a domain
bbox (used by the gap evaluator's bootstrap coverage scan).
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Final, Sequence

BASE32: Final = "0123456789bcdefghjkmnpqrstuvwxyz"
_BITS: Final = (16, 8, 4, 2, 1)

BBox = tuple[float, float, float, float]  # (min_lat, min_lon, max_lat, max_lon)


def encode(latitude: float, longitude: float, precision: int = 6) -> str:
    """Encode a lat/lng point into a geohash string of the requested precision."""
    if precision < 1 or precision > 12:
        raise ValueError("precision must be between 1 and 12")

    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    bit = 0
    ch = 0
    chars: list[str] = []

    while len(chars) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if longitude >= mid:
                ch |= _BITS[bit]
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if latitude >= mid:
                ch |= _BITS[bit]
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            chars.append(BASE32[ch])
            bit = 0
            ch = 0
    return "".join(chars)


def decode_bbox(hash_str: str) -> BBox:
    """Decode a geohash into (min_lat, min_lon, max_lat, max_lon)."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True

    for char in hash_str.strip().lower():
        idx = BASE32.find(char)
        if idx == -1:
            raise ValueError(f"invalid geohash character: {char!r}")
        for bit in _BITS:
            if even:
                mid = (lon_lo + lon_hi) / 2
                if idx & bit:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if idx & bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lon_lo, lat_hi, lon_hi


def decode(hash_str: str) -> tuple[float, float]:
    """Decode a geohash into its center (lat, lng)."""
    lat_lo, lon_lo, lat_hi, lon_hi = decode_bbox(hash_str)
    return (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2


def enumerate_cells(bbox: Sequence[float], precision: int = 6) -> list[str]:
    """Enumerate every distinct geohash cell intersecting the bbox.

    Args:
        bbox: (min_lat, min_lon, max_lat, max_lon).
        precision: geohash precision (cell size). Precision 6 is ~1.2km x ~0.6km.

    Returns:
        Unique cell identifiers covering the bbox.
    """
    min_lat, min_lon, max_lat, max_lon = bbox[:4]
    if min_lat >= max_lat or min_lon >= max_lon:
        raise ValueError(f"invalid bbox: {bbox!r}")

    probe = decode_bbox(encode(min_lat, min_lon, precision))
    cell_height = abs(probe[2] - probe[0])
    cell_width = abs(probe[3] - probe[1])

    cells: list[str] = []
    seen: set[str] = set()
    lat = min_lat
    guard = 0
    max_steps = 10_000
    while lat <= max_lat and guard < max_steps:
        guard += 1
        lon = min_lon
        steps = 0
        while lon <= max_lon and steps < max_steps:
            steps += 1
            cell = encode(lat, lon, precision)
            if cell not in seen:
                seen.add(cell)
                cells.append(cell)
            lon += cell_width
        lat += cell_height
    return cells


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in meters."""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * 6_371_000.0 * asin(sqrt(a))
