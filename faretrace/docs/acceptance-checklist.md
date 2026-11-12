# 验收清单 — FareTrace 网约车透明账单归因系统

> 验收口径：`config_version=cfg-1.0.0`、`algorithm_version=faretrace-attr-1.0.0`。
> 全量测试命令：`python -m pytest -q` → **73 passed**。

## 1. 生活化验收（核心场景）

**场景**：预估价 18.00 元，实付 27.00 元，总差额 9.00 元（订单 FT-1800）。

| 要求 | 系统输出 | 结果 |
|---|---|---|
| 不能只显示 "+9 元" | 六个归因项逐项金额 + 业务说明 + 证据 JSON | ✅ |
| 拥堵延迟 ≈ +4.20 | `CONGESTION_DELAY = 4.20`（840 秒 × 0.30 元/分钟，附时间区间与均速证据） | ✅ |
| 路径偏离 ≈ +3.10 | `PATH_DEVIATION = 3.10`（拟合度 0.33 < 0.80，ΔD=2000m × 1.55 元/km，附偏离段轨迹片段） | ✅ |
| 计价规则变化 ≈ +1.70 | `PRICING_RULE_CHANGE = 1.70`（起步价 4.56→6.26，附字段对比证据） | ✅ |
| 残差显示 | `UNEXPLAINED_RESIDUAL = 0.00`（显式存在，描述"无未归因差额"） | ✅ |
| 轨迹缺失提示 | FT-1005 页面横幅「本次账单基于不完整轨迹生成」+ `INCOMPLETE` + 质量标记 | ✅ |
| 无法归因差额不隐藏 | FT-1005 残差 0.20 元单独成项展示，橙色警示 | ✅ |

**测试锚点**：`test_ft1800_exact_acceptance_numbers`（逐项断言 3.10/4.20/1.70/0.00/0.00/0.00）、`test_bill_page_warns_on_incomplete_trajectory`。

## 2. 金额守恒验收

**公式**：`Σ(六归因项，含 UNEXPLAINED_RESIDUAL) = total_delta = paid_amount − quoted_price`，容差 ±0.01 元。

**实测（FT-1800）**：`3.10 + 4.20 + 0.00 + 1.70 + 0.00 + 0.00 = 9.00 = 27.00 − 18.00` ✅

**覆盖范围**：

| 范围 | 用例 | 结果 |
|---|---|---|
| 六笔种子订单逐一守恒 | `test_all_seeds_conserve_money` | ✅ |
| 随机订单 3 组 × 12 笔（随机费率/里程/时长/规则切换/杂费/轨迹有无） | `test_random_orders_conserve_money[101|202|303]` | ✅ |
| 负差额（实付 < 预估） | `test_negative_total_delta_is_attributed` | ✅ |
| 接口层端到端 | `test_e2e_create_complete_bill_dispute`（响应 `conservation.ok=true`） | ✅ |
| 页面展示守恒面板 | `test_bill_page_renders_both_views_and_evidence` | ✅ |

## 3. 功能需求验收

| 编号 | 需求 | 验收证据 | 结果 |
|---|---|---|---|
| FR1 | 订单数据接入（快照/账单/轨迹/参考路径） | `POST /api/orders` + `/complete`；轨迹允许为空 | ✅ |
| FR2 | 三维偏差归因（六项+残差） | `test_low_fit_produces_path_deviation` 等 | ✅ |
| FR3 | 双视角透明账单 | `test_bill_views_passenger_and_driver`（分组 3.10/4.20/1.70） | ✅ |
| FR4 | 争议与证据回溯 | `test_dispute_flow_binds_evidence_and_locks_snapshot`（含重算后快照不变） | ✅ |
| FR5 | 口径可追溯 | `GET /api/config` 只读阈值；归因记录含 config_version | ✅ |
| FR6 | 五类场景种子（+验收样本共 6 笔） | `seed.py`；`test_list_orders_from_seed` | ✅ |
| — | 拟合度门控路径偏离 | `test_high_fit_means_zero_path_amount` / `test_low_fit_produces_path_deviation` | ✅ |
| — | 拥堵识别（15 km/h、60 s） | `test_congestion_seed`、`test_short_stop_not_counted_as_congestion` | ✅ |
| — | 状态机校验 | 非法流转 → 409 `INVALID_STATE_TRANSITION` | ✅ |
| — | 统一错误结构 | `test_unified_error_structure` | ✅ |

## 4. 非功能验收

| 维度 | 要求 | 结果 |
|---|---|---|
| 降级不崩 | 空/乱序/重复/瞬移/同刻/缺口 6 类畸形轨迹 | 12 项轨迹测试全绿 ✅ |
| 确定性 | 同输入同配置两次归因一致 | `test_attribution_is_deterministic` ✅ |
| 幂等 | 重算不产生重复记录，revision+1 | `test_reattribution_keeps_single_record_and_bumps_revision` ✅ |
| 残差治理 | \|残差\|>1.00 → NEEDS_REVIEW | 配置 + flags 断言 ✅ |
| 一键运行 | `pip install -r requirements.txt && uvicorn`，启动自动建表灌种子 | 实测 `/orders` `/bill` `/disputes/new` 均 200 ✅ |
| 数据声明 | 全部合成数据、无真实支付/地图 | README + ASSUMPTIONS 声明 ✅ |

## 5. 交付物清单（对照实验手册）

| 手册要求 | 交付物 | 状态 |
|---|---|---|
| 需求与可行性分析（10%）：TELOS + 利益相关者 + 市场/用户群体 | `docs/requirements-feasibility.md` | ✅ |
| 系统设计（10%）：模块、数据库、UI/截图说明、示例用例 | `docs/system-design.md` + `docs/use-cases.md` | ✅（截图见 §5a） |
| 哈佛引用格式 | `docs/references.md`（正文引用 + 列表 + 对照表） | ✅（占位需核对） |
| 报告末尾团队角色表 | `TEAM_ROLES.md` | ✅ |
| 展示（10%）：20 分钟含 5 分钟问答、角色扮演 | `docs/presentation-script.md` | ✅ |
| 仓库结构（app/seed.py/tests/docs/README/ASSUMPTIONS/TEAM_ROLES） | 均在仓库根 | ✅ |
| curl 示例 | `README.md` 接口一览 | ✅ |

**5a 截图待办**（需人工操作）：启动服务后截取三张图放入 `docs/screenshots/`：
1. `/orders` 订单列表；2. `/orders/FT-1800/bill` 乘客视角（含守恒面板）；3. `/disputes/new` 争议提交。

## 6. 遗留事项与局限（诚实声明）

1. 参考文献为哈佛格式占位，提交前需按实际检索结果核对年份/卷期/链接。
2. UI 截图需人工截取（自动化截图工具未引入，见 ASSUMPTIONS A8）。
3. 偏离判定基于直线距离，无法区分封路/禁左等合理绕行；归因结果定位为"待复核证据"。
4. 轨迹缺失时的时间归因为降级口径（拥堵 0、正时长计入司机延迟），不代表真实责任。
5. 单机 SQLite，非生产并发方案。
