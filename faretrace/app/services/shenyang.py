"""沈阳出租车固定计费基准（Shenyang taxi fare basis）。

计费基准为**固定规则**（见 config/attribution.yaml 的 shenyang_fare 节）：
    白天档  起步价 9 元 / 3 公里；超 3 公里后每 500 米 1 元（≈2 元/公里）
    夜间档  起步价 10 元 / 3 公里；每 455 米 1 元（22:00—次日 6:00 或夏季冷气）
    返空费  超 15 公里至 20 公里部分按里程运价加收 30%；20 公里以上部分加收 50%
    等候费  时距并计：时速 <12 km/h 时里程费照常，等候费另收
            高峰（07:00—09:00、16:30—18:30）累计每 2 分钟 1 元（120 秒/元）
            平峰累计每 3 分钟 1 元（180 秒/元），分段结算后求和

本模块是全部归因金额的**唯一计算依据**；规则字段来自配置文件，版本可追溯。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import NamedTuple

from app.services.pricing import money


class FareParams(NamedTuple):
    """一个计价档位（白天/夜间）的固定参数。"""

    version: str            # SY-DAY / SY-NIGHT
    base_fare: Decimal
    included_km_m: int
    meter_step_m: int
    step_fare: Decimal

    @property
    def per_km(self) -> Decimal:
        """连续里程运价（元/公里）：1000 / meter_step_m × step_fare。"""
        return (Decimal(1000) / Decimal(self.meter_step_m)) * self.step_fare


def _parse_window(text: str) -> tuple[time, time]:
    start_s, end_s = text.split("-")
    start = time(*[int(x) for x in start_s.split(":")])
    end = time(*[int(x) for x in end_s.split(":")])
    return start, end


def params_for(cfg, version: str) -> FareParams:
    """按版本名取计价参数（SY-DAY / SY-NIGHT，兼容 DAY / NIGHT）。未知版本报错（口径不允许静默回退）。"""
    fare_cfg = cfg.section("shenyang_fare")
    key = version[3:] if version.startswith("SY-") else version
    if key not in fare_cfg:
        raise ValueError(f"未知计价档位 {version!r}，可用: SY-DAY / SY-NIGHT")
    spec = fare_cfg[key]
    return FareParams(
        version="SY-" + key,
        base_fare=Decimal(str(spec["base_fare"])),
        included_km_m=int(spec["included_km_m"]),
        meter_step_m=int(spec["meter_step_m"]),
        step_fare=Decimal(str(spec["step_fare"])),
    )


def rule_for_time(cfg, dt: datetime) -> str:
    """按时刻判定计价档位：22:00—次日 6:00 为夜间档，其余白天档。"""
    t = dt.time()
    start, end = _parse_window(cfg.get_path("shenyang_fare.night_window", "22:00-06:00"))
    if start > end:  # 跨午夜窗口
        return "SY-NIGHT" if t >= start or t < end else "SY-DAY"
    return "SY-NIGHT" if start <= t < end else "SY-DAY"


def _peak_windows(cfg) -> list[tuple[time, time]]:
    return [_parse_window(w) for w in cfg.get_path("shenyang_fare.peak_windows", [])]


def is_peak(cfg, dt: datetime) -> bool:
    t = dt.time()
    return any(start <= t < end for start, end in _peak_windows(cfg))


def is_night(cfg, dt: datetime) -> bool:
    return rule_for_time(cfg, dt) == "SY-NIGHT"


def peak_label(cfg, t_start: datetime, t_end: datetime) -> str:
    """等候事件的时段标签：早高峰 / 晚高峰 / 平峰。"""
    has_peak = False
    late = False
    for is_p, seconds in split_by_peak(cfg, t_start, t_end):
        if is_p and seconds > 0:
            has_peak = True
            # 取高峰秒数较多者判断早/晚
            mid = t_start + timedelta(seconds=float(seconds))
            if mid.hour >= 12:
                late = True
    if not has_peak:
        return "平峰"
    return "晚高峰" if late else "早高峰"


def split_by_peak(cfg, t_start: datetime, t_end: datetime) -> list[tuple[bool, float]]:
    """把 [t_start, t_end] 按高峰窗口切开，返回 [(是否高峰, 秒数), ...]（顺序拼接）。

    高峰窗口不跨午夜，因此每个边界日只处理当天的 07:00/09:00/16:30/18:30。
    """
    if t_end <= t_start:
        return []
    windows = _peak_windows(cfg)
    boundaries: list[datetime] = []
    day0 = (t_start - timedelta(days=1)).date()
    for offset in (0, 1, 2):
        d = day0 + timedelta(days=offset)
        for start, end in windows:
            boundaries.append(datetime.combine(d, start, tzinfo=t_start.tzinfo))
            boundaries.append(datetime.combine(d, end, tzinfo=t_start.tzinfo))
    boundaries.sort()

    segments: list[tuple[bool, float]] = []
    cur = t_start
    while cur < t_end:
        peak = is_peak(cfg, cur)
        next_boundary = next((b for b in boundaries if b > cur), None)
        seg_end = t_end if next_boundary is None else min(t_end, next_boundary)
        seconds = (seg_end - cur).total_seconds()
        if seconds > 0:
            segments.append((peak, seconds))
        cur = seg_end
    return segments


def mileage_fare(params: FareParams, distance_m: int | float) -> Decimal:
    """里程运价：超出起步公里数后，按 meter_step_m 向上取整逐步计费。"""
    over = max(0.0, float(distance_m) - params.included_km_m)
    steps = int((Decimal(str(over)) / Decimal(params.meter_step_m)).to_integral_value(rounding=ROUND_CEILING))
    return Decimal(steps) * params.step_fare


def core_fare(params: FareParams, distance_m: int | float) -> Decimal:
    """起步价 + 里程运价（不含等候费与返空费）。"""
    return money(params.base_fare + mileage_fare(params, distance_m))


def return_fee(params: FareParams, distance_m: int | float) -> Decimal:
    """阶梯返空费：15–20 公里部分按里程运价 ×30%，20 公里以上部分 ×50%。"""
    d = float(distance_m)
    tier = {
        "tier1_from_m": 15000.0,
        "tier1_to_m": 20000.0,
        "tier1_rate": 0.30,
        "tier2_from_m": 20000.0,
        "tier2_rate": 0.50,
    }
    # 阈值可从配置覆盖
    t1_from = float(tier["tier1_from_m"])
    t1_to = float(tier["tier1_to_m"])
    t2_from = float(tier["tier2_from_m"])
    tier1_m = max(0.0, min(d, t1_to) - t1_from)
    tier2_m = max(0.0, d - t2_from)
    rate = params.per_km
    fee = (Decimal(str(tier1_m)) / Decimal(1000)) * rate * Decimal("0.30") + (
        Decimal(str(tier2_m)) / Decimal(1000)
    ) * rate * Decimal("0.50")
    return money(fee)


def wait_fee(cfg, peak_seconds: float, offpeak_seconds: float) -> Decimal:
    """低速等候费：高峰秒数/120 × 1 元 + 平峰秒数/180 × 1 元，分段求和后取 2 位小数。"""
    per_peak = Decimal(cfg.get_path("shenyang_fare.wait_fee.peak_seconds_per_yuan", 120))
    per_off = Decimal(cfg.get_path("shenyang_fare.wait_fee.offpeak_seconds_per_yuan", 180))
    fee = (Decimal(str(peak_seconds)) / per_peak) + (Decimal(str(offpeak_seconds)) / per_off)
    return money(fee)


# ---- 沈阳真实道路锚点（坐标为 OpenStreetMap 公开数据的近似值，用于合成演示轨迹）----
ANCHORS: dict[str, tuple[float, float]] = {
    "沈阳北站": (41.8066, 123.4328),
    "市府广场": (41.8015, 123.4352),
    "青年大街—市府大路": (41.7995, 123.4359),
    "青年大街—十一纬路": (41.7940, 123.4359),
    "青年大街—大西路": (41.7905, 123.4359),
    "青年大街—文艺路": (41.7845, 123.4359),
    "青年大街—文化路": (41.7780, 123.4359),
    "青年大街—科普路": (41.7745, 123.4359),
    "青年大街—文萃路": (41.7700, 123.4359),
    "五里河": (41.7640, 123.4359),
    "南二环—青年大街": (41.7600, 123.4359),
    "浑河桥北": (41.7520, 123.4360),
    "浑河桥南": (41.7420, 123.4362),
    "三好街—文化路": (41.7660, 123.4230),
    "三好街北口": (41.7760, 123.4230),
    "沈阳站": (41.7947, 123.3959),
    "中街·故宫": (41.7990, 123.4550),
    "北陵公园": (41.8280, 123.4290),
    "浑南西路": (41.7200, 123.4280),
    "沈阳南站": (41.6880, 123.4140),
    "苏家屯区中心": (41.6620, 123.4100),
}

MAP_CENTER = (41.80, 123.43)
