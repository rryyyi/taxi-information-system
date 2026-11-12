"""几何层测试（shapely 投影 + Haversine）。"""

from __future__ import annotations

import pytest

from app.services import geo

REF = [[30.0, 120.0], [30.0, 120.01], [30.005, 120.01]]


def test_haversine_one_degree_latitude() -> None:
    d = geo.haversine_m(30.0, 120.0, 31.0, 120.0)
    assert 110_000 < d < 112_000


def test_haversine_zero_distance() -> None:
    assert geo.haversine_m(30.0, 120.0, 30.0, 120.0) == pytest.approx(0.0, abs=1e-9)


def test_polyline_length_matches_segment_sum() -> None:
    total = geo.polyline_length_m(REF)
    manual = sum(
        geo.haversine_m(REF[i - 1][0], REF[i - 1][1], REF[i][0], REF[i][1]) for i in range(1, len(REF))
    )
    assert total == pytest.approx(manual)


def test_project_point_on_polyline_has_zero_deviation() -> None:
    proj = geo.project_to_polyline((30.0, 120.005), REF)
    assert proj is not None
    assert proj.distance_m < 1.0
    assert proj.along_m > 0


def test_project_off_route_measures_deviation() -> None:
    # 向北偏移约 111 米
    proj = geo.project_to_polyline((30.001, 120.005), REF)
    assert proj is not None
    assert 100 < proj.distance_m < 125


def test_project_degenerate_polyline_returns_none() -> None:
    assert geo.project_to_polyline((30.0, 120.0), [[30.0, 120.0], [30.0, 120.0]]) is None
    assert geo.project_to_polyline((30.0, 120.0), []) is None


def test_interpolate_along_hits_endpoints_and_midpoint() -> None:
    start = geo.interpolate_along(REF, 0)
    assert start[0] == pytest.approx(REF[0][0], abs=1e-6) and start[1] == pytest.approx(REF[0][1], abs=1e-6)
    total = geo.polyline_length_m(REF)
    end = geo.interpolate_along(REF, total)
    assert end[0] == pytest.approx(REF[-1][0], abs=1e-4)
    mid = geo.interpolate_along(REF, total / 2)
    assert start != mid


def test_offset_point_moves_by_meters() -> None:
    lat, lng = geo.offset_point(30.0, 120.0, 0.0, 111.319)
    d = geo.haversine_m(30.0, 120.0, lat, lng)
    assert d == pytest.approx(111.319, abs=1.0)
