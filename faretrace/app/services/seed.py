"""沈阳模拟种子数据（全部合成，锚点坐标为 OpenStreetMap 公开数据近似值）。

7 笔种子订单，起点全部固定为东北大学浑南校区（约 41.6600, 123.4200），
终点从沈阳真实地点列表中选取：
    SY-1001    正常订单：东北大学浑南校区 → 沈阳南站（起步价区间），拟合度高，无差额
    SY-1002    晚高峰拥堵：浑南中路 3 处路口低速等候（160/120/90 秒，高峰费率）
    SY-1003    绕路订单：浑南西路提前绕行胜利大街（多走约 380 米）
    SY-1004    跨夜间时段：21:55 上车、约 22:03 结束，按夜间/冷气档换档
    SY-1005    含残差：轨迹缺失 + 实付存在无法归因调整（残差 >0.01 元 → 待核查）
    SY-1006    返空费：东北大学浑南校区 → 北陵公园（>15 公里触发阶梯返空费）
    FT-SY-1800 生活化验收：晚高峰，预估 18.00 元 → 实付 27.00 元
              （等候费 +6.00 = 4×180 秒高峰；绕路增量 +3.00 = 1.5 公里；残差 0）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_config
from app.db import reset_db
from app.models import Bill, EstimateSnapshot, Order, PricingRule, ReferenceRoute, TrajectoryPoint
from app.services import roadnet, shenyang
from app.services.attribution import run_attribution
from app.services.ingest import persist_attribution
from app.services.pricing import money

# 校园 feeder 走廊：东北大学浑南校区 → 浑南中路 → 浑南西路 → 浑河桥南（约 9.1 公里）
CAMPUS_LINE = ["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站", "浑南西路", "浑河桥南"]
# 北向长线（返空费场景）：校园 → 浑河桥南 → 青年大街 → 市府广场 → 北陵公园（约 18.7 公里）
CAMPUS_NORTH_LINE = CAMPUS_LINE + [
    "南二环—青年大街", "五里河", "青年大街—文萃路", "青年大街—科普路",
    "青年大街—文化路", "青年大街—文艺路", "青年大街—大西路", "青年大街—十一纬路",
    "青年大街—市府大路", "市府广场", "沈阳北站", "北陵公园",
]


def _mk_order(
    session: Session,
    cfg,
    *,
    order_no: str,
    origin: str,
    destination: str,
    ref: roadnet.Route,
    actual: roadnet.Route,
    trajectory: list[dict],
    requested_at: datetime,
    est_distance_m: int,
    est_duration_s: int,
    actual_distance_m: int,
    wait_fee_total: Decimal,
    paid_adjustment: Decimal = Decimal("0"),
) -> Order:
    """按沈阳固定规则构造预估价快照与实付账单，写入轨迹点。

    实付 = 实付档起步价 + 里程运价(实付里程) + 返空费(实付里程) + 等候费合计 + 调整
    预估 = 预估档起步价 + 里程运价(预估里程) + 返空费(预估里程)（预估不含等候）
    """
    est_version = shenyang.rule_for_time(cfg, requested_at)
    est_params = shenyang.params_for(cfg, est_version)
    actual_duration_s = (
        int((trajectory[-1]["ts"] - trajectory[0]["ts"]).total_seconds())
        if trajectory
        else est_duration_s
    )
    completed_at = requested_at + timedelta(seconds=actual_duration_s)
    bill_version = shenyang.rule_for_time(cfg, completed_at)
    bill_params = shenyang.params_for(cfg, bill_version)

    quoted = money(
        shenyang.core_fare(est_params, est_distance_m)
        + shenyang.return_fee(cfg, est_params, est_distance_m)
    )
    paid = money(
        shenyang.core_fare(bill_params, actual_distance_m)
        + shenyang.return_fee(cfg, bill_params, actual_distance_m)
        + wait_fee_total
        + paid_adjustment
    )

    order = Order(
        order_no=order_no,
        city="沈阳",
        status="COMPLETED",
        requested_at=requested_at,
        completed_at=completed_at,
        origin_label=origin,
        destination_label=destination,
    )
    order.estimate = EstimateSnapshot(
        quoted_price=quoted,
        est_distance_m=est_distance_m,
        est_duration_s=est_duration_s,
        rule_version=est_version,
        surge_multiplier=Decimal("1.00"),
        surcharges_json={},
        discounts_json={},
    )
    order.bill = Bill(
        paid_amount=paid,
        actual_distance_m=actual_distance_m,
        actual_duration_s=actual_duration_s,
        base_fare=bill_params.base_fare,
        distance_fare=money(shenyang.mileage_fare(bill_params, actual_distance_m)),
        time_fare=Decimal("0"),
        surge_multiplier=Decimal("1.00"),
        surcharges_json={},
        other_fees_json={},
        discounts_json={},
        rule_version=bill_version,
    )
    order.reference_route = ReferenceRoute(
        polyline_json=[[p[0], p[1]] for p in ref.points],
        nodes_json=[{"name": n["name"], "lat": n["lat"], "lng": n["lng"]} for n in ref.nodes],
        length_m=int(round(ref.length_m)),
        est_duration_s=est_duration_s,
        free_flow_speed_kmh=40,
    )
    for i, point in enumerate(trajectory):
        order.points.append(
            TrajectoryPoint(
                seq=i, ts=point["ts"], lat=point["lat"], lng=point["lng"], speed_mps=point["speed_mps"]
            )
        )
    session.add(order)
    session.flush()
    return order


def seed_orders(session: Session) -> list[Order]:
    """写入 7 笔沈阳种子订单（含归因、等候/绕路事件与双视角账单）。"""
    cfg = get_config()
    for version in ("SY-DAY", "SY-NIGHT"):
        params = shenyang.params_for(cfg, version)
        session.merge(
            PricingRule(
                version=version,
                base_fare=params.base_fare,
                per_km=money(params.per_km),
                per_min=Decimal("0"),
                min_fare=Decimal("0"),
                surge_multiplier=Decimal("1.00"),
                note=(
                    f"沈阳出租车固定计费档位 {version}：起步价 {params.base_fare} 元/"
                    f"{params.included_km_m // 1000} 公里，每 {params.meter_step_m} 米 1 元"
                ),
            )
        )
    session.flush()
    orders: list[Order] = []

    # ---------- SY-1001 正常订单（起步价区间，无差额） ----------
    ref1 = roadnet.polyline_from_names(["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站"])
    t1 = datetime(2026, 3, 5, 10, 0, 0)
    pts1 = roadnet.drive(ref1.points, t1, speed_mps=8.0)
    est1 = int(round(ref1.length_m))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1001", origin="东北大学浑南校区", destination="沈阳南站",
            ref=ref1, actual=ref1, trajectory=pts1,
            requested_at=t1, est_distance_m=est1, est_duration_s=int(est1 / 8.0),
            actual_distance_m=est1, wait_fee_total=Decimal("0"),
        )
    )

    # ---------- SY-1002 晚高峰拥堵（3 处路口等候：160/120/90 秒，全高峰） ----------
    ref2 = roadnet.polyline_from_names(CAMPUS_LINE)
    t2 = datetime(2026, 3, 5, 16, 40, 0)
    idx2 = {n["name"]: n["index"] for n in ref2.nodes}
    pts2 = roadnet.drive(
        ref2.points, t2, speed_mps=11.0,
        stops=[("浑南中路—金阳大街", 160), ("沈阳南站", 120), ("浑南西路", 90)],
        node_index_by_name=idx2,
    )
    est2 = int(round(ref2.length_m))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1002", origin="东北大学浑南校区", destination="浑河桥南",
            ref=ref2, actual=ref2, trajectory=pts2,
            requested_at=t2, est_distance_m=est2, est_duration_s=int(est2 / 11.0),
            actual_distance_m=est2,
            wait_fee_total=money(Decimal(160) / 120 + Decimal(120) / 120 + Decimal(90) / 120),
        )
    )

    # ---------- SY-1003 绕路订单（浑南西路绕行胜利大街，多走约 380 米） ----------
    ref3 = roadnet.polyline_from_names(CAMPUS_LINE)
    t3 = datetime(2026, 3, 5, 10, 10, 0)
    act3, _meta3 = roadnet.with_detour(ref3, "浑南西路", "浑河桥南", extra_m=375, via_name="胜利大街")
    pts3 = roadnet.drive(act3.points, t3, speed_mps=11.0)
    actual3 = int(round(roadnet.trajectory_distance(pts3)))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1003", origin="东北大学浑南校区", destination="浑河桥南",
            ref=ref3, actual=act3, trajectory=pts3,
            requested_at=t3, est_distance_m=est2, est_duration_s=int(est2 / 11.0),
            actual_distance_m=actual3, wait_fee_total=Decimal("0"),
        )
    )

    # ---------- SY-1004 跨夜间时段（21:55 上车 → 夜间档换档 + 90 秒平峰等候） ----------
    ref4 = roadnet.polyline_from_names(["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站"])
    t4 = datetime(2026, 3, 5, 21, 55, 0)
    idx4 = {n["name"]: n["index"] for n in ref4.nodes}
    pts4 = roadnet.drive(
        ref4.points, t4, speed_mps=8.0, stops=[("浑南中路—金阳大街", 90)], node_index_by_name=idx4
    )
    est4 = int(round(ref4.length_m))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1004", origin="东北大学浑南校区", destination="沈阳南站",
            ref=ref4, actual=ref4, trajectory=pts4,
            requested_at=t4, est_distance_m=est4, est_duration_s=int(est4 / 8.0),
            actual_distance_m=est4, wait_fee_total=Decimal("0.50"),
        )
    )

    # ---------- SY-1005 含残差（轨迹缺失 + 1.50 元无法归因调整 → 待核查） ----------
    ref5 = roadnet.polyline_from_names(["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站", "浑南西路"])
    est5 = int(round(ref5.length_m))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1005", origin="东北大学浑南校区", destination="浑南西路",
            ref=ref5, actual=ref5, trajectory=[],
            requested_at=datetime(2026, 3, 5, 14, 0, 0), est_distance_m=est5,
            est_duration_s=int(est5 / 11.0), actual_distance_m=est5,
            wait_fee_total=Decimal("0"), paid_adjustment=Decimal("1.50"),
        )
    )

    # ---------- SY-1006 返空费（校园 → 北陵公园，>15 公里触发阶梯返空） ----------
    ref6 = roadnet.polyline_from_names(CAMPUS_NORTH_LINE)
    t6 = datetime(2026, 3, 5, 10, 0, 0)
    pts6 = roadnet.drive(ref6.points, t6, speed_mps=11.0)
    actual6 = int(round(roadnet.trajectory_distance(pts6)))
    orders.append(
        _mk_order(
            session, cfg, order_no="SY-1006", origin="东北大学浑南校区", destination="北陵公园",
            ref=ref6, actual=ref6, trajectory=pts6,
            requested_at=t6, est_distance_m=14800, est_duration_s=int(14800 / 11.0),
            actual_distance_m=actual6, wait_fee_total=Decimal("0"),
        )
    )

    # ---------- FT-SY-1800 生活化验收（预估 18.00 → 实付 27.00，晚高峰） ----------
    ref7 = roadnet.polyline_from_names(CAMPUS_LINE)
    t7 = datetime(2026, 3, 6, 17, 5, 0)
    act7, _meta7 = roadnet.with_detour(ref7, "浑南西路", "浑河桥南", extra_m=1498, via_name="胜利大街")
    idx7 = {n["name"]: n["index"] for n in act7.nodes if n.get("index") is not None}
    pts7 = roadnet.drive(
        act7.points, t7, speed_mps=11.0,
        stops=[
            ("浑南中路—金阳大街", 180),
            ("沈阳南站", 180),
            ("浑南西路", 180),
            ("浑河桥南", 180),
        ],
        node_index_by_name=idx7,
    )
    orders.append(
        _mk_order(
            session, cfg, order_no="FT-SY-1800", origin="东北大学浑南校区", destination="浑河桥南",
            ref=ref7, actual=act7, trajectory=pts7,
            requested_at=t7, est_distance_m=7500, est_duration_s=int(7500 / 11.0),
            actual_distance_m=9000, wait_fee_total=Decimal("6.00"),  # 4 × 180s 高峰 = 4 × 1.50
        )
    )

    session.flush()
    for order in orders:
        draft = run_attribution(session, order, cfg)
        persist_attribution(session, order, draft, cfg)
    session.flush()
    return orders


def reset_and_seed() -> list[Order]:
    reset_db()
    from app.db import SessionLocal

    with SessionLocal() as session:
        orders = seed_orders(session)
        session.commit()
        return orders
