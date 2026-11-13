"""领域枚举（前后端、文档共享契约）。

沈阳版归因项（顺序即账单展示顺序）：
    WAIT_FEE            等候费（堵车/低速，时距并计）
    DETOUR_EXTRA        绕路增量（偏离参考路径多出的里程）
    MILEAGE_EXTRA       里程费增量（参考路径上的计费里程较预估增减）
    RETURN_FEE          阶梯返空费（>15km 按沈阳规则）
    NIGHT_SURCHARGE     夜间/冷气档差价（起步价与里程运价换档）
    PRICING_RULE_CHANGE 计价规则变化（沈阳基准固定，日常恒为 0）
    UNEXPLAINED_RESIDUAL 残差（>0.01 元必须"待核查"，不得凑平）
"""

from __future__ import annotations

from enum import Enum


class AttributionCode(str, Enum):
    WAIT_FEE = "WAIT_FEE"
    DETOUR_EXTRA = "DETOUR_EXTRA"
    MILEAGE_EXTRA = "MILEAGE_EXTRA"
    RETURN_FEE = "RETURN_FEE"
    NIGHT_SURCHARGE = "NIGHT_SURCHARGE"
    PRICING_RULE_CHANGE = "PRICING_RULE_CHANGE"
    UNEXPLAINED_RESIDUAL = "UNEXPLAINED_RESIDUAL"


ITEM_LABELS: dict[str, str] = {
    AttributionCode.WAIT_FEE.value: "等候费（堵车/低速）",
    AttributionCode.DETOUR_EXTRA.value: "绕路增量",
    AttributionCode.MILEAGE_EXTRA.value: "里程费增量",
    AttributionCode.RETURN_FEE.value: "阶梯返空费",
    AttributionCode.NIGHT_SURCHARGE.value: "夜间/冷气档差价",
    AttributionCode.PRICING_RULE_CHANGE.value: "计价规则变化",
    AttributionCode.UNEXPLAINED_RESIDUAL.value: "残差（待核查）",
}

ITEM_ORDER: dict[str, int] = {code.value: i for i, code in enumerate(AttributionCode)}


class ImpactClass(str, Enum):
    DRIVER_RELATED = "driver_related"
    ROAD_CONDITION = "road_condition"
    PLATFORM_RULE = "platform_rule"   # 沈阳固定计价规则相关
    UNKNOWN = "unknown"


IMPACT_LABELS: dict[str, str] = {
    ImpactClass.DRIVER_RELATED.value: "与司机驾驶行为相关",
    ImpactClass.ROAD_CONDITION.value: "与路况相关（堵车）",
    ImpactClass.PLATFORM_RULE.value: "与沈阳固定计价规则相关",
    ImpactClass.UNKNOWN.value: "无法归因（待核查）",
}


IMPACT_BY_CODE: dict[str, str] = {
    AttributionCode.WAIT_FEE.value: ImpactClass.ROAD_CONDITION.value,
    AttributionCode.DETOUR_EXTRA.value: ImpactClass.DRIVER_RELATED.value,
    AttributionCode.MILEAGE_EXTRA.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.RETURN_FEE.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.NIGHT_SURCHARGE.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.PRICING_RULE_CHANGE.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.UNEXPLAINED_RESIDUAL.value: ImpactClass.UNKNOWN.value,
}


class DataQuality(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class AttributionStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    NEEDS_REVIEW = "NEEDS_REVIEW"   # 残差 > 0.01 元 → 待核查


class DisputeStatus(str, Enum):
    OPEN = "OPEN"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class InitiatorRole(str, Enum):
    PASSENGER = "passenger"
    DRIVER = "driver"
