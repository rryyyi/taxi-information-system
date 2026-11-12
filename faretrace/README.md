# FareTrace 网约车透明账单归因系统

> 课程项目（MIS 实验室 · 2026）。订单完成后的**费用差额归因层**：输入预估价快照、实付账单、实际轨迹与参考路径，
> 输出一份可解释、可审计、双向透明的账单 —— 不是"更准的预估价"，而是"每一分钱差额配证据"。
>
> ⚠️ 全部订单/轨迹/账单为**模拟数据**，不接入真实支付与地图 API，不产生真实扣款。

## 快速开始

```bash
cd faretrace
pip install -r requirements.txt      # 若依赖已装可跳过

python -m pytest -q                  # 全部自动化测试（73 项）
python seed.py                       # 重置并灌入 6 笔种子订单（可选，服务启动时会自动灌）
python -m uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000/orders
```

- 三个页面：`/orders`（订单列表）、`/orders/FT-1800/bill`（透明账单详情）、`/disputes/new`（争议提交）
- API 文档（Swagger）：`http://127.0.0.1:8000/docs`
- 生活化验收样例：`FT-1800`（预估价 18.00 元 → 实付 27.00 元）

## 目录结构

```
faretrace/
├── app/
│   ├── main.py            # 应用装配、统一错误结构、启动建表+种子
│   ├── config.py db.py models.py schemas.py enums.py errors.py
│   ├── routers/           # orders / bills / disputes / meta / pages / serializers
│   ├── services/          # geo(投影) trajectory(拟合/拥堵) pricing(计价) attribution(★归因) ...
│   ├── templates/ static/ # 三个页面（Jinja2 + 原生 JS）
├── config/attribution.yaml  # 阈值与规则版本（唯一口径来源）
├── seed.py                  # 种子脚本
├── tests/                   # 73 项 pytest
├── docs/                    # 计划、手册转录、后续设计文档
├── ASSUMPTIONS.md TEAM_ROLES.md
└── requirements.txt pytest.ini
```

## 核心算法（规则化三维偏差分解）

- ΔD = 实际里程 − 预估里程；ΔT = 实际时长 − 预估时长；ΔP = 规则/杂费字段差异（不做抽象系数）
- 6 个归因项固定输出：`PATH_DEVIATION`、`CONGESTION_DELAY`、`DRIVER_DELAY`、`PRICING_RULE_CHANGE`、`TOLL_OR_FEE`、`UNEXPLAINED_RESIDUAL`
- 路径偏离仅在 `fit_score < FIT_THRESHOLD(0.8)` 时计价（`FIT_BUFFER_M=100`）；高拟合度时金额 0 但保留拟合度证据
- 拥堵：速度 < `CONGESTION_SPEED_KMH=15` 且连续 ≥ `MIN_CONGESTION_SECONDS=60` 秒
- 残差 = 总差额 − 其余五项，`|残差| ≤ 0.01` 元归零；**任何情况不并入其它项**，超 1.00 元标记 `NEEDS_REVIEW`

## 接口一览与 curl 示例

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（含 config_version） |
| GET | `/api/config` | 只读：阈值、规则版本、归因项编码 |
| POST | `/api/orders` | 创建订单（写预估价快照 + 参考路径） |
| POST | `/api/orders/{order_no}/complete` | 完成订单（写账单 + 轨迹并触发归因） |
| POST | `/api/orders/{order_no}/attribute` | 重算归因（revision+1，争议快照不受影响） |
| GET | `/api/orders` | 订单列表 |
| GET | `/api/orders/{order_no}` | 订单原始数据 |
| GET | `/api/orders/{order_no}/bill?view=passenger\|driver\|both` | 透明账单（双视角） |
| GET | `/api/orders/{order_no}/attribution` | 原始归因记录 + 证据 bundle |
| POST | `/api/disputes` | 提交争议（冻结证据快照） |
| GET | `/api/disputes` / `/api/disputes/{id}` | 争议列表 / 详情（含快照与证据绑定） |
| PATCH | `/api/disputes/{id}` | 运营复核流转 OPEN→REVIEWING→RESOLVED/REJECTED |
| POST | `/api/demo/reset` | 重置演示库并重灌 6 笔种子 |

```bash
# 1) 健康检查 / 只读配置
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/config

# 2) 创建订单（预估价快照 + 参考路径；rule_version 必须已存在）
curl -X POST http://127.0.0.1:8000/api/orders -H "Content-Type: application/json" -d '{
  "order_no": "FT-CURL-1",
  "city": "演示城市",
  "estimate": {"quoted_price": "21.00", "est_distance_m": 5000, "est_duration_s": 900,
               "rule_version": "R-EST-1", "surge_multiplier": "1.00"},
  "reference_route": {"polyline": [[30.0,120.0],[30.0,120.05]], "length_m": 5000}
}'

# 3) 完成订单（账单 + 轨迹），自动归因并生成透明账单
curl -X POST http://127.0.0.1:8000/api/orders/FT-CURL-1/complete -H "Content-Type: application/json" -d '{
  "bill": {"paid_amount": "23.50", "actual_distance_m": 5000, "actual_duration_s": 1400,
           "base_fare": "5.00", "distance_fare": "10.00", "time_fare": "8.50",
           "rule_version": "R-EST-1"},
  "trajectory": [
    {"seq":0,"ts":"2026-03-02T09:00:00Z","lat":30.0,"lng":120.0,"speed_mps":6.0},
    {"seq":1,"ts":"2026-03-02T09:05:00Z","lat":30.0,"lng":120.0125,"speed_mps":6.0},
    {"seq":2,"ts":"2026-03-02T09:10:00Z","lat":30.0,"lng":120.025,"speed_mps":6.0}
  ]
}'

# 4) 查询透明账单（乘客 + 司机）
curl "http://127.0.0.1:8000/api/orders/FT-1800/bill"
curl "http://127.0.0.1:8000/api/orders/FT-1800/bill?view=driver"

# 5) 原始归因记录（审计）
curl http://127.0.0.1:8000/api/orders/FT-1800/attribution

# 6) 提交争议并查询
curl -X POST http://127.0.0.1:8000/api/disputes -H "Content-Type: application/json" -d '{
  "order_no": "FT-1800", "item_code": "PATH_DEVIATION",
  "initiator_role": "passenger", "reason": "这段路只绕了 0.5 公里"
}'
curl http://127.0.0.1:8000/api/disputes

# 7) 运营复核
curl -X PATCH http://127.0.0.1:8000/api/disputes/1 -H "Content-Type: application/json" \
  -d '{"status":"REVIEWING","reviewed_by":"运营-张"}'

# 8) 演示复位
curl -X POST http://127.0.0.1:8000/api/demo/reset
```

统一错误结构：

```json
{"error": {"code": "NOT_FOUND", "message": "订单 NOT-EXISTS 不存在", "details": {"order_no": "NOT-EXISTS"}}}
```

## 种子数据（6 笔，全部合成）

| 订单 | 场景 | 预估价 → 实付 | 归因要点 | 数据质量 |
|---|---|---|---|---|
| FT-1001 | 正常订单 | 21.00 → 20.60 | 首单优惠 −0.40，路径/拥堵均 0 | COMPLETE |
| FT-1002 | 拥堵延迟 | 20.00 → 23.50 | 拥堵 600 秒 → +3.50 | COMPLETE |
| FT-1003 | 路径偏离 | 19.50 → 25.40 | 路径 +3.60、拥堵 +1.75、过路费 +0.55 | COMPLETE |
| FT-1004 | 计价规则变化 | 24.80 → 28.60 | 起步价/里程单价上调 +3.80 | COMPLETE |
| FT-1005 | 轨迹缺失 + 残差 | 21.00 → 23.30 | 司机延迟 +2.10、残差 0.20（降级） | INCOMPLETE |
| **FT-1800** | **生活化验收** | **18.00 → 27.00** | **拥堵 4.20 + 路径 3.10 + 规则 1.70 + 残差 0.00 = 9.00** | COMPLETE |

## 测试

```bash
python -m pytest -q                                  # 全部
python -m pytest tests/test_attribution.py -q        # 守恒 / 场景 / 随机订单
python -m pytest tests/test_api.py -q                # 接口与争议快照冻结
python -m pytest tests/test_frontend.py -q           # 三页面冒烟
```

覆盖：金额守恒（±0.01）、高拟合度无路径偏离、低拟合度有路径偏离、连续低速判拥堵、轨迹缺失降级、规则/杂费归因、残差显式记录、争议证据绑定与冻结、端到端链路、统一错误结构。

## 口径可追溯

`config/attribution.yaml` 是唯一口径来源（`config_version`），归因记录同时保存 `config_version`、`algorithm_version`、`revision`、
阈值快照与数据集质量标记；`GET /api/config` 可只读核对。
