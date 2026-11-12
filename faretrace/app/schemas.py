"""HTTP 入参模型（Pydantic v2）。

单位约定：金额=元（2 位小数）、距离=米、时间=秒。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Amount = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]
SignedAmount = Annotated[Decimal, Field(max_digits=10, decimal_places=2)]
PositiveInt = Annotated[int, Field(gt=0)]


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quoted_price: Amount
    est_distance_m: PositiveInt
    est_duration_s: PositiveInt
    rule_version: str
    surge_multiplier: Annotated[Decimal, Field(ge=0, max_digits=5, decimal_places=2)] = Decimal("1.00")
    surcharges: dict[str, SignedAmount] = Field(default_factory=dict)
    discounts: dict[str, Amount] = Field(default_factory=dict)


class ReferenceRouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    polyline: list[list[float]] = Field(min_length=2)
    length_m: PositiveInt | None = None
    free_flow_speed_kmh: PositiveInt = 40

    @field_validator("polyline")
    @classmethod
    def _check_polyline(cls, value: list[list[float]]) -> list[list[float]]:
        for point in value:
            if len(point) != 2:
                raise ValueError("polyline 每个点必须是 [lat, lng]")
            lat, lng = float(point[0]), float(point[1])
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                raise ValueError("polyline 坐标超出经纬度范围")
        return value


class OrderCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str | None = Field(default=None, max_length=32)
    city: str = Field(default="演示城市", max_length=32)
    requested_at: datetime | None = None
    origin_label: str | None = None
    destination_label: str | None = None
    estimate: EstimateIn
    reference_route: ReferenceRouteIn


class BillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_amount: Amount
    actual_distance_m: int = Field(ge=0)
    actual_duration_s: int = Field(ge=0)
    base_fare: Amount = Decimal("0")
    distance_fare: Amount = Decimal("0")
    time_fare: Amount = Decimal("0")
    surge_multiplier: Annotated[Decimal, Field(ge=0, max_digits=5, decimal_places=2)] = Decimal("1.00")
    surcharges: dict[str, SignedAmount] = Field(default_factory=dict)
    other_fees: dict[str, SignedAmount] = Field(default_factory=dict)
    discounts: dict[str, Amount] = Field(default_factory=dict)
    rule_version: str


class TrajectoryPointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int | None = None
    ts: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    speed_mps: float | None = Field(default=None, ge=0)


class OrderCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill: BillIn
    trajectory: list[TrajectoryPointIn] = Field(default_factory=list)
    completed_at: datetime | None = None


class DisputeCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str
    item_code: str
    initiator_role: Literal["passenger", "driver"]
    reason: str = Field(default="", max_length=2000)


class DisputeUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["OPEN", "REVIEWING", "RESOLVED", "REJECTED"] | None = None
    resolution: str | None = Field(default=None, max_length=2000)
    reviewed_by: str | None = Field(default=None, max_length=64)
