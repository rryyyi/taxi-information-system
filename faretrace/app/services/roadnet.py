"""沈阳出租车演示路网：真实地名锚点 + 轨迹合成。

坐标为 OpenStreetMap 公开数据的近似值（锚点表见 app/services/shenyang.py），
轨迹沿锚点折线插值生成，含经纬度、时间戳、速度三字段。

提供：
    polyline_from_names(names)          锚点序列 → 插值折线 + 节点表
    detour_midpoint(A, B, extra_m)      求偏航中点（使绕行段恰好多走 extra_m）
    with_detour(...)                    在参考折线上构造绕行后的实际折线
    drive(...)                          沿折线按速度行驶 + 路口停驶，生成轨迹点
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services import geo
from app.services.shenyang import ANCHORS

STEP_M = 120.0  # 折线插值步长（米）


def _lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


@dataclass
class Route:
    points: list[list[float]]          # [[lat, lng], ...] 插值后的折线
    nodes: list[dict]                  # [{"name", "lat", "lng", "index", "along_m"}]
    length_m: float

    def node_coords(self, name: str) -> tuple[float, float]:
        for n in self.nodes:
            if n["name"] == name:
                return (n["lat"], n["lng"])
        raise KeyError(name)


def polyline_from_names(names: list[str]) -> Route:
    """把锚点名序列转成插值折线（每 ~120 米一个点），并记录节点位置。"""
    if len(names) < 2:
        raise ValueError("至少需要两个锚点")
    coords = []
    nodes: list[dict] = []
    for name in names:
        if name not in ANCHORS:
            raise KeyError(f"未知沈阳锚点: {name}")
        lat, lng = ANCHORS[name]
        nodes.append({"name": name, "lat": lat, "lng": lng})
    raw = [(n["lat"], n["lng"]) for n in nodes]

    points: list[list[float]] = [list(raw[0])]
    nodes[0]["index"] = 0
    nodes[0]["along_m"] = 0.0
    for k in range(1, len(raw)):
        a, b = raw[k - 1], raw[k]
        seg_len = geo.haversine_m(a[0], a[1], b[0], b[1])
        n_sub = max(1, int(math.ceil(seg_len / STEP_M)))
        for j in range(1, n_sub + 1):
            lat, lng = _lerp(a, b, j / n_sub)
            points.append([round(lat, 7), round(lng, 7)])
        nodes[k]["index"] = len(points) - 1
        nodes[k]["along_m"] = round(geo.polyline_length_m(points), 1)
    cum = geo.cumulative_m(points)
    for n in nodes:
        n["along_m"] = round(cum[n["index"]], 1)
    return Route(points=points, nodes=nodes, length_m=round(cum[-1], 1))


def nearest_node(route: Route, lat: float, lng: float) -> dict:
    return min(route.nodes, key=lambda n: geo.haversine_m(lat, lng, n["lat"], n["lng"]))


def detour_midpoint(a: tuple[float, float], b: tuple[float, float], extra_m: float) -> tuple[float, float]:
    """求绕行中点 P：使 A→P→B 的长度比 A→B 直线恰好多 extra_m。

    P 位于 A—B 的垂直平分线上（向西偏），用牛顿迭代校正 Haversine 与平面近似的误差。
    """
    lat_mid = (a[0] + b[0]) / 2
    lng_mid = (a[1] + b[1]) / 2
    cos_lat = math.cos(math.radians(lat_mid))
    # A→B 的平面向量（米）
    ax = (b[1] - a[1]) * cos_lat * geo.METERS_PER_DEG
    ay = (b[0] - a[0]) * geo.METERS_PER_DEG
    norm = math.hypot(ax, ay)
    if norm <= 0:
        raise ValueError("锚点重合，无法构造绕行")
    ux, uy = ax / norm, ay / norm
    # 西侧法向量（垂直于路线方向）
    nx, ny = -uy, ux
    if nx > 0:  # 保证向西（lng 减小）
        nx, ny = -nx, -ny

    half = norm / 2
    target_half = (norm + extra_m) / 2
    h = math.sqrt(max(0.0, target_half ** 2 - half ** 2))
    for _ in range(4):  # 牛顿迭代修正球面与平面近似的误差
        # 球面实际长度（A→P→B）
        px = lng_mid + nx * h / (cos_lat * geo.METERS_PER_DEG)
        py = lat_mid + ny * h / geo.METERS_PER_DEG
        actual = geo.haversine_m(a[0], a[1], py, px) + geo.haversine_m(py, px, b[0], b[1])
        err = target_half - actual / 2  # 目标半长 - 实际半长
        if abs(err) < 0.1:
            break
        slope = h / math.hypot(h, half) if math.hypot(h, half) > 0 else 1.0
        h += err / slope
    return (lat_mid + ny * h / geo.METERS_PER_DEG, lng_mid + nx * h / (cos_lat * geo.METERS_PER_DEG))


def _interp_segment(a: tuple[float, float], b: tuple[float, float], step_m: float = STEP_M) -> list[list[float]]:
    seg_len = geo.haversine_m(a[0], a[1], b[0], b[1])
    n = max(1, int(math.ceil(seg_len / step_m)))
    return [
        [round(_lerp(a, b, j / n)[0], 7), round(_lerp(a, b, j / n)[1], 7)]
        for j in range(1, n + 1)
    ]


def with_detour(
    ref: Route, entry_name: str, exit_name: str, extra_m: float, via_name: str = "三好街"
) -> tuple[Route, dict]:
    """在参考路径上构造绕行折线：entry → via（西侧绕行点）→ exit。

    返回 (实际 Route, 绕行元数据)。绕行点命名为 via_name（演示中即三好街一带）。
    """
    ia = next(i for i, n in enumerate(ref.nodes) if n["name"] == entry_name)
    ib = next(i for i, n in enumerate(ref.nodes) if n["name"] == exit_name)
    if ib <= ia:
        raise ValueError("exit 必须位于 entry 之后")
    a = (ref.nodes[ia]["lat"], ref.nodes[ia]["lng"])
    b = (ref.nodes[ib]["lat"], ref.nodes[ib]["lng"])
    via = detour_midpoint(a, b, extra_m)

    seg = [[a[0], a[1]]] + _interp_segment(a, via) + _interp_segment(via, b)
    # 用绕行段替换 entry→exit 之间的参考点（按节点的折线索引切割，而非节点列表下标）
    ia_pt = int(ref.nodes[ia]["index"])
    ib_pt = int(ref.nodes[ib]["index"])
    points = ref.points[: ia_pt + 1] + seg[1:] + ref.points[ib_pt + 1:]
    via_node = {"name": via_name, "lat": round(via[0], 7), "lng": round(via[1], 7)}

    cum = geo.cumulative_m(points)
    nodes = [dict(n) for n in ref.nodes]
    # 重建节点索引（绕行后参考节点位置变化）
    rebuilt: list[dict] = []
    for n in nodes:
        if ia_pt < int(n["index"]) < ib_pt:
            continue  # 被绕行的中间参考节点不再在实际路径上
        lat, lng = n["lat"], n["lng"]
        idx = min(range(len(points)), key=lambda i: geo.haversine_m(lat, lng, points[i][0], points[i][1]))
        rebuilt.append({"name": n["name"], "lat": lat, "lng": lng, "index": idx, "along_m": round(cum[idx], 1)})
    rebuilt.append({"name": via_name, "lat": via_node["lat"], "lng": via_node["lng"],
                    "index": None, "along_m": None})
    meta = {
        "entry_name": entry_name,
        "exit_name": exit_name,
        "via": via_node,
        "ref_leg_m": round(geo.haversine_m(a[0], a[1], b[0], b[1]), 1),
    }
    return Route(points=points, nodes=rebuilt, length_m=round(cum[-1], 1)), meta


def drive(
    points: list[list[float]],
    t0: datetime,
    speed_mps: float = 11.0,
    stops: list[tuple[str, float]] | None = None,
    node_index_by_name: dict[str, int] | None = None,
    step_s: float = 15.0,
) -> list[dict]:
    """沿折线行驶生成轨迹点（ts/lat/lng/speed_mps），可在指定节点停驶。

    stops: [(节点名, 停驶秒数), ...]；节点名经 node_index_by_name 映射到折线索引。
    """
    if len(points) < 2:
        raise ValueError("轨迹折线至少两个点")
    stop_by_index: dict[int, float] = {}
    for name, seconds in (stops or []):
        idx = (node_index_by_name or {})[name]
        stop_by_index[idx] = float(seconds)

    out: list[dict] = [{"ts": t0, "lat": points[0][0], "lng": points[0][1], "speed_mps": 0.0}]
    t = t0
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        dist = geo.haversine_m(a[0], a[1], b[0], b[1])
        dt = dist / speed_mps
        k = max(1, int(math.ceil(dt / step_s)))
        for j in range(1, k + 1):
            frac = j / k
            lat, lng = _lerp((a[0], a[1]), (b[0], b[1]), frac)
            t = t + timedelta(seconds=dt / k)
            is_stop_arrival = i in stop_by_index and j == k
            out.append(
                {
                    "ts": t,
                    "lat": round(lat, 7),
                    "lng": round(lng, 7),
                    "speed_mps": 0.0 if is_stop_arrival else round(speed_mps, 3),
                }
            )
        if i in stop_by_index:
            seconds = stop_by_index[i]
            emitted = 0.0
            while emitted < seconds - 1e-9:
                inc = min(step_s, seconds - emitted)
                t = t + timedelta(seconds=inc)
                out.append({"ts": t, "lat": b[0], "lng": b[1], "speed_mps": 0.0})
                emitted += inc
    return out


def trajectory_distance(points: list[dict]) -> float:
    return sum(
        geo.haversine_m(points[i - 1]["lat"], points[i - 1]["lng"], points[i]["lat"], points[i]["lng"])
        for i in range(1, len(points))
    )
