"""★ 核心归因服务：沈阳出租车规则化差额归因。

以沈阳出租车固定计费基准（config/attribution.yaml 的 shenyang_fare 节）为唯一计算依据：

    总差额 total_delta = 实付 paid_amount − 预估价 quoted_price

归因项（全部恒输出，含金额为 0.00 的项）：
    1 WAIT_FEE             等候费：轨迹中每条连续低速段（<12km/h）→ 一次等候事件，
                           高峰秒数/120 + 平峰秒数/180（元），逐事件举证（路口/时间/坐标）
    2 DETOUR_EXTRA         绕路增量：偏离段多行驶里程 × 连续里程运价，逐事件举证
    3 MILEAGE_EXTRA        里程费增量：参考路径上的计费里程较预估增减部分（固定费率）
    4 RETURN_FEE           阶梯返空费：>15km 部分按沈阳规则（账单-预估差）
    5 NIGHT_SURCHARGE      夜间/冷气档差价：起步价与里程运价换档差异
    6 PRICING_RULE_CHANGE  计价规则变化：沈阳基准固定，日常恒为 0 元
    7 UNEXPLAINED_RESIDUAL 残差：|残差|≤0.01 归零；>0.01 元显式"待核查"，绝不凑平

金额守恒由构造保证：残差兜底定义，±0.01 元容差（tests 断言）。
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
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
from app.services import geo, shenyang
from app.services.pricing import money, to_decimal
from app.services.trajectory import RawPoint, Trajectory, preprocess


class AttributionError(Exception):
    def __init__(self, message: str, code: str = "ATTRIBUTION_PRECONDITION_FAILED") -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------
# preprocess 结果缓存：preprocess(raw_points, cfg, polyline) 是纯函数，
# 只读取（1）原始轨迹点、（2）参考折线、（3）cfg 的 trajectory / low_speed
# 两个配置节（见 trajectory.py）。以这四项内容为键做 LRU 记忆化：
# 相同输入直接复用已算好的 Trajectory，省去 O(点数×折线节点) 的几何重算。
# attribute() 对返回的 Trajectory 只读不写（wait_spans/deviation_spans/path
# 均只读取并复制），复用安全；条目命中后所有下游归因逻辑仍逐次执行。
# ------------------------------------------------------------------
_TRAJECTORY_CACHE_MAX = 128
_trajectory_cache: OrderedDict[tuple, Trajectory] = OrderedDict()
_trajectory_cache_lock = threading.Lock()


def _cfg_json(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _preprocess_cached(raw_points, cfg, polyline) -> Trajectory:
    key = (
        None if polyline is None else _cfg_json(polyline),
        _cfg_json(cfg.get_path("trajectory") or {}),
        _cfg_json(cfg.get_path("low_speed") or {}),
        tuple((p.ts.isoformat(), p.lat, p.lng, p.speed_mps) for p in raw_points),
    )
    with _trajectory_cache_lock:
        cached = _trajectory_cache.get(key)
        if cached is not None:
            _trajectory_cache.move_to_end(key)
            return cached
    trajectory = preprocess(raw_points, cfg, polyline)
    with _trajectory_cache_lock:
        _trajectory_cache[key] = trajectory
        while len(_trajectory_cache) > _TRAJECTORY_CACHE_MAX:
            _trajectory_cache.popitem(last=False)
    return trajectory


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
class WaitEventDraft:
    location: str
    t_start: object
    t_end: object
    duration_s: float
    avg_speed_kmh: float
    peak_seconds: float
    offpeak_seconds: float
    peak_label: str
    fee: Decimal
    lat: float
    lng: float
    evidence: dict


@dataclass
class DetourEventDraft:
    entry_name: str
    exit_name: str
    entry_lat: float
    entry_lng: float
    exit_lat: float
    exit_lng: float
    ref_distance_m: float
    actual_distance_m: float
    extra_distance_m: float
    fee: Decimal
    path: list
    evidence: dict


@dataclass
class AttributionDraft:
    order_no: str
    total_delta: Decimal
    explained_amount: Decimal
    residual: Decimal
    items: list[ItemDraft] = field(default_factory=list)
    wait_events: list[WaitEventDraft] = field(default_factory=list)
    detour_events: list[DetourEventDraft] = field(default_factory=list)
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


def _yuan(value: Decimal) -> str:
    return f"{money(value):.2f}"


def _nearest_node_name(nodes: list[dict], lat: float, lng: float) -> str:
    """在参考路径节点（沈阳道路名）中找最近的节点名；无节点表时退回全局锚点。"""
    pool = nodes or [
        {"name": name, "lat": latlng[0], "lng": latlng[1]} for name, latlng in shenyang.ANCHORS.items()
    ]
    best = min(
        pool,
        key=lambda n: geo.haversine_m(lat, lng, float(n["lat"]), float(n["lng"])),
    )
    dist = geo.haversine_m(lat, lng, float(best["lat"]), float(best["lng"]))
    return best["name"] if dist <= 800 else "参考路径沿途"


def _rule_evidence(cfg) -> dict:
    fare = cfg.section("shenyang_fare")
    return {
        "DAY": fare["DAY"],
        "NIGHT": fare["NIGHT"],
        "peak_windows": fare["peak_windows"],
        "night_window": fare["night_window"],
        "return_fee": fare["return_fee"],
        "wait_fee": fare["wait_fee"],
        "source": "沈阳市出租车现行运价规则（固定基准，见 docs/references.md）",
    }


def attribute(*, order, est_params, bill_params, cfg) -> AttributionDraft:
    """纯计算入口（不写库，便于单元测试）。"""
    est, bill = order.estimate, order.bill
    if est is None or bill is None:
        raise AttributionError(f"订单 {order.order_no} 缺少预估价快照或实付账单，无法归因")

    quoted = money(est.quoted_price)
    paid = money(bill.paid_amount)
    total_delta = money(paid - quoted)

    delta_d_m = int(bill.actual_distance_m) - int(est.est_distance_m)
    delta_t_s = int(bill.actual_duration_s) - int(est.est_duration_s)

    raw_points = [RawPoint(ts=p.ts, lat=p.lat, lng=p.lng, speed_mps=p.speed_mps) for p in order.points]
    ref = order.reference_route
    polyline = ref.polyline_json if ref is not None else None
    nodes = list(ref.nodes_json or []) if ref is not None else []
    traj = _preprocess_cached(raw_points, cfg, polyline)

    flags: list[str] = list(traj.flags)
    items: list[ItemDraft] = []

    # ---------------- 1) 等候费（堵车/低速，时距并计） ----------------
    wait_cfg = cfg.get_path("shenyang_fare.wait_fee", {"peak_seconds_per_yuan": 120, "offpeak_seconds_per_yuan": 180})
    wait_events: list[WaitEventDraft] = []
    for span in traj.wait_spans:
        split = shenyang.split_by_peak(cfg, span.t_start, span.t_end)
        peak_seconds = sum(s for is_p, s in split if is_p)
        offpeak_seconds = sum(s for is_p, s in split if not is_p)
        fee = shenyang.wait_fee(cfg, peak_seconds, offpeak_seconds)
        location = _nearest_node_name(nodes, span.lat, span.lng)
        label = shenyang.peak_label(cfg, span.t_start, span.t_end)
        rate_text = "每 2 分钟 1 元" if peak_seconds >= offpeak_seconds else "每 3 分钟 1 元"
        description = (
            f"{location}，{span.t_start.strftime('%H:%M:%S')}—{span.t_end.strftime('%H:%M:%S')}，"
            f"持续 {span.duration_s:.0f} 秒，平均速度 {span.avg_speed_kmh:.1f} km/h，{label}，"
            f"按{rate_text}计 {_yuan(fee)} 元"
        )
        wait_events.append(
            WaitEventDraft(
                location=location,
                t_start=span.t_start,
                t_end=span.t_end,
                duration_s=round(span.duration_s, 1),
                avg_speed_kmh=round(span.avg_speed_kmh, 2),
                peak_seconds=round(peak_seconds, 1),
                offpeak_seconds=round(offpeak_seconds, 1),
                peak_label=label,
                fee=fee,
                lat=round(span.lat, 6),
                lng=round(span.lng, 6),
                evidence={
                    "location": location,
                    "time_range": [span.t_start.isoformat(), span.t_end.isoformat()],
                    "duration_s": round(span.duration_s, 1),
                    "avg_speed_kmh": round(span.avg_speed_kmh, 2),
                    "peak_seconds": round(peak_seconds, 1),
                    "offpeak_seconds": round(offpeak_seconds, 1),
                    "peak_label": label,
                    "fee": _yuan(fee),
                    "formula": "等候费 = 高峰低速秒数/120 × 1 元 + 平峰低速秒数/180 × 1 元（时距并计，分段结算）",
                    "rule": {"peak_seconds_per_yuan": wait_cfg["peak_seconds_per_yuan"],
                             "offpeak_seconds_per_yuan": wait_cfg["offpeak_seconds_per_yuan"]},
                    "coordinate": {"lat": round(span.lat, 6), "lng": round(span.lng, 6)},
                    "speed_threshold_kmh": cfg.get_path("low_speed.speed_threshold_kmh", 12.0),
                    "explanation": description,
                },
            )
        )
    wait_total = money(sum((e.fee for e in wait_events), Decimal("0")))
    if traj.usable:
        wait_desc = (
            f"识别到 {len(wait_events)} 条连续低速段（< {cfg.get_path('low_speed.speed_threshold_kmh')} km/h），"
            f"合计 {traj.wait_seconds:.0f} 秒（高峰 {sum(e.peak_seconds for e in wait_events):.0f} 秒 / "
            f"平峰 {sum(e.offpeak_seconds for e in wait_events):.0f} 秒），"
            f"按沈阳时距并计规则计 {_yuan(wait_total)} 元"
        )
    else:
        wait_desc = (
            "轨迹缺失或数据不足，无法识别低速等候段，等候费按 0 元处理；"
            "实付中如含无法举证的等候费用，将进入残差并标记待核查"
        )
        flags.append("WAIT_UNVERIFIED")
    items.append(
        ItemDraft(
            code=AttributionCode.WAIT_FEE.value,
            label=ITEM_LABELS[AttributionCode.WAIT_FEE.value],
            amount=wait_total,
            description=wait_desc,
            evidence={
                "formula": "WAIT_FEE = Σ 高峰低速秒数/120 + Σ 平峰低速秒数/180（元）",
                "event_count": len(wait_events),
                "total_low_speed_seconds": round(traj.wait_seconds, 1),
                "events": [e.evidence for e in wait_events],
                "data_quality": traj.quality,
                "explanation": wait_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.WAIT_FEE.value],
        )
    )

    # ---------------- 2) 绕路增量（偏离段） ----------------
    detour_events: list[DetourEventDraft] = []
    for span in traj.deviation_spans:
        ref_distance = max(0.0, (span.along_end or 0.0) - (span.along_start or 0.0))
        actual_distance = span.actual_distance_m
        extra = round(actual_distance - ref_distance, 1)
        if extra <= 0:
            continue
        fee = money(extra / 1000.0 * float(bill_params.per_km))
        entry_name = _nearest_node_name(nodes, span.path[0][0], span.path[0][1])
        exit_name = _nearest_node_name(nodes, span.path[-1][0], span.path[-1][1])
        description = (
            f"参考路径在 {entry_name} → {exit_name} 之间应行驶 {ref_distance:.0f} 米，"
            f"实际绕行 {actual_distance:.0f} 米，多行驶 {extra:.0f} 米，"
            f"按 {_yuan(bill_params.per_km)} 元/公里计绕路费 {_yuan(fee)} 元"
        )
        detour_events.append(
            DetourEventDraft(
                entry_name=entry_name,
                exit_name=exit_name,
                entry_lat=round(span.path[0][0], 6),
                entry_lng=round(span.path[0][1], 6),
                exit_lat=round(span.path[-1][0], 6),
                exit_lng=round(span.path[-1][1], 6),
                ref_distance_m=round(ref_distance, 1),
                actual_distance_m=round(actual_distance, 1),
                extra_distance_m=extra,
                fee=fee,
                path=[[round(p[0], 6), round(p[1], 6)] for p in span.path],
                evidence={
                    "entry": {"name": entry_name, "lat": round(span.path[0][0], 6), "lng": round(span.path[0][1], 6)},
                    "exit": {"name": exit_name, "lat": round(span.path[-1][0], 6), "lng": round(span.path[-1][1], 6)},
                    "time_range": [span.t_start.isoformat(), span.t_end.isoformat()],
                    "ref_distance_m": round(ref_distance, 1),
                    "actual_distance_m": round(actual_distance, 1),
                    "extra_distance_m": extra,
                    "fee": _yuan(fee),
                    "formula": "绕路费 = 偏离段多行驶米数 / 1000 × 沈阳里程运价（元/公里）",
                    "per_km_rate": _yuan(bill_params.per_km),
                    "max_deviation_m": round(span.max_deviation_m, 1),
                    "path": [[round(p[0], 6), round(p[1], 6)] for p in span.path],
                    "explanation": description,
                },
            )
        )
    detour_total = money(sum((e.fee for e in detour_events), Decimal("0")))
    if traj.fit_score is None:
        detour_desc = "缺少参考路径或轨迹，无法计算拟合度，绕路增量不予认定（不产生金额）"
        flags.append("DETOUR_UNVERIFIABLE")
    elif not detour_events:
        detour_desc = (
            f"轨迹拟合度 {traj.fit_score:.2f}（平均偏离 {traj.avg_deviation_m:.0f} 米），"
            "未发现偏离参考路径的绕行段，绕路增量为 0"
        )
    else:
        detour_desc = (
            f"发现 {len(detour_events)} 段偏离参考路径的绕行，合计多行驶 "
            f"{sum(e.extra_distance_m for e in detour_events):.0f} 米，按沈阳里程运价计 {_yuan(detour_total)} 元"
        )
    items.append(
        ItemDraft(
            code=AttributionCode.DETOUR_EXTRA.value,
            label=ITEM_LABELS[AttributionCode.DETOUR_EXTRA.value],
            amount=detour_total,
            description=detour_desc,
            evidence={
                "formula": "DETOUR_EXTRA = Σ 偏离段(实际里程 − 参考里程) / 1000 × 里程运价",
                "fit_score": None if traj.fit_score is None else round(traj.fit_score, 4),
                "fit_threshold": cfg.get_path("trajectory.FIT_THRESHOLD", 0.8),
                "avg_deviation_m": None if traj.avg_deviation_m is None else round(traj.avg_deviation_m, 2),
                "event_count": len(detour_events),
                "events": [e.evidence for e in detour_events],
                "explanation": detour_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.DETOUR_EXTRA.value],
        )
    )

    # ---------------- 3) 里程费增量（固定规则直接计算） ----------------
    est_core_a = shenyang.core_fare(est_params, bill.actual_distance_m)
    est_core_e = shenyang.core_fare(est_params, est.est_distance_m)
    mileage_delta = money(est_core_a - est_core_e)
    mileage_extra = money(mileage_delta - detour_total)
    if mileage_extra == 0:
        mileage_desc = (
            f"计费里程较预估的增量已全部由绕路事件解释（ΔD={delta_d_m} 米），里程费增量为 0"
        )
    elif mileage_extra > 0:
        mileage_desc = (
            f"实际计费里程 {bill.actual_distance_m} 米较预估 {est.est_distance_m} 米多 {delta_d_m} 米，"
            f"扣除绕路事件解释的部分后，参考路径上的里程增量按沈阳固定费率计 {_yuan(mileage_extra)} 元"
        )
    else:
        mileage_desc = (
            f"实际计费里程较预估少 {abs(delta_d_m)} 米（取整与里程运价递进所致），"
            f"里程费增量 {_yuan(mileage_extra)} 元（负值表示比预估便宜）"
        )
    items.append(
        ItemDraft(
            code=AttributionCode.MILEAGE_EXTRA.value,
            label=ITEM_LABELS[AttributionCode.MILEAGE_EXTRA.value],
            amount=mileage_extra,
            description=mileage_desc,
            evidence={
                "formula": "MILEAGE_EXTRA = [运价(实际里程) − 运价(预估里程)] − 绕路增量（沈阳固定费率）",
                "delta_distance_m": delta_d_m,
                "estimated_distance_m": int(est.est_distance_m),
                "actual_distance_m": int(bill.actual_distance_m),
                "mileage_fare_est": _yuan(est_core_e - est_params.base_fare),
                "mileage_fare_actual_under_est_rule": _yuan(est_core_a - est_params.base_fare),
                "per_km_rate": _yuan(est_params.per_km),
                "meter_step_m": est_params.meter_step_m,
                "explanation": mileage_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.MILEAGE_EXTRA.value],
        )
    )

    # ---------------- 4) 阶梯返空费 ----------------
    est_return = shenyang.return_fee(cfg, est_params, est.est_distance_m)
    bill_return = shenyang.return_fee(cfg, bill_params, bill.actual_distance_m)
    return_amount = money(bill_return - est_return)
    if return_amount == 0:
        return_desc = (
            f"行程里程未触发阶梯返空费阈值（15 公里），返空费为 0"
            if bill.actual_distance_m <= 15000
            else "预估与实付的返空费一致，差额为 0"
        )
    else:
        return_desc = (
            f"实际里程 {bill.actual_distance_m / 1000:.1f} 公里超过 15 公里返空阈值，"
            f"按沈阳阶梯规则（15–20 公里部分 ×30%，20 公里以上部分 ×50%）计 {_yuan(bill_return)} 元，"
            f"较预估增加 {_yuan(return_amount)} 元"
        )
    items.append(
        ItemDraft(
            code=AttributionCode.RETURN_FEE.value,
            label=ITEM_LABELS[AttributionCode.RETURN_FEE.value],
            amount=return_amount,
            description=return_desc,
            evidence={
                "formula": "RETURN_FEE = 返空费(实付里程) − 返空费(预估里程)；15–20km 部分×30%，>20km 部分×50%",
                "estimated_distance_m": int(est.est_distance_m),
                "actual_distance_m": int(bill.actual_distance_m),
                "estimate_return_fee": _yuan(est_return),
                "bill_return_fee": _yuan(bill_return),
                "tier": cfg.section("shenyang_fare")["return_fee"],
                "explanation": return_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.RETURN_FEE.value],
        )
    )

    # ---------------- 5) 夜间/冷气档差价 ----------------
    bill_core_a = shenyang.core_fare(bill_params, bill.actual_distance_m)
    night_amount = money(bill_core_a - est_core_a)
    if night_amount == 0:
        night_desc = (
            f"预估与实付均按 {est_params.version} 档计价（起步价 {_yuan(est_params.base_fare)} 元、"
            f"每 {est_params.meter_step_m} 米 1 元），无换档差异"
        )
    else:
        night_desc = (
            f"行程跨入夜间/冷气档：起步价 {_yuan(est_params.base_fare)}→{_yuan(bill_params.base_fare)} 元，"
            f"里程运价由每 {est_params.meter_step_m} 米 1 元变为每 {bill_params.meter_step_m} 米 1 元，"
            f"按实际里程 {bill.actual_distance_m} 米计换档差价 {_yuan(night_amount)} 元"
        )
    items.append(
        ItemDraft(
            code=AttributionCode.NIGHT_SURCHARGE.value,
            label=ITEM_LABELS[AttributionCode.NIGHT_SURCHARGE.value],
            amount=night_amount,
            description=night_desc,
            evidence={
                "formula": "NIGHT_SURCHARGE = 起步价+运价(实际里程, 夜间档) − 起步价+运价(实际里程, 预估档)",
                "estimate_rule": est_params.version,
                "bill_rule": bill_params.version,
                "requested_at": order.requested_at.isoformat() if order.requested_at else None,
                "completed_at": order.completed_at.isoformat() if order.completed_at else None,
                "night_window": cfg.get_path("shenyang_fare.night_window"),
                "core_fare_bill_rule": _yuan(bill_core_a),
                "core_fare_est_rule": _yuan(est_core_a),
                "explanation": night_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.NIGHT_SURCHARGE.value],
        )
    )

    # ---------------- 6) 计价规则变化（沈阳基准固定，恒为 0） ----------------
    rule_desc = (
        "沈阳出租车计价规则为固定基准（起步价 9 元/3 公里、2 元/公里、夜间/冷气换档、"
        "阶梯返空费、时距并计等候费），不随订单变化，日常归因中恒为 0 元"
    )
    items.append(
        ItemDraft(
            code=AttributionCode.PRICING_RULE_CHANGE.value,
            label=ITEM_LABELS[AttributionCode.PRICING_RULE_CHANGE.value],
            amount=Decimal("0.00"),
            description=rule_desc,
            evidence={
                "formula": "PRICING_RULE_CHANGE = 0（沈阳计价规则为固定基准）",
                "rules": _rule_evidence(cfg),
                "explanation": rule_desc,
            },
            impact_class=IMPACT_BY_CODE[AttributionCode.PRICING_RULE_CHANGE.value],
        )
    )

    # ---------------- 7) 残差（兜底，不凑平） ----------------
    explained = money(sum((i.amount for i in items), Decimal("0")))
    residual = money(total_delta - explained)
    zero_tol = to_decimal(cfg.section("residual")["ZERO_TOLERANCE"])
    residual_evidence: dict = {}
    if abs(residual) <= zero_tol:
        residual = Decimal("0.00")
        residual_desc = "无未归因差额（残差 0.00 元）"
        if total_delta != explained:
            residual_desc = "归因项之和与总差额的差异在 ±0.01 元舍入容差内，残差记为 0.00 元"
    else:
        quote_recomputed = money(
            shenyang.core_fare(est_params, est.est_distance_m)
            + shenyang.return_fee(cfg, est_params, est.est_distance_m)
        )
        wait_from_events = wait_total
        bill_recomputed = money(
            shenyang.core_fare(bill_params, bill.actual_distance_m)
            + shenyang.return_fee(cfg, bill_params, bill.actual_distance_m)
            + wait_from_events
        )
        quote_gap = money(quote_recomputed - quoted)
        bill_gap = money(paid - bill_recomputed)
        parts = []
        if quote_gap != 0:
            parts.append(f"预估价快照与按沈阳规则重算值差异 {_yuan(quote_gap)} 元")
        if bill_gap != 0:
            parts.append(f"实付总额高于按规则重算值（含可举证等候 {_yuan(wait_from_events)} 元）{_yuan(bill_gap)} 元")
        residual_desc = (
            f"残差 {_yuan(residual)} 元，无法归因到堵车或绕路，已标记为待核查："
            + "；".join(parts)
        )
        residual_evidence = {
            "quote_recomputed": _yuan(quote_recomputed),
            "bill_recomputed": _yuan(bill_recomputed),
            "quote_gap": _yuan(quote_gap),
            "bill_gap": _yuan(bill_gap),
            "needs_review": True,
        }
    items.append(
        ItemDraft(
            code=AttributionCode.UNEXPLAINED_RESIDUAL.value,
            label=ITEM_LABELS[AttributionCode.UNEXPLAINED_RESIDUAL.value],
            amount=residual,
            description=residual_desc,
            evidence={
                "formula": "residual = total_delta − Σ(其余归因项)；>0.01 元必须待核查，不得并入堵车/绕路",
                "total_delta": _yuan(total_delta),
                "explained_amount": _yuan(explained),
                "residual": _yuan(residual),
                "zero_tolerance": str(zero_tol),
                "components": residual_evidence,
                "explanation": residual_desc,
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
        flags.append("RESIDUAL_NEEDS_REVIEW")

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
            "estimate_rule": est_params.version,
            "bill_rule": bill_params.version,
        },
        "deltas": {
            "delta_distance_m": delta_d_m,
            "delta_duration_s": delta_t_s,
        },
        "fit": {
            "fit_score": None if traj.fit_score is None else round(traj.fit_score, 4),
            "avg_deviation_m": None if traj.avg_deviation_m is None else round(traj.avg_deviation_m, 2),
        },
        "trajectory": traj.as_evidence(),
        "events": {"wait_count": len(wait_events), "detour_count": len(detour_events)},
        "totals": {
            "total_delta": _yuan(total_delta),
            "explained_amount": _yuan(explained),
            "residual": _yuan(residual),
            "sum_items_including_residual": _yuan(explained + residual),
            "conservation_ok": abs((explained + residual) - total_delta)
            <= to_decimal(cfg.section("money")["epsilon"]),
        },
        "fare_basis": _rule_evidence(cfg),
    }

    return AttributionDraft(
        order_no=order.order_no,
        total_delta=total_delta,
        explained_amount=explained,
        residual=residual,
        items=items,
        wait_events=wait_events,
        detour_events=detour_events,
        fit_score=traj.fit_score,
        avg_deviation_m=traj.avg_deviation_m,
        data_quality=data_quality,
        status=status,
        quality_flags=sorted(set(flags)),
        evidence_bundle=bundle,
        algorithm_version=cfg.algorithm_version,
        config_version=cfg.config_version,
    )


def run_attribution(session, order, cfg) -> AttributionDraft:
    """解析计价档位并执行归因（不做持久化）。档位按沈阳规则由时间自动判定。"""
    if order.estimate is None or order.bill is None:
        raise AttributionError(f"订单 {order.order_no} 尚未完成，缺少账单无法归因")
    est_params = shenyang.params_for(cfg, order.estimate.rule_version)
    bill_params = shenyang.params_for(cfg, order.bill.rule_version)
    return attribute(order=order, est_params=est_params, bill_params=bill_params, cfg=cfg)
