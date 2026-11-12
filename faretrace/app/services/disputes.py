"""争议：发起 → 冻结证据快照 → 运营复核（状态机）。

冻结原则：争议发起那一刻，把订单、预估价快照、实付账单、归因记录、全部归因项
与证据 JSON 拷贝进 disputes.evidence_snapshot_json，并逐项绑定到 dispute_evidence。
之后即使订单被重算（revision+1），争议快照也不会改变。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import DisputeStatus, InitiatorRole
from app.errors import NotFoundError, StateError, ValidationError
from app.models import AttributionItem, Dispute, DisputeEvidence, Order, utcnow
from app.services import geo
from app.services.pricing import money, to_decimal

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    DisputeStatus.OPEN.value: {DisputeStatus.REVIEWING.value, DisputeStatus.RESOLVED.value, DisputeStatus.REJECTED.value},
    DisputeStatus.REVIEWING.value: {DisputeStatus.RESOLVED.value, DisputeStatus.REJECTED.value},
    DisputeStatus.RESOLVED.value: set(),
    DisputeStatus.REJECTED.value: set(),
}


def _money_str(value) -> str:
    return f"{money(to_decimal(value)):.2f}"


def _order_bill_snapshot(order: Order) -> dict:
    est = order.estimate
    bill = order.bill
    return {
        "order": {
            "order_no": order.order_no,
            "city": order.city,
            "status": order.status,
            "requested_at": order.requested_at.isoformat() if order.requested_at else None,
            "completed_at": order.completed_at.isoformat() if order.completed_at else None,
            "origin": order.origin_label,
            "destination": order.destination_label,
        },
        "estimate_snapshot": {
            "quoted_price": _money_str(est.quoted_price),
            "est_distance_m": est.est_distance_m,
            "est_duration_s": est.est_duration_s,
            "rule_version": est.rule_version,
            "surge_multiplier": str(est.surge_multiplier),
            "surcharges": est.surcharges_json,
            "discounts": est.discounts_json,
        }
        if est
        else None,
        "bill": {
            "paid_amount": _money_str(bill.paid_amount),
            "actual_distance_m": bill.actual_distance_m,
            "actual_duration_s": bill.actual_duration_s,
            "base_fare": _money_str(bill.base_fare),
            "distance_fare": _money_str(bill.distance_fare),
            "time_fare": _money_str(bill.time_fare),
            "surcharges": bill.surcharges_json,
            "other_fees": bill.other_fees_json,
            "discounts": bill.discounts_json,
            "rule_version": bill.rule_version,
            "surge_multiplier": str(bill.surge_multiplier),
        }
        if bill
        else None,
        "trajectory": {
            "point_count": len(order.points),
            "distance_m": round(
                sum(
                    geo.haversine_m(
                        order.points[i - 1].lat,
                        order.points[i - 1].lng,
                        order.points[i].lat,
                        order.points[i].lng,
                    )
                    for i in range(1, len(order.points))
                ),
                1,
            )
            if len(order.points) > 1
            else 0.0,
        },
    }


def _item_snapshot(item: AttributionItem) -> dict:
    return {
        "id": item.id,
        "code": item.code,
        "label": item.label,
        "amount": _money_str(item.amount),
        "description": item.description,
        "impact_class": item.impact_class,
        "evidence": item.evidence_json,
    }


def create_dispute(
    session: Session,
    *,
    order_no: str,
    item_code: str,
    initiator_role: str,
    reason: str,
) -> Dispute:
    order = session.scalars(select(Order).where(Order.order_no == order_no)).one_or_none()
    if order is None:
        raise NotFoundError(f"订单 {order_no} 不存在", details={"order_no": order_no})
    if order.attribution is None:
        raise ValidationError(
            f"订单 {order_no} 尚未生成透明账单，无法发起争议", details={"order_no": order_no}
        )
    if initiator_role not in {r.value for r in InitiatorRole}:
        raise ValidationError(
            "发起角色非法，只能是 passenger 或 driver", details={"initiator_role": initiator_role}
        )

    attribution = order.attribution
    item = next((i for i in attribution.items if i.code == item_code), None)
    if item is None:
        raise ValidationError(
            f"归因项 {item_code} 不存在，可用项见 GET /api/orders/{order_no}/bill",
            details={"item_code": item_code, "available": [i.code for i in attribution.items]},
        )

    snapshot = {
        "captured_at": utcnow().isoformat(),
        "locked": True,
        "attribution": {
            "id": attribution.id,
            "revision": attribution.revision,
            "total_delta": _money_str(attribution.total_delta),
            "explained_amount": _money_str(attribution.explained_amount),
            "residual": _money_str(attribution.residual),
            "fit_score": None if attribution.fit_score is None else float(attribution.fit_score),
            "avg_deviation_m": None if attribution.avg_deviation_m is None else float(attribution.avg_deviation_m),
            "data_quality": attribution.data_quality,
            "status": attribution.status,
            "quality_flags": list(attribution.quality_flags_json or []),
            "algorithm_version": attribution.algorithm_version,
            "config_version": attribution.config_version,
        },
        "focused_item": _item_snapshot(item),
        "items": [_item_snapshot(i) for i in attribution.items],
        "raw": _order_bill_snapshot(order),
    }

    dispute = Dispute(
        order_no=order.order_no,
        attribution_id=attribution.id,
        item_code=item.code,
        initiator_role=initiator_role,
        reason=reason or "",
        status=DisputeStatus.OPEN.value,
        evidence_snapshot_json=snapshot,
        snapshot_locked=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(dispute)
    session.flush()
    session.add(
        DisputeEvidence(
            dispute_id=dispute.id,
            attribution_item_id=item.id,
            evidence_json={
                "item": _item_snapshot(item),
                "attribution_id": attribution.id,
                "revision": attribution.revision,
                "locked": True,
            },
        )
    )
    session.flush()
    return dispute


def get_dispute(session: Session, dispute_id: int) -> Dispute:
    dispute = session.get(Dispute, dispute_id)
    if dispute is None:
        raise NotFoundError(f"争议 {dispute_id} 不存在", details={"dispute_id": dispute_id})
    return dispute


def list_disputes(session: Session, *, status: str | None = None, order_no: str | None = None) -> list[Dispute]:
    stmt = select(Dispute)
    if status:
        stmt = stmt.where(Dispute.status == status)
    if order_no:
        stmt = stmt.where(Dispute.order_no == order_no)
    return list(session.scalars(stmt.order_by(Dispute.created_at.desc())).all())


def update_dispute(
    session: Session,
    dispute: Dispute,
    *,
    status: str | None = None,
    resolution: str | None = None,
    reviewed_by: str | None = None,
) -> Dispute:
    if status is not None:
        allowed = ALLOWED_TRANSITIONS[dispute.status]
        if status not in {s.value for s in DisputeStatus} or status not in allowed:
            raise StateError(
                f"争议状态不能从 {dispute.status} 变为 {status}",
                details={"from": dispute.status, "to": status, "allowed": sorted(allowed)},
            )
        dispute.status = status
        if status in {DisputeStatus.RESOLVED.value, DisputeStatus.REJECTED.value}:
            dispute.reviewed_at = utcnow()
    if resolution is not None:
        dispute.resolution = resolution
    if reviewed_by is not None:
        dispute.reviewed_by = reviewed_by
    dispute.updated_at = utcnow()
    session.flush()
    return dispute


def serialize_dispute(dispute: Dispute, *, include_snapshot: bool = False) -> dict:
    payload = {
        "id": dispute.id,
        "order_no": dispute.order_no,
        "attribution_id": dispute.attribution_id,
        "item_code": dispute.item_code,
        "initiator_role": dispute.initiator_role,
        "reason": dispute.reason,
        "status": dispute.status,
        "snapshot_locked": dispute.snapshot_locked,
        "created_at": dispute.created_at.isoformat() if dispute.created_at else None,
        "updated_at": dispute.updated_at.isoformat() if dispute.updated_at else None,
        "reviewed_by": dispute.reviewed_by,
        "reviewed_at": dispute.reviewed_at.isoformat() if dispute.reviewed_at else None,
        "resolution": dispute.resolution,
        "evidence_count": len(dispute.evidence_links),
    }
    if include_snapshot:
        payload["evidence_snapshot"] = dispute.evidence_snapshot_json
        payload["evidence_bindings"] = [
            {
                "id": link.id,
                "attribution_item_id": link.attribution_item_id,
                "bound_at": link.bound_at.isoformat() if link.bound_at else None,
                "evidence": link.evidence_json,
            }
            for link in dispute.evidence_links
        ]
    return payload
