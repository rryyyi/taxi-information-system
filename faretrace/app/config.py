"""配置加载：环境变量 + config/attribution.yaml。

设计要点（对应 ASSUMPTIONS.md）：
* 配置只读加载一次并缓存，归因结果记录 `config_version`，保证"口径可追溯"。
* 阈值全部走配置文件，代码中不出现魔法数字。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "attribution.yaml"
DEFAULT_DB_PATH = PROJECT_ROOT / "faretrace.db"


class Config:
    """对 yaml 字典的轻量包装，支持 cfg["a"]["b"] 与 cfg.get_path("a.b", default)。"""

    def __init__(self, data: dict[str, Any], source: str) -> None:
        self._data = data
        self.source = source

    # --- 版本与追溯 ---
    @property
    def config_version(self) -> str:
        return str(self._data.get("config_version", "unknown"))

    @property
    def algorithm_version(self) -> str:
        return str(self._data.get("algorithm_version", "unknown"))

    # --- 访问 ---
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"配置节 {name!r} 必须是映射")
        return value

    def as_dict(self) -> dict[str, Any]:
        """用于 /api/config 暴露只读的当前规则口径。"""
        return self._data


def load_config(path: str | Path | None = None) -> Config:
    """读取 yaml 配置；文件缺失直接抛错（不允许静默使用默认值）。"""
    resolved = Path(path or os.environ.get("FARETRACE_CONFIG") or DEFAULT_CONFIG_PATH)
    if not resolved.exists():
        raise FileNotFoundError(f"归因配置文件不存在: {resolved}")
    with resolved.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"归因配置格式非法（根节点必须是映射）: {resolved}")
    missing = {"config_version", "algorithm_version"} - set(data)
    if missing:
        raise ValueError(f"归因配置缺少必需键: {sorted(missing)}")
    return Config(data, source=str(resolved))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def database_url() -> str:
    """数据库地址：默认项目内 SQLite 文件，可用 FARETRACE_DB_URL 覆盖（测试用内存/临时文件）。"""
    return os.environ.get("FARETRACE_DB_URL") or f"sqlite:///{DEFAULT_DB_PATH}"


def seed_on_start() -> bool:
    """启动时是否自动灌入演示种子数据（默认开启，便于课程现场演示）。"""
    return os.environ.get("FARETRACE_SEED_ON_START", "1").lower() not in {"0", "false", "no"}
