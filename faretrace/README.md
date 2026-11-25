# FareTrace 沈阳出租车透明账单归因系统

> 项目总览、界面预览与工程说明见[仓库首页](../README.md)；本页保留运行命令与详细 API 示例。
>
> 课程项目（MIS 实验室 · 2026）。订单完成后的**费用差额归因层**：以**沈阳出租车计价规则的演示口径为固定基准**，
> 输入预估价快照、实付账单、实际轨迹与参考路径，输出一份可解释、可审计、双向透明的账单——
> 堵车的钱拆到具体路口和等候秒数，绕路的钱拆到具体偏离路段和多出的米数，每一项都能在 Leaflet 沈阳地图上定位。
>
> ⚠️ 全部订单/轨迹/账单为**模拟数据**（沈阳真实道路锚点 + 合成轨迹），不接入真实支付与付费地图 API，不产生真实扣款。

## 快速开始

```bash
cd faretrace
pip install -r requirements.txt      # 依赖已装可跳过

python -m pytest -q                  # 全部自动化测试（83 项）
python seed.py                       # 重置并灌入 7 笔沈阳种子订单（服务启动时也会自动灌）
python -m uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000/orders
```

- 三个页面：`/orders`（订单列表）、`/orders/FT-SY-1800/bill`（透明账单 + Leaflet 地图）、`/disputes/new`（争议提交）
- API 文档（Swagger）：`http://127.0.0.1:8000/docs`
- 生活化验收样例：`FT-SY-1800`（沈阳晚高峰，预估价 18.00 元 → 实付 27.00 元）

## 沈阳固定计费基准（不可运行时修改，见 config/attribution.yaml）

| 项目 | 规则 |
|---|---|
| 起步价（白天） | 9 元 / 3 公里 |
| 里程运价 | 超 3 公里后每 500 米 1 元（≈2 元/公里） |
| 夜间/夏季冷气 | 22:00—次日 6:00：起步价 10 元/3 公里，每 455 米 1 元 |
| 阶梯返空费 | 15–20 公里部分按里程运价加收 30%；20 公里以上部分加收 50% |
| 低速等候费（时距并计） | 时速 <12 km/h 时里程费照常，等候费另收：高峰（07:00—09:00、16:30—18:30）累计每 2 分钟 1 元（120 秒/元）；平峰累计每 3 分钟 1 元（180 秒/元），按高峰/平峰分段结算 |

规则字段全部外置于 `config/attribution.yaml`（`shenyang_fare` 节），经 `GET /api/config` 只读可查，归因记录落库 `config_version` 可追溯；日常归因中"计价规则变化"恒为 0 元。

## 目录结构

```
faretrace/
├── app/
│   ├── main.py            # 应用装配、统一错误结构、启动建表+种子
│   ├── config.py db.py models.py schemas.py enums.py errors.py
│   ├── routers/           # orders / bills / disputes / meta / pages / serializers
│   ├── services/
│   │   ├── shenyang.py    # ★沈阳固定计费基准（档位/里程/返空/等候费/高峰窗口）
│   │   ├── roadnet.py     # ★沈阳真实道路锚点 + 绕行几何 + 轨迹合成
│   │   ├── trajectory.py  # 去重/剔异常/拟合度/低速段/偏离段
│   │   ├── attribution.py # ★三维偏差归因 → 七归因项 + 等候/绕路事件 + 残差
│   │   ├── bill_view.py   # 双视角投影 + Leaflet 地图数据
│   │   └── ingest.py disputes.py geo.py pricing.py seed.py
│   └── templates/ static/ # 三个页面（账单页内嵌 Leaflet 地图）
├── config/attribution.yaml  # 沈阳计费基准 + 阈值（唯一口径来源）
├── seed.py                  # 种子脚本
├── tests/                   # 自动化测试（以 pytest 输出为准）
├── docs/                    # 需求可行性/系统设计/用例/展示脚本/引用/验收清单
├── ASSUMPTIONS.md TEAM_ROLES.md
└── requirements.txt pytest.ini
```

## 归因项（七个，全部恒输出，含 0.00 项）

| 归因项 | 说明 | 证据要点 |
|---|---|---|
| `WAIT_FEE` 等候费（堵车/低速） | 每条连续低速段（<12km/h）= 一次等候事件，高峰秒/120 + 平峰秒/180 | 路口名、起止时间、持续秒数、平均速度、是否高峰、坐标 |
| `DETOUR_EXTRA` 绕路增量 | 每段偏离参考路径的多行驶里程 × 里程运价 | 起终点道路名、参考/实际/多出米数、坐标、偏离段轨迹 |
| `MILEAGE_EXTRA` 里程费增量 | 参考路径上计费里程较预估增减（固定费率） | ΔD、费率、取整规则 |
| `RETURN_FEE` 阶梯返空费 | >15km 按阶梯规则（账单−预估） | 里程、档位比例 |
| `NIGHT_SURCHARGE` 夜间/冷气差价 | 跨 22:00 换档差异 | 档位字段对比 |
| `PRICING_RULE_CHANGE` | 沈阳基准固定，**恒为 0 元** | 规则字段 |
| `UNEXPLAINED_RESIDUAL` 残差 | \|残差\|≤0.01 元归零；超出即提示待核查；\|残差\|>1.00 元时整体状态为 `NEEDS_REVIEW` | 差额来源分解 |

金额守恒：`Σ七项 = 实付 − 预估`（±0.01 元），残差永不并入堵车/绕路项凑平。

当前仓库是无鉴权的演示原型，SQLite 使用非耐久性优化参数。`python seed.py` 与 `/api/demo/reset` 会重建数据；部署边界见[运行维护说明](../docs/operations.md)。

## 接口一览与 curl 示例

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（含 config_version） |
| GET | `/api/config` | 只读：沈阳计费基准 + 阈值 + 归因项编码 |
| POST | `/api/orders` | 创建订单（预估价快照 + 参考路径；档位按时间自动判定） |
| POST | `/api/orders/{no}/complete` | 完成订单（账单 + 轨迹 → 归因 → 透明账单 + 事件） |
| POST | `/api/orders/{no}/attribute` | 重算归因（revision+1，争议快照不受影响） |
| GET | `/api/orders` / `/api/orders/{no}` | 订单列表 / 原始数据 |
| GET | `/api/orders/{no}/bill?view=passenger\|driver\|both` | 双视角透明账单（含地图数据） |
| GET | `/api/orders/{no}/attribution` | 原始归因记录 + 证据 bundle |
| GET | `/api/orders/{no}/events` | **等候事件与绕路事件列表（含地图坐标）** |
| POST/GET/PATCH | `/api/disputes…` | 争议提交（冻结证据快照）/ 查询 / 复核流转 |
| POST | `/api/demo/reset` | 重置演示库并重灌 7 笔种子 |

```bash
# 1) 健康检查 / 沈阳计费规则
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/config

# 2) 创建订单（预估价快照 + 参考路径，档位按 requested_at 自动判定）
curl -X POST http://127.0.0.1:8000/api/orders -H "Content-Type: application/json" -d '{
  "order_no": "SY-CURL-1",
  "origin_label": "市府广场", "destination_label": "浑河桥南",
  "requested_at": "2026-03-06T17:00:00",
  "estimate": {"quoted_price": "16.00", "est_distance_m": 6500, "est_duration_s": 590},
  "reference_route": {
    "polyline": [[41.8015,123.4352],[41.742,123.4362]],
    "length_m": 6500,
    "node_names": ["市府广场", "浑河桥南"]
  }
}'

# 3) 完成订单（账单 + 轨迹），自动归因并生成透明账单与等候/绕路事件
curl -X POST http://127.0.0.1:8000/api/orders/SY-CURL-1/complete -H "Content-Type: application/json" -d '{
  "bill": {"paid_amount": "17.00", "actual_distance_m": 6500, "actual_duration_s": 590},
  "trajectory": [
    {"seq":0,"ts":"2026-03-06T17:00:00","lat":41.8015,"lng":123.4352,"speed_mps":0.0},
    {"seq":1,"ts":"2026-03-06T17:02:00","lat":41.8015,"lng":123.4352,"speed_mps":0.0},
    {"seq":2,"ts":"2026-03-06T17:02:15","lat":41.8005,"lng":123.4355,"speed_mps":11.0},
    {"seq":3,"ts":"2026-03-06T17:09:50","lat":41.742,"lng":123.4362,"speed_mps":11.0}
  ],
  "completed_at": "2026-03-06T17:09:50"
}'

# 4) 查询透明账单（含 Leaflet 地图数据）与等候/绕路事件
curl "http://127.0.0.1:8000/api/orders/FT-SY-1800/bill"
curl "http://127.0.0.1:8000/api/orders/FT-SY-1800/bill?view=driver"
curl "http://127.0.0.1:8000/api/orders/FT-SY-1800/events"

# 5) 原始归因记录（审计）
curl http://127.0.0.1:8000/api/orders/FT-SY-1800/attribution

# 6) 提交争议并查询（快照冻结）
curl -X POST http://127.0.0.1:8000/api/disputes -H "Content-Type: application/json" -d '{
  "order_no": "FT-SY-1800", "item_code": "DETOUR_EXTRA",
  "initiator_role": "passenger", "reason": "浑南西路到浑河桥南不该多走 1.5 公里"
}'
curl http://127.0.0.1:8000/api/disputes

# 7) 运营复核
curl -X PATCH http://127.0.0.1:8000/api/disputes/1 -H "Content-Type: application/json" \
  -d '{"status":"REVIEWING","reviewed_by":"运营-沈阳"}'

# 8) 演示复位
curl -X POST http://127.0.0.1:8000/api/demo/reset
```

统一错误结构：

```json
{"error": {"code": "NOT_FOUND", "message": "订单 NOT-EXISTS 不存在", "details": {"order_no": "NOT-EXISTS"}}}
```

## 种子数据（7 笔沈阳模拟订单）

| 订单 | 场景 | 预估→实付 | 归因要点 |
|---|---|---|---|
| SY-1001 | 正常（起步价区间） | 10.00→10.00 | 全部归因项 0，拟合度 1.0 |
| SY-1002 | 晚高峰拥堵 | 22.00→25.08 | 3 处路口等候（160/120/90 秒高峰）→ 等候费 3.08 |
| SY-1003 | 绕路 | 22.00→23.00 | 浑南西路绕行胜利大街多走约 380 米 → 绕路增量 0.76 |
| SY-1004 | 跨夜间时段 | 10.00→11.50 | 夜间档差价 1.00 + 90 秒平峰等候 0.50 |
| SY-1005 | 含残差（轨迹缺失） | 17.00→18.50 | 残差 1.50 → **待核查**（NEEDS_REVIEW） |
| SY-1006 | 返空费 | 33.00→44.44 | 校园→北陵公园约 19 公里触发阶梯返空 |
| **FT-SY-1800** | **生活化验收** | **18.00→27.00** | **等候费 +6.00（4×180 秒高峰）+ 绕路增量 +3.00（1.5 公里）+ 残差 0** |

全部订单起点固定为东北大学浑南校区（约 41.6600, 123.4200），终点从沈阳真实地点列表中选取。规则中跨夜档位与冷气处理包含演示假设，详见 [ASSUMPTIONS.md](ASSUMPTIONS.md)。

地图：账单页使用 Leaflet + OpenStreetMap 瓦片（免费、无需 Key），中心 (41.70°N, 123.43°E)；
实际 / 参考路线、绕路偏离段、等候路口及起终点使用不同图例展示；具体颜色以页面图例为准。Leaflet 资源随仓库提供，底图瓦片需要联网；离线时保留矢量图层并提示。

参考路径批量生成（可选，一次性）：`python scripts/build_routes.py` 调用 OSRM 公共 demo + Nominatim
（每秒 ≤1 次请求、结果缓存到 `data/`），不可达时自动回退预置锚点折线；运行时归因引擎只读数据库折线，绝不实时请求。

## 测试

```bash
python -m pytest -q                            # 全部 83 项
python -m pytest tests/test_shenyang.py -q     # 沈阳计费基准
python -m pytest tests/test_attribution.py -q  # 守恒/事件/返空/夜间/残差
python -m pytest tests/test_api.py -q          # 接口与端到端
python -m pytest tests/test_frontend.py -q     # 三页面 + Leaflet 冒烟
```
