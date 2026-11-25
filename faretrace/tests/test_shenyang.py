"""沈阳固定计费基准测试（起步价/里程运价/返空费/等候费/高峰判定）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.services import shenyang


@pytest.fixture()
def cfg():
    from app.config import get_config

    return get_config()


def test_day_params(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-DAY")
    assert p.base_fare == Decimal("9.00")
    assert p.included_km_m == 3000
    assert p.meter_step_m == 500
    assert p.per_km == Decimal("2.00")


def test_night_params(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-NIGHT")
    assert p.base_fare == Decimal("10.00")
    assert p.meter_step_m == 455
    assert p.per_km == pytest.approx(Decimal(1000) / Decimal(455), rel=1e-6)


def test_unknown_rule_rejected(cfg) -> None:
    with pytest.raises(ValueError):
        shenyang.params_for(cfg, "SY-NOPE")


def test_day_mileage_500m_steps(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-DAY")
    assert shenyang.mileage_fare(p, 3000) == Decimal("0.00")     # 起步 3 公里内
    assert shenyang.mileage_fare(p, 3001) == Decimal("1.00")     # 超 1 米也进 500 米档
    assert shenyang.mileage_fare(p, 7500) == Decimal("9.00")     # (7500-3000)/500 = 9
    assert shenyang.mileage_fare(p, 9000) == Decimal("12.00")


def test_night_mileage_455m_steps(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-NIGHT")
    assert shenyang.mileage_fare(p, 3000) == Decimal("0.00")
    assert shenyang.mileage_fare(p, 3454) == Decimal("1.00")
    assert shenyang.mileage_fare(p, 3455) == Decimal("1.00")
    assert shenyang.mileage_fare(p, 6369) == Decimal("8.00")     # ceil(3369/455)=8


def test_core_fare_day(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-DAY")
    assert shenyang.core_fare(p, 7500) == Decimal("18.00")       # 9 + 9.00
    assert shenyang.core_fare(p, 9000) == Decimal("21.00")       # 9 + 12.00
    assert shenyang.core_fare(p, 2000) == Decimal("9.00")        # 起步价


def test_return_fee_tiers(cfg) -> None:
    p = shenyang.params_for(cfg, "SY-DAY")
    assert shenyang.return_fee(cfg, p, 15000) == Decimal("0.00")      # 未超 15 公里
    assert shenyang.return_fee(cfg, p, 16000) == Decimal("0.60")      # 1km × 2元 × 30%
    assert shenyang.return_fee(cfg, p, 20000) == Decimal("3.00")      # 5km × 2 × 0.3
    assert shenyang.return_fee(cfg, p, 22000) == Decimal("5.00")      # 5km×2×0.3 + 2km×2×0.5


def test_wait_fee_peak_and_offpeak(cfg) -> None:
    assert shenyang.wait_fee(cfg, 120, 0) == Decimal("1.00")     # 高峰每 2 分钟 1 元
    assert shenyang.wait_fee(cfg, 160, 0) == Decimal("1.33")     # 160/120
    assert shenyang.wait_fee(cfg, 0, 180) == Decimal("1.00")     # 平峰每 3 分钟 1 元
    assert shenyang.wait_fee(cfg, 60, 90) == Decimal("1.00")     # 0.5 + 0.5
    assert shenyang.wait_fee(cfg, 0, 0) == Decimal("0.00")


def test_is_peak_windows(cfg) -> None:
    assert shenyang.is_peak(cfg, datetime(2026, 3, 6, 7, 0, 0))
    assert shenyang.is_peak(cfg, datetime(2026, 3, 6, 8, 59, 0))
    assert not shenyang.is_peak(cfg, datetime(2026, 3, 6, 9, 1, 0))
    assert shenyang.is_peak(cfg, datetime(2026, 3, 6, 16, 30, 0))
    assert shenyang.is_peak(cfg, datetime(2026, 3, 6, 18, 29, 0))
    assert not shenyang.is_peak(cfg, datetime(2026, 3, 6, 18, 31, 0))


def test_is_night_window(cfg) -> None:
    assert shenyang.is_night(cfg, datetime(2026, 3, 6, 22, 0, 0))
    assert shenyang.is_night(cfg, datetime(2026, 3, 6, 23, 30, 0))
    assert shenyang.is_night(cfg, datetime(2026, 3, 7, 5, 59, 0))
    assert not shenyang.is_night(cfg, datetime(2026, 3, 6, 6, 0, 0))
    assert not shenyang.is_night(cfg, datetime(2026, 3, 6, 21, 59, 0))


def test_rule_for_time(cfg) -> None:
    assert shenyang.rule_for_time(cfg, datetime(2026, 3, 6, 9, 0, 0)) == "SY-DAY"
    assert shenyang.rule_for_time(cfg, datetime(2026, 3, 6, 22, 5, 0)) == "SY-NIGHT"


def test_split_by_peak_crosses_evening_boundary(cfg) -> None:
    """16:20—16:50 跨 16:30 高峰边界：平峰 600 秒 + 高峰 1200 秒。"""
    split = shenyang.split_by_peak(cfg, datetime(2026, 3, 6, 16, 20, 0), datetime(2026, 3, 6, 16, 50, 0))
    assert split[0] == (False, 600.0)
    assert split[1] == (True, 1200.0)
    total = sum(s for _, s in split)
    assert total == 1800.0


def test_split_by_peak_morning(cfg) -> None:
    split = shenyang.split_by_peak(cfg, datetime(2026, 3, 6, 7, 30, 0), datetime(2026, 3, 6, 7, 32, 0))
    assert split == [(True, 120.0)]


def test_peak_label(cfg) -> None:
    assert shenyang.peak_label(cfg, datetime(2026, 3, 6, 17, 0, 0), datetime(2026, 3, 6, 17, 3, 0)) == "晚高峰"
    assert shenyang.peak_label(cfg, datetime(2026, 3, 6, 7, 0, 0), datetime(2026, 3, 6, 7, 3, 0)) == "早高峰"
    assert shenyang.peak_label(cfg, datetime(2026, 3, 6, 14, 0, 0), datetime(2026, 3, 6, 14, 3, 0)) == "平峰"


def test_shenyang_anchors_are_in_city(cfg) -> None:
    """锚点必须落在沈阳城区范围（约 41.6–41.9°N, 123.3–123.6°E）。"""
    for name, (lat, lng) in shenyang.ANCHORS.items():
        assert 41.6 < lat < 41.9, name
        assert 123.3 < lng < 123.6, name
    assert "青年大街—文艺路" in shenyang.ANCHORS
