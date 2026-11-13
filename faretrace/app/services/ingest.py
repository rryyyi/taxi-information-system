"""订单接入与归因落库（业务编排层）。

沈阳口径：
    预估档位 rule_for_time(requested_at)、实付档位 rule_for_time(completed_at)
    由沈阳固定规则自动判定（白天/夜间），无需调用方传入。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Attribution,
    AttributionItem,
    Bill,
    EstimateSnapshot,
    Order,
    PricingRule,
    ReferenceRoute,
    TransparentBill,
    TrajectoryPoint,
    utcnow,
)
from app.services import geo, shenyang
from app.services.attribution import AttributionDraft, run_attribution
from app.services.bill_view import build_views
from app.services.pricing import money


def d(value) -> Decimal:
    return money(value if value is not None else 0)


def get_order(session: Session, order_no: str) -> Order:
    order = session.scalars(select(Order).where(Order.order_no == order_no)).one_or_none()
    if order is None:
        raise NotFoundError(f"订单 {order_no} 不存在", details={"order_no": order_no})
    return order


def _ensure_rule(session: Session, cfg, version: str):
    """沈阳固定档位（SY-DAY/SY-NIGHT）首次使用时写入 pricing_rules 表（审计留痕）。"""
    rule = session.get(PricingRule, version)
    if rule is None:
        params = shenyang.params_for(cfg, version)
        rule = PricingRule(
            version=version,
            base_fare=params.base_fare,
            per_km=money(params.per_km),
            per_min=Decimal("0"),
            min_fare=Decimal("0"),
            surge_multiplier=Decimal("1.00"),
            note=f"沈阳出租车固定计费档位 {version}（起步价/里程运价/返空/等候见 config）",
        )
        session.add(rule)
        session.flush()
    return rule


def create_order(session: Session, payload, cfg) -> Order:
    """创建订单 + 预估价快照 + 参考路径（沈阳档位按请求时间自动判定）。"""
    order_no = payload.order_no or f"FT-SY-{utcnow().strftime('%Y%m%d%H%M%S%f')[:18]}"
    if session.scalars(select(Order).where(Order.order_no == order_no)).one_or_none() is not None:
        raise ConflictError(f"订单号 {order_no} 已存在", details={"order_no": order_no})

    estimate = payload.estimate
    requested_at = payload.requested_at or utcnow()
    est_version = shenyang.rule_for_time(cfg, requested_at)
    _ensure_rule(session, cfg, est_version)

    polyline = [[float(p[0]), float(p[1])] for p in payload.reference_route.polyline]
    if not geo.is_valid_polyline(polyline):
        raise ValidationError("参考路径至少需要两个不同坐标点", details={"polyline": polyline})
    length_m = payload.reference_route.length_m or int(round(geo.polyline_length_m(polyline)))
    if length_m <= 0:
        raise ValidationError("参考路径长度必须大于 0")
    # 节点名（沈阳道路锚点）解析为坐标，供等候/绕路事件定位与地图展示
    nodes_payload: list[dict] = []
    for name in payload.reference_route.node_names or []:
        if name in shenyang.ANCHORS:
            lat, lng = shenyang.ANCHORS[name]
        else:
            lat, lng = polyline[0]
        nodes_payload.append({"name": name, "lat": lat, "lng": lng})

    order = Order(
        order_no=order_no,
        city="沈阳",
        status="CREATED",
        requested_at=requested_at,
        origin_label=payload.origin_label,
        destination_label=payload.destination_label,
        created_at=utcnow(),
    )
    order.estimate = EstimateSnapshot(
        quoted_price=d(estimate.quoted_price),
        est_distance_m=int(estimate.est_distance_m),
        est_duration_s=int(estimate.est_duration_s),
        rule_version=est_version,
        surge_multiplier=Decimal("1.00"),
        surcharges_json={},
        discounts_json={},
    )
    order.reference_route = ReferenceRoute(
        polyline_json=polyline,
        nodes_json=nodes_payload,
        length_m=int(length_m),
        est_duration_s=int(estimate.est_duration_s),
        free_flow_speed_kmh=int(payload.reference_route.free_flow_speed_kmh),
    )
    session.add(order)
    session.flush()
    return order


def complete_order(session: Session, order: Order, payload, cfg) -> Attribution:
    """完成订单：写入实付账单 + 轨迹，触发归因，生成透明账单与事件。"""
    if order.bill is not None:
        raise ConflictError(
            f"订单 {order.order_no} 已完成，如需重算请调用 /attribute 接口",
            details={"order_no": order.order_no},
        )

    bill_payload = payload.bill
    completed_at = payload.completed_at or utcnow()
    bill_version = shenyang.rule_for_time(cfg, completed_at)
    _ensure_rule(session, cfg, bill_version)
    bill_params = shenyang.params_for(cfg, bill_version)

    order.bill = Bill(
        paid_amount=d(bill_payload.paid_amount),
        actual_distance_m=int(bill_payload.actual_distance_m),
        actual_duration_s=int(bill_payload.actual_duration_s),
        base_fare=bill_params.base_fare,
        distance_fare=money(
            shenyang.mileage_fare(bill_params, bill_payload.actual_distance_m)
        ),
        time_fare=Decimal("0"),
        surge_multiplier=Decimal("1.00"),
        surcharges_json={},
        other_fees_json={},
        discounts_json={},
        rule_version=bill_version,
    )
    for i, point in enumerate(payload.trajectory or []):
        order.points.append(
            TrajectoryPoint(
                seq=int(point.seq if point.seq is not None else i),
                ts=point.ts,
                lat=float(point.lat),
                lng=float(point.lng),
                speed_mps=None if point.speed_mps is None else float(point.speed_mps),
            )
        )
    order.completed_at = completed_at
    order.status = "COMPLETED"
    session.flush()

    draft = run_attribution(session, order, cfg)
    return persist_attribution(session, order, draft, cfg)


def persist_attribution(session: Session, order: Order, draft: AttributionDraft, cfg) -> Attribution:
    """归因结果落库（幂等更新，revision+1）：归因项 + 等候/绕路事件 + 双视角账单。"""
    attribution = order.attribution
    if attribution is None:
        attribution = Attribution(order_id=order.id, revision=0)
        session.add(attribution)
        order.attribution = attribution
    attribution.total_delta = d(draft.total_delta)
    attribution.explained_amount = d(draft.explained_amount)
    attribution.residual = d(draft.residual)
    attribution.fit_score = None if draft.fit_score is None else Decimal(str(round(draft.fit_score, 4)))
    attribution.avg_deviation_m = (
        None if draft.avg_deviation_m is None else Decimal(str(round(draft.avg_deviation_m, 2)))
    )
    attribution.data_quality = draft.data_quality
    attribution.status = draft.status
    attribution.quality_flags_json = list(draft.quality_flags)
    attribution.evidence_bundle_json = draft.evidence_bundle
    attribution.algorithm_version = draft.algorithm_version or cfg.algorithm_version
    attribution.config_version = draft.config_version or cfg.config_version
    attribution.revision = int(attribution.revision or 0) + 1
    attribution.computed_at = utcnow()
    attribution.wait_events = []
    attribution.detour_events = []
    session.flush()

    existing = {item.code: item for item in attribution.items}
    for draft_item in draft.sorted_items():
        item = existing.get(draft_item.code)
        if item is None:
            item = AttributionItem(attribution_id=attribution.id, code=draft_item.code, label=draft_item.label)
            session.add(item)
        item.label = draft_item.label
        item.amount = d(draft_item.amount)
        item.description = draft_item.description
        item.evidence_json = draft_item.evidence
        item.impact_class = draft_item.impact_class
        item.sort_order = draft_item.sort_order
    session.flush()
    session.refresh(attribution)  # 刷新关系集合（同会话内新增 items 后需重载）

    from app.models import DetourEvent, WaitEvent

    for e in draft.wait_events:
        attribution.wait_events.append(
            WaitEvent(
                order_id=order.id,
                attribution_id=attribution.id,
                location=e.location,
                t_start=e.t_start,
                t_end=e.t_end,
                duration_s=e.duration_s,
                avg_speed_kmh=e.avg_speed_kmh,
                peak_seconds=e.peak_seconds,
                offpeak_seconds=e.offpeak_seconds,
                peak_label=e.peak_label,
                fee=d(e.fee),
                lat=e.lat,
                lng=e.lng,
                evidence_json=e.evidence,
            )
        )
    for e in draft.detour_events:
        attribution.detour_events.append(
            DetourEvent(
                order_id=order.id,
                attribution_id=attribution.id,
                entry_name=e.entry_name,
                exit_name=e.exit_name,
                entry_lat=e.entry_lat,
                entry_lng=e.entry_lng,
                exit_lat=e.exit_lat,
                exit_lng=e.exit_lng,
                ref_distance_m=e.ref_distance_m,
                actual_distance_m=e.actual_distance_m,
                extra_distance_m=e.extra_distance_m,
                fee=d(e.fee),
                path_json=e.path,
                evidence_json=e.evidence,
            )
        )
    session.flush()

    passenger_view, driver_view = build_views(order, attribution, attribution.items, cfg)
    if attribution.transparent_bill is None:
        attribution.transparent_bill = TransparentBill(
            attribution_id=attribution.id,
            passenger_view_json=passenger_view,
            driver_view_json=driver_view,
        )
        session.add(attribution.transparent_bill)
    else:
        attribution.transparent_bill.passenger_view_json = passenger_view
        attribution.transparent_bill.driver_view_json = driver_view
    order.status = "COMPLETED"
    session.flush()
    return attribution
