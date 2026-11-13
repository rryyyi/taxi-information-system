# 系统设计 — FareTrace 沈阳出租车透明账单归因系统

> 对应手册第一部分第 2 项（系统设计 10%）。口径：`config_version=cfg-sy-1.0.0`；金额=元（2 位小数）、距离=米、时间=秒（沈阳本地时间 UTC+8）。

## 1. 总体架构与功能模块

```
┌──────────────────────────  客户端  ──────────────────────────┐
│ 乘客/司机（三页面，账单页内嵌 Leaflet + OSM 沈阳地图）          │
│ 平台订单系统（REST）        运营仲裁人员 / 监管方（只读审计）   │
└──────────────┬───────────────────────────────┬──────────────┘
┌──────────────▼───────────────────────────────▼──────────────┐
│ FastAPI（统一错误结构；lifespan 建表+灌种子）                  │
│  routers/orders   创建/完单(触发归因)/列表/详情/事件/原始归因   │
│  routers/bills    双视角透明账单（含地图数据）                 │
│  routers/disputes 争议提交(冻结快照)/查询/复核流转             │
│  routers/meta     health / 沈阳计费基准只读查询 / demo reset   │
│  routers/pages    三个页面 + 表单提交                         │
├──────────────────────  领域服务层  ───────────────────────────┤
│ shenyang.py   ★沈阳固定计费基准：档位判定/里程运价(500m/455m   │
│               向上取整)/阶梯返空/高峰窗口切分/等候费           │
│ roadnet.py    ★沈阳真实道路锚点(21个)→插值折线；绕行中点牛顿   │
│               迭代(精确多走米数)；沿折线行驶+路口停驶轨迹合成   │
│ trajectory.py 去重/排序/剔异常/拟合度 fit_score/低速段/偏离段   │
│ geo.py        Haversine + shapely 投影 + 沿路里程             │
│ attribution.py★三维偏差归因：等候费/绕路增量/里程增量/返空/    │
│               夜间换档/规则0/残差兜底 + 证据 JSON              │
│ bill_view.py  双视角投影 + Leaflet 地图数据组装                │
│ ingest.py     事务编排：建单/完单/归因落库(含事件表)           │
│ disputes.py   争议快照冻结与状态机                            │
├──────────────────────  数据层  ──────────────────────────────┤
│ SQLAlchemy 2.0（13 张表）⇄ SQLite；config/attribution.yaml     │
└──────────────────────────────────────────────────────────────┘
```

## 2. 核心算法

### 2.1 沈阳固定计费基准（shenyang.py，唯一计算依据）

```
白天档 SY-DAY：起步 9 元/3km；超 3km 按 500m 向上取整 × 1 元
夜间档 SY-NIGHT：起步 10 元/3km；按 455m 向上取整 × 1 元
档位判定：22:00—06:00 → 夜间档（预估按上车时刻、实付按结束时刻）
返空费：15–20km 部分运价×30%；>20km 部分运价×50%（分段）
等候费：低速(<12km/h)段按高峰窗口切分秒数；高峰秒/120 + 平峰秒/180（元）
```

### 2.2 轨迹预处理与拟合度

去重（同时刻≤1m）→ 排序（留痕）→ 剔异常（>200km/h）→ 逐段距离/速度 → 投影参考路径 → `fit_score = max(0, 1 − 平均偏离/100m)`；有效点 <3 → INCOMPLETE 降级。

### 2.3 三维偏差与七归因项

| # | 归因项 | 计算式 | 证据 |
|---|---|---|---|
| 1 | WAIT_FEE | Σ事件(高峰秒/120 + 平峰秒/180) | 路口名/起止时间/秒数/均速/高峰标签/坐标 |
| 2 | DETOUR_EXTRA | Σ偏离段(实际米 − 参考米)/1000 × 运价 | 起终点道路名/三距离/坐标/偏离段轨迹 |
| 3 | MILEAGE_EXTRA | [运价(实际里程)−运价(预估里程)] − 绕路增量（固定费率） | ΔD/费率/取整规则 |
| 4 | RETURN_FEE | 返空费(实际) − 返空费(预估) | 里程/档位比例 |
| 5 | NIGHT_SURCHARGE | 核心费(实际里程,夜间档) − 核心费(实际里程,预估档) | 档位字段对比 |
| 6 | PRICING_RULE_CHANGE | 恒 0（沈阳基准固定） | 完整规则字段 |
| 7 | UNEXPLAINED_RESIDUAL | 总差额 − 前六项；≤0.01 归零，>0.01 待核查 | 差额来源分解 |

守恒由构造保证（残差兜底）；>1.00 元 → NEEDS_REVIEW；轨迹缺失 → DEGRADED（等候 0、绕路不予认定）。

## 3. 数据库设计（SQLite，13 张表）

```
pricing_rules 1─┬─* estimate_snapshots *─1┐
                ├─* bills *───────────────1┤
orders 1────────┼─1 reference_routes(折线+道路节点名表) │
                ├─* trajectory_points        │
                └─1 attributions 1─┬─* attribution_items
                                   ├─* wait_events（等候事件表★）
                                   ├─* detour_events（绕路事件表★）
                                   ├─1 transparent_bills
                                   └─* disputes 1─* dispute_evidence
```

| 表 | 关键字段 |
|---|---|
| `pricing_rules` | version(SY-DAY/SY-NIGHT)、base_fare、per_km（审计留痕） |
| `orders` | order_no、city=沈阳、status、requested_at、completed_at、起终点标签 |
| `estimate_snapshots` | quoted_price、est_distance_m、est_duration_s、rule_version（档位自动判定） |
| `bills` | paid_amount、actual_distance_m、actual_duration_s、base_fare、distance_fare、rule_version |
| `reference_routes` | polyline_json、**nodes_json（沈阳道路节点名+坐标）**、length_m |
| `trajectory_points` | seq、ts、lat、lng、speed_mps |
| `attributions` | total_delta、explained_amount、residual、fit_score、avg_deviation_m、data_quality、status、quality_flags_json、evidence_bundle_json、config_version、revision |
| `attribution_items` | code、label、amount、description、evidence_json、impact_class |
| `wait_events` ★ | location(路口名)、t_start/t_end、duration_s、avg_speed_kmh、peak_seconds、offpeak_seconds、peak_label、fee、**lat/lng**、evidence_json |
| `detour_events` ★ | entry/exit_name、entry/exit_lat/lng、ref/actual/extra_distance_m、fee、**path_json（偏离段轨迹）**、evidence_json |
| `transparent_bills` | passenger_view_json、driver_view_json（含 map 数据） |
| `disputes` / `dispute_evidence` | item_code、initiator_role、status、evidence_snapshot_json（冻结）、绑定行 |

## 4. 接口设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health`、`/api/config` | 健康；沈阳计费基准+阈值只读 |
| POST | `/api/orders` | 创建（快照+参考路径+节点名；档位自动判定） |
| POST | `/api/orders/{no}/complete` | 完单（账单+轨迹→归因→事件+双视角账单） |
| POST | `/api/orders/{no}/attribute` | 重算（幂等 revision+1） |
| GET | `/api/orders`、`/api/orders/{no}`、`/api/orders/{no}/attribution` | 列表/详情/原始归因 |
| GET | `/api/orders/{no}/bill?view=…` | 双视角账单（含 map：actual_path/reference_path/wait_events/detour_segments 坐标） |
| GET | `/api/orders/{no}/events` | **等候+绕路事件列表（含坐标）** |
| POST/GET/PATCH | `/api/disputes…` | 提交/查询/复核 |
| POST | `/api/demo/reset` | 复位重灌 7 笔种子 |

统一错误：`{"error":{"code","message","details"}}`。curl 示例见 README。

## 5. 用户界面 / 截图说明

**页面 1 订单列表 `/orders`**：表格列含预估价、实付、差额、残差、拟合度、数据质量、等候/绕路事件数；演示操作面板指向验收样例 FT-SY-1800。

**页面 2 透明账单 `/orders/FT-SY-1800/bill`**：
- Leaflet + OSM 沈阳地图（中心 41.80,123.43，缩放 12）：蓝线=实际轨迹、灰虚线=参考路径、红色高亮=绕路偏离段（弹窗：参考/实际/多出米数与费用）、橙色圆点=等候路口（弹窗：路口名、时间、秒数、均速、高峰标签、费用）；离线时显示降级提示
- 乘客/司机视角标签页：乘客=四卡片（预估/实付/总差额/残差）+叙述；司机=责任分组（驾驶行为/路况/沈阳固定规则/待核查）
- 归因项明细表：每行可展开证据 JSON（含等候事件逐条坐标、绕路逐条米数）
- 金额守恒校验面板与口径行（config_version/algorithm_version/revision/fit_score）
- 降级横幅（轨迹缺失）与待核查横幅（残差>0.01）

**页面 3 争议提交 `/disputes/new`**：订单→归因项级联下拉（显示金额）、角色、原因；提交后回跳账单页显示"证据快照已冻结"；下方最近争议列表。

截图待人工截取至 `docs/screenshots/`（自动化不引入，见 ASSUMPTIONS A5）。

## 6. 示例用例描述（摘要，完整见 docs/use-cases.md）

- **UC-1 乘客查看差额构成**：FT-SY-1800 账单页 → headline"多付 9.00 元" → 展开等候费证据（青年大街—市府大路 180 秒晚高峰 1.50 元）→ 地图点击橙色圆点核对 → 守恒面板通过。
- **UC-2 完单自动归因**：POST complete → 预处理→拟合→三维分解→事件落库→201 返回。
- **UC-3 司机对绕路归因发起争议**：提交即冻结快照；重算后 revision 变化但快照不变；运营 OPEN→REVIEWING→RESOLVED；非法流转 409。
- **UC-4 轨迹缺失降级**：SY-1005 → INCOMPLETE 横幅 + 残差 1.50 待核查。
- **UC-5 监管只读审计**：GET /api/config 核对沈阳基准字段与归因记录口径版本。

## 7. 关键设计决策

| 决策 | 理由 |
|---|---|
| 沈阳基准实现为纯函数（shenyang.py）+ yaml 字段 | "固定基准不可运行时修改"；口径可追溯、可单测 |
| 档位按上车/结束时间自动判定 | 调用方无需理解档位；跨 22:00 订单自然产生换档差价归因 |
| 等候/绕路独立事件表（含坐标） | 每条事件可在地图定位，是"路口级/路段级举证"的落库形态 |
| 绕行中点牛顿迭代 | 使种子绕行米数精确（±1.5m），保证验收金额可复现 |
| 绕行费用连续费率（380m→0.76 元） | 与需求举证示例一致；500m 取整仅用于计费器口径 |
| 残差兜底 + >0.01 待核查 | 严禁把取整/未知差额塞进堵车或绕路项 |
| 争议快照 JSON 冻结 | 复核所见=发起所见，重算不影响 |
| Leaflet + OSM 免费瓦片 | 无付费 API Key；离线时矢量图层仍可展示 |
