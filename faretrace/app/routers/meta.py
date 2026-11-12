"""系统元信息接口：健康检查、只读配置（阈值与规则版本，可追溯）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_config
from app.enums import AttributionCode, ITEM_LABELS
from app.errors import ValidationError

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health() -> dict:
    cfg = get_config()
    return {
        "status": "ok",
        "service": "fare-trace",
        "config_version": cfg.config_version,
        "algorithm_version": cfg.algorithm_version,
    }


@router.get("/config")
def config() -> dict:
    """只读：当前算法阈值、规则版本、归因项编码（供前端与审计核对）。"""
    cfg = get_config()
    traj = cfg.section("trajectory")
    cong = cfg.section("congestion")
    resid = cfg.section("residual")
    return {
        "config_version": cfg.config_version,
        "algorithm_version": cfg.algorithm_version,
        "money": cfg.section("money"),
        "thresholds": {
            "FIT_BUFFER_M": traj["FIT_BUFFER_M"],
            "FIT_THRESHOLD": traj["FIT_THRESHOLD"],
            "DEVIATION_SPAN_THRESHOLD_M": traj["DEVIATION_SPAN_THRESHOLD_M"],
            "CONGESTION_SPEED_KMH": cong["CONGESTION_SPEED_KMH"],
            "MIN_CONGESTION_SECONDS": cong["MIN_CONGESTION_SECONDS"],
            "MIN_POINTS": traj["MIN_POINTS"],
            "MAX_GAP_SECONDS": traj["MAX_GAP_SECONDS"],
            "OUTLIER_SPEED_MPS": traj["OUTLIER_SPEED_MPS"],
            "ZERO_TOLERANCE": str(resid["ZERO_TOLERANCE"]),
            "REVIEW_THRESHOLD": str(resid["REVIEW_THRESHOLD"]),
        },
        "attribution_items": [
            {"code": code.value, "label": ITEM_LABELS[code.value]} for code in AttributionCode
        ],
    }


@router.get("/demo/reset", include_in_schema=False)
def demo_reset_guard() -> None:
    raise ValidationError("演示复位请使用 POST /api/demo/reset")
