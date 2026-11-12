"""订单接入与归因落库（业务编排层）。

职责边界：
    routers  -> 解析/校验 HTTP，调用本层
    ingest   -> 事务内编排（建单、完单、归因、生成透明账单）
    attribution -> 纯计算，不碰数据库
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from app.services import geo
from app.services.attribution import AttributionDraft, run_attribution
from app.services.bill_view import build_views
from app.services.pricing import money


def d(value) -> Decimal:
    return money(value if value is not None else 0)


def fees_to_json(items: dict | None) -> dict:
    """JSON 列只存字符串金额，避免 Decimal 无法序列化。"""
    if not items:
        return {}
    return {str(k): f"{money(v):.2f}" for k, v in items.items()}


def get_order(session: Session, order_no: str) -> Order:
    order = session.scalars(select(Order).where(Order.order_no == order_no)).one_or_none()
    if order is None:
        raise NotFoundError(f"订单 {order_no} 不存在", details={"order_no": order_no})
    return order


def _require_rule(session: Session, version: str) -> PricingRule:
    rule = session.get(PricingRule, version)
    if rule is None:
        raise ValidationError(
            f"计价规则版本 {version} 不存在，请先创建该规则或改用已存在的版本",
            details={"rule_version": version},
        )
    return rule


def create_order(session: Session, payload, cfg) -> Order:
    """创建订单 + 预估价快照 + 参考路径。"""
    order_no = payload.order_no or f"FT-{utcnow().strftime('%Y%m%d%H%M%S%f')[:18]}"
    if session.scalars(select(Order).where(Order.order_no == order_no)).one_or_none() is not None:
        raise ConflictError(f"订单号 {order_no} 已存在", details={"order_no": order_no})

    estimate = payload.estimate
    rule = _require_rule(session, estimate.rule_version)

    polyline = [[float(p[0]), float(p[1])] for p in payload.reference_route.polyline]
    if not geo.is_valid_polyline(polyline):
        raise ValidationError("参考路径至少需要两个不同坐标点", details={"polyline": polyline})
    length_m = payload.reference_route.length_m or int(round(geo.polyline_length_m(polyline)))
    if length_m <= 0:
        raise ValidationError("参考路径长度必须大于 0")

    order = Order(
        order_no=order_no,
        city=payload.city,
        status="CREATED",
        requested_at=payload.requested_at or utcnow(),
        origin_label=payload.origin_label,
        destination_label=payload.destination_label,
        created_at=utcnow(),
    )
    order.estimate = EstimateSnapshot(
        quoted_price=d(estimate.quoted_price),
        est_distance_m=int(estimate.est_distance_m),
        est_duration_s=int(estimate.est_duration_s),
        rule_version=rule.version,
        surge_multiplier=Decimal(str(estimate.surge_multiplier)),
        surcharges_json=fees_to_json(estimate.surcharges),
        discounts_json=fees_to_json(estimate.discounts),
    )
    order.reference_route = ReferenceRoute(
        polyline_json=polyline,
        length_m=int(length_m),
        est_duration_s=int(estimate.est_duration_s),
        free_flow_speed_kmh=int(payload.reference_route.free_flow_speed_kmh),
    )
    session.add(order)
    session.flush()
    return order


def complete_order(session: Session, order: Order, payload, cfg) -> Attribution:
    """完成订单：写入实付账单 + 轨迹，触发归因，生成透明账单。"""
    if order.bill is not None:
        raise ConflictError(
            f"订单 {order.order_no} 已完成，如需重算请调用 /attribute 接口",
            details={"order_no": order.order_no},
        )

    bill_payload = payload.bill
    rule = _require_rule(session, bill_payload.rule_version)

    order.bill = Bill(
        paid_amount=d(bill_payload.paid_amount),
        actual_distance_m=int(bill_payload.actual_distance_m),
        actual_duration_s=int(bill_payload.actual_duration_s),
        base_fare=d(bill_payload.base_fare),
        distance_fare=d(bill_payload.distance_fare),
        time_fare=d(bill_payload.time_fare),
        surge_multiplier=Decimal(str(bill_payload.surge_multiplier)),
        surcharges_json=fees_to_json(bill_payload.surcharges),
        other_fees_json=fees_to_json(bill_payload.other_fees),
        discounts_json=fees_to_json(bill_payload.discounts),
        rule_version=rule.version,
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
    order.completed_at = payload.completed_at or utcnow()
    order.status = "COMPLETED"
    session.flush()

    draft = run_attribution(session, order, cfg)
    return persist_attribution(session, order, draft, cfg)


def persist_attribution(session: Session, order: Order, draft: AttributionDraft, cfg) -> Attribution:
    """把归因结果落库（同订单幂等更新，revision+1），并生成双视角透明账单。"""
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
    session.refresh(attribution)

    passenger_view, driver_view = build_views(order, attribution, attribution.items)
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
