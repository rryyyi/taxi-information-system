"""轨迹预处理与路径拟合（数据质量 + 拟合度 + 低速等候段/偏离段识别）。

处理流程：
    去重 → 按时间排序 → 剔除明显异常点 → 逐段计算距离/速度
    → 投影到参考路径算平均偏离与 fit_score
    → 识别连续低速段（等候事件候选，<12 km/h）与偏离段（绕路事件候选）

降级原则：任何一步失败都返回 quality=INCOMPLETE 与原因 flags，**不抛异常**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.enums import DataQuality
from app.services import geo


@dataclass(frozen=True)
class RawPoint:
    ts: datetime
    lat: float
    lng: float
    speed_mps: float | None = None


@dataclass(frozen=True)
class CleanPoint:
    seq: int
    ts: datetime
    lat: float
    lng: float
    speed_mps: float


@dataclass(frozen=True)
class Segment:
    start_seq: int
    end_seq: int
    t_start: datetime
    t_end: datetime
    dt_s: float
    dist_m: float
    speed_mps: float
    speed_kmh: float
    along_start: float | None = None
    along_end: float | None = None
    deviation_m: float | None = None


@dataclass(frozen=True)
class WaitSpan:
    """一条连续低速段（等候事件候选）。"""

    start_seq: int
    end_seq: int
    t_start: datetime
    t_end: datetime
    duration_s: float
    avg_speed_kmh: float
    lat: float
    lng: float


@dataclass(frozen=True)
class DeviationSpan:
    """一段偏离参考路径的行驶（绕路事件候选）。"""

    start_seq: int
    end_seq: int
    t_start: datetime
    t_end: datetime
    duration_s: float
    max_deviation_m: float
    avg_deviation_m: float
    along_start: float | None
    along_end: float | None
    actual_distance_m: float
    path: list[list[float]]


@dataclass
class Trajectory:
    points: list[CleanPoint] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    distance_m: float = 0.0
    duration_s: float = 0.0
    avg_deviation_m: float | None = None
    max_deviation_m: float | None = None
    fit_score: float | None = None
    wait_seconds: float = 0.0
    wait_spans: list[WaitSpan] = field(default_factory=list)
    deviation_spans: list[DeviationSpan] = field(default_factory=list)
    quality: str = DataQuality.INCOMPLETE.value
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.quality == DataQuality.COMPLETE.value

    def as_evidence(self, limit: int = 12) -> dict:
        return {
            "quality": self.quality,
            "flags": list(self.flags),
            "point_count": len(self.points),
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s, 1),
            "fit_score": None if self.fit_score is None else round(self.fit_score, 4),
            "avg_deviation_m": None if self.avg_deviation_m is None else round(self.avg_deviation_m, 2),
            "max_deviation_m": None if self.max_deviation_m is None else round(self.max_deviation_m, 2),
            "low_speed_seconds": round(self.wait_seconds, 1),
            "notes": list(self.notes),
            "sample_points": [
                {
                    "seq": p.seq,
                    "ts": p.ts.isoformat(),
                    "lat": round(p.lat, 6),
                    "lng": round(p.lng, 6),
                    "speed_mps": round(p.speed_mps, 3),
                }
                for p in self.points[:limit]
            ],
        }


def _clean_points(raw: list[RawPoint], cfg, flags: list[str]) -> list[CleanPoint]:
    traj_cfg = cfg.section("trajectory")
    min_points = int(traj_cfg["MIN_POINTS"])
    duplicate_m = float(traj_cfg["DUPLICATE_DISTANCE_M"])
    outlier_speed = float(traj_cfg["OUTLIER_SPEED_MPS"])

    valid = [p for p in raw if p.lat is not None and p.lng is not None]
    if len(valid) != len(raw):
        flags.append("INVALID_COORDINATES_REMOVED")

    ordered = sorted(valid, key=lambda p: p.ts)
    if [p.ts for p in ordered] != [p.ts for p in valid] and len(valid) > 1:
        flags.append("TRAJECTORY_UNSORTED_FIXED")

    deduped: list[RawPoint] = []
    removed = 0
    for p in ordered:
        if deduped:
            prev = deduped[-1]
            if p.ts == prev.ts and geo.haversine_m(prev.lat, prev.lng, p.lat, p.lng) <= duplicate_m:
                removed += 1
                continue
        deduped.append(p)
    if removed:
        flags.append("TRAJECTORY_DUPLICATES_REMOVED")

    filtered: list[RawPoint] = []
    outliers = 0
    for p in deduped:
        if filtered:
            prev = filtered[-1]
            dt = (p.ts - prev.ts).total_seconds()
            dist = geo.haversine_m(prev.lat, prev.lng, p.lat, p.lng)
            if dt > 0 and dist / dt > outlier_speed:
                outliers += 1
                continue
        filtered.append(p)
    if outliers:
        flags.append("TRAJECTORY_OUTLIERS_REMOVED")

    if len(filtered) < min_points:
        return []

    return [
        CleanPoint(
            seq=i,
            ts=p.ts,
            lat=p.lat,
            lng=p.lng,
            speed_mps=float(p.speed_mps) if p.speed_mps is not None else 0.0,
        )
        for i, p in enumerate(filtered)
    ]


def _build_segments(
    points: list[CleanPoint],
    reference_polyline,
    index: "geo.PolylineIndex | None" = None,
) -> list[Segment]:
    segments: list[Segment] = []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        dt = (b.ts - a.ts).total_seconds()
        if dt <= 0:
            continue
        dist = geo.haversine_m(a.lat, a.lng, b.lat, b.lng)
        speed = dist / dt
        along_a = along_b = None
        dev_a = dev_b = None
        if index is not None:
            pa = index.project((a.lat, a.lng))
            pb = index.project((b.lat, b.lng))
            if pa and pb:
                along_a, along_b = pa.along_m, pb.along_m
                dev_a, dev_b = pa.distance_m, pb.distance_m
        segments.append(
            Segment(
                start_seq=a.seq,
                end_seq=b.seq,
                t_start=a.ts,
                t_end=b.ts,
                dt_s=dt,
                dist_m=dist,
                speed_mps=speed,
                speed_kmh=speed * 3.6,
                along_start=along_a,
                along_end=along_b,
                deviation_m=None if dev_a is None or dev_b is None else max(dev_a, dev_b),
            )
        )
    return segments


def _compute_fit(
    points: list[CleanPoint],
    reference_polyline,
    cfg,
    flags: list[str],
    index: "geo.PolylineIndex | None" = None,
) -> tuple[float | None, float | None, float | None]:
    traj_cfg = cfg.section("trajectory")
    if index is None:
        # 参考路径缺失或退化：无法计算拟合度（preprocess 已标记 MISSING）
        return None, None, None
    deviations: list[float] = []
    for p in points:
        proj = index.project((p.lat, p.lng))
        if proj is None:
            flags.append("REFERENCE_ROUTE_DEGRADED")
            return None, None, None
        deviations.append(proj.distance_m)
    avg = sum(deviations) / len(deviations)
    buffer_m = float(traj_cfg["FIT_BUFFER_M"])
    fit = max(0.0, 1.0 - avg / buffer_m) if buffer_m > 0 else 0.0
    return fit, avg, max(deviations)


def find_spans(
    segments: list[Segment], points: list[CleanPoint], cfg
) -> tuple[list[WaitSpan], float, list[DeviationSpan]]:
    """识别连续低速段（等候事件）与明显偏离段（绕路事件）。"""
    low = cfg.section("low_speed")
    threshold_kmh = float(low["speed_threshold_kmh"])
    min_seconds = float(low["min_wait_seconds"])
    by_seq = {p.seq: p for p in points}

    wait_spans: list[WaitSpan] = []
    total_low = 0.0
    run: list[Segment] = []

    def flush_wait() -> None:
        nonlocal total_low
        if not run:
            return
        duration = sum(s.dt_s for s in run)
        if duration >= min_seconds:
            total_low += duration
            first, last = run[0], run[-1]
            start_point = by_seq[first.start_seq]
            wait_spans.append(
                WaitSpan(
                    start_seq=first.start_seq,
                    end_seq=last.end_seq,
                    t_start=first.t_start,
                    t_end=last.t_end,
                    duration_s=duration,
                    avg_speed_kmh=(
                        sum(s.dist_m for s in run) / duration * 3.6 if duration > 0 else 0.0
                    ),
                    lat=start_point.lat,
                    lng=start_point.lng,
                )
            )
        run.clear()

    dev_threshold = float(cfg.section("trajectory")["DEVIATION_SPAN_THRESHOLD_M"])
    deviation_spans: list[DeviationSpan] = []
    dev_run: list[Segment] = []

    def flush_dev() -> None:
        if not dev_run:
            return
        devs = [s.deviation_m or 0.0 for s in dev_run]
        duration = sum(s.dt_s for s in dev_run)
        first, last = dev_run[0], dev_run[-1]
        start_point = by_seq[first.start_seq]
        end_point = by_seq[last.end_seq]
        path = [[start_point.lat, start_point.lng]]
        for s in dev_run:
            end = by_seq[s.end_seq]
            path.append([end.lat, end.lng])
        deviation_spans.append(
            DeviationSpan(
                start_seq=first.start_seq,
                end_seq=last.end_seq,
                t_start=first.t_start,
                t_end=last.t_end,
                duration_s=duration,
                max_deviation_m=max(devs),
                avg_deviation_m=sum(devs) / len(devs),
                along_start=first.along_start,
                along_end=last.along_end,
                actual_distance_m=sum(s.dist_m for s in dev_run),
                path=path,
            )
        )
        dev_run.clear()

    for seg in segments + [None]:
        if seg is not None:
            is_low = seg.speed_kmh < threshold_kmh
            if is_low:
                run.append(seg)
            else:
                flush_wait()
            is_dev = (seg.deviation_m or 0.0) > dev_threshold
            if is_dev:
                dev_run.append(seg)
            else:
                flush_dev()
        else:
            flush_wait()
            flush_dev()
    return wait_spans, total_low, deviation_spans


def preprocess(raw_points: list[RawPoint], cfg, reference_polyline=None) -> Trajectory:
    """轨迹预处理主入口。任何畸形输入都返回可解释的 Trajectory（不抛异常）。"""
    flags: list[str] = []
    notes: list[str] = []
    trajectory = Trajectory()

    if not raw_points:
        trajectory.flags = ["TRAJECTORY_MISSING"]
        trajectory.notes.append("未提供轨迹数据，全部时间/距离证据缺失")
        trajectory.quality = DataQuality.INCOMPLETE.value
        return trajectory

    points = _clean_points(raw_points, cfg, flags)
    if not points:
        trajectory.flags = flags + ["TRAJECTORY_MISSING"]
        trajectory.notes.append(
            f"有效轨迹点少于阈值 {cfg.section('trajectory')['MIN_POINTS']}，视为轨迹缺失"
        )
        trajectory.quality = DataQuality.INCOMPLETE.value
        return trajectory

    trajectory.points = points

    # 参考折线索引只构建一次，供段构建与拟合度计算复用（行为不变）
    index: geo.PolylineIndex | None = None
    if geo.is_valid_polyline(reference_polyline):
        try:
            index = geo.PolylineIndex(reference_polyline)
        except ValueError:
            index = None
    if index is None:
        flags.append("REFERENCE_ROUTE_MISSING")

    segments = _build_segments(points, reference_polyline, index)
    if not segments:
        flags.append("TRAJECTORY_NO_SEGMENT")
        trajectory.flags = flags
        trajectory.quality = DataQuality.INCOMPLETE.value
        trajectory.notes.append("轨迹点时间戳全部相同，无法构造行驶段")
        return trajectory

    max_gap = float(cfg.section("trajectory")["MAX_GAP_SECONDS"])
    for seg in segments:
        if seg.dt_s > max_gap:
            notes.append(f"轨迹缺口 {seg.dt_s:.0f}s（seq {seg.start_seq}->{seg.end_seq}）")
    if any(seg.dt_s > max_gap for seg in segments):
        flags.append("TRAJECTORY_GAP")

    trajectory.segments = segments
    trajectory.distance_m = sum(s.dist_m for s in segments)
    trajectory.duration_s = (points[-1].ts - points[0].ts).total_seconds()

    fit, avg_dev, max_dev = _compute_fit(points, reference_polyline, cfg, flags, index)
    trajectory.fit_score, trajectory.avg_deviation_m, trajectory.max_deviation_m = fit, avg_dev, max_dev

    wait_spans, total_low, deviations = find_spans(segments, points, cfg)
    trajectory.wait_spans = wait_spans
    trajectory.wait_seconds = total_low
    trajectory.deviation_spans = deviations

    trajectory.flags = flags
    trajectory.notes = notes
    trajectory.quality = DataQuality.COMPLETE.value
    if "TRAJECTORY_GAP" in trajectory.flags:
        trajectory.notes.append("轨迹存在缺口，里程/时长按可用片段计算（降级但可用）")
    return trajectory
