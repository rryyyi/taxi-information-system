"""合成演示数据（全部为模拟数据，不含任何真实订单/轨迹/用户信息）。

6 笔种子订单：
    FT-1001  正常订单（拟合度高、无拥堵，仅一笔优惠差异）
    FT-1002  拥堵延迟（连续低速段 600 秒）
    FT-1003  路径偏离（拟合度低 + 拥堵 + 路侧杂费）
    FT-1004  计价规则变化（起步价/里程单价上调）
    FT-1005  轨迹缺失 + 残差不为零（降级计算）
    FT-1800  生活化验收样本：18.00 → 27.00（拥堵 4.20 + 路径 3.10 + 规则 1.70 + 残差 0）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_config
from app.db import reset_db
from app.models import (
    Bill,
    EstimateSnapshot,
    Order,
    PricingRule,
    ReferenceRoute,
    TrajectoryPoint,
)
from app.services import geo
from app.services.attribution import run_attribution
from app.services.ingest import persist_attribution
from app.services.pricing import PricingParams, evaluate, money, sum_fees


# --------------------------------------------------------------------------- #
# 参考路径与轨迹合成
# --------------------------------------------------------------------------- #
def make_reference(length_m: int, start_lat: float = 30.0, start_lng: float = 120.0) -> list[list[float]]:
    """生成一条总长约 length_m 的折线（4 段、带两个转弯）。"""
    lat, lng = start_lat, start_lng
    points = [[lat, lng]]
    bearings = [(90.0, 0.30), (60.0, 0.25), (120.0, 0.25), (85.0, 0.20)]
    for bearing_deg, ratio in bearings:
        step = length_m * ratio
        rad = math.radians(bearing_deg)
        dx, dy = step * math.sin(rad), step * math.cos(rad)
        lat, lng = geo.offset_point(lat, lng, dx, dy)
        points.append([round(lat, 7), round(lng, 7)])
    return points


@dataclass
class Leg:
    """轨迹片段的合成参数（沿参考路径推进 along，并可横向偏移 offset）。"""

    duration_s: float
    along_from: float
    along_to: float
    offset_from: float = 0.0
    offset_to: float = 0.0
    stopped: bool = False


def _offset_position(ref: list[list[float]], along: float, offset_m: float) -> tuple[float, float]:
    lat, lng = geo.interpolate_along(ref, along)
    if offset_m == 0:
        return lat, lng
    a = geo.interpolate_along(ref, max(0.0, along - 1.0))
    b = geo.interpolate_along(ref, along + 1.0)
    cos_lat = math.cos(math.radians(lat))
    ax = (a[1]) * cos_lat * geo.METERS_PER_DEG
    ay = (a[0]) * geo.METERS_PER_DEG
    bx = (b[1]) * cos_lat * geo.METERS_PER_DEG
    by = (b[0]) * geo.METERS_PER_DEG
    vx, vy = bx - ax, by - ay
    norm = math.hypot(vx, vy) or 1.0
    nx, ny = -vy / norm, vx / norm
    return geo.offset_point(lat, lng, nx * offset_m, ny * offset_m)


def build_trajectory(
    ref: list[list[float]], t0: datetime, legs: list[Leg], step_s: float = 20.0
) -> list[dict]:
    """按 Leg 列表合成轨迹点（ts/lat/lng/speed_mps）。"""
    points: list[dict] = []
    t = t0
    for leg in legs:
        n = max(2, int(math.ceil(leg.duration_s / step_s)) + 1)
        prev_pos = None
        for k in range(n):
            frac = k / (n - 1)
            along = leg.along_from + (leg.along_to - leg.along_from) * frac
            offset = leg.offset_from + (leg.offset_to - leg.offset_from) * frac
            lat, lng = _offset_position(ref, along, offset)
            ts = t + timedelta(seconds=leg.duration_s * frac)
            speed = 0.0
            if leg.stopped:
                speed = 0.0
            elif k > 0 and prev_pos is not None:
                dist = geo.haversine_m(prev_pos[0], prev_pos[1], lat, lng)
                dt = leg.duration_s / (n - 1)
                speed = dist / dt if dt > 0 else 0.0
            points.append(
                {"ts": ts, "lat": round(lat, 7), "lng": round(lng, 7), "speed_mps": round(speed, 3)}
            )
            prev_pos = (lat, lng)
        t = t + timedelta(seconds=leg.duration_s)
    return points


def trajectory_distance(points: list[dict]) -> float:
    return sum(
        geo.haversine_m(points[i - 1]["lat"], points[i - 1]["lng"], points[i]["lat"], points[i]["lng"])
        for i in range(1, len(points))
    )


# --------------------------------------------------------------------------- #
# 种子定义
# --------------------------------------------------------------------------- #
RULES = {
    "R-EST-1": dict(base=Decimal("5.00"), per_km=Decimal("2.00"), per_min=Decimal("0.40"), min_fare=Decimal("0")),
    "R-TRAFFIC-1": dict(base=Decimal("4.55"), per_km=Decimal("1.90"), per_min=Decimal("0.35"), min_fare=Decimal("0")),
    "R-PATH-1": dict(base=Decimal("4.20"), per_km=Decimal("1.80"), per_min=Decimal("0.35"), min_fare=Decimal("0")),
    "R-EST-4": dict(base=Decimal("3.40"), per_km=Decimal("2.10"), per_min=Decimal("0.40"), min_fare=Decimal("0")),
    "R-NEW-4": dict(base=Decimal("5.40"), per_km=Decimal("2.40"), per_min=Decimal("0.40"), min_fare=Decimal("0")),
    "R-LOWINFO-1": dict(base=Decimal("6.50"), per_km=Decimal("1.50"), per_min=Decimal("0.35"), min_fare=Decimal("0")),
    "R-1800-EST": dict(base=Decimal("4.56"), per_km=Decimal("1.55"), per_min=Decimal("0.30"), min_fare=Decimal("0")),
    "R-1800-BILL": dict(base=Decimal("6.26"), per_km=Decimal("1.55"), per_min=Decimal("0.30"), min_fare=Decimal("0")),
}


def _params(version: str) -> PricingParams:
    spec = RULES[version]
    return PricingParams(
        base_fare=spec["base"],
        per_km=spec["per_km"],
        per_min=spec["per_min"],
        min_fare=spec["min_fare"],
        surge_multiplier=Decimal("1.00"),
    )


def _order(
    session: Session,
    *,
    order_no: str,
    city: str,
    origin: str,
    destination: str,
    ref_length_m: int,
    est_duration_s: int,
    est_rule: str,
    bill_rule: str,
    actual_distance_m: int,
    actual_duration_s: int,
    t0: datetime,
    trajectory: list[dict],
    est_surcharges: dict | None = None,
    bill_surcharges: dict | None = None,
    bill_other: dict | None = None,
    est_discounts: dict | None = None,
    bill_discounts: dict | None = None,
    paid_adjustment: Decimal = Decimal("0"),
) -> Order:
    ref = make_reference(ref_length_m)
    est_distance_m = int(ref_length_m)

    est_p = _params(est_rule)
    bill_p = _params(bill_rule)
    quoted = evaluate(
        est_p,
        est_distance_m / 1000.0,
        est_duration_s / 60.0,
        surcharges=sum_fees(est_surcharges),
        discounts=sum_fees(est_discounts),
    )
    distance_fare = money(bill_p.per_km * Decimal(str(actual_distance_m)) / Decimal("1000"))
    time_fare = money(bill_p.per_min * Decimal(str(actual_duration_s)) / Decimal("60"))
    base_fare = money(bill_p.base_fare)
    paid = money(
        base_fare
        + distance_fare
        + time_fare
        + sum_fees(bill_surcharges)
        + sum_fees(bill_other)
        - sum_fees(bill_discounts)
        + paid_adjustment
    )

    order = Order(
        order_no=order_no,
        city=city,
        status="COMPLETED",
        requested_at=t0,
        completed_at=t0 + timedelta(seconds=actual_duration_s),
        origin_label=origin,
        destination_label=destination,
    )
    order.estimate = EstimateSnapshot(
        quoted_price=quoted,
        est_distance_m=est_distance_m,
        est_duration_s=est_duration_s,
        rule_version=est_rule,
        surge_multiplier=Decimal("1.00"),
        surcharges_json=est_surcharges or {},
        discounts_json=est_discounts or {},
    )
    order.bill = Bill(
        paid_amount=paid,
        actual_distance_m=actual_distance_m,
        actual_duration_s=actual_duration_s,
        base_fare=base_fare,
        distance_fare=distance_fare,
        time_fare=time_fare,
        surge_multiplier=Decimal("1.00"),
        surcharges_json=bill_surcharges or {},
        other_fees_json=bill_other or {},
        discounts_json=bill_discounts or {},
        rule_version=bill_rule,
    )
    order.reference_route = ReferenceRoute(
        polyline_json=ref,
        length_m=int(ref_length_m),
        est_duration_s=est_duration_s,
        free_flow_speed_kmh=40,
    )
    for i, point in enumerate(trajectory):
        order.points.append(
            TrajectoryPoint(
                seq=i,
                ts=point["ts"],
                lat=point["lat"],
                lng=point["lng"],
                speed_mps=point["speed_mps"],
            )
        )
    session.add(order)
    session.flush()
    return order


def seed_orders(session: Session) -> list[Order]:
    """写入 6 笔种子订单（含归因与透明账单）。"""
    for version, spec in RULES.items():
        session.merge(
            PricingRule(
                version=version,
                base_fare=spec["base"],
                per_km=spec["per_km"],
                per_min=spec["per_min"],
                min_fare=spec["min_fare"],
                surge_multiplier=Decimal("1.00"),
                note="合成演示计价规则（非真实平台费率）",
            )
        )
    session.flush()
    orders: list[Order] = []

    # ---------- FT-1001 正常订单：拟合度高，仅一笔优惠差异 ----------
    ref1 = make_reference(5000)
    t1 = datetime(2026, 3, 2, 8, 0, 0, tzinfo=timezone.utc)
    traj1 = build_trajectory(ref1, t1, [Leg(900, 0, 5000)])
    orders.append(
        _order(
            session,
            order_no="FT-1001",
            city="演示城市",
            origin="阳光小区东门",
            destination="科创大厦 B 座",
            ref_length_m=5000,
            est_duration_s=900,
            est_rule="R-EST-1",
            bill_rule="R-EST-1",
            actual_distance_m=5000,
            actual_duration_s=900,
            t0=t1,
            trajectory=traj1,
            est_discounts={},
            bill_discounts={"首单立减": "0.40"},
        )
    )

    # ---------- FT-1002 拥堵延迟：600 秒连续低速 ----------
    ref2 = make_reference(5000)
    t2 = t1 + timedelta(minutes=12)
    traj2 = build_trajectory(
        ref2,
        t2,
        [
            Leg(510, 0, 2500),
            Leg(600, 2500, 2500, stopped=True),
            Leg(510, 2500, 5000),
        ],
    )
    orders.append(
        _order(
            session,
            order_no="FT-1002",
            city="演示城市",
            origin="地铁人民广场站",
            destination="软件园 3 号楼",
            ref_length_m=5000,
            est_duration_s=1020,
            est_rule="R-TRAFFIC-1",
            bill_rule="R-TRAFFIC-1",
            actual_distance_m=5000,
            actual_duration_s=1620,
            t0=t2,
            trajectory=traj2,
        )
    )

    # ---------- FT-1003 路径偏离：绕行 2 公里 + 拥堵 + 路侧过路费 ----------
    ref3 = make_reference(5000)
    t3 = t2 + timedelta(minutes=12)
    traj3 = build_trajectory(
        ref3,
        t3,
        [
            Leg(415, 0, 2500),
            Leg(125, 2500, 2500, offset_from=0, offset_to=1000),
            Leg(125, 2500, 2500, offset_from=1000, offset_to=0),
            Leg(300, 2500, 2500, stopped=True),
            Leg(415, 2500, 5000),
        ],
    )
    orders.append(
        _order(
            session,
            order_no="FT-1003",
            city="演示城市",
            origin="火车站南广场",
            destination="江畔花园",
            ref_length_m=5000,
            est_duration_s=1080,
            est_rule="R-PATH-1",
            bill_rule="R-PATH-1",
            actual_distance_m=7000,
            actual_duration_s=1380,
            t0=t3,
            trajectory=traj3,
            bill_surcharges={"过路费": "0.55"},
        )
    )

    # ---------- FT-1004 计价规则变化：起步价与里程单价上调 ----------
    ref4 = make_reference(6000)
    t4 = t3 + timedelta(minutes=12)
    traj4 = build_trajectory(ref4, t4, [Leg(1320, 0, 6000)])
    orders.append(
        _order(
            session,
            order_no="FT-1004",
            city="演示城市",
            origin="大学城南门",
            destination="会展中心",
            ref_length_m=6000,
            est_duration_s=1320,
            est_rule="R-EST-4",
            bill_rule="R-NEW-4",
            actual_distance_m=6000,
            actual_duration_s=1320,
            t0=t4,
            trajectory=traj4,
        )
    )

    # ---------- FT-1005 轨迹缺失 + 残差 ----------
    t5 = t4 + timedelta(minutes=12)
    orders.append(
        _order(
            session,
            order_no="FT-1005",
            city="演示城市",
            origin="老城厢弄堂口",
            destination="新区人民医院",
            ref_length_m=5000,
            est_duration_s=1200,
            est_rule="R-LOWINFO-1",
            bill_rule="R-LOWINFO-1",
            actual_distance_m=5000,
            actual_duration_s=1560,
            t0=t5,
            trajectory=[],
            paid_adjustment=Decimal("0.20"),  # 账单存在未列明调整，形成残差
        )
    )

    # ---------- FT-1800 生活化验收样本：18.00 -> 27.00 ----------
    ref6 = make_reference(4800)
    t6 = datetime(2026, 3, 3, 8, 30, 0, tzinfo=timezone.utc)
    traj6 = build_trajectory(
        ref6,
        t6,
        [
            Leg(475, 0, 2400),
            Leg(125, 2400, 2400, offset_from=0, offset_to=1000),
            Leg(125, 2400, 2400, offset_from=1000, offset_to=0),
            Leg(840, 2400, 2400, stopped=True),
            Leg(475, 2400, 4800),
        ],
    )
    orders.append(
        _order(
            session,
            order_no="FT-1800",
            city="演示城市",
            origin="桂花路 12 号",
            destination="金融城 A 塔",
            ref_length_m=4800,
            est_duration_s=1200,
            est_rule="R-1800-EST",
            bill_rule="R-1800-BILL",
            actual_distance_m=6800,
            actual_duration_s=2040,
            t0=t6,
            trajectory=traj6,
        )
    )

    session.flush()

    for order in orders:
        cfg = get_config()
        draft = run_attribution(session, order, cfg)
        persist_attribution(session, order, draft, cfg)
    session.flush()
    return orders


def reset_and_seed(session_factory) -> list[Order]:
    """重置数据库并重灌种子（演示复位 / 测试夹具共用）。"""
    reset_db()
    with session_factory() as session:
        orders = seed_orders(session)
        session.commit()
        return orders
