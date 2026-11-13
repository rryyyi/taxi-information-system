"""轨迹预处理与路网合成测试（沈阳版：12 km/h 低速阈值 + 真实道路锚点）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services import geo, roadnet
from app.services.shenyang import ANCHORS
from app.services.trajectory import RawPoint, preprocess

T0 = datetime(2026, 3, 6, 17, 0, 0)
REF_NAMES = ["市府广场", "青年大街—市府大路", "青年大街—十一纬路", "青年大街—文艺路", "青年大街—文化路", "浑河桥南"]
REF = roadnet.polyline_from_names(REF_NAMES)


def _points_along(duration_s: int = 600, n: int = 21) -> list[RawPoint]:
    total = geo.polyline_length_m(REF.points)
    pts = []
    for i in range(n):
        frac = i / (n - 1)
        lat, lng = geo.interpolate_along(REF.points, total * frac)
        pts.append(RawPoint(ts=T0 + timedelta(seconds=duration_s * frac), lat=lat, lng=lng, speed_mps=11.0))
    return pts


def test_missing_trajectory_marks_incomplete(cfg) -> None:
    traj = preprocess([], cfg, REF.points)
    assert traj.quality == "INCOMPLETE"
    assert "TRAJECTORY_MISSING" in traj.flags
    assert traj.fit_score is None


def test_too_few_points_marks_incomplete(cfg) -> None:
    traj = preprocess(
        [RawPoint(T0, 41.8015, 123.4352), RawPoint(T0 + timedelta(seconds=30), 41.8, 123.4355)],
        cfg,
        REF.points,
    )
    assert traj.quality == "INCOMPLETE"


def test_unsorted_points_sorted_and_flagged(cfg) -> None:
    pts = _points_along(n=11)
    shuffled = [pts[0], pts[5], pts[2], pts[9], pts[1], pts[3], pts[4], pts[6], pts[7], pts[8], pts[10]]
    traj = preprocess(shuffled, cfg, REF.points)
    assert "TRAJECTORY_UNSORTED_FIXED" in traj.flags
    assert [p.ts for p in traj.points] == sorted(p.ts for p in traj.points)


def test_duplicates_and_outliers_removed(cfg) -> None:
    pts = _points_along(n=11)
    dupe = RawPoint(pts[0].ts, pts[0].lat, pts[0].lng)
    bad = RawPoint(pts[5].ts + timedelta(seconds=10), 41.0, 124.0)  # 瞬移
    traj = preprocess([dupe] + pts[:5] + [bad] + pts[5:], cfg, REF.points)
    assert "TRAJECTORY_DUPLICATES_REMOVED" in traj.flags
    assert "TRAJECTORY_OUTLIERS_REMOVED" in traj.flags


def test_high_fit_on_reference(cfg) -> None:
    traj = preprocess(_points_along(), cfg, REF.points)
    assert traj.quality == "COMPLETE"
    assert traj.fit_score > 0.9
    assert traj.avg_deviation_m < 5


def test_low_fit_with_detour(cfg) -> None:
    act, _meta = roadnet.with_detour(REF, "青年大街—文艺路", "青年大街—文化路", extra_m=500)
    pts = roadnet.drive(act.points, T0, speed_mps=11.0)
    raw = [RawPoint(p["ts"], p["lat"], p["lng"], p["speed_mps"]) for p in pts]
    traj = preprocess(raw, cfg, REF.points)
    assert traj.fit_score < 0.8
    assert traj.deviation_spans, "应识别出偏离段"
    span = traj.deviation_spans[0]
    assert span.actual_distance_m > span.along_end - span.along_start
    assert len(span.path) >= 2


def test_low_speed_span_detected_at_12_kmh(cfg) -> None:
    """速度低于 12 km/h 的连续段 → 等候事件候选。"""
    # 在参考路径中点停驶 150 秒
    pts = roadnet.drive(REF.points, T0, speed_mps=11.0, stops=[("青年大街—十一纬路", 150)],
                        node_index_by_name={n["name"]: n["index"] for n in REF.nodes})
    raw = [RawPoint(p["ts"], p["lat"], p["lng"], p["speed_mps"]) for p in pts]
    traj = preprocess(raw, cfg, REF.points)
    assert len(traj.wait_spans) == 1
    span = traj.wait_spans[0]
    assert 149 <= span.duration_s <= 151
    assert span.avg_speed_kmh < 12
    assert span.lat == pytest.approx(ANCHORS["青年大街—十一纬路"][0], abs=1e-4)


def test_moving_speed_not_flagged(cfg) -> None:
    pts = roadnet.drive(REF.points, T0, speed_mps=11.0)
    raw = [RawPoint(p["ts"], p["lat"], p["lng"], p["speed_mps"]) for p in pts]
    traj = preprocess(raw, cfg, REF.points)
    assert traj.wait_spans == []
    assert traj.wait_seconds == 0


def test_detour_midpoint_hits_target_extra() -> None:
    a = ANCHORS["青年大街—文艺路"]
    b = ANCHORS["青年大街—文化路"]
    direct = geo.haversine_m(*a, *b)
    for extra in (380, 1500):
        via = roadnet.detour_midpoint(a, b, extra)
        actual = geo.haversine_m(a[0], a[1], via[0], via[1]) + geo.haversine_m(via[0], via[1], b[0], b[1])
        assert actual - direct == pytest.approx(extra, abs=1.5)


def test_detour_route_preserves_non_detour_length() -> None:
    act, meta = roadnet.with_detour(REF, "青年大街—文艺路", "青年大街—文化路", extra_m=500)
    assert act.length_m - REF.length_m == pytest.approx(500, abs=3)
    assert meta["entry_name"] == "青年大街—文艺路"


def test_missing_reference_does_not_crash(cfg) -> None:
    traj = preprocess(_points_along(), cfg, None)
    assert traj.quality == "COMPLETE"
    assert traj.fit_score is None
    assert "REFERENCE_ROUTE_MISSING" in traj.flags


def test_all_same_timestamp_degrades(cfg) -> None:
    lat, lng = geo.interpolate_along(REF.points, 1000)
    traj = preprocess(
        [RawPoint(T0, lat, lng), RawPoint(T0, lat, lng), RawPoint(T0, lat, lng)], cfg, REF.points
    )
    assert traj.quality == "INCOMPLETE"
