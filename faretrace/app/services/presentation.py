"""面向页面的中文说明；只整理已保存的计费证据，不重新归因或计价。"""
from decimal import Decimal

LABELS = {
    "WAIT_FEE": "低速等候费", "DETOUR_EXTRA": "绕路增加的费用",
    "MILEAGE_EXTRA": "里程费用变化", "RETURN_FEE": "长途返空费变化",
    "NIGHT_SURCHARGE": "夜间或冷气档差价", "PRICING_RULE_CHANGE": "计价规则变化",
    "UNEXPLAINED_RESIDUAL": "待核实差额",
}
WORDS = {
    "COMPLETE": "轨迹完整", "INCOMPLETE": "轨迹不完整", "OK": "已核对",
    "GENERATED": "已生成", "DEGRADED": "部分依据不足", "NEEDS_REVIEW": "待人工核实",
    "PENDING_REVIEW": "待人工核实", "CREATED": "行程待完成", "COMPLETED": "行程已完成",
    "OPEN": "已受理", "REVIEWING": "正在复核", "UNDER_REVIEW": "正在复核",
    "RESOLVED": "已处理", "REJECTED": "未通过", "passenger": "乘客", "driver": "司机",
    "driver_related": "与行驶路线有关", "road_condition": "与路况有关",
    "platform_rule": "与固定计价规则有关", "unknown": "原因待核实",
    "SY-DAY": "白天档", "SY-NIGHT": "夜间或冷气档",
}


def chinese(value):
    return LABELS.get(str(value), WORDS.get(str(value), "待确认"))


def number(value, digits=2):
    return f"{Decimal(str(value or 0)):.{digits}f}"


def readable_items(attribution):
    """计算式使用同一归因运行的原始证据值，明确区分全程费用与费用变化。"""
    result = []
    basis = (attribution.evidence_bundle_json or {}).get("fare_basis", {})
    for item in attribution.items:
        e = item.evidence_json or {}
        steps, events = [], []
        code = item.code
        if code == "WAIT_FEE":
            rule = "速度低于每小时 12 公里时另计等候费：高峰每 120 秒 1 元，平峰每 180 秒 1 元；跨时段分别计算，每次等候四舍五入到分后相加。"
            for ev in attribution.wait_events:
                ev_rule = (ev.evidence_json or {}).get("rule", {})
                peak_rate = ev_rule.get("peak_seconds_per_yuan", 120)
                off_rate = ev_rule.get("offpeak_seconds_per_yuan", 180)
                events.append({
                    "title": ev.location,
                    "detail": f"{ev.t_start:%m月%d日 %H:%M:%S} 至 {ev.t_end:%H:%M:%S} · {number(ev.duration_s, 1)} 秒 · {ev.peak_label}",
                    "formula": f"高峰 {number(ev.peak_seconds, 1)} 秒 ÷ {peak_rate} ＋ 平峰 {number(ev.offpeak_seconds, 1)} 秒 ÷ {off_rate} ＝ {number(ev.fee)} 元",
                    "note": f"平均速度 {number(ev.avg_speed_kmh, 1)} 公里/小时；位置 {ev.lat:.5f}°北纬，{ev.lng:.5f}°东经。",
                })
            steps = ["各次等候费用相加：" + " ＋ ".join(number(ev.fee) for ev in attribution.wait_events) + f" ＝ {number(item.amount)} 元"] if events else ["没有可计费的连续低速等候证据，本项为 0.00 元。轨迹不完整时，不推测等候费用。"]
        elif code == "DETOUR_EXTRA":
            rule = "对已识别的偏离路段，比较实际距离与对应参考距离；多出的距离按本单里程运价折算。本项已从里程费用变化中扣除，避免重复计算。路线偏离不等于责任认定。"
            for ev in attribution.detour_events:
                rate = (ev.evidence_json or {}).get("per_km_rate", "—")
                events.append({"title": f"{ev.entry_name} → {ev.exit_name}",
                    "detail": f"参考距离 {number(ev.ref_distance_m, 1)} 米 · 实际距离 {number(ev.actual_distance_m, 1)} 米",
                    "formula": f"多行驶 {number(ev.extra_distance_m, 1)} 米 ÷ 1000 × {rate} 元/公里 ＝ {number(ev.fee)} 元",
                    "note": "距离按保留精度展示，费用四舍五入到分；可在下方地图查看偏离路段。"})
            steps = [f"以上 {len(events)} 段绕路费用合计 {number(item.amount)} 元。"] if events else ["本单没有认定可计费的绕路段，绕路费用为 0.00 元。"]
        elif code == "MILEAGE_EXTRA":
            step_m = e.get('meter_step_m', 500)
            rule = f"起步包含 3 公里，超出后每 {step_m} 米按本单档位计费，不足一档向上取整。先比较实际与预估的里程费，再扣除已单列的绕路费用。"
            detour = next((i.amount for i in attribution.items if i.code == "DETOUR_EXTRA"), 0)
            steps = [f"预估里程 {e.get('estimated_distance_m', '—')} 米：超出起步部分 ÷ {step_m} 米，向上取整为计费档数，每档 1 元，对应里程费 {e.get('mileage_fare_est', '—')} 元。",
                     f"实际里程 {e.get('actual_distance_m', '—')} 米：先扣除 3000 米，再除以 {step_m} 米并向上取整，沿用预估档位的里程费 {e.get('mileage_fare_actual_under_est_rule', '—')} 元。",
                     f"{e.get('mileage_fare_actual_under_est_rule', '—')} − {e.get('mileage_fare_est', '—')} − 绕路费用 {number(detour)} ＝ {number(item.amount)} 元。",
                     "本项可以为负，表示这部分费用比预估更低；夜间换档的影响在另一项单列。"]
        elif code == "RETURN_FEE":
            tier = e.get("tier", {})
            rule = f"超过 {tier.get('tier1_from_m', 15000) / 1000:g} 公里至 {tier.get('tier1_to_m', 20000) / 1000:g} 公里的部分，按连续里程运价加收 {number(Decimal(str(tier.get('tier1_rate', .3))) * 100, 0)}%；超过 {tier.get('tier2_from_m', 20000) / 1000:g} 公里的部分加收 {number(Decimal(str(tier.get('tier2_rate', .5))) * 100, 0)}%。仅对超出部分计费。"
            rates = {k: Decimal(1000) / Decimal(str(basis[k]['meter_step_m'])) * Decimal(str(basis[k]['step_fare'])) for k in ('DAY', 'NIGHT') if k in basis}
            for label, distance, version, saved_fee in (
                ('预估', e.get('estimated_distance_m', 0), attribution.order.estimate.rule_version, e.get('estimate_return_fee', '—')),
                ('实际', e.get('actual_distance_m', 0), attribution.order.bill.rule_version, e.get('bill_return_fee', '—')),
            ):
                first = max(0, min(distance, tier.get('tier1_to_m', 20000)) - tier.get('tier1_from_m', 15000))
                second = max(0, distance - tier.get('tier2_from_m', 20000))
                rate = rates.get(version.removeprefix('SY-'))
                if rate is not None:
                    steps.append(f"{label}分段计算：（{first} 米 ÷ 1000 × {number(Decimal(str(tier.get('tier1_rate', .3))) * 100, 0)}% ＋ {second} 米 ÷ 1000 × {number(Decimal(str(tier.get('tier2_rate', .5))) * 100, 0)}%）× {number(rate, 4)} 元/公里 ＝ {saved_fee} 元（金额保留到分）。")
            steps += [f"预估里程 {e.get('estimated_distance_m', '—')} 米，返空费 {e.get('estimate_return_fee', '—')} 元。" ,
                     f"实际里程 {e.get('actual_distance_m', '—')} 米，返空费 {e.get('bill_return_fee', '—')} 元。",
                     f"实际返空费 {e.get('bill_return_fee', '—')} − 预估返空费 {e.get('estimate_return_fee', '—')} ＝ {number(item.amount)} 元。"]
        elif code == "NIGHT_SURCHARGE":
            rule = "白天起步 9 元含 3 公里、超出每 500 米 1 元；夜间或冷气档起步 10 元含 3 公里、超出每 455 米 1 元。夜间为 22:00 至次日 06:00。本演示按上车时刻确定预估档、结束时刻确定实付档。"
            steps = [f"预估使用{chinese(e.get('estimate_rule'))}，实付使用{chinese(e.get('bill_rule'))}。",
                     f"按实际里程重算：实付档起步与里程费 {e.get('core_fare_bill_rule', '—')} 元 − 预估档起步与里程费 {e.get('core_fare_est_rule', '—')} 元 ＝ {number(item.amount)} 元。"]
        elif code == "PRICING_RULE_CHANGE":
            rule = "沈阳计价基准在本系统中固定只读，不随订单变动。白天与夜间档位切换单独展示，不视为计价规则修改。"
            steps = ["本次计价规则没有变化，因此该项为 0.00 元。"]
        else:
            rule = "待核实差额（原称残差）是暂时无法解释的金额，必须单独保留。绝对值不超过 0.01 元按舍入容差归零，超过时需要人工复核。"
            steps = [f"总差额 {e.get('total_delta', number(attribution.total_delta))} − 已解释金额 {e.get('explained_amount', number(attribution.explained_amount))} ＝ 待核实差额 {number(item.amount)} 元。"]
            comp = e.get("components", {})
            if comp:
                steps += [f"按规则重算的预估费用 {comp.get('quote_recomputed', '—')} 元，实付费用（含已举证等候费）{comp.get('bill_recomputed', '—')} 元。",
                          f"预估重算偏差 {comp.get('quote_gap', '—')} 元；实付与规则重算值的差异 {comp.get('bill_gap', '—')} 元，交由人工核实。"]
        result.append({"code": code, "label": LABELS.get(code, item.label), "amount": number(item.amount),
                       "nonzero": item.amount != 0, "impact": chinese(item.impact_class), "rule": rule,
                       "steps": steps, "events": events})
    return sorted(result, key=lambda i: not i["nonzero"])
