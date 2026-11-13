"""配置与口径可追溯性测试（沈阳版）。"""

from __future__ import annotations

import pytest

from app.config import Config, load_config


def test_versions_present(cfg: Config) -> None:
    assert cfg.config_version == "cfg-sy-1.0.0"
    assert cfg.algorithm_version == "faretrace-sy-attr-1.0.0"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("shenyang_fare.DAY.base_fare", 9.00),
        ("shenyang_fare.DAY.meter_step_m", 500),
        ("shenyang_fare.NIGHT.base_fare", 10.00),
        ("shenyang_fare.NIGHT.meter_step_m", 455),
        ("shenyang_fare.return_fee.tier1_rate", 0.30),
        ("shenyang_fare.return_fee.tier2_rate", 0.50),
        ("shenyang_fare.wait_fee.peak_seconds_per_yuan", 120),
        ("shenyang_fare.wait_fee.offpeak_seconds_per_yuan", 180),
        ("low_speed.speed_threshold_kmh", 12.0),
        ("trajectory.FIT_BUFFER_M", 100),
        ("trajectory.FIT_THRESHOLD", 0.8),
        ("map.center", [41.80, 123.43]),
    ],
)
def test_thresholds_come_from_config(cfg: Config, path: str, expected: object) -> None:
    assert cfg.get_path(path) == expected


def test_missing_config_fails_loudly(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_config_requires_versions(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("money: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad)
