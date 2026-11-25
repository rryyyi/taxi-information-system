"""OSRM 公共 demo + Nominatim 批量生成参考路径（种子数据阶段一次性执行）。

用法：
    python scripts/build_routes.py                # 生成 data/routes_cache.json
    python scripts/build_routes.py --no-network   # 仅用预置 fallback 折线（完全离线）

规范要点（见 README / docs/requirements-feasibility.md）：
    * 运行时归因引擎只读数据库折线，绝不实时请求 OSRM / Nominatim；
    * OSRM demo 限速：每秒最多 1 次请求（每次请求后 sleep ≥ 1.0s）；
    * Nominatim 地名转坐标结果缓存到 data/geocode_cache.json，运行时不再请求；
    * OSRM / Nominatim 不可达时自动回退到预置 fallback 折线（沈阳道路锚点，
      与 app/services/seed.py 离线合成口径一致），保证脚本在任何网络环境可完成。

产物 data/routes_cache.json 结构：
    {
      "generated_at": "...",
      "source": "osrm-demo" | "fallback",
      "routes": [
        {"name": "东北大学浑南校区→浑河桥南",
         "origin": {"lat":..., "lng":...}, "destination": {"lat":..., "lng":...},
         "geometry_geojson": [[lng, lat], ...],   # OSRM Route geometry（GeoJSON 顺序）
         "polyline_latlng": [[lat, lng], ...],    # 落库/渲染用（与 L.polyline 一致）
         "distance_m": ..., "duration_s": ...},
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import geo  # noqa: E402
from app.services import roadnet  # noqa: E402
from app.services.shenyang import ANCHORS  # noqa: E402

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
UA = "FareTrace-Demo/1.0 (course project; offline-first, batch once at seed time)"
RPS_SLEEP_S = 1.05  # 每秒最多 1 次请求（含缓冲）

DATA_DIR = ROOT / "data"
ROUTES_CACHE = DATA_DIR / "routes_cache.json"
GEOCODE_CACHE = DATA_DIR / "geocode_cache.json"

# 种子场景的起终点（全部真实地点；起点固定为东北大学浑南校区）
ROUTE_SPECS: list[tuple[str, str]] = [
    ("东北大学浑南校区", "沈阳南站"),
    ("东北大学浑南校区", "浑河桥南"),
    ("东北大学浑南校区", "浑南西路"),
    ("东北大学浑南校区", "北陵公园"),
]

# fallback：预置锚点折线（OSRM 不可达时使用，与 seed.py 同口径）
FALLBACK_ROUTES: dict[str, list[str]] = {
    "东北大学浑南校区→沈阳南站": ["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站"],
    "东北大学浑南校区→浑河桥南": ["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站", "浑南西路", "浑河桥南"],
    "东北大学浑南校区→浑南西路": ["东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站", "浑南西路"],
    "东北大学浑南校区→北陵公园": [
        "东北大学浑南校区", "浑南中路—金阳大街", "沈阳南站", "浑南西路", "浑河桥南",
        "南二环—青年大街", "五里河", "青年大街—文萃路", "青年大街—科普路",
        "青年大街—文化路", "青年大街—文艺路", "青年大街—大西路", "青年大街—十一纬路",
        "青年大街—市府大路", "市府广场", "沈阳北站", "北陵公园",
    ],
}


def load_geocode_cache() -> dict:
    if GEOCODE_CACHE.exists():
        return json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
    return {}


def save_geocode_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    GEOCODE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def geocode(name: str, cache: dict, use_network: bool) -> tuple[float, float]:
    """Nominatim 地名 → 坐标（先查缓存；缓存未命中且允许联网才请求）。"""
    if name in cache:
        return cache[name]["lat"], cache[name]["lng"]
    if name in ANCHORS:  # 本地锚点表本身就是缓存的一部分
        cache[name] = {"lat": ANCHORS[name][0], "lng": ANCHORS[name][1], "source": "local-anchor"}
        return ANCHORS[name]
    if not use_network:
        raise RuntimeError(f"地理编码缓存未命中且已离线：{name}")
    url = f"{NOMINATIM_BASE}?q={urllib.request.quote(name + ', 沈阳')}&format=json&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    time.sleep(RPS_SLEEP_S)
    if not data:
        raise RuntimeError(f"Nominatim 未命中：{name}")
    lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
    cache[name] = {"lat": lat, "lng": lng, "source": "nominatim"}
    return lat, lng


def osrm_route(lng1, lat1, lng2, lat2) -> dict | None:
    """OSRM demo Route；不可达或超时返回 None（调用方回退 fallback）。"""
    url = f"{OSRM_BASE}/{lng1},{lat1};{lng2},{lat2}?overview=full&geometries=geojson"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        time.sleep(RPS_SLEEP_S)
        route = data["routes"][0]
        return {
            "geometry_geojson": route["geometry"]["coordinates"],  # [lng, lat] 序列
            "distance_m": route["distance"],
            "duration_s": route["duration"],
        }
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
        return None


def fallback_route(names: list[str]) -> dict:
    route = roadnet.polyline_from_names(names)
    return {
        "geometry_geojson": [[p[1], p[0]] for p in route.points],
        "distance_m": route.length_m,
        "duration_s": int(route.length_m / 11.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-network", action="store_true", help="完全离线，仅用 fallback 折线")
    args = parser.parse_args()
    use_network = not args.no_network

    geocache = load_geocode_cache()
    routes_out = []
    source = "fallback"

    for origin_name, dest_name in ROUTE_SPECS:
        label = f"{origin_name}→{dest_name}"
        try:
            o_lat, o_lng = geocode(origin_name, geocache, use_network)
            d_lat, d_lng = geocode(dest_name, geocache, use_network)
        except RuntimeError as exc:
            print(f"[geocode] {exc} → 跳过 {label}")
            continue

        entry: dict = {
            "name": label,
            "origin": {"lat": o_lat, "lng": o_lng},
            "destination": {"lat": d_lat, "lng": d_lng},
        }
        got = None
        if use_network:
            got = osrm_route(o_lng, o_lat, d_lng, d_lat)
        if got:
            source = "osrm-demo"
            entry.update(got)
            entry["polyline_latlng"] = [[c[1], c[0]] for c in got["geometry_geojson"]]
            print(f"[osrm] {label}: {got['distance_m']:.0f} m")
        else:
            fb = fallback_route(FALLBACK_ROUTES[label])
            entry.update(fb)
            entry["polyline_latlng"] = [[c[1], c[0]] for c in fb["geometry_geojson"]]
            entry["source"] = "fallback"
            print(f"[fallback] {label}: {fb['distance_m']:.0f} m")
        routes_out.append(entry)

    DATA_DIR.mkdir(exist_ok=True)
    ROUTES_CACHE.write_text(
        json.dumps(
            {"generated_at": datetime.now().isoformat(timespec="seconds"), "source": source, "routes": routes_out},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    save_geocode_cache(geocache)
    print(f"OK → {ROUTES_CACHE.relative_to(ROOT)}（source={source}，{len(routes_out)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
