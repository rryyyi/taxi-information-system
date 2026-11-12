"""透明账单视图：同一份归因，两种叙述视角。

* 乘客视角：预估价 → 实付价的差额构成，每项可展开证据
* 司机视角：与"我的驾驶行为"相关的金额、与"路况"相关的金额、与"平台规则"相关的金额

系统只做结构化呈现，不做责任裁决。
"""

from __future__ import annotations

from decimal import Decimal

from app.enums import IMPACT_BY_CODE, IMPACT_LABELS, AttributionCode, DataQuality, ImpactClass
from app.services.pricing import money, to_decimal

IMPACT_GROUPS = {
    ImpactClass.DRIVER_RELATED.value: "与司机驾驶行为相关",
    ImpactClass.ROAD_CONDITION.value: "与路况相关",
    ImpactClass.PLATFORM_RULE.value: "与平台计价规则相关",
    ImpactClass.UNKNOWN.value: "无法归因（残差）",
}


def _yuan(value) -> str:
    return f"{money(to_decimal(value)):.2f}"


def _direction(amount: Decimal) -> str:
    if amount > 0:
        return "increase"
    if amount < 0:
        return "decrease"
    return "none"


def _share(amount: Decimal, total: Decimal) -> float | None:
    if total == 0:
        return None
    return round(float(amount / total) * 100, 2)


def _quality_notice(data_quality: str, flags: list[str], status: str) -> str | None:
    if data_quality == DataQuality.INCOMPLETE.value:
        return (
            "本次账单基于不完整轨迹生成：轨迹缺失或有效点不足，"
            "拥堵延迟无法识别、路径偏离不予认定，部分归因项为降级计算结果。"
        )
    if status == "NEEDS_REVIEW":
        return "本次账单存在超出阈值的残差（无法归因差额），已标记为待人工复核。"
    if "TRAJECTORY_GAP" in flags:
        return "轨迹存在缺口，里程与时长按可用片段计算，归因结果可能不完整。"
    return None


def build_views(order, attribution, items) -> tuple[dict, dict]:
    """返回 (passenger_view, driver_view)。"""
    total = money(attribution.total_delta)
    items_payload: list[dict] = []
    group_totals: dict[str, Decimal] = {k: Decimal("0") for k in IMPACT_GROUPS}

    for item in items:
        amount = money(item.amount)
        impact = item.impact_class or IMPACT_BY_CODE.get(item.code, ImpactClass.UNKNOWN.value)
        group_totals[impact] = group_totals.get(impact, Decimal("0")) + amount
        items_payload.append(
            {
                "code": item.code,
                "label": item.label,
                "amount": float(_yuan(amount)),
                "amount_text": f"{_yuan(amount)} 元",
                "direction": _direction(amount),
                "share_of_total_pct": _share(amount, total),
                "description": item.description,
                "impact_class": impact,
                "impact_label": IMPACT_LABELS.get(impact, IMPACT_LABELS[ImpactClass.UNKNOWN.value]),
                "evidence": item.evidence_json,
            }
        )

    quality_notice = _quality_notice(
        attribution.data_quality, list(attribution.quality_flags_json or []), attribution.status
    )
    residual = money(attribution.residual)

    base = {
        "order_no": order.order_no,
        "city": order.city,
        "origin": order.origin_label,
        "destination": order.destination_label,
        "data_quality": attribution.data_quality,
        "quality_flags": list(attribution.quality_flags_json or []),
        "quality_notice": quality_notice,
        "fit_score": None if attribution.fit_score is None else float(attribution.fit_score),
        "avg_deviation_m": None if attribution.avg_deviation_m is None else float(attribution.avg_deviation_m),
        "config_version": attribution.config_version,
        "algorithm_version": attribution.algorithm_version,
        "computed_at": attribution.computed_at.isoformat() if attribution.computed_at else None,
        "revision": attribution.revision,
        "items": items_payload,
        "total_delta": float(_yuan(total)),
        "explained_amount": float(_yuan(attribution.explained_amount)),
        "residual": float(_yuan(residual)),
        "residual_text": f"{_yuan(residual)} 元",
        "conservation": {
            "sum_items_including_residual": float(_yuan(attribution.explained_amount + residual)),
            "total_delta": float(_yuan(total)),
            "ok": abs((attribution.explained_amount + residual) - total) <= to_decimal("0.01"),
        },
    }

    passenger = {
        **base,
        "role": "passenger",
        "title": f"订单 {order.order_no} 透明账单（乘客视角）",
        "headline": _passenger_headline(order, total),
        "summary": {
            "quoted_price": _yuan(order.estimate.quoted_price) if order.estimate else None,
            "paid_amount": _yuan(order.bill.paid_amount) if order.bill else None,
            "total_delta": _yuan(total),
            "delta_direction": _direction(total),
            "narrative": _passenger_narrative(total, items),
        },
    }

    driver_mine = money(group_totals.get(ImpactClass.DRIVER_RELATED.value, Decimal("0")))
    driver_road = money(group_totals.get(ImpactClass.ROAD_CONDITION.value, Decimal("0")))
    driver_rule = money(group_totals.get(ImpactClass.PLATFORM_RULE.value, Decimal("0")))
    driver_unknown = money(group_totals.get(ImpactClass.UNKNOWN.value, Decimal("0")))

    driver = {
        **base,
        "role": "driver",
        "title": f"订单 {order.order_no} 明细账单（司机视角）",
        "headline": _driver_headline(driver_mine, driver_road, driver_rule),
        "summary": {
            "total_delta": _yuan(total),
            "paid_amount": _yuan(order.bill.paid_amount) if order.bill else None,
            "groups": {
                ImpactClass.DRIVER_RELATED.value: {
                    "label": IMPACT_GROUPS[ImpactClass.DRIVER_RELATED.value],
                    "amount": _yuan(driver_mine),
                },
                ImpactClass.ROAD_CONDITION.value: {
                    "label": IMPACT_GROUPS[ImpactClass.ROAD_CONDITION.value],
                    "amount": _yuan(driver_road),
                },
                ImpactClass.PLATFORM_RULE.value: {
                    "label": IMPACT_GROUPS[ImpactClass.PLATFORM_RULE.value],
                    "amount": _yuan(driver_rule),
                },
                ImpactClass.UNKNOWN.value: {
                    "label": IMPACT_GROUPS[ImpactClass.UNKNOWN.value],
                    "amount": _yuan(driver_unknown),
                },
            },
            "narrative": _driver_narrative(driver_mine, driver_road, driver_rule, driver_unknown),
            "disclaimer": "系统不判定责任，仅呈现差额构成与证据；如有异议可发起争议由运营复核。",
        },
    }
    return passenger, driver


def _passenger_headline(order, total: Decimal) -> str:
    quoted = _yuan(order.estimate.quoted_price) if order.estimate else "?"
    paid = _yuan(order.bill.paid_amount) if order.bill else "?"
    if total > 0:
        return f"预估价 {quoted} 元，实付 {paid} 元，多付 {_yuan(total)} 元，差额构成如下"
    if total < 0:
        return f"预估价 {quoted} 元，实付 {paid} 元，少付 {_yuan(-total)} 元，差额构成如下"
    return f"预估价 {quoted} 元，实付 {paid} 元，无差额"


def _passenger_narrative(total: Decimal, items) -> str:
    if total == 0:
        return "本次行程实付与预估价一致，无需归因说明。"
    parts = []
    for item in items:
        if item.code == AttributionCode.UNEXPLAINED_RESIDUAL.value:
            continue
        amount = money(item.amount)
        if amount != 0:
            parts.append(f"{item.label} {('+' if amount > 0 else '')}{_yuan(amount)} 元")
    text = "差额主要来自：" + "，".join(parts) + "。" if parts else "差额未找到可解释的业务因子。"
    if any(i.code == AttributionCode.UNEXPLAINED_RESIDUAL.value and money(i.amount) != 0 for i in items):
        text += "另有无法归因的差额已单列为「残差」，未隐藏、未并入其它项。"
    return text


def _driver_headline(mine: Decimal, road: Decimal, rule: Decimal) -> str:
    return (
        f"本单差额中，与你的驾驶行为相关 {_yuan(mine)} 元，"
        f"与路况相关 {_yuan(road)} 元，与平台计价规则相关 {_yuan(rule)} 元"
    )


def _driver_narrative(mine: Decimal, road: Decimal, rule: Decimal, unknown: Decimal) -> str:
    return (
        f"与驾驶行为相关部分（路径偏离、司机/其他延迟）合计 {_yuan(mine)} 元；"
        f"路况导致的拥堵延迟 {_yuan(road)} 元；"
        f"平台规则/杂费变化 {_yuan(rule)} 元；"
        f"无法归因残差 {_yuan(unknown)} 元。"
        "每项均附时间区间、轨迹片段或字段对比证据，可逐项复核。"
    )
