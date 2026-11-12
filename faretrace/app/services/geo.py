"""几何工具层（shapely + Haversine）。

职责：
1) 球面两点距离（Haversine，米）；
2) 轨迹点 → 参考路径的投影（返回沿路里程、垂直偏离距离、所在线段）；
3) 按沿路里程在参考路径上取点（用于生成/展示轨迹证据）。

坐标以 (lat, lng) 传递；投影在"以参考路径首点为原点的局部等距平面"内用
shapely LineString 完成（城市尺度误差 < 0.5%），再映射回球面累计里程。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point

EARTH_RADIUS_M = 6371000.0
METERS_PER_DEG = 111319.49079327358


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def cumulative_m(points: list[list[float]] | list[tuple[float, float]]) -> list[float]:
    """累计球面里程，与 points 等长。"""
    if not points:
        return []
    out = [0.0]
    for i in range(1, len(points)):
        out.append(out[-1] + haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
    return out


def polyline_length_m(points: list[list[float]] | list[tuple[float, float]]) -> float:
    return cumulative_m(points)[-1] if points else 0.0


def is_valid_polyline(points: list[list[float]] | list[tuple[float, float]] | None) -> bool:
    if not points or len(points) < 2:
        return False
    return polyline_length_m(points) > 0.0


def _local_xy(lat: float, lng: float, origin_lat: float, origin_lng: float, cos_lat: float) -> tuple[float, float]:
    x = (lng - origin_lng) * cos_lat * METERS_PER_DEG
    y = (lat - origin_lat) * METERS_PER_DEG
    return x, y


@dataclass(frozen=True)
class Projection:
    along_m: float          # 从参考路径起点到投影点的累计里程（米，球面）
    distance_m: float       # 轨迹点到参考路径的垂直偏离距离（米）
    segment_index: int      # 投影落在参考路径的第几个线段（0 起）
    t: float                # 线段内比例 [0, 1]


def build_line(points: list[list[float]] | list[tuple[float, float]]) -> tuple[LineString, float]:
    """把 (lat,lng) 折线转成局部平面 LineString，返回 (line, 球面总长)。"""
    origin_lat, origin_lng = points[0][0], points[0][1]
    cos_lat = math.cos(math.radians(origin_lat))
    coords = [_local_xy(p[0], p[1], origin_lat, origin_lng, cos_lat) for p in points]
    return LineString(coords), polyline_length_m(points)


def project_to_polyline(
    point: tuple[float, float],
    points: list[list[float]] | list[tuple[float, float]],
) -> Projection | None:
    """轨迹点投影到参考折线；折线退化时返回 None。"""
    if not is_valid_polyline(points):
        return None
    line, spherical_len = build_line(points)
    local_len = line.length
    if local_len <= 0:
        return None
    origin_lat, origin_lng = points[0][0], points[0][1]
    cos_lat = math.cos(math.radians(origin_lat))
    p = Point(*_local_xy(point[0], point[1], origin_lat, origin_lng, cos_lat))
    target = line.project(p)
    projected = line.interpolate(target)
    distance = p.distance(projected)
    fraction = max(0.0, min(1.0, target / local_len))
    along = fraction * spherical_len

    # 通过局部长度定位所在线段与线段内比例
    coords = list(line.coords)
    cum = 0.0
    seg_index, seg_t = 0, 0.0
    for i in range(len(coords) - 1):
        seg_len = math.dist(coords[i], coords[i + 1])
        if seg_len <= 0:
            continue
        if cum + seg_len >= target or i == len(coords) - 2:
            seg_index = i
            seg_t = 0.0 if seg_len <= 0 else max(0.0, min(1.0, (target - cum) / seg_len))
            break
        cum += seg_len
    return Projection(along_m=along, distance_m=distance, segment_index=seg_index, t=seg_t)


def interpolate_along(
    points: list[list[float]] | list[tuple[float, float]], along_m: float
) -> tuple[float, float]:
    """按沿路里程在参考折线上取点（越界夹到端点）。"""
    if not points:
        raise ValueError("参考路径为空")
    line, spherical_len = build_line(points)
    if spherical_len <= 0 or line.length <= 0:
        return float(points[0][0]), float(points[0][1])
    ratio = max(0.0, min(1.0, along_m / spherical_len))
    x, y = line.interpolate(ratio, normalized=True).coords[0]
    origin_lat, origin_lng = points[0][0], points[0][1]
    cos_lat = math.cos(math.radians(origin_lat))
    lat = origin_lat + y / METERS_PER_DEG
    lng = origin_lng + x / (cos_lat * METERS_PER_DEG)
    return lat, lng


def offset_point(lat: float, lng: float, bearing_dx: float, dy: float) -> tuple[float, float]:
    """在局部平面内给点加偏移 (dx, dy)（米），用于合成轨迹。"""
    cos_lat = math.cos(math.radians(lat))
    return lat + dy / METERS_PER_DEG, lng + bearing_dx / (cos_lat * METERS_PER_DEG)
