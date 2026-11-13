"""响应序列化（金额统一输出为 2 位小数的字符串，避免浮点误差与前端歧义）。"""

from __future__ import annotations

from decimal import Decimal

from app.models import Attribution, Order
from app.services.pricing import money


def yuan(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{money(value):.2f}"


def order_summary(order: Order) -> dict:
    attribution = order.attribution
    est, bill, ref = order.estimate, order.bill, order.reference_route
    quality = attribution.data_quality if attribution else None
    return {
        "order_no": order.order_no,
        "city": order.city,
        "status": order.status,
        "requested_at": order.requested_at.isoformat() if order.requested_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "origin": order.origin_label,
        "destination": order.destination_label,
        "quoted_price": yuan(est.quoted_price) if est else None,
        "paid_amount": yuan(bill.paid_amount) if bill else None,
        "total_delta": yuan(attribution.total_delta) if attribution else None,
        "residual": yuan(attribution.residual) if attribution else None,
        "fit_score": None if not attribution or attribution.fit_score is None else float(attribution.fit_score),
        "data_quality": quality,
        "attribution_status": attribution.status if attribution else None,
        "attribution_id": attribution.id if attribution else None,
        "reference_route_length_m": ref.length_m if ref else None,
        "trajectory_point_count": len(order.points),
        "wait_event_count": len(attribution.wait_events) if attribution else 0,
        "detour_event_count": len(attribution.detour_events) if attribution else 0,
    }


def order_detail(order: Order) -> dict:
    payload = order_summary(order)
    est, bill = order.estimate, order.bill
    payload.update(
        {
            "estimate_snapshot": {
                "quoted_price": yuan(est.quoted_price),
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
                "paid_amount": yuan(bill.paid_amount),
                "actual_distance_m": bill.actual_distance_m,
                "actual_duration_s": bill.actual_duration_s,
                "base_fare": yuan(bill.base_fare),
                "distance_fare": yuan(bill.distance_fare),
                "time_fare": yuan(bill.time_fare),
                "surcharges": bill.surcharges_json,
                "other_fees": bill.other_fees_json,
                "discounts": bill.discounts_json,
                "rule_version": bill.rule_version,
            }
            if bill
            else None,
            "reference_route": {
                "length_m": order.reference_route.length_m,
                "est_duration_s": order.reference_route.est_duration_s,
                "point_count": len(order.reference_route.polyline_json),
                "polyline": order.reference_route.polyline_json,
                "nodes": order.reference_route.nodes_json,
            }
            if order.reference_route
            else None,
            "trajectory": [
                {
                    "seq": p.seq,
                    "ts": p.ts.isoformat(),
                    "lat": p.lat,
                    "lng": p.lng,
                    "speed_mps": p.speed_mps,
                }
                for p in order.points
            ],
        }
    )
    return payload


def attribution_detail(attribution: Attribution) -> dict:
    return {
        "attribution_id": attribution.id,
        "order_no": attribution.order.order_no if attribution.order else None,
        "total_delta": yuan(attribution.total_delta),
        "explained_amount": yuan(attribution.explained_amount),
        "residual": yuan(attribution.residual),
        "fit_score": None if attribution.fit_score is None else float(attribution.fit_score),
        "avg_deviation_m": None if attribution.avg_deviation_m is None else float(attribution.avg_deviation_m),
        "data_quality": attribution.data_quality,
        "status": attribution.status,
        "quality_flags": list(attribution.quality_flags_json or []),
        "algorithm_version": attribution.algorithm_version,
        "config_version": attribution.config_version,
        "revision": attribution.revision,
        "computed_at": attribution.computed_at.isoformat() if attribution.computed_at else None,
        "items": [
            {
                "code": item.code,
                "label": item.label,
                "amount": yuan(item.amount),
                "description": item.description,
                "impact_class": item.impact_class,
                "evidence": item.evidence_json,
            }
            for item in attribution.items
        ],
        "evidence_bundle": attribution.evidence_bundle_json,
    }
