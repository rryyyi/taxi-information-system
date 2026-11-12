# 用例描述 — FareTrace 网约车透明账单归因系统

> 用例图（文字版）：
>
> ```
>                    ┌──────── FareTrace 系统 ────────┐
>   乘客 ────────────┤ UC1 查看透明账单（乘客视角）     │
>        └───────────┤ UC3 发起争议                    │
>   司机 ────────────┤ UC4 查看扣款依据（司机视角）     │
>        └───────────┤ UC3 发起争议                    │
>   平台订单系统 ────┤ UC2 接入订单数据（创建/完单）    │
>   运营仲裁人员 ────┤ UC5 复核争议（状态流转）         │
>   监管方 ──────────┤ UC6 只读审计（原始归因/配置）    │
>                    └────────────────────────────────┘
> ```

## UC-1 乘客查看差额构成

- **参与者**：乘客
- **前置条件**：订单完成且已归因
- **触发**：乘客打开订单列表点击订单号
- **主流程**：
  1. 打开 `/orders`，看到 6 笔订单的预估价/实付/差额/残差/拟合度/数据质量；
  2. 点击 `FT-1800`，默认进入乘客视角；
  3. 阅读 headline「预估价 18.00 元，实付 27.00 元，多付 9.00 元」与四张金额卡片；
  4. 展开拥堵延迟行的证据 JSON：时间区间、累计 840 秒、均速、阈值（15 km/h / 60 s）、沿参考路径位置；
  5. 核对金额守恒面板：归因项之和 9.00 元 = 总差额 9.00 元，通过。
- **后置条件/返回值**：无状态变更；页面渲染六归因项与守恒校验。
- **异常流**：订单未完成归因 → 提示「该订单尚未完成归因」；轨迹缺失 → 顶部降级横幅。
- **覆盖测试**：`test_bill_page_renders_both_views_and_evidence`、`test_bill_views_passenger_and_driver`。

## UC-2 平台接入订单数据（创建 + 完单）

- **参与者**：平台订单系统（外部）、后端服务
- **前置条件**：计价规则版本已存在（如 `R-EST-1`）
- **触发**：`POST /api/orders`（创建）与 `POST /api/orders/{no}/complete`（完单）
- **主流程**：
  1. 创建订单：写入预估价快照（金额/距离/时长/规则版本/杂费优惠）与参考路径折线；
  2. 完单：写入实付账单（分项+汇总）与轨迹点（允许为空）；
  3. 系统自动执行归因：轨迹预处理 → 拟合度 → 三维偏差分解 → 残差兜底；
  4. 落库归因记录、六个归因项（含证据 JSON）、双视角透明账单；
  5. 返回 201，响应含原始归因与乘客/司机账单。
- **后置条件**：订单 `COMPLETED`，可查询账单与归因。
- **异常流**：重复完单 → 409；规则不存在 → 422 `VALIDATION_ERROR`；轨迹畸形 → 归因降级不报错（`INCOMPLETE` + 质量标记）。
- **覆盖测试**：`test_e2e_create_complete_bill_dispute`、`test_missing_pricing_rule_is_reported`。

## UC-3 司机/乘客发起争议（证据绑定）

- **参与者**：司机或乘客
- **前置条件**：目标订单已生成透明账单
- **触发**：账单页「发起争议」链接或 `POST /api/disputes`
- **主流程**：
  1. 选择归因项（如 PATH_DEVIATION），填写发起角色与原因；
  2. 系统校验角色合法、归因项存在；
  3. **冻结证据快照**：当前归因（金额/拟合度/质量标记/口径版本）+ 全部六项 + 原始预估/账单/轨迹摘要，写入 `evidence_snapshot_json`（`snapshot_locked=true`）；
  4. 按归因项绑定 `dispute_evidence` 行；
  5. 返回 201，争议状态 `OPEN`。
- **后置条件**：之后订单被重算（revision+1）不影响快照内容——复核所见即发起所见。
- **异常流**：归因项不存在 → 422（附可用项列表）；订单未归因 → 422；角色非法 → 422。
- **覆盖测试**：`test_dispute_flow_binds_evidence_and_locks_snapshot`（含重算后快照不变断言）。

## UC-4 司机查看扣款依据

- **参与者**：司机
- **前置条件**：订单完成且已归因
- **触发**：`GET /api/orders/{no}/bill?view=driver` 或账单页「司机视角」标签
- **主流程**：
  1. 查看 headline「本单差额中，与你的驾驶行为相关 3.10 元，与路况相关 4.20 元，与平台计价规则相关 1.70 元」；
  2. 四个责任分组卡片核对金额；
  3. 展开与驾驶行为相关项的证据（偏离段轨迹片段、参考路径投影位置）；
  4. 若认为路况责任被误判，走 UC-3 发起争议。
- **后置条件**：无状态变更。
- **业务约束**：页面固定免责声明「系统不判定责任，仅呈现差额构成与证据」。
- **覆盖测试**：`test_bill_views_passenger_and_driver`（分组金额断言 3.10/4.20/1.70）。

## UC-5 运营复核争议

- **参与者**：平台运营/仲裁人员
- **前置条件**：存在 `OPEN` 状态争议
- **触发**：`PATCH /api/disputes/{id}`
- **主流程**：
  1. `GET /api/disputes/{id}`（含快照与证据绑定）复核发起时的证据；
  2. 状态 `OPEN → REVIEWING`（记录 reviewed_by）；
  3. 依据快照证据得出结论，状态 → `RESOLVED`（或 `REJECTED`），填写 resolution；
  4. 系统记录 reviewed_at，争议闭环。
- **后置条件**：终态不可再流转（`RESOLVED`/`REJECTED` → 空集合）。
- **异常流**：非法流转 → 409 `INVALID_STATE_TRANSITION`（附允许的目标状态）。
- **覆盖测试**：`test_dispute_flow_binds_evidence_and_locks_snapshot` 步骤 3–5。

## UC-6 监管/审计只读查询

- **参与者**：监管方、内部审计
- **触发**：`GET /api/config`、`GET /api/orders/{no}/attribution`、`GET /api/health`
- **主流程**：核对当前阈值口径（FIT_BUFFER_M=100、FIT_THRESHOLD=0.8、CONGESTION_SPEED_KMH=15、MIN_CONGESTION_SECONDS=60）与归因记录中的 `config_version`、`algorithm_version`、`revision`、阈值快照、质量标记是否一致。
- **后置条件**：只读，无副作用。
- **覆盖测试**：`test_config_endpoint_is_read_only_and_exposes_thresholds`、`test_attribution_raw_endpoint_has_evidence_bundle`。

## UC-7 轨迹缺失降级（系统行为，扩展用例）

- **触发**：完单时轨迹为空或有效点 < 3
- **主流程**：预处理返回 `INCOMPLETE`（`TRAJECTORY_MISSING`）→ 归因降级：路径偏离 0（证据注明"缺少可用轨迹，不予认定"）、拥堵 0（`CONGESTION_UNVERIFIED`）、正时间偏差全部计入司机/其他延迟 → 状态 `DEGRADED` → 乘客页横幅「本次账单基于不完整轨迹生成」。
- **价值**：宁可降级并声明，也不崩溃或静默给出高置信度结论。
- **覆盖测试**：`test_missing_trajectory_degrades_and_records_residual`、`test_bill_page_warns_on_incomplete_trajectory`。
