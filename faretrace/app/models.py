"""持久化模型（11 张表）。

金额字段统一用 Numeric(10,2)（单位：元，2 位小数）；
距离字段单位米（int），时间字段单位秒（int）。
所有金额在业务层通过 app.services.pricing.money() 做 ROUND_HALF_UP 量化，
保证归因项之和 + 残差 与总差额在 ±0.01 元内闭合。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

Money = Numeric(10, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PricingRule(Base):
    """计价规则版本（预估规则 / 实付规则都引用此表，便于"规则变化"举证）。"""

    __tablename__ = "pricing_rules"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_fare: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    per_km: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    per_min: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    min_fare: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    surge_multiplier: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1.00"))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(32), nullable=False, default="演示城市")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="CREATED")
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    origin_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    destination_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    estimate: Mapped["EstimateSnapshot | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )
    bill: Mapped["Bill | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )
    reference_route: Mapped["ReferenceRoute | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )
    points: Mapped[list["TrajectoryPoint"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="TrajectoryPoint.seq"
    )
    attribution: Mapped["Attribution | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )


class EstimateSnapshot(Base):
    """下单时的预估价快照（乘客看到的"预估价 18 元"）。"""

    __tablename__ = "fare_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    quoted_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    est_distance_m: Mapped[int] = mapped_column(Integer, nullable=False)
    est_duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(ForeignKey("pricing_rules.version"), nullable=False)
    surge_multiplier: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1.00"))
    surcharges_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    discounts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="estimate")


class Bill(Base):
    """实付账单（分项 + 汇总）。"""

    __tablename__ = "actual_bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    paid_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    actual_distance_m: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    base_fare: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    distance_fare: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    time_fare: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    surge_multiplier: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1.00"))
    surcharges_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    other_fees_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    discounts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rule_version: Mapped[str] = mapped_column(ForeignKey("pricing_rules.version"), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="bill")


class ReferenceRoute(Base):
    """参考路径（下单时规划路径）：折线 + 沈阳道路节点名表（供地图与事件定位）。"""

    __tablename__ = "reference_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    polyline_json: Mapped[list] = mapped_column(JSON, nullable=False)
    nodes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    length_m: Mapped[int] = mapped_column(Integer, nullable=False)
    est_duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    free_flow_speed_kmh: Mapped[int] = mapped_column(Integer, nullable=False, default=40)

    order: Mapped["Order"] = relationship(back_populates="reference_route")


class TrajectoryPoint(Base):
    __tablename__ = "track_points"
    __table_args__ = (Index("ix_traj_order_seq", "order_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lat: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    lng: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    speed_mps: Mapped[float | None] = mapped_column(nullable=True)  # type: ignore[arg-type]

    order: Mapped["Order"] = relationship(back_populates="points")


class Attribution(Base):
    """归因记录（一次归因运行的结果，订单唯一，重算 revision+1）。"""

    __tablename__ = "attributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    total_delta: Mapped[Decimal] = mapped_column(Money, nullable=False)
    explained_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    residual: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    fit_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    avg_deviation_m: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False, default="COMPLETE")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")
    quality_flags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_bundle_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="attribution")
    items: Mapped[list["AttributionItem"]] = relationship(
        back_populates="attribution",
        cascade="all, delete-orphan",
        order_by="AttributionItem.sort_order",
    )
    wait_events: Mapped[list["WaitEvent"]] = relationship(
        back_populates="attribution", cascade="all, delete-orphan", order_by="WaitEvent.t_start"
    )
    detour_events: Mapped[list["DetourEvent"]] = relationship(
        back_populates="attribution", cascade="all, delete-orphan", order_by="DetourEvent.id"
    )
    transparent_bill: Mapped["TransparentBill | None"] = relationship(
        back_populates="attribution", uselist=False, cascade="all, delete-orphan"
    )


class AttributionItem(Base):
    """透明账单结果表：单条归因项（金额 + 说明 + 证据 JSON）。"""

    __tablename__ = "attribution_records"
    __table_args__ = (UniqueConstraint("attribution_id", "code", name="uq_item_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attribution_id: Mapped[int] = mapped_column(
        ForeignKey("attributions.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    impact_class: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    attribution: Mapped["Attribution"] = relationship(back_populates="items")


class TransparentBill(Base):
    """面向乘客/司机的成品账单视图（同一归因，两种叙述，含地图数据）。"""

    __tablename__ = "transparent_bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attribution_id: Mapped[int] = mapped_column(
        ForeignKey("attributions.id", ondelete="CASCADE"), unique=True
    )
    passenger_view_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    driver_view_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    attribution: Mapped["Attribution"] = relationship(back_populates="transparent_bill")


class WaitEvent(Base):
    """等候事件表：一条连续低速段 = 一次路口/路段低速等候（时距并计计费，地图可定位）。"""

    __tablename__ = "wait_events"
    __table_args__ = (Index("ix_wait_attr", "attribution_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    attribution_id: Mapped[int] = mapped_column(
        ForeignKey("attributions.id", ondelete="CASCADE"), nullable=False
    )
    location: Mapped[str] = mapped_column(String(64), nullable=False)   # 路口/路段名
    t_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    t_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_s: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    avg_speed_kmh: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    peak_seconds: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    offpeak_seconds: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    peak_label: Mapped[str] = mapped_column(String(16), nullable=False, default="平峰")
    fee: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    lat: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    lng: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    attribution: Mapped["Attribution"] = relationship(back_populates="wait_events")


class DetourEvent(Base):
    """绕路事件表：一段偏离参考路径的行驶（含地图坐标与多出里程）。"""

    __tablename__ = "detour_events"
    __table_args__ = (Index("ix_detour_attr", "attribution_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    attribution_id: Mapped[int] = mapped_column(
        ForeignKey("attributions.id", ondelete="CASCADE"), nullable=False
    )
    entry_name: Mapped[str] = mapped_column(String(64), nullable=False)
    exit_name: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_lat: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    entry_lng: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    exit_lat: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    exit_lng: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    ref_distance_m: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    actual_distance_m: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    extra_distance_m: Mapped[float] = mapped_column(nullable=False)  # type: ignore[arg-type]
    fee: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    path_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # 偏离段实际轨迹
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    attribution: Mapped["Attribution"] = relationship(back_populates="detour_events")


class Dispute(Base):
    """争议：发起时冻结归因记录与全部证据，之后重算不影响已冻结快照。"""

    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attribution_id: Mapped[int] = mapped_column(
        ForeignKey("attributions.id", ondelete="CASCADE"), nullable=False
    )
    item_code: Mapped[str] = mapped_column(String(32), nullable=False)
    initiator_role: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    evidence_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snapshot_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    evidence_links: Mapped[list["DisputeEvidence"]] = relationship(
        back_populates="dispute", cascade="all, delete-orphan"
    )


class DisputeEvidence(Base):
    """争议证据绑定表：争议 ↔ 归因项 ↔ 证据快照。"""

    __tablename__ = "dispute_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dispute_id: Mapped[int] = mapped_column(
        ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False
    )
    attribution_item_id: Mapped[int] = mapped_column(
        ForeignKey("attribution_records.id", ondelete="CASCADE"), nullable=False
    )
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    bound_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    dispute: Mapped["Dispute"] = relationship(back_populates="evidence_links")
