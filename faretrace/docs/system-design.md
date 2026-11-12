# 系统设计 — FareTrace 网约车透明账单归因系统

> 对应实验手册第一部分第 2 项（系统设计 10%）：主要功能/模块、数据库设计、用户界面/截图、示例用例描述。
> 口径来源：`config/attribution.yaml`（`cfg-1.0.0`）；单位约定：金额=元（2 位小数）、距离=米、时间=秒。

## 1. 总体架构与功能模块

```
┌────────────────────────────  客户端  ────────────────────────────┐
│  乘客 / 司机（浏览器三页面）      运营仲裁人员（页面+API）          │
└───────────────┬──────────────────────────────┬──────────────────┘
                │ HTML (Jinja2)                │ JSON (REST)
┌───────────────▼──────────────────────────────▼──────────────────┐
│ FastAPI app/main.py（统一错误结构 / lifespan 建表+种子）           │
│  ├ routers/orders.py   订单接入/完单/重算/列表/详情/原始归因       │
│  ├ routers/bills.py    透明账单双视角（passenger|driver|both）    │
│  ├ routers/disputes.py 争议提交/查询/状态流转                     │
│  ├ routers/meta.py     health / 只读配置 / demo reset            │
│  └ routers/pages.py    三页面 + 表单提交                         │
├──────────────────────────  服务层  ──────────────────────────────┤
│  ingest.py（事务编排）  disputes.py（快照冻结/状态机）             │
│  ┌────────────────────── 领域服务 ───────────────────────────┐   │
│  │ geo.py        Haversine + shapely 投影/插值/累计里程        │   │
│  │ trajectory.py 去重/排序/剔异常/拟合度/拥堵段/偏离段/质量标记 │   │
│  │ pricing.py    Decimal 计价求值（起步+里程+时长+倍率+杂费）   │   │
│  │ attribution.py★ 规则化三维偏差分解 → 六归因项 + 残差 + 证据   │   │
│  │ bill_view.py  同一归因 → 乘客/司机双视角投影                 │   │
│  └───────────────────────────────────────────────────────────┘   │
├──────────────────────────  数据层  ──────────────────────────────┤
│ SQLAlchemy 2.0 (11 表) ⇄ SQLite 单文件；配置 config/attribution.yaml │
└──────────────────────────────────────────────────────────────────┘
```

模块职责边界：`routers` 只做解析/校验/序列化；`ingest` 负责事务编排；`attribution` 纯计算不碰库（可独立单元测试）；所有阈值来自 `config.py` 读取的 yaml，代码零魔法数字。

## 2. 核心算法设计（规则化三维偏差分解）

### 2.1 三维偏差定义

- 距离偏差 `ΔD = actual_distance_m − estimated_distance_m`
- 时间偏差 `ΔT = actual_duration_s − estimated_duration_s`
- 价格差异 `ΔP`：**不做抽象系数估算**，直接逐字段对比快照与账单的起步价、里程单价、时长单价、加价系数、杂费、优惠与规则版本

### 2.2 轨迹预处理与拟合度

去重（同时刻且 ≤1m）→ 按时间排序（留痕 `TRAJECTORY_UNSORTED_FIXED`）→ 剔异常（隐含速度 > 55.6 m/s）→ 逐段算距离/速度 → 投影到参考折线得平均偏离 → `fit_score = max(0, 1 − 平均偏离 / FIT_BUFFER_M)`（`FIT_BUFFER_M=100`）。有效点 < 3 → `INCOMPLETE` 降级。

### 2.3 归因项与计算式

| # | 归因项 | 触发条件 | 计算式（元） | 证据要点 |
|---|---|---|---|---|
| 1 | PATH_DEVIATION | `fit_score < 0.8` | `max(0, ΔD)/1000 × 账单里程单价`；高拟合度时 = 0 但保留 fit_score 与平均偏离证据 | 偏离段时间区间、参考路径投影位置、阈值、ΔD |
| 2 | CONGESTION_DELAY | 轨迹可用 | `拥堵秒数/60 × 账单时长单价`；拥堵 = 速度 < 15 km/h 且连续 ≥ 60 s | 低速段时间区间、均速、沿路位置 |
| 3 | DRIVER_DELAY | `ΔT − 拥堵秒数 > 0` | `max(0, ΔT − 拥堵秒数)/60 × 账单时长单价`；剩余为负不产生正金额（保留说明） | ΔT、拥堵秒数、剩余正秒数 |
| 4 | PRICING_RULE_CHANGE | 总是输出 | `Δ起步价 + Δ里程单价×实际km + Δ时长单价×实际min + Δ加价系数×计费基数` | 规则版本对、逐字段对比表 |
| 5 | TOLL_OR_FEE | 总是输出 | `(账单杂费+其他−优惠) − (快照杂费−优惠)` | 逐条杂费/优惠 diff |
| 6 | UNEXPLAINED_RESIDUAL | 总是输出 | `total_delta − 前五项之和`；`|残差|≤0.01` 归零并注明 | 重算差额来源分解（快照差/账单未列明调整/其它） |

守恒由构造保证：残差兜底定义，测试断言 `Σ六项 = total_delta`（±0.01 元）。残差永不并入其它项；`|残差| > 1.00` 元 → 状态 `NEEDS_REVIEW`。

### 2.4 数据质量标记

`TRAJECTORY_MISSING / UNSORTED_FIXED / DUPLICATES_REMOVED / OUTLIERS_REMOVED / GAP`、`REFERENCE_ROUTE_MISSING / DEGRADED`、`CONGESTION_UNVERIFIED`、`PATH_FIT_UNAVAILABLE`、`RESIDUAL_ABOVE_REVIEW_THRESHOLD`。轨迹缺失时：拥堵记 0（降级）、正时间偏差全部计入司机/其他延迟，页面显示"本次账单基于不完整轨迹生成"。

## 3. 数据库设计（SQLite，11 张表）

```
pricing_rules 1─┬─* estimate_snapshots *─1┐
                ├─* bills *───────────────1┤
orders 1────────┼─1 reference_routes        │
                ├─* trajectory_points       │
                └─1 attributions 1─┬─* attribution_items
                                   ├─1 transparent_bills
                                   └─* disputes 1─* dispute_evidence *─1 attribution_items
```

| 表 | 关键字段 | 说明 |
|---|---|---|
| `pricing_rules` | `version`(PK), `base_fare`, `per_km`, `per_min`, `min_fare`, `surge_multiplier` | 计价规则版本（预估/实付分别引用，支撑规则变化举证） |
| `orders` | `order_no`(uniq), `city`, `status`(CREATED/COMPLETED), `requested_at`, `completed_at`, 起终点标签 | 订单主表 |
| `estimate_snapshots` | `quoted_price`, `est_distance_m`, `est_duration_s`, `rule_version`, `surcharges_json`, `discounts_json` | 下单时冻结的预估价快照 |
| `bills` | `paid_amount`, `actual_distance_m`, `actual_duration_s`, `base_fare`, `distance_fare`, `time_fare`, `other_fees_json`, `rule_version` | 实付账单（分项+汇总） |
| `reference_routes` | `polyline_json`([[lat,lng]…]), `length_m`, `est_duration_s`, `free_flow_speed_kmh` | 参考路径折线 |
| `trajectory_points` | `seq`, `ts`, `lat`, `lng`, `speed_mps`（索引 `(order_id, seq)`） | 实际轨迹，允许为空 |
| `attributions` | `total_delta`, `explained_amount`, `residual`, `fit_score`, `avg_deviation_m`, `data_quality`, `status`(OK/DEGRADED/NEEDS_REVIEW), `quality_flags_json`, `evidence_bundle_json`, `algorithm_version`, `config_version`, `revision` | 归因记录（订单唯一，重算 revision+1） |
| `attribution_items` | `code`(uniq per attribution), `label`, `amount`, `description`, `evidence_json`, `impact_class`, `sort_order` | 六个归因项（含残差项） |
| `transparent_bills` | `passenger_view_json`, `driver_view_json` | 双视角成品账单 |
| `disputes` | `item_code`, `initiator_role`, `reason`, `status`, `evidence_snapshot_json`(冻结), `snapshot_locked`, `resolution` | 争议记录 |
| `dispute_evidence` | `dispute_id`, `attribution_item_id`, `evidence_json`, `bound_at` | 争议↔归因项证据绑定 |

金额列统一 `Numeric(10,2)`（元）；业务层 `Decimal + ROUND_HALF_UP` 量化，JSON 列内的金额以字符串存储避免序列化歧义。

## 4. 接口设计

| 方法 | 路径 | 用途 | 成功码 |
|---|---|---|---|
| GET | `/api/health` | 健康检查（含 config_version） | 200 |
| GET | `/api/config` | 只读阈值/规则版本/归因项编码 | 200 |
| POST | `/api/orders` | 创建订单（快照+参考路径） | 201 |
| POST | `/api/orders/{no}/complete` | 完单（账单+轨迹→归因→账单） | 201 |
| POST | `/api/orders/{no}/attribute` | 重算归因（幂等，revision+1） | 200 |
| GET | `/api/orders`、`/api/orders/{no}` | 列表 / 原始数据 | 200 |
| GET | `/api/orders/{no}/bill?view=passenger\|driver\|both` | 双视角透明账单 | 200 |
| GET | `/api/orders/{no}/attribution` | 原始归因记录+证据 bundle | 200 |
| POST/GET/PATCH | `/api/disputes…` | 提交（冻结快照）/查询/复核流转 | 201/200 |
| POST | `/api/demo/reset` | 演示复位重灌种子 | 200 |

统一错误结构：`{"error": {"code", "message", "details"}}`；校验失败 422、状态冲突 409、不存在 404。curl 示例见 `README.md`。

## 5. 用户界面 / 截图说明

三个页面（截图位置：`docs/screenshots/`，演示时用 `uvicorn` 启动后截取；以下为页面结构与关键元素说明）：

**页面 1 订单列表 `/orders`**：表格列——订单号（链接）、起点→终点、预估价、实付、差额（正红/负绿）、残差（非零橙色警示）、拟合度、数据质量、状态；底部演示操作面板（重置种子命令、验收样例 FT-1800 入口）。

**页面 2 透明账单详情 `/orders/FT-1800/bill`**：
- 顶部降级横幅（仅 `INCOMPLETE`/`NEEDS_REVIEW` 时出现："本次账单基于不完整轨迹生成…"）
- 「乘客视角 / 司机视角」标签页切换：乘客视角 = 预估价/实付/总差额/残差四卡片 + 叙述（"差额主要来自：路径偏离 +3.10 元，拥堵延迟 +4.20 元，计价规则变化 +1.70 元"）；司机视角 = 四个责任分组卡片（驾驶行为相关/路况/平台规则/无法归因）+ 免责声明
- 归因项明细表：金额、业务说明、责任归属、**可展开的证据 JSON**（时间段、轨迹片段、阈值、字段对比）、每行"发起争议"链接
- 金额守恒校验面板：`归因项之和（含残差）9.00 元 | 总差额 9.00 元 | 通过（±0.01 元内）`；口径行显示 config_version / algorithm_version / revision / fit_score / 平均偏离
- 原始归因记录：可展开完整 evidence bundle（审计用）

**页面 3 争议提交 `/disputes/new`**：订单下拉（级联过滤归因项）、归因项下拉（显示金额）、发起角色（乘客/司机）、原因文本域；提交后 303 回跳账单页并显示"已提交争议 #N，证据快照已冻结"；下方最近争议列表（状态、原因、时间）。

## 6. 示例用例描述

### 用例 UC-1 乘客查看差额构成（主流程）

| 项 | 内容 |
|---|---|
| 参与者 | 乘客 |
| 前置 | 订单 FT-1800 已完成并归因 |
| 主流 | 1. 打开 `/orders` → 2. 点击 FT-1800 → 3. 默认乘客视角查看 headline"预估价 18.00 元，实付 27.00 元，多付 9.00 元" → 4. 展开拥堵延迟证据（时间区间、均速、阈值）→ 5. 切换司机视角核对责任分组 |
| 后置 | 无状态变更 |
| 验证 | 页面显示守恒校验通过；测试 `test_bill_page_renders_both_views_and_evidence` |

### 用例 UC-2 完成订单并自动归因（系统流程）

| 项 | 内容 |
|---|---|
| 参与者 | 平台订单系统（外部触发）/ 后端 |
| 前置 | 订单已创建（有快照与参考路径），规则版本存在 |
| 主流 | 1. `POST /api/orders/{no}/complete` 携带账单+轨迹 → 2. ingest 写库 → 3. 归因（预处理→拟合→三维分解→残差）→ 4. 落库归因+六归因项+双视角账单 → 5. 返回 201 |
| 扩展 | 轨迹为空：归因降级 `DEGRADED`，拥堵记 0，页面出现降级横幅；规则缺失：422 统一错误 |
| 验证 | `test_e2e_create_complete_bill_dispute`、`test_missing_trajectory_degrades_and_records_residual` |

### 用例 UC-3 司机对拥堵归因发起争议（异常/争议流程）

| 项 | 内容 |
|---|---|
| 参与者 | 司机、运营仲裁人员 |
| 前置 | 账单已生成 |
| 主流 | 1. 司机在账单页点"发起争议"→ 2. 提交表单（CONGESTION_DELAY / 司机 / 原因）→ 3. 系统冻结整份归因+原始账单+轨迹摘要为 `evidence_snapshot_json`，绑定 `dispute_evidence` → 4. 运营 PATCH `REVIEWING` → 5. 依据快照证据 PATCH `RESOLVED` + 处理说明 |
| 扩展 | 非法状态流转（RESOLVED→OPEN）返回 409 `INVALID_STATE_TRANSITION`；归因项不存在返回 422 |
| 关键性质 | 步骤 4–5 之间即使订单被重算（revision+1），争议快照金额与内容不变 |
| 验证 | `test_dispute_flow_binds_evidence_and_locks_snapshot` |

### 用例 UC-4 轨迹缺失降级（异常流程）

订单 FT-1005：无轨迹 → 归因状态 `DEGRADED`、质量标记 `TRAJECTORY_MISSING`、拥堵 0.00、正时长 360 s 全部计入司机延迟 2.10 元、账单存在 0.20 元未列明调整 → 残差 0.20 元显式展示；乘客页顶部横幅"本次账单基于不完整轨迹生成"。验证：`test_bill_page_warns_on_incomplete_trajectory`。

## 7. 设计决策与替代方案

| 决策 | 备选 | 选择理由 |
|---|---|---|
| SQLite + 同步 SQLAlchemy | MySQL/Postgres + 异步 | 课程演示零部署；无并发需求 |
| shapely 平面投影 + Haversine 复核 | PostGIS / 路网地图 API | 离线可用；城市尺度误差 < 0.5% |
| 残差兜底式守恒 | 平均分摊 / 强制凑平 | 分摊会掩盖证据缺口；残差显式保留符合"可审计"核心诉求 |
| 快照 JSON 冻结争议证据 | 争议引用实时归因 | 保证"复核时看到的与发起时一致"，即使订单被重算 |
| 双视角为同一归因的投影 | 乘客/司机各算一份 | 保证双方看到的数字一致，只差叙述方式——"双向透明"的实现基础 |
