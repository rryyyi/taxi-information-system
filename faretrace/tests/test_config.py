"""配置与口径可追溯性测试。"""

from __future__ import annotations

import pytest

from app.config import Config, load_config


def test_versions_present(cfg: Config) -> None:
    assert cfg.config_version == "cfg-1.0.0"
    assert cfg.algorithm_version == "faretrace-attr-1.0.0"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("trajectory.FIT_BUFFER_M", 100),
        ("trajectory.FIT_THRESHOLD", 0.8),
        ("congestion.CONGESTION_SPEED_KMH", 15),
        ("congestion.MIN_CONGESTION_SECONDS", 60),
        ("money.epsilon", 0.01),
        ("money.decimals", 2),
        ("residual.REVIEW_THRESHOLD", 1.0),
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
