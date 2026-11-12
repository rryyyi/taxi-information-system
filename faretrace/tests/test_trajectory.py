"""轨迹预处理测试：去重、排序、异常点剔除、拟合度、拥堵识别、降级。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import geo
from app.services.trajectory import RawPoint, preprocess
from tests.helpers import sample_polyline

T0 = datetime(2026, 3, 2, 8, 0, 0, tzinfo=timezone.utc)
REF = sample_polyline(meters=5000)


def _points_along_ref(duration_s: int, n: int = 21, offset_m: float = 0.0) -> list[RawPoint]:
    total = geo.polyline_length_m(REF)
    points = []
    for i in range(n):
        frac = i / (n - 1)
        lat, lng = geo.interpolate_along(REF, total * frac)
        if offset_m:
            lat, lng = geo.offset_point(lat, lng, 0.0, offset_m * min(1.0, 2 * frac) if frac < 0.5 else offset_m * (2 - 2 * frac))
        points.append(RawPoint(ts=T0 + timedelta(seconds=duration_s * frac), lat=lat, lng=lng, speed_mps=5.0))
    return points


def test_missing_trajectory_marks_incomplete(cfg) -> None:
    traj = preprocess([], cfg, REF)
    assert traj.quality == "INCOMPLETE"
    assert "TRAJECTORY_MISSING" in traj.flags
    assert traj.fit_score is None
    assert traj.congestion_seconds == 0


def test_too_few_points_marks_incomplete(cfg) -> None:
    traj = preprocess([RawPoint(T0, 30.0, 120.0), RawPoint(T0 + timedelta(seconds=30), 30.0, 120.01)], cfg, REF)
    assert traj.quality == "INCOMPLETE"
    assert traj.fit_score is None


def test_unsorted_points_are_sorted_and_flagged(cfg) -> None:
    pts = _points_along_ref(600, n=11)
    shuffled = [pts[0], pts[5], pts[2], pts[9], pts[1], pts[3], pts[4], pts[6], pts[7], pts[8], pts[10]]
    traj = preprocess(shuffled, cfg, REF)
    assert "TRAJECTORY_UNSORTED_FIXED" in traj.flags
    assert [p.ts for p in traj.points] == sorted(p.ts for p in traj.points)
    assert traj.duration_s == pytest.approx(600, abs=1)


def test_duplicate_points_removed(cfg) -> None:
    pts = _points_along_ref(600, n=11)
    dupe = RawPoint(pts[0].ts, pts[0].lat, pts[0].lng)
    traj = preprocess([dupe] + pts, cfg, REF)
    assert "TRAJECTORY_DUPLICATES_REMOVED" in traj.flags
    assert len(traj.points) == len(pts)


def test_outlier_point_removed(cfg) -> None:
    pts = _points_along_ref(600, n=11)
    bad = RawPoint(pts[5].ts + timedelta(seconds=10), 31.5, 121.5)  # 瞬移到百公里外
    traj = preprocess(pts[:5] + [bad] + pts[5:], cfg, REF)
    assert "TRAJECTORY_OUTLIERS_REMOVED" in traj.flags
    assert len(traj.points) == len(pts)


def test_high_fit_on_reference_route(cfg) -> None:
    traj = preprocess(_points_along_ref(600), cfg, REF)
    assert traj.quality == "COMPLETE"
    assert traj.fit_score is not None and traj.fit_score > 0.95
    assert traj.avg_deviation_m < 5
    assert traj.distance_m == pytest.approx(geo.polyline_length_m(REF), rel=0.02)


def test_low_fit_when_detouring(cfg) -> None:
    traj = preprocess(_points_along_ref(600, n=21, offset_m=800.0), cfg, REF)
    assert traj.fit_score is not None and traj.fit_score < 0.8
    assert traj.avg_deviation_m > 20
    assert traj.deviation_spans, "应有偏离片段证据"


def test_congestion_span_detected_when_low_speed_long_enough(cfg) -> None:
    lat, lng = geo.interpolate_along(REF, 2500)
    jam = [RawPoint(T0 + timedelta(seconds=5 * i), lat, lng, 0.0) for i in range(0, 21)]  # 100 秒
    before = _points_along_ref(300, n=11)
    traj = preprocess(before + jam, cfg, REF)
    assert traj.congestion_seconds == pytest.approx(100, abs=1)
    assert traj.congestion_spans
    assert traj.congestion_spans[0].duration_s >= 60


def test_short_stop_not_counted_as_congestion(cfg) -> None:
    lat, lng = geo.interpolate_along(REF, 2500)
    short = [RawPoint(T0 + timedelta(seconds=5 * i), lat, lng, 0.0) for i in range(0, 11)]  # 50 秒
    traj = preprocess(_points_along_ref(60, n=5) + short, cfg, REF)
    assert traj.congestion_seconds == 0


def test_gap_flagged_but_still_usable(cfg) -> None:
    pts = _points_along_ref(300, n=11)
    spaced = pts[:5] + [
        RawPoint(p.ts + timedelta(seconds=400), p.lat, p.lng, p.speed_mps) for p in pts[5:]
    ]
    traj = preprocess(spaced, cfg, REF)
    assert "TRAJECTORY_GAP" in traj.flags
    assert traj.quality == "COMPLETE"


def test_missing_reference_route_does_not_crash(cfg) -> None:
    traj = preprocess(_points_along_ref(600), cfg, None)
    assert traj.quality == "COMPLETE"
    assert traj.fit_score is None
    assert "REFERENCE_ROUTE_MISSING" in traj.flags


def test_all_same_timestamp_degrades_gracefully(cfg) -> None:
    lat, lng = geo.interpolate_along(REF, 1000)
    traj = preprocess([RawPoint(T0, lat, lng), RawPoint(T0, lat, lng), RawPoint(T0, lat, lng)], cfg, REF)
    assert traj.quality == "INCOMPLETE"
