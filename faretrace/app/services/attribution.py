"""★ 核心归因服务：基于规则化三维偏差分解的可归因费用差额计算。

三维偏差
    ΔD = 实际里程 - 预估里程（米）
    ΔT = 实际时长 - 预估时长（秒）
    ΔP = 计价规则/杂费差异（直接比较快照与账单字段，不做抽象系数估算）

归因项（顺序固定，残差最后兜底，绝不被并入其它项）
    1 PATH_DEVIATION       fit_score < FIT_THRESHOLD 时：max(0, ΔD)·账单里程单价
    2 CONGESTION_DELAY     连续低速段秒数 · 账单时长单价
    3 DRIVER_DELAY         (ΔT - 拥堵秒数) 的正剩余 · 账单时长单价
    4 PRICING_RULE_CHANGE  起步价/里程单价/时长单价/加价系数的可解释差异
    5 TOLL_OR_FEE          路侧杂费/其他费用/优惠的可解释差异
    6 UNEXPLAINED_RESIDUAL total_delta - 上述五项之和（|残差| ≤ 0.01 归零）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.enums import (
    IMPACT_BY_CODE,
    ITEM_LABELS,
    ITEM_ORDER,
    AttributionCode,
    AttributionStatus,
    DataQuality,
    ImpactClass,
)
from app.services.pricing import PricingParams, evaluate, money, sum_fees, to_decimal
from app.services.trajectory import RawPoint, Trajectory, preprocess


class AttributionError(Exception):
    """归因前置条件不满足（如缺少计价规则、缺少账单）。"""

    def __init__(self, message: str, code: str = "ATTRIBUTION_PRECONDITION_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ItemDraft:
    code: str
    label: str
    amount: Decimal
    description: str
    evidence: dict
    impact_class: str = ImpactClass.UNKNOWN.value

    @property
    def sort_order(self) -> int:
        return ITEM_ORDER[self.code]


@dataclass
class AttributionDraft:
    order_no: str
    total_delta: Decimal
    explained_amount: Decimal
    residual: Decimal
    items: list[ItemDraft] = field(default_factory=list)
    fit_score: float | None = None
    avg_deviation_m: float | None = None
    data_quality: str = DataQuality.COMPLETE.value
    status: str = AttributionStatus.OK.value
    quality_flags: list[str] = field(default_factory=list)
    evidence_bundle: dict = field(default_factory=dict)
    algorithm_version: str = ""
    config_version: str = ""

    def sorted_items(self) -> list[ItemDraft]:
        return sorted(self.items, key=lambda i: i.sort_order)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _yuan(value: Decimal) -> str:
    return f"{money(value):.2f}"


def _cents_free_km(meters: float) -> float:
    return meters / 1000.0


def _rule_diff(est_params: PricingParams, bill_params: PricingParams, km: float, minutes: float) -> dict:
    """规则差异的可解释拆解（元）。"""
    base_delta = bill_params.base_fare - est_params.base_fare
    km_delta = (bill_params.per_km - est_params.per_km) * to_decimal(km)
    min_delta = (bill_params.per_min - est_params.per_min) * to_decimal(minutes)
    metered_bill = bill_params.base_fare + bill_params.per_km * to_decimal(km) + bill_params.per_min * to_decimal(minutes)
    surge_delta = (bill_params.surge_multiplier - est_params.surge_multiplier) * metered_bill
    return {
        "base_fare_delta": base_delta,
        "per_km_delta": km_delta,
        "per_min_delta": min_delta,
        "surge_delta": surge_delta,
        "total": money(base_delta + km_delta + min_delta + surge_delta),
    }


def _hourly(per_min: Decimal) -> Decimal:
    return money(per_min * 60)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def attribute(
    *,
    order,
    est_rule,
    bill_rule,
    cfg,
) -> AttributionDraft:
    """纯计算入口（不写库，便于单元测试）。"""
    est, bill, ref = order.estimate, order.bill, order.reference_route
    if est is None or bill is None:
        raise AttributionError(f"订单 {order.order_no} 缺少预估价快照或实付账单，无法归因")

    est_params = PricingParams.from_rule(est_rule)
    bill_params = PricingParams.from_rule(bill_rule)

    quoted = money(est.quoted_price)
    paid = money(bill.paid_amount)
    total_delta = money(paid - quoted)

    delta_d_m = int(bill.actual_distance_m) - int(est.est_distance_m)
    delta_t_s = int(bill.actual_duration_s) - int(est.est_duration_s)
    act_km = bill.actual_distance_m / 1000.0
    act_min = bill.actual_duration_s / 60.0

    raw_points = [
        RawPoint(ts=p.ts, lat=p.lat, lng=p.lng, speed_mps=p.speed_mps) for p in order.points
    ]
    polyline = ref.polyline_json if ref is not None else None
    traj = preprocess(raw_points, cfg, polyline)

    flags: list[str] = list(traj.flags)
    traj_cfg = cfg.section("trajectory")
    fit_threshold = float(traj_cfg["FIT_THRESHOLD"])
    fit_buffer = float(traj_cfg["FIT_BUFFER_M"])

    items: list[ItemDraft] = []

    # ---------------- 1) 路径偏离 ----------------
    path_evidence_base = {
        "rule_version": bill.rule_version,
        "thresholds": {
            "FIT_BUFFER_M": fit_buffer,
            "FIT_THRESHOLD": fit_threshold,
            "DEVIATION_SPAN_THRESHOLD_M": float(traj_cfg["DEVIATION_SPAN_THRESHOLD_M"]),
        },
        "fit_score": None if traj.fit_score is None else round(traj.fit_score, 4),
        "avg_deviation_m": None if traj.avg_deviation_m is None else round(traj.avg_deviation_m, 2),
        "max_deviation_m": None if traj.max_deviation_m is None else round(traj.max_deviation_m, 2),
        "delta_distance_m": delta_d_m,
        "estimated_distance_m": int(est.est_distance_m),
        "actual_distance_m": int(bill.actual_distance_m),
        "bill_per_km": _yuan(bill_params.per_km),
        "trajectory_distance_m": round(traj.distance_m, 1),
        "reference_route_length_m": int(ref.length_m) if ref is not None else None,
        "deviation_spans": [
            {
                "start_seq": s.start_seq,
                "end_seq": s.end_seq,
                "time_range": [s.t_start.isoformat(), s.t_end.isoformat()],
                "duration_s": round(s.duration_s, 1),
                "avg_deviation_m": round(s.avg_deviation_m, 1),
                "max_deviation_m": round(s.max_deviation_m, 1),
                "reference_along_m": [
                    None if s.along_start is None else round(s.along_start, 1),
                    None if s.along_end is None else round(s.along_end, 1),
                ],
            }
            for s in traj.deviation_spans
        ],
        "formula": "path_amount = max(0, actual_distance_m - estimated_distance_m) / 1000 * bill_per_km，仅在 fit_score < FIT_THRESHOLD 时生效",
    }

    if traj.fit_score is not None and traj.fit_score < fit_threshold:
        amount = money(to_decimal(max(0, delta_d_m)) / Decimal("1000") * bill_params.per_km)
        description = (
            f"轨迹与参考路径拟合度 {traj.fit_score:.2f}（平均偏离 {traj.avg_deviation_m:.0f} 米，"
            f"低于阈值 {fit_threshold:.2f}），账单里程比预估多 {delta_d_m / 1000:.2f} 公里，"
            f"按账单里程单价 {_yuan(bill_params.per_km)} 元/公里 计 {_yuan(amount)} 元"
        )
        note = "轨迹偏离参考路径，存在绕行/改道证据"
    else:
        amount = Decimal("0.00")
        if traj.fit_score is None:
            description = "缺少可用轨迹或参考路径，无法计算拟合度，路径偏离不予认定（不产生金额）"
            note = "证据不足，不产生路径偏离金额"
            flags.append("PATH_FIT_UNAVAILABLE")
        else:
            description = (
                f"轨迹拟合度 {traj.fit_score:.2f} ≥ 阈值 {fit_threshold:.2f}"
                f"（平均偏离 {traj.avg_deviation_m:.0f} 米），未认定为路径偏离，金额为 0"
            )
            note = "高拟合度：走的是参考路径"
    path_evidence_base["note"] = note
    items.append(
        ItemDraft(
            code=AttributionCode.PATH_DEVIATION.value,
            label=ITEM_LABELS[AttributionCode.PATH_DEVIATION.value],
            amount=amount,
            description=description,
            evidence={**path_evidence_base, "explanation": description},
            impact_class=IMPACT_BY_CODE[AttributionCode.PATH_DEVIATION.value],
        )
    )

    # ---------------- 2) 拥堵延迟 ----------------
    congestion_cfg = cfg.section("congestion")
    congestion_amount = money(to_decimal(traj.congestion_seconds) / Decimal("60") * bill_params.per_min)
    congestion_spans_evidence = [
        {
            "start_seq": s.start_seq,
            "end_seq": s.end_seq,
            "time_range": [s.t_start.isoformat(), s.t_end.isoformat()],
            "duration_s": round(s.duration_s, 1),
            "avg_speed_kmh": round(s.avg_speed_kmh, 1),
            "reference_along_m": [
                None if s.along_start is None else round(s.along_start, 1),
                None if s.along_end is None else round(s.along_end, 1),
            ],
        }
        for s in traj.congestion_spans
    ]
    if traj.usable:
        congestion_description = (
            f"识别到 {len(traj.congestion_spans)} 段连续低速（< {congestion_cfg['CONGESTION_SPEED_KMH']} km/h，"
            f"单段 ≥ {congestion_cfg['MIN_CONGESTION_SECONDS']} 秒），累计 {traj.congestion_seconds:.0f} 秒，"
            f"按时长单价 {_yuan(bill_params.per_min)} 元/分钟 计 {_yuan(congestion_amount)} 元"
        )
        congestion_note = "轨迹速度证据支持拥堵延迟"
    else:
        congestion_description = (
            "轨迹缺失或数据不足，无法识别拥堵低速段，拥堵延迟按 0 元处理"
            "（该部分时间差额若存在，会归入司机/其他延迟或残差）"
        )
        congestion_note = "无轨迹证据，拥堵金额置 0（降级）"
        flags.append("CONGESTION_UNVERIFIED")
    items.append(
        ItemDraft(
            code=AttributionCode.CONGESTION_DELAY.value,
            label=ITEM_LABELS[AttributionCode.CONGESTION_DELAY.value],
            amount=congestion_amount if traj.usable else Decimal("0.00"),
            description=congestion_description,
            evidence={
                "formula": "congestion_amount = congestion_seconds / 60 * bill_per_min",
                "congestion_seconds": round(traj.congestion_seconds, 1),
                "congestion_spans": congestion_spans_evidence,
                "thresholds": {
                    "CONGESTION_SPEED_KMH": congestion_cfg["CONGESTION_SPEED_KMH"],
                    "MIN_CONGESTION_SECONDS": congestion_cfg["MIN_CONGESTION_SECONDS"],
                },
                "bill_per_min": _yuan(bill_params.per_min),
                "bill_per_hour": _yuan(_hourly(bill_params.per_min)),
                "rule_version": bill.rule_version,
                "data_quality": traj.quality,
                "note": congestion_note,
                "explanation": congestion_description,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.CONGESTION_DELAY.value],
        )
    )
    if not traj.usable:
        congestion_amount = Decimal("0.00")

    # ---------------- 3) 司机/其他延迟 ----------------
    remaining_s = float(delta_t_s) - float(traj.congestion_seconds if traj.usable else 0.0)
    driver_amount = money(to_decimal(max(0.0, remaining_s)) / Decimal("60") * bill_params.per_min)
    if remaining_s > 0:
        driver_description = (
            f"时间偏差 {delta_t_s} 秒，扣除拥堵 {traj.congestion_seconds:.0f} 秒后剩余 {remaining_s:.0f} 秒，"
            f"按时长单价 {_yuan(bill_params.per_min)} 元/分钟 计 {_yuan(driver_amount)} 元"
        )
        driver_note = (
            "剩余正时长为司机或其他原因（等候、绕行、慢行等），"
            "系统仅呈现差额来源，不判定责任"
        )
    else:
        driver_description = (
            f"时间偏差 {delta_t_s} 秒，扣除拥堵 {traj.congestion_seconds:.0f} 秒后无正剩余，"
            "不产生司机延迟金额"
        )
        driver_note = "剩余时长为负/零，按规则不产生正向金额（实际行程更快）"
    if not traj.usable:
        driver_note += "；轨迹缺失，无法区分拥堵与司机延迟，全部正时长按降级口径计入本项"
    items.append(
        ItemDraft(
            code=AttributionCode.DRIVER_DELAY.value,
            label=ITEM_LABELS[AttributionCode.DRIVER_DELAY.value],
            amount=driver_amount,
            description=driver_description,
            evidence={
                "formula": "driver_amount = max(0, delta_duration_s - congestion_seconds) / 60 * bill_per_min",
                "delta_duration_s": delta_t_s,
                "congestion_seconds": round(traj.congestion_seconds, 1),
                "remaining_positive_seconds": round(max(0.0, remaining_s), 1),
                "bill_per_min": _yuan(bill_params.per_min),
                "rule_version": bill.rule_version,
                "data_quality": traj.quality,
                "note": driver_note,
                "explanation": driver_description,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.DRIVER_DELAY.value],
        )
    )

    # ---------------- 4) 计价规则变化 ----------------
    rule_diff = _rule_diff(est_params, bill_params, act_km, act_min)
    rule_amount = rule_diff["total"]
    if rule_amount == 0:
        rule_description = "预估价快照与实付账单使用同一计价规则且费率一致，规则差异为 0"
    else:
        rule_description = (
            f"计价规则由 {est.rule_version} 变为 {bill.rule_version}："
            f"起步价 {_yuan(est_params.base_fare)}→{_yuan(bill_params.base_fare)}，"
            f"里程单价 {_yuan(est_params.per_km)}→{_yuan(bill_params.per_km)} 元/公里，"
            f"时长单价 {_yuan(est_params.per_min)}→{_yuan(bill_params.per_min)} 元/分钟，"
            f"按实际 {act_km:.2f} 公里 / {act_min:.1f} 分钟计算差异 {_yuan(rule_amount)} 元"
        )
    items.append(
        ItemDraft(
            code=AttributionCode.PRICING_RULE_CHANGE.value,
            label=ITEM_LABELS[AttributionCode.PRICING_RULE_CHANGE.value],
            amount=rule_amount,
            description=rule_description,
            evidence={
                "formula": "rule_change = Δ起步价 + Δ里程单价*actual_km + Δ时长单价*actual_min + Δ加价系数*计费基数",
                "rule_versions": {"estimate": est.rule_version, "bill": bill.rule_version},
                "field_compare": {
                    "base_fare": [_yuan(est_params.base_fare), _yuan(bill_params.base_fare)],
                    "per_km": [_yuan(est_params.per_km), _yuan(bill_params.per_km)],
                    "per_min": [_yuan(est_params.per_min), _yuan(bill_params.per_min)],
                    "surge_multiplier": [
                        str(est_params.surge_multiplier),
                        str(bill_params.surge_multiplier),
                    ],
                    "min_fare": [_yuan(est_params.min_fare), _yuan(bill_params.min_fare)],
                },
                "computed_on": {"actual_km": round(act_km, 3), "actual_min": round(act_min, 3)},
                "deltas": {
                    "base_fare": _yuan(rule_diff["base_fare_delta"]),
                    "per_km": _yuan(rule_diff["per_km_delta"]),
                    "per_min": _yuan(rule_diff["per_min_delta"]),
                    "surge": _yuan(rule_diff["surge_delta"]),
                },
                "explanation": rule_description,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.PRICING_RULE_CHANGE.value],
        )
    )

    # ---------------- 5) 路侧杂费/优惠变化 ----------------
    est_surcharges = sum_fees(est.surcharges_json)
    est_discounts = sum_fees(est.discounts_json)
    bill_surcharges = sum_fees(bill.surcharges_json)
    bill_other = sum_fees(bill.other_fees_json)
    bill_discounts = sum_fees(bill.discounts_json)
    toll_amount = money(bill_surcharges + bill_other - bill_discounts - (est_surcharges - est_discounts))
    toll_description = (
        f"杂费/其他费用/优惠合计由 {_yuan(est_surcharges - est_discounts)} 元"
        f"变为 {_yuan(bill_surcharges + bill_other - bill_discounts)} 元，差异 {_yuan(toll_amount)} 元"
    )
    items.append(
        ItemDraft(
            code=AttributionCode.TOLL_OR_FEE.value,
            label=ITEM_LABELS[AttributionCode.TOLL_OR_FEE.value],
            amount=toll_amount,
            description=toll_description,
            evidence={
                "formula": "toll_or_fee = (账单杂费 + 账单其他费用 - 账单优惠) - (快照杂费 - 快照优惠)",
                "estimate": {
                    "surcharges": {k: str(v) for k, v in (est.surcharges_json or {}).items()},
                    "discounts": {k: str(v) for k, v in (est.discounts_json or {}).items()},
                    "total": _yuan(est_surcharges - est_discounts),
                },
                "bill": {
                    "surcharges": {k: str(v) for k, v in (bill.surcharges_json or {}).items()},
                    "other_fees": {k: str(v) for k, v in (bill.other_fees_json or {}).items()},
                    "discounts": {k: str(v) for k, v in (bill.discounts_json or {}).items()},
                    "total": _yuan(bill_surcharges + bill_other - bill_discounts),
                },
                "itemized_delta": _itemized_fee_delta(est, bill),
                "rule_version": bill.rule_version,
                "explanation": toll_description,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.TOLL_OR_FEE.value],
        )
    )

    # ---------------- 6) 残差（兜底） ----------------
    explained = money(sum((i.amount for i in items), Decimal("0")))
    residual = money(total_delta - explained)

    zero_tol = to_decimal(cfg.section("residual")["ZERO_TOLERANCE"])
    residual_evidence: dict = {}
    if abs(residual) <= zero_tol:
        residual = Decimal("0.00")
        residual_description = "无未归因差额（残差 0.00 元）"
        if total_delta != explained:
            residual_description = "归因项之和与总差额的差异在 ±0.01 元舍入容差内，残差记为 0.00 元"
    else:
        quote_recomputed = evaluate(
            est_params,
            est.est_distance_m / 1000.0,
            est.est_duration_s / 60.0,
            surge_multiplier=est.surge_multiplier,
            surcharges=est_surcharges,
            discounts=est_discounts,
        )
        bill_recomputed = evaluate(
            bill_params,
            act_km,
            act_min,
            surge_multiplier=bill.surge_multiplier,
            surcharges=bill_surcharges + bill_other,
            discounts=bill_discounts,
        )
        quote_gap = money(quote_recomputed - quoted)
        bill_gap = money(paid - bill_recomputed)
        other_gap = money(residual - quote_gap - bill_gap)
        parts = []
        if quote_gap != 0:
            parts.append(f"预估价快照与按快照规则重算值差异 {_yuan(quote_gap)} 元")
        if bill_gap != 0:
            parts.append(f"实付总额与账单分项之和差异 {_yuan(bill_gap)} 元（账单存在未列明调整）")
        if other_gap != 0:
            parts.append(f"其它无法归因差异 {_yuan(other_gap)} 元")
        residual_description = "残差 " + _yuan(residual) + " 元：" + "；".join(parts)
        residual_evidence = {
            "quote_recomputed": _yuan(quote_recomputed),
            "bill_recomputed": _yuan(bill_recomputed),
            "quote_gap": _yuan(quote_gap),
            "bill_gap": _yuan(bill_gap),
            "other_gap": _yuan(other_gap),
        }
    items.append(
        ItemDraft(
            code=AttributionCode.UNEXPLAINED_RESIDUAL.value,
            label=ITEM_LABELS[AttributionCode.UNEXPLAINED_RESIDUAL.value],
            amount=residual,
            description=residual_description,
            evidence={
                "formula": "residual = total_delta - sum(attribution_items)",
                "total_delta": _yuan(total_delta),
                "explained_amount": _yuan(explained),
                "residual": _yuan(residual),
                "zero_tolerance": str(zero_tol),
                "components": residual_evidence,
                "explanation": residual_description,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.UNEXPLAINED_RESIDUAL.value],
        )
    )

    # ---------------- 数据质量与状态 ----------------
    data_quality = traj.quality
    status = AttributionStatus.OK.value
    if data_quality != DataQuality.COMPLETE.value:
        status = AttributionStatus.DEGRADED.value
    review_threshold = to_decimal(cfg.section("residual")["REVIEW_THRESHOLD"])
    if abs(residual) > review_threshold:
        status = AttributionStatus.NEEDS_REVIEW.value
        flags.append("RESIDUAL_ABOVE_REVIEW_THRESHOLD")

    bundle = {
        "algorithm_version": cfg.algorithm_version,
        "config_version": cfg.config_version,
        "order_no": order.order_no,
        "data_quality": data_quality,
        "quality_flags": sorted(set(flags)),
        "input": {
            "quoted_price": _yuan(quoted),
            "paid_amount": _yuan(paid),
            "estimated_distance_m": int(est.est_distance_m),
            "actual_distance_m": int(bill.actual_distance_m),
            "estimated_duration_s": int(est.est_duration_s),
            "actual_duration_s": int(bill.actual_duration_s),
            "estimate_rule": est.rule_version,
            "bill_rule": bill.rule_version,
        },
        "deltas": {
            "delta_distance_m": delta_d_m,
            "delta_duration_s": delta_t_s,
            "delta_price_rule": _yuan(rule_amount),
            "delta_toll_or_fee": _yuan(toll_amount),
        },
        "fit": {
            "fit_score": None if traj.fit_score is None else round(traj.fit_score, 4),
            "avg_deviation_m": None if traj.avg_deviation_m is None else round(traj.avg_deviation_m, 2),
            "fit_buffer_m": fit_buffer,
            "fit_threshold": fit_threshold,
        },
        "trajectory": traj.as_evidence(),
        "totals": {
            "total_delta": _yuan(total_delta),
            "explained_amount": _yuan(explained),
            "residual": _yuan(residual),
            "sum_items_including_residual": _yuan(explained + residual),
            "conservation_ok": abs((explained + residual) - total_delta) <= to_decimal(
                cfg.section("money")["epsilon"]
            ),
        },
        "thresholds": {
            "FIT_BUFFER_M": fit_buffer,
            "FIT_THRESHOLD": fit_threshold,
            "CONGESTION_SPEED_KMH": congestion_cfg["CONGESTION_SPEED_KMH"],
            "MIN_CONGESTION_SECONDS": congestion_cfg["MIN_CONGESTION_SECONDS"],
            "ZERO_TOLERANCE": str(zero_tol),
            "REVIEW_THRESHOLD": str(review_threshold),
        },
    }

    return AttributionDraft(
        order_no=order.order_no,
        total_delta=total_delta,
        explained_amount=explained,
        residual=residual,
        items=items,
        fit_score=traj.fit_score,
        avg_deviation_m=traj.avg_deviation_m,
        data_quality=data_quality,
        status=status,
        quality_flags=sorted(set(flags)),
        evidence_bundle=bundle,
        algorithm_version=cfg.algorithm_version,
        config_version=cfg.config_version,
    )


def _itemized_fee_delta(est, bill) -> dict:
    """逐条列出快照/账单的杂费与优惠差异，供证据展开。"""
    est_all = {f"surcharge:{k}": v for k, v in (est.surcharges_json or {}).items()}
    est_all.update({f"discount:{k}": v for k, v in (est.discounts_json or {}).items()})
    bill_all = {f"surcharge:{k}": v for k, v in (bill.surcharges_json or {}).items()}
    bill_all.update({f"other_fee:{k}": v for k, v in (bill.other_fees_json or {}).items()})
    bill_all.update({f"discount:{k}": v for k, v in (bill.discounts_json or {}).items()})
    out = {}
    for key in sorted(set(est_all) | set(bill_all)):
        before = to_decimal(est_all.get(key, 0))
        after = to_decimal(bill_all.get(key, 0))
        if before != after:
            out[key] = {"estimate": _yuan(before), "bill": _yuan(after), "delta": _yuan(after - before)}
    return out


def run_attribution(session, order, cfg) -> AttributionDraft:
    """读取计价规则并执行归因（不做持久化）。"""
    from app.models import PricingRule

    if order.estimate is None or order.bill is None:
        raise AttributionError(f"订单 {order.order_no} 尚未完成，缺少账单无法归因")
    est_rule = session.get(PricingRule, order.estimate.rule_version)
    bill_rule = session.get(PricingRule, order.bill.rule_version)
    if est_rule is None or bill_rule is None:
        missing = order.estimate.rule_version if est_rule is None else order.bill.rule_version
        raise AttributionError(f"计价规则版本 {missing} 不存在，无法归因")
    return attribute(order=order, est_rule=est_rule, bill_rule=bill_rule, cfg=cfg)
