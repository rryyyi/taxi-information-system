"""轨迹预处理与路径拟合（数据质量 + 拟合度 + 拥堵识别）。

处理流程（对应需求 2）：
    去重 → 按时间排序 → 剔除明显异常点 → 逐段计算距离/速度
    → 投影到参考路径算平均偏离与 fit_score
    → 识别连续低速段（拥堵）

降级原则：任何一步失败都返回 quality=INCOMPLETE 与原因 flags，**不抛异常**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.enums import DataQuality
from app.services import geo
from app.services.pricing import to_decimal


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
    """相邻两个轨迹点之间的一段路。"""

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
class CongestionSpan:
    start_seq: int
    end_seq: int
    t_start: datetime
    t_end: datetime
    duration_s: float
    avg_speed_kmh: float
    along_start: float | None
    along_end: float | None


@dataclass(frozen=True)
class DeviationSpan:
    start_seq: int
    end_seq: int
    t_start: datetime
    t_end: datetime
    duration_s: float
    max_deviation_m: float
    avg_deviation_m: float
    along_start: float | None
    along_end: float | None


@dataclass
class Trajectory:
    points: list[CleanPoint] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    distance_m: float = 0.0
    duration_s: float = 0.0
    avg_deviation_m: float | None = None
    max_deviation_m: float | None = None
    fit_score: float | None = None
    congestion_seconds: float = 0.0
    congestion_spans: list[CongestionSpan] = field(default_factory=list)
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
            "congestion_seconds": round(self.congestion_seconds, 1),
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


def _clean_points(
    raw: list[RawPoint], cfg, flags: list[str]
) -> list[CleanPoint]:
    traj_cfg = cfg.section("trajectory")
    min_points = int(traj_cfg["MIN_POINTS"])
    duplicate_m = float(traj_cfg["DUPLICATE_DISTANCE_M"])
    outlier_speed = float(traj_cfg["OUTLIER_SPEED_MPS"])

    valid = [p for p in raw if p.lat is not None and p.lng is not None]
    if len(valid) != len(raw):
        flags.append("INVALID_COORDINATES_REMOVED")

    # 1) 按时间排序（打乱顺序不崩，但要留痕）
    ordered = sorted(valid, key=lambda p: p.ts)
    if [p.ts for p in ordered] != [p.ts for p in valid] and len(valid) > 1:
        flags.append("TRAJECTORY_UNSORTED_FIXED")

    # 2) 去重（时间戳相同，且位置几乎相同）
    deduped: list[RawPoint] = []
    removed = 0
    for p in ordered:
        if deduped:
            prev = deduped[-1]
            same_ts = p.ts == prev.ts
            near = geo.haversine_m(prev.lat, prev.lng, p.lat, p.lng) <= duplicate_m
            if same_ts and near:
                removed += 1
                continue
        deduped.append(p)
    if removed:
        flags.append("TRAJECTORY_DUPLICATES_REMOVED")

    # 3) 剔除明显异常点（隐含速度超过物理上限）
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


def _build_segments(points: list[CleanPoint], reference_polyline, gaps: list[float]) -> list[Segment]:
    segments: list[Segment] = []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        dt = (b.ts - a.ts).total_seconds()
        if dt <= 0:
            continue
        dist = geo.haversine_m(a.lat, a.lng, b.lat, b.lng)
        speed = dist / dt
        if b.speed_mps <= 0 and a.speed_mps <= 0 and dist <= 1.0:
            speed = 0.0
        along_a = along_b = None
        dev_a = dev_b = None
        if geo.is_valid_polyline(reference_polyline):
            pa = geo.project_to_polyline((a.lat, a.lng), reference_polyline)
            pb = geo.project_to_polyline((b.lat, b.lng), reference_polyline)
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
    points: list[CleanPoint], reference_polyline, cfg, flags: list[str]
) -> tuple[float | None, float | None, float | None]:
    """返回 (fit_score, avg_deviation_m, max_deviation_m)。"""
    traj_cfg = cfg.section("trajectory")
    if not geo.is_valid_polyline(reference_polyline):
        flags.append("REFERENCE_ROUTE_MISSING")
        return None, None, None
    deviations: list[float] = []
    for p in points:
        proj = geo.project_to_polyline((p.lat, p.lng), reference_polyline)
        if proj is None:
            flags.append("REFERENCE_ROUTE_DEGRADED")
            return None, None, None
        deviations.append(proj.distance_m)
    avg = sum(deviations) / len(deviations)
    buffer_m = float(traj_cfg["FIT_BUFFER_M"])
    fit = max(0.0, 1.0 - avg / buffer_m) if buffer_m > 0 else 0.0
    return fit, avg, max(deviations)


def _find_spans(
    segments: list[Segment], cfg
) -> tuple[list[CongestionSpan], float, list[DeviationSpan]]:
    """识别连续低速段（拥堵）与明显偏离段。"""
    cong_cfg = cfg.section("congestion")
    speed_threshold_kmh = float(cong_cfg["CONGESTION_SPEED_KMH"])
    min_seconds = float(cong_cfg["MIN_CONGESTION_SECONDS"])

    congestion_spans: list[CongestionSpan] = []
    total_congestion_s = 0.0
    run: list[Segment] = []
    for seg in segments + [None]:  # 哨兵收尾
        is_low = seg is not None and seg.speed_kmh < speed_threshold_kmh
        if is_low and seg is not None:
            run.append(seg)
            continue
        if run:
            duration = sum(s.dt_s for s in run)
            if duration >= min_seconds:
                total_congestion_s += duration
                congestion_spans.append(
                    CongestionSpan(
                        start_seq=run[0].start_seq,
                        end_seq=run[-1].end_seq,
                        t_start=run[0].t_start,
                        t_end=run[-1].t_end,
                        duration_s=duration,
                        avg_speed_kmh=(
                            sum(s.dist_m for s in run) / duration * 3.6 if duration > 0 else 0.0
                        ),
                        along_start=run[0].along_start,
                        along_end=run[-1].along_end,
                    )
                )
            run = []

    dev_threshold = float(cfg.section("trajectory")["DEVIATION_SPAN_THRESHOLD_M"])
    deviation_spans: list[DeviationSpan] = []
    dev_run: list[Segment] = []
    for seg in segments + [None]:
        is_dev = seg is not None and (seg.deviation_m or 0.0) > dev_threshold
        if is_dev and seg is not None:
            dev_run.append(seg)
            continue
        if dev_run:
            devs = [s.deviation_m or 0.0 for s in dev_run]
            duration = sum(s.dt_s for s in dev_run)
            deviation_spans.append(
                DeviationSpan(
                    start_seq=dev_run[0].start_seq,
                    end_seq=dev_run[-1].end_seq,
                    t_start=dev_run[0].t_start,
                    t_end=dev_run[-1].t_end,
                    duration_s=duration,
                    max_deviation_m=max(devs),
                    avg_deviation_m=sum(devs) / len(devs),
                    along_start=dev_run[0].along_start,
                    along_end=dev_run[-1].along_end,
                )
            )
            dev_run = []
    return congestion_spans, total_congestion_s, deviation_spans


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

    gaps: list[float] = []
    segments = _build_segments(points, reference_polyline, gaps)
    if not segments:
        flags.append("TRAJECTORY_NO_SEGMENT")
        trajectory.flags = flags
        trajectory.quality = DataQuality.INCOMPLETE.value
        trajectory.notes.append("轨迹点时间戳全部相同，无法构造行驶段")
        return trajectory

    max_gap = float(cfg.section("trajectory")["MAX_GAP_SECONDS"])
    for i, seg in enumerate(segments):
        if seg.dt_s > max_gap:
            gaps.append(seg.dt_s)
            notes.append(f"轨迹缺口 {seg.dt_s:.0f}s（seq {seg.start_seq}->{seg.end_seq}）")
    if gaps:
        flags.append("TRAJECTORY_GAP")

    trajectory.segments = segments
    trajectory.distance_m = sum(s.dist_m for s in segments)
    trajectory.duration_s = (points[-1].ts - points[0].ts).total_seconds()

    fit, avg_dev, max_dev = _compute_fit(points, reference_polyline, cfg, flags)
    trajectory.fit_score, trajectory.avg_deviation_m, trajectory.max_deviation_m = fit, avg_dev, max_dev

    congestions, congestion_s, deviations = _find_spans(segments, cfg)
    trajectory.congestion_spans = congestions
    trajectory.congestion_seconds = congestion_s
    trajectory.deviation_spans = deviations

    trajectory.flags = flags
    trajectory.notes = notes
    trajectory.quality = DataQuality.COMPLETE.value
    if gaps:
        trajectory.notes.append("轨迹存在缺口，里程/时长按可用片段计算（降级但可用）")
    return trajectory
