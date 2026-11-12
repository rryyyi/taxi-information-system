"""域内枚举（前后端、文档共享契约）。

归因项编码一经发布不得改名；改名视为口径变更，必须升级 config_version。
"""

from __future__ import annotations

from enum import Enum


class AttributionCode(str, Enum):
    PATH_DEVIATION = "PATH_DEVIATION"
    CONGESTION_DELAY = "CONGESTION_DELAY"
    DRIVER_DELAY = "DRIVER_DELAY"
    PRICING_RULE_CHANGE = "PRICING_RULE_CHANGE"
    TOLL_OR_FEE = "TOLL_OR_FEE"
    UNEXPLAINED_RESIDUAL = "UNEXPLAINED_RESIDUAL"


ITEM_LABELS: dict[str, str] = {
    AttributionCode.PATH_DEVIATION.value: "路径偏离",
    AttributionCode.CONGESTION_DELAY.value: "拥堵延迟",
    AttributionCode.DRIVER_DELAY.value: "司机或其他延迟",
    AttributionCode.PRICING_RULE_CHANGE.value: "计价规则变化",
    AttributionCode.TOLL_OR_FEE.value: "路侧杂费变化",
    AttributionCode.UNEXPLAINED_RESIDUAL.value: "残差",
}

# 归因项的展示顺序：距离 -> 时间 -> 规则 -> 杂费 -> 残差
ITEM_ORDER: dict[str, int] = {code.value: i for i, code in enumerate(AttributionCode)}


class ImpactClass(str, Enum):
    """责任归属：用于乘客视角/司机视角的叙述分流（不做裁决，只做归类）"""

    DRIVER_RELATED = "driver_related"
    ROAD_CONDITION = "road_condition"
    PLATFORM_RULE = "platform_rule"
    UNKNOWN = "unknown"


IMPACT_LABELS: dict[str, str] = {
    ImpactClass.DRIVER_RELATED.value: "与司机驾驶行为相关",
    ImpactClass.ROAD_CONDITION.value: "与路况相关",
    ImpactClass.PLATFORM_RULE.value: "与平台计价规则相关",
    ImpactClass.UNKNOWN.value: "无法归因",
}


IMPACT_BY_CODE: dict[str, str] = {
    AttributionCode.PATH_DEVIATION.value: ImpactClass.DRIVER_RELATED.value,
    AttributionCode.CONGESTION_DELAY.value: ImpactClass.ROAD_CONDITION.value,
    AttributionCode.DRIVER_DELAY.value: ImpactClass.DRIVER_RELATED.value,
    AttributionCode.PRICING_RULE_CHANGE.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.TOLL_OR_FEE.value: ImpactClass.PLATFORM_RULE.value,
    AttributionCode.UNEXPLAINED_RESIDUAL.value: ImpactClass.UNKNOWN.value,
}


class DataQuality(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class AttributionStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"        # 基于不完整轨迹的降级计算
    NEEDS_REVIEW = "NEEDS_REVIEW"  # 残差超阈值


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    COMPLETED = "COMPLETED"


class DisputeStatus(str, Enum):
    OPEN = "OPEN"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class InitiatorRole(str, Enum):
    PASSENGER = "passenger"
    DRIVER = "driver"
