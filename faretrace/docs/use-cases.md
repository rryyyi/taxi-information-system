# 用例描述 — FareTrace 沈阳出租车透明账单归因系统

> 用例图（文字版）：
>
> ```
>                    ┌──────── FareTrace 沈阳版 ───────┐
>   乘客 ────────────┤ UC1 查看透明账单（含沈阳地图）   │
>        └───────────┤ UC3 发起争议                    │
>   司机 ────────────┤ UC4 查看扣款依据（司机视角）     │
>        └───────────┤ UC3 发起争议                    │
>   平台订单系统 ────┤ UC2 接入订单数据（创建/完单）    │
>   运营仲裁人员 ────┤ UC5 复核争议（状态流转）         │
>   监管方 ──────────┤ UC6 只读审计（沈阳基准/归因口径）│
>                    └─────────────────────────────────┘
> ```

## UC-1 乘客查看差额构成（含地图定位）

- **参与者**：乘客
- **前置**：订单 FT-SY-1800 已完成并归因（沈阳晚高峰，预估 18.00 → 实付 27.00）
- **主流程**：
  1. 打开 `/orders`，看到差额/残差/拟合度/事件数列；
  2. 点击 FT-SY-1800，默认乘客视角：headline「预估价 18.00 元，实付 27.00 元，多付 9.00 元」；
  3. 展开等候费证据：4 条等候事件（青年大街—市府大路/十一纬路/大西路/文艺路，各 180 秒、晚高峰、每条 1.50 元）；
  4. 在 Leaflet 地图上点击橙色圆点，核对路口位置与等候秒数；
  5. 展开绕路增量证据：文艺路→科普路偏离段，参考 1111.9 米 / 实际 2613.2 米 / 多走 1501.2 米 / 绕路费 3.00 元；地图红色折线高亮偏离段并与灰色参考路径对比；
  6. 核对金额守恒面板：6.00 + 3.00 + 0 = 9.00，通过。
- **异常流**：订单未归因 → 提示；轨迹缺失 → 降级横幅。
- **覆盖测试**：`test_bill_page_renders_views_and_leaflet_map`、`test_bill_views_with_map_data`。

## UC-2 平台接入订单数据（创建 + 完单）

- **参与者**：平台订单系统、后端
- **前置**：无（计价档位按时间自动判定）
- **主流程**：
  1. `POST /api/orders`：写预估价快照与参考路径（含沈阳道路节点名）；
  2. `POST /api/orders/{no}/complete`：写账单与轨迹（可为空）；
  3. 系统按上车/结束时间自动判定白天/夜间档，执行归因（预处理→拟合→三维分解→残差）；
  4. 落库归因项、等候事件、绕路事件、双视角账单；
  5. 返回 201。
- **异常流**：重复完单 409；畸形轨迹降级不崩；参考路径非法 422。
- **覆盖测试**：`test_e2e_create_complete_bill_events_dispute`、`test_night_rule_auto_detected_in_api`。

## UC-3 司机/乘客发起争议（证据绑定与冻结）

- **主流程**：选择归因项（如 DETOUR_EXTRA）→ 提交 → 系统冻结整份归因+原始账单+轨迹摘要为 `evidence_snapshot_json`（`snapshot_locked=true`）并绑定 `dispute_evidence` → 状态 OPEN。
- **关键性质**：之后订单重算（revision+1）不影响快照——复核所见=发起所见。
- **异常流**：归因项不存在 422（附可用项）；未归因 422；角色非法 422。
- **覆盖测试**：`test_dispute_flow_binds_evidence_and_locks_snapshot`。

## UC-4 司机查看扣款依据

- **主流程**：司机视角 headline「与你的驾驶行为相关 3.00 元，与路况相关 6.00 元，与沈阳固定计价规则相关 0.00 元」→ 分组卡片核对 → 展开绕路段轨迹证据。
- **约束**：页面固定免责声明「系统不判定责任」。
- **覆盖测试**：`test_bill_views_with_map_data`（分组金额断言 3.00/6.00）。

## UC-5 运营复核争议

- **主流程**：GET 争议详情（含快照与证据绑定）→ OPEN→REVIEWING（记录复核人）→ RESOLVED/REJECTED（填处理说明）。
- **异常流**：非法流转 409 `INVALID_STATE_TRANSITION`。
- **覆盖测试**：`test_dispute_flow_binds_evidence_and_locks_snapshot`。

## UC-6 监管/审计只读查询

- **主流程**：`GET /api/config` 核对沈阳基准（起步价 9/10 元、500m/455m 步进、返空阶梯、等候费率）与归因记录的 config_version、algorithm_version 是否一致；`GET /api/orders/{no}/attribution` 审计证据 bundle。
- **覆盖测试**：`test_config_exposes_shenyang_fare_and_thresholds`、`test_attribution_raw_endpoint`。

## UC-7 轨迹缺失降级（扩展用例）

- **触发**：完单时轨迹为空（SY-1005）。
- **主流程**：预处理 → INCOMPLETE（TRAJECTORY_MISSING）→ 等候费 0、绕路不予认定 → 实付中无法举证的 1.50 元进入残差 → 状态 NEEDS_REVIEW → 页面横幅「本次账单基于不完整轨迹生成」+ 残差标注「待核查」。
- **覆盖测试**：`test_missing_trajectory_degrades_and_residual_needs_review`、`test_bill_page_warns_on_incomplete_trajectory`。
