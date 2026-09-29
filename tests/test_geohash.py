"""Tests for geohash utilities."""

from __future__ import annotations

import pytest

from ghkge.utils.geohash import decode, decode_bbox, encode, enumerate_cells, haversine_m


class TestEncodeDecode:
    def test_roundtrip_center(self):
        for lat, lng in [(25.3, 83.0), (-33.87, 151.21), (0.0, 0.0), (51.5, -0.12)]:
            cell = encode(lat, lng, 6)
            dec_lat, dec_lng = decode(cell)
            assert abs(dec_lat - lat) < 0.02
            assert abs(dec_lng - lng) < 0.02

    def test_known_vector(self):
        # Classic geohash test vectors.
        assert encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
        assert encode(42.6, -5.6, 6) == "ezs42e"

    def test_precision_affects_length(self):
        assert len(encode(25.3, 83.0, 5)) == 5
        assert len(encode(25.3, 83.0, 7)) == 7

    def test_invalid_precision(self):
        with pytest.raises(ValueError):
            encode(25.3, 83.0, 0)
        with pytest.raises(ValueError):
            encode(25.3, 83.0, 20)

    def test_invalid_character(self):
        with pytest.raises(ValueError):
            decode_bbox("u4pru!")  # '!' not in base32


class TestDecodeBbox:
    def test_bbox_contains_point(self):
        lat, lng = 25.3, 83.0
        cell = encode(lat, lng, 6)
        min_lat, min_lon, max_lat, max_lon = decode_bbox(cell)
        assert min_lat <= lat <= max_lat
        assert min_lon <= lng <= max_lon

    def test_longer_precision_is_smaller_cell(self):
        coarse = decode_bbox(encode(25.3, 83.0, 4))
        fine = decode_bbox(encode(25.3, 83.0, 7))
        coarse_h = coarse[2] - coarse[0]
        fine_h = fine[2] - fine[0]
        assert fine_h < coarse_h

    def test_bbox_center_matches_decode(self):
        cell = encode(25.3, 83.0, 6)
        min_lat, min_lon, max_lat, max_lon = decode_bbox(cell)
        center_lat, center_lng = decode(cell)
        assert (min_lat + max_lat) / 2 == pytest.approx(center_lat)
        assert (min_lon + max_lon) / 2 == pytest.approx(center_lng)

    def test_bbox_ordered(self):
        min_lat, min_lon, max_lat, max_lon = decode_bbox(encode(25.3, 83.0, 6))
        assert min_lat < max_lat
        assert min_lon < max_lon
        assert abs((min_lat + max_lat) / 2 - 25.3) < 0.02
        assert abs((min_lon + max_lon) / 2 - 83.0) < 0.02


class TestEnumerateCells:
    def test_small_bbox_returns_multiple_cells(self):
        cells = enumerate_cells([25.30, 83.00, 25.31, 83.01], precision=6)
        assert len(cells) > 1
        assert len(cells) == len(set(cells))  # no duplicates

    def test_all_cells_cover_bbox(self):
        min_lat, min_lon, max_lat, max_lon = [25.29, 82.98, 25.30, 82.99]
        cells = enumerate_cells([min_lat, min_lon, max_lat, max_lon], precision=5)
        # At least one cell must intersect the region we asked for.
        for cell in cells:
            c_min_lat, c_min_lon, c_max_lat, c_max_lon = decode_bbox(cell)
            overlaps_lat = c_min_lat <= max_lat and c_max_lat >= min_lat
            overlaps_lon = c_min_lon <= max_lon and c_max_lon >= min_lon
            assert overlaps_lat and overlaps_lon

    def test_invalid_bbox(self):
        with pytest.raises(ValueError):
            enumerate_cells([25.3, 83.0, 25.3, 83.0])  # zero area

    def test_default_domain_bbox_terminates(self):
        from ghkge.config.settings import settings
        from ghkge.orchestrator.domain import load_domain_config

        config = load_domain_config()
        cells = enumerate_cells(config.geography_bbox, precision=6)
        assert len(cells) > 0
        assert settings.grid_precision >= 1


class TestHaversine:
    def test_zero_distance(self):
        assert haversine_m(25.3, 83.0, 25.3, 83.0) == pytest.approx(0.0)

    def test_known_distance(self):
        # One degree of latitude is ~111 km.
        d = haversine_m(25.0, 83.0, 26.0, 83.0)
        assert d == pytest.approx(111_195, rel=0.01)

    def test_symmetry(self):
        a = haversine_m(25.3, 83.0, 25.4, 83.1)
        b = haversine_m(25.4, 83.1, 25.3, 83.0)
        assert a == pytest.approx(b)
