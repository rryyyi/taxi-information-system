# FareTrace 网约车透明账单归因系统 — 实施计划（v1）

> **状态（2026-03）**：已按课程补充要求（《技术实现补充要求》）实现并全部测试通过。
> 与初版计划的差异：金额单位由"分"改为**元（2 位小数，Decimal 量化）**；几何投影改用 **shapely**；
> 表由 9 张扩为 **11 张**（新增 `transparent_bills`、`dispute_evidence`）；归因口径改为
> **fit_score 门控的路径偏离**（`FIT_BUFFER_M=100`、`FIT_THRESHOLD=0.8`）+ 连续低速拥堵（15 km/h / 60 s）。
> 以代码与 `README.md`、`ASSUMPTIONS.md` 为准。
>
> 本文件是本项目的**唯一开发契约**。若需求不明确，按本计划与 `ASSUMPTIONS.md` 中的假设执行，不阻塞开发。
> 开发按阶段推进，每阶段以「测试全绿」为进入下一阶段的唯一放行条件。

---

## 0. 环境勘察结论（已实测）

| 项目 | 实测结果 | 结论 |
|---|---|---|
| Python | 3.14.4（`AppData\Local\Programs\Python\Python314`） | 可用 |
| FastAPI / Starlette | 0.133.1 / 1.6.0 | 已装，直接用 |
| SQLAlchemy | 2.0.50 | 已装（同步 ORM，不引入 Alembic） |
| Pydantic / pydantic-settings | 2.13.4 / 2.14.2 | 已装 |
| pytest / httpx | 9.0.2 / 0.28.1 | 已装，`TestClient` 走 httpx |
| Jinja2 / PyYAML | 3.1.6 / 6.0.3 | 已装，模板 + 可配置规则 |
| Node | v24.14.1 | **不使用**（前端不做工程化） |
| Playwright | 未安装 | 截图改为「启动后人工截屏 + 文档中的页面结构说明」，不引入新依赖 |

**结论：零新依赖（除可选 `requirements.txt` 固化版本），全部功能可在当前离线环境跑通。**

### 0.1 实验手册（MIS Lab Handbook 2026）对齐

已解析 `商务数据分析/7b165d89-8391-47c8-9597-f3954b2db38b(1).pdf`（扫描件、无文本层），逐页转录见 `docs/reference/handbook-notes.md`，页图 `docs/reference/handbook-p1..p7.png`。手册要求 → 本项目交付物映射：

| 手册要求（分数） | 本项目交付物 |
|---|---|
| 需求与可行性分析 10%：现有电子商务/业务流程 + 关键问题 + 信息系统方案 + 市场/用户群体/利益相关者 | `docs/requirements-feasibility.md`（**TELOS 五维**：技术/经济/法律/运营/进度 + 市场与资源/文化可行性 + 利益相关者矩阵 + 问题陈述） |
| 系统设计 10%：主要功能/模块、数据库设计、用户界面/截图、示例用例描述 | `docs/system-design.md`（模块图、9 表 ER 说明、接口表、三页面截图与说明、用例图/描述） |
| 正式引用格式（哈佛）+ 报告末尾团队角色表 | `docs/references.md`（哈佛占位 `(Author, Year)` + 参考文献表）、`docs/team-roles.md`（末尾角色表） |
| 展示 10%（20 分钟含 5 分钟问答；角色扮演 PM/CIO/CEO/CFO/CSIRO）| `docs/presentation-script.md`（15 分钟讲解脚本 + 5 分钟 Q&A 预答 + 角色扮演分工 + 术语表） |
| 截止第 13 周（周三），迟交每天扣 10% | README 顶部标注交付时间线；阶段 5 全量验收 |

**定位话术（写进报告开篇）**：本项目是一份「**面向网约车/出行科技平台账单流程的、基于信息系统的流程改进提案**」，并以「小组正在申请**平台数据分析/系统架构/运营优化**岗位实习」的商务情境撰写，对应手册要求的“希望近期申请工作”的电子商务/业务流程对象。

**展示形态**：20 分钟商务会议式角色扮演（CEO 提需求 → CIO 讲系统架构 → 数据/算法负责人讲归因算法 → CFO 讲经济可行性 → 项目经理收口与 Q&A），由 `docs/presentation-script.md` 固定台词与时间轴。

---

## 1. 技术选型（轻量、单机、课程演示友好）

- **后端**：FastAPI + Uvicorn（同步 SQLAlchemy Session，单进程）
- **数据库**：SQLite（`faretrace.db`，启动自动建表 + 自动注入种子数据）
- **ORM**：SQLAlchemy 2.0 Declarative；**不使用** Alembic（课程项目无迁移需求，模型变更用重建库兜底）
- **规则/阈值**：`config/attribution.yaml`（含 `config_version`），启动加载；阈值、拆分比例、路径偏离容差、舍入容差全部可配置、可追溯
- **算法**：纯 Python + `math`（自实现 Haversine、折线投影、分段速度），**不引入** GIS/地图库、不调用外部 API
- **前端**：Jinja2 模板 + 单文件 CSS + 少量原生 JS；三页面；无登录、无打包工具
- **测试**：pytest（单元 + 集成 + 端到端 API），`TestClient` 驱动
- **文档**：Markdown 全量交付

**明确不做**：真实支付、真实地图导航/路网路径规划、账号体系与权限、复杂图表库、微服务拆分、容器化。

---

## 2. 项目结构（目标文件树）

```
faretrace/
├── README.md                     # 启动命令 / 演示路径 / 验收清单
├── ASSUMPTIONS.md                # 全部技术假设与模拟数据声明
├── requirements.txt
├── pytest.ini
├── config/
│   └── attribution.yaml          # 规则版本、阈值、费率回退、拆分比例
├── app/
│   ├── __init__.py
│   ├── main.py                   # FastAPI 装配、路由注册、启动钩子（建表+种子）
│   ├── config.py                 # yaml 加载 + 环境变量（DB 路径、配置路径）
│   ├── db.py                     # engine / SessionLocal / get_session / reset_db
│   ├── models.py                 # 全部 ORM 表
│   ├── schemas.py                # Pydantic 入参/出参（统一金额用 Decimal→float 分位）
│   ├── enums.py                  # 归因项编码、争议状态、数据质量标记
│   ├── routers/
│   │   ├── orders.py             # 接入 / 列表 / 详情 / 触发归因
│   │   ├── bills.py              # 透明账单（passenger|driver 双视角）
│   │   ├── disputes.py           # 发起 / 查询 / 复核状态流转
│   │   ├── meta.py               # health / config / seed 重置
│   │   └── pages.py              # 三个前端页面
│   ├── services/
│   │   ├── geo.py                # haversine / 折线投影 / 沿路线进度 / 偏离段检测
│   │   ├── trajectory.py         # 轨迹预处理 + 质量标记 + 距离/时长/停驶统计
│   │   ├── pricing.py            # 计价规则求值（含最低消费、倍率、杂费、优惠）
│   │   ├── attribution.py        # ★核心：四步望远镜式差额分解 + 证据生成 + 残差
│   │   ├── bill_view.py          # 同一归因结果的乘客视角 / 司机视角投影
│   │   ├── disputes.py           # 争议快照冻结与状态机
│   │   └── seed.py               # 五类种子订单（含 18→27 验收样本）
│   ├── templates/
│   │   ├── base.html
│   │   ├── orders.html           # 订单列表
│   │   ├── bill.html             # 透明账单详情（双视角切换、可展开证据）
│   │   └── dispute_form.html     # 争议提交
│   └── static/style.css
├── scripts/
│   └── run_demo.py               # 一条命令跑完 5 笔订单并打印账单文本
├── tests/
│   ├── conftest.py               # 临时 SQLite + TestClient fixture
│   ├── test_geo.py
│   ├── test_trajectory.py
│   ├── test_pricing.py
│   ├── test_attribution_core.py  # 金额守恒（参数化 + 随机扰动）
│   ├── test_attribution_scenarios.py  # 5 类场景 + 18→27 精确验收
│   ├── test_api_e2e.py           # 接入→归因→账单→争议→复核
│   └── test_frontend_smoke.py    # 三页面 200 + 关键文案存在
└── docs/
    ├── requirements-feasibility.md   # TELOS 五维 + 市场/用户群体 + 利益相关者 + 问题陈述
    ├── system-design.md              # 模块 / 库表 / 接口 / UI 截图说明 / 用例
    ├── use-cases.md                  # 示例用例（主流程/异常/争议）
    ├── team-roles.md                 # 六人角色表（PM/CIO/后端/数据算法/前端/文档）
    ├── presentation-script.md        # 20 分钟展示脚本 + 5 分钟 Q&A + 角色扮演台词
    ├── references.md                 # 哈佛引用占位与参考文献表
    ├── acceptance-checklist.md       # 验收清单（含守恒证明）
    ├── reference/                    # 实验手册转录与页图（原始依据）
    └── PLAN.md                       # 本文件
```

存放位置：`商务数据分析/faretrace/`（与已有 PDF 并存，不动原文件）。

---

## 3. 数据库设计（SQLite，11 张表；金额列 `Numeric(10,2)` 单位元）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `pricing_rules` | `version`(PK str), `base_fare`, `per_km`, `per_min`, `min_fare`, `surcharges_json`, `effective_from` | 计价规则版本快照，用于「规则变化」归因与追溯 |
| `orders` | `id`, `order_no`(uniq), `city`, `requested_at`, `completed_at`, `status` | 订单主表 |
| `estimate_snapshots` | `order_id`, `quoted_price`, `est_distance_m`, `est_duration_s`, `rule_version`, `surge_multiplier`, `surge_json` | **下单时冻结的预估价快照**（不可变） |
| `bills` | `order_id`, `paid_amount`, `actual_distance_m`, `actual_duration_s`, `base_fare`, `distance_fare`, `time_fare`, `surcharges_json`, `discounts_json`, `rule_version` | 实付账单（分项 + 汇总） |
| `reference_routes` | `order_id`, `polyline_json`, `length_m`, `est_duration_s`, `free_flow_speed_mps` | 参考路径（折线点集） |
| `trajectory_points` | `order_id`, `seq`, `ts`, `lat`, `lng`, `speed_mps` | 实际轨迹点（可为空 → 降级） |
| `attributions` | `order_id`, `total_delta`, `residual`, `explained_sum`, `algorithm_version`, `config_version`, `quality_flags_json`, `status`, `created_at`, `evidence_bundle_json` | 一次归因运行（幂等：同订单重复触发则覆盖重算） |
| `attribution_items` | `attribution_id`, `code`, `name`, `amount`, `amount_kind`(distance/time/rule/surcharge), `evidence_json`, `data_quality`, `sort_order` | 归因项（每项必有证据摘要） |
| `transparent_bills` | `attribution_id`, `passenger_view_json`, `driver_view_json` | 双视角成品账单 |
| `dispute_evidence` | `dispute_id`, `attribution_item_id`, `evidence_json`, `bound_at` | 争议证据绑定 |
| `disputes` | `id`, `order_no`, `attribution_id`, `attribution_item_id`, `initiator_role`, `reason`, `status`, `snapshot_json`, `created_at`, `reviewed_by`, `reviewed_at`, `resolution` | **争议发起瞬间冻结账单快照**（跨表冗余 JSON，保证可回溯） |

设计要点：
1. 金额统一用 `Numeric(10,2)`/float 分位计算，**比较容差 0.01 元**，写入数据库按 2 位小数舍入。
2. 每个归因项 `evidence_json` 结构固定（`kind` + 明细），前端可展开、仲裁可复核。
3. `residual` 独立字段 + 独立归因项（`code=RESIDUAL`），**不可静默丢弃**；`|residual| > 阈值` 时 `status=DEGRADED` 并在账单页高亮。
4. 争议快照 `snapshot_json` = 当次归因 + 全部归因项 + 订单/账单/估价原始数据，后续任何重算都不影响已冻结的争议证据。

---

## 4. ★核心归因算法：四步望远镜式分解（金额守恒由构造保证）

### 4.1 定义
- `E0 = 预估价 (quoted_price)`，`P = 实付 (paid_amount)`，`D = P - E0`
- 计价函数 `fare(km, min, rule, surcharges, discounts)`：`max(min_fare, base + per_km*km + per_min*min) * surge + Σ杂费 - Σ优惠`

### 4.2 分解链（每一步只改一个自变量，差额可加）
| 步 | 变更 | 差额公式 | 归因桶 |
|---|---|---|---|
| ① 距离效应 | `est_km → act_km`（价格规则仍用**预估价规则**） | `Δ1 = (act_km - est_km) * per_km_est` | 路径偏离 / 残差 |
| ② 时间效应 | `est_min → act_min`（仍用**预估价规则**） | `Δ2 = (act_min - est_min) * per_min_est` | 拥堵延迟 / 司机及其他延迟 / 残差 |
| ③ 规则效应 | 规则切换为**实付规则**（里程、时长保持实际值） | `Δ3 = fare(act, 实付规则) - fare(act, 预估规则)`（含最低消费、倍率差异） | 计价规则变化 |
| ④ 杂费/优惠效应 | 杂费、优惠切换为实付值 | `Δ4 = Δ杂费 - Δ优惠`（逐条列明细） | 计价规则/杂费变化 |

`Δ1 + Δ2 + Δ3 + Δ4 = D`（代数恒等，误差仅来自分位舍入，容差 0.01 元）。

### 4.3 桶内拆分（依据证据，而非平均分摊）
- **① 距离桶**：用轨迹对参考路径做投影，识别「偏离参考路径超过 `deviation_tolerance_m`（默认 50m）」的连续段，计算偏离段实际长度与对应参考段长度之差 = `detour_m`。
  - `路径偏离 = detour_km * per_km_est`
  - 剩余距离差额（如账单里程 > 轨迹里程、投影误差）→ **残差**，并写入 `unexplained_distance_m` 证据。
  - 轨迹缺失/过稀 → 距离桶整体标记 `degraded`，证据写「基于账单里程差估计，无法逐段举证」。
- **② 时间桶**：逐段比较实际速度与参考路径自由流速度/预估速度，
  - 在参考路径上、且速度低于 `congestion_speed_ratio * free_flow` 的时长 → **拥堵延迟**（附时间区间 + 平均速度）
  - 偏离段之外的非行驶停驶（停车/低速等待超 `stopped_tolerance_s`）→ **司机及其他延迟**
  - 两类合计 ≠ Δ2 时，差额按「实测时长与轨迹计时长不一致」进入**残差**（`unexplained_time_s`）
  - 轨迹缺失 → 按 `config.degraded_time_split`（默认 congestion 0.7 / driver 0.3）分摊并标记 `degraded`（该比例写入 ASSUMPTIONS）
- **③④ 规则/杂费桶**：天然可精确举证（费率表 diff、杂费逐条 diff、最低消费触发、倍率变化），几乎不留残差。
- **残差** `= D - Σ(已解释项)`，独立成项；`abs(residual) <= rounding_tolerance` 记 `ROUNDING`，否则记 `UNEXPLAINED` 并在 `quality_flags` 中告警。

### 4.4 数据质量标记（枚举，写入 `attributions.quality_flags_json`）
`TRAJECTORY_COMPLETE` / `TRAJECTORY_MISSING` / `TRAJECTORY_PARTIAL` / `TRAJECTORY_UNSORTED_FIXED` / `REFERENCE_ROUTE_MISSING` / `REFERENCE_ROUTE_DEGRADED` / `RESIDUAL_ABOVE_TOLERANCE` / `DISTANCE_FROM_BILL_FALLBACK` / `DURATION_FROM_BILL_FALLBACK`

任何降级都必须：① 有 flag ② 有额外 `evidence.note` ③ 账单页显式提示「本次账单基于不完整轨迹生成」。

### 4.5 验证性质（写进测试）
1. **守恒**：`|Σ items + residual - total_delta| <= 0.01`（每笔订单、含随机扰动订单 200 组）。
2. **无隐藏**：`residual` 永远存在（可为 0.00），且必为 `attribution_items` 中的一行。
3. **确定性**：同一输入 + 同一 `config_version` 两次归因结果完全一致。
4. **幂等**：重复触发只产生一份最新归因，不重复记账。
5. **降级不崩**：轨迹 `[]`、乱序、重复点、时间倒流、单点、无参考路径 6 种畸形输入全部返回可用账单。

---

## 5. 种子数据（5 类订单，全部合成数据）

| 场景 | 订单号 | 预估价→实付 | 期望归因（≈） | 数据质量 |
|---|---|---|---|---|
| S1 正常 | FT-1001 | 21.00 → 20.60 | 首单优惠 −0.40，其余项 0.00 | COMPLETE |
| S2 拥堵延迟 | FT-1002 | 20.00 → 23.50 | 拥堵 600 秒 → +3.50，司机延迟 0 | COMPLETE |
| S3 路径偏离 | FT-1003 | 19.50 → 25.40 | 路径 +3.60、拥堵 +1.75、过路费 +0.55 | COMPLETE |
| S4 计价规则变化 | FT-1004 | 24.80 → 28.60 | 起步价 +2.00、里程单价 +0.30×6km | COMPLETE |
| S5 残差 + 轨迹缺失 | FT-1005 | 21.00 → 23.30 | 司机延迟 +2.10 + 残差 0.20（INCOMPLETE） | MISSING |
| **★验收样本** | **FT-1800** | **18.00 → 27.00（差 9.00）** | **路径偏离 3.10 + 拥堵延迟 4.20 + 规则变化 1.70 + 残差 0.00** | COMPLETE |

`FT-1800` 通过反向设计合成数据（费率、里程、时长、轨迹分段速度）使 Δ1=3.10、Δ2=4.20、Δ3=1.70 精确命中，作为生活化验收用例。

---

## 6. 接口（REST + 三页面）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/orders` | 一次性接入订单 + 估价快照 + 账单 + 参考路径 + 轨迹，返回 `order_no` |
| GET | `/api/orders` | 订单列表（含归因状态、差额摘要） |
| GET | `/api/orders/{order_no}` | 订单原始数据（含数据质量标记） |
| POST | `/api/orders/{order_no}/attribute` | 触发归因，返回归因汇总；`?force=true` 重算 |
| GET | `/api/orders/{order_no}/bill?role=passenger|driver` | **透明账单双视角**（同一归因，不同叙述与高亮） |
| GET | `/api/attributions/{id}` | 归因明细 + 证据 bundle |
| POST | `/api/disputes` | `{order_no, item_code, initiator_role, reason}` → 冻结快照 |
| GET | `/api/disputes` `/api/disputes/{id}` | 争议列表 / 详情（含快照） |
| PATCH | `/api/disputes/{id}` | 仲裁流转 `open→reviewing→resolved/rejected` + `resolution` |
| GET | `/api/config` | 当前 `config_version` + 阈值（可追溯） |
| GET | `/api/health` | 健康检查 |
| POST | `/api/demo/reset` | 重置库并重灌种子（仅演示） |
| GET | `/`, `/orders`, `/orders/{order_no}/bill`, `/disputes/new` | 三个前端页面 |

**双视角差异（bill_view 规则）**：归因项增加 `driver_relevance`（`driver_related` / `road_condition` / `platform_rule` / `mixed`）。乘客页显示「为什么多付」+ 现金额构成；司机页显示「本次扣款与你的驾驶行为相关的金额小计 vs 路况/规则相关」，并把 `driver_related` 项高亮。

---

## 7. 测试矩阵（阶段 2/3 的放行门槛）

| 文件 | 覆盖 | 关键断言 |
|---|---|---|
| `test_geo.py` | haversine / 投影 / 偏离段 | 已知距离误差 <1%；投影落在折线段上 |
| `test_trajectory.py` | 预处理 6 类畸形输入 | 不抛异常、标记正确、距离单调可信 |
| `test_pricing.py` | 最低消费、倍率、杂费、优惠 | 与手算值一致 |
| `test_attribution_core.py` | 4 步分解 | 守恒；单变量变化只影响对应桶；确定性；幂等 |
| `test_attribution_scenarios.py` | 6 笔种子（含 FT-1800） | FT-1800 输出 3.10/4.20/1.70/0.00；S5 有残差且标记 degraded |
| `test_api_e2e.py` | 接入→归因→双视角账单→争议→仲裁 | 全链路 2xx；争议快照冻结后重算不影响快照 |
| `test_frontend_smoke.py` | 三页面 | 200；含「残差」「数据质量」文案 |

---

## 8. 阶段计划（每阶段「先跑测试，再进下一阶段」）

### 阶段 1｜骨架 + 配置 + 模型 + 假设（0.5 步）
- 建目录、`requirements.txt`、`pytest.ini`、`config/attribution.yaml`
- `models.py` 9 表 + `db.py` 会话 + `enums.py`
- 写 `ASSUMPTIONS.md`（技术假设、合成数据声明、降级策略、默认拆分比例、金额精度、时区/单位约定）
- 放行：`pytest -q` 通过（此时 `test_geo`/`test_pricing` 基础用例已写、建表 smoke 通过）

### 阶段 2｜轨迹/计价/归因核心 + 单元测试（重点）
- `geo.py` → `trajectory.py` → `pricing.py` → `attribution.py`
- 守恒性质测试 + 6 类畸形轨迹 + 4 步分解单变量隔离测试
- 放行：`pytest tests/test_geo.py tests/test_trajectory.py tests/test_pricing.py tests/test_attribution_core.py` 全绿

### 阶段 3｜接口 + 种子 + 端到端
- `schemas.py` + 5 个 router + `seed.py` + `scripts/run_demo.py`
- FT-1800 精确金额验收、争议快照冻结、双视角
- 放行：`pytest -q` 全绿（含 e2e）

### 阶段 4｜极简前端（测试通过后才做）
- 3 页面 + 1 CSS；账单页支持视角切换与证据展开、降级横幅、残差高亮
- 放行：`test_frontend_smoke.py` + 手工 `uvicorn` 启动截图

### 阶段 5｜文档 + 全量验收 + 提交
- `README.md`、TELOS 可行性、系统设计、用例、角色表、**展示脚本**、**哈佛引用表**、验收清单（含守恒证明与示例账单）
- 手册对齐核对：需求与可行性 10%（TELOS 五维齐备）、系统设计 10%（模块+库表+UI 截图+用例）、末尾角色表、哈佛引用、20 分钟展示时间轴
- `pytest -q` 全量 + `uvicorn app.main:app --reload` 手工走查
- git 分阶段提交，最后 `git push origin main`

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 几何/投影自实现易出偏差 | 只做「点到折线段最近点 + 沿路累计里程」最小几何集；用已知直线/折线做单元测试 |
| 四步分解出现负金额（如实际更快更短） | 允许负值归因项（表示「比预估省了」），前端用颜色 + 文案区分，不做绝对值截断 |
| 时间桶拆分不到 Δ2 造成残差过大 | 顺序保证：拥堵+司机延迟只解释可举证部分，剩余时间差显式进残差并在 flags 告警 |
| Python 3.14 新解释器兼容性 | 只用标准库 + 已实测可导入的依赖；不引入 C 扩展新包 |
| 中文编码 | 全部文件 UTF-8；模板显式 `<meta charset="utf-8">`；Windows 控制台输出避免 emoji |
| 演示库脏数据 | `POST /api/demo/reset` + 启动时 `SEED_ON_START` 开关 |

---

## 10. 交付验收（最终回复必须包含）

1. 完整文件树
2. 启动命令（含测试命令）
3. `pytest -q` 结果
4. FT-1800 示例透明账单（乘客视角 + 司机视角摘要）
5. 「归因项金额加残差等于总差额」的验收说明（公式 + 实测数字 + 测试用例名）
