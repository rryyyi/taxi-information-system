# 验收清单 — FareTrace 沈阳出租车透明账单归因系统

> 验收口径：`config_version=cfg-sy-1.0.0`、`algorithm_version=faretrace-sy-attr-1.0.0`。
> 全量测试：`python -m pytest -q` → **83 passed**。

## 1. 生活化验收（核心场景）

**场景**：沈阳晚高峰订单 FT-SY-1800，预估价 18.00 元，实付 27.00 元，总差额 9.00 元。

| 要求 | 系统输出 | 结果 |
|---|---|---|
| 不能只显示 "+9 元" | 七个归因项逐项金额 + 业务说明 + 证据 JSON | ✅ |
| 等候费拆到路口和秒数 | `WAIT_FEE = 6.00`：4 条等候事件（青年大街—市府大路/十一纬路/大西路/文艺路，各 180 秒、平均速度 0、晚高峰、每条 1.50 元 = 180/120） | ✅ |
| 每条等候事件可在地图定位 | 每条事件含 `lat/lng`（青年大街沿线 41.7995→41.7845, 123.4359），账单页橙色圆点弹窗显示 | ✅ |
| 绕路增量拆到偏离路段和米数 | `DETOUR_EXTRA = 3.00`：文艺路→科普路，参考 1111.9 米 / 实际 2613.2 米 / **多行驶 1501.2 米** × 2 元/公里 | ✅ |
| 地图对比参考路径与实际路径、偏离段高亮 | Leaflet + OSM：灰虚线参考路径、蓝线实际轨迹、红色高亮偏离段（含入口/出口标记） | ✅ |
| 残差显示 | `UNEXPLAINED_RESIDUAL = 0.00`（显式存在） | ✅ |
| 金额守恒 | 6.00 + 3.00 + 0.00(里程增量) + 0(返空) + 0(换档) + 0(规则) + 0(残差) = 9.00 | ✅ |

**说明**：需求示例中"等候费 +4.2 元（360 秒）"与高峰费率"每 2 分钟 1 元"在算术上不一致（360/120 = 3.00）；本系统按沈阳固定费率给出规则精确的数字（720 秒 → 6.00 元、1.5 公里 → 3.00 元），结构完全一致。见 `ASSUMPTIONS.md` §6.5。

**测试锚点**：`test_ft_sy_1800_acceptance`（逐项断言）、`test_ft_sy_1800_events_have_map_coordinates`、`test_ft_sy_1800_wait_evidence_matches_spec_format`。

## 2. 金额守恒验收

公式：`Σ七归因项（含残差）= 实付 − 预估`，容差 ±0.01 元。残差 >0.01 元必须"待核查"，不得并入堵车/绕路。

| 范围 | 用例 | 结果 |
|---|---|---|
| 7 笔种子逐一守恒 | `test_all_seeds_conserve_money` | ✅ |
| 证据 bundle 守恒标志 | `test_evidence_bundle_conservation_flag` | ✅ |
| 负差额/取整差额由 MILEAGE_EXTRA 吸收 | FT-SY-1800 中 MILEAGE_EXTRA=0.00 | ✅ |
| 接口端到端守恒 | `test_e2e_create_complete_bill_events_dispute` | ✅ |
| 页面守恒面板 | `test_bill_page_renders_views_and_leaflet_map` | ✅ |

## 3. 计费基准与归因行为验收

| 要求 | 用例 | 结果 |
|---|---|---|
| 起步价 9 元/3km、500m 步进运价 | `test_day_mileage_500m_steps`、`test_core_fare_day` | ✅ |
| 夜间 10 元/3km、455m 步进 | `test_night_mileage_455m_steps` | ✅ |
| 返空费 >15km 触发、阶梯 30%/50% | `test_return_fee_tiers`、`test_return_fee_triggered_over_15km` | ✅ |
| 连续低速段 → 等候事件（高峰/平峰分段） | `test_low_speed_span_detected_at_12_kmh`、`test_split_by_peak_crosses_evening_boundary`、`test_peak_wait_fees_per_event` | ✅ |
| 高拟合度不产生绕路金额 | `test_high_fit_orders_have_no_detour` | ✅ |
| 低拟合度产生绕路事件（含坐标） | `test_low_fit_order_produces_detour_event` | ✅ |
| 跨 22:00 夜间换档差价 | `test_night_surcharge_when_crossing_2200` | ✅ |
| 计价规则变化恒为 0 | `test_pricing_rule_change_is_always_zero` | ✅ |
| 轨迹缺失降级 + 残差待核查 | `test_missing_trajectory_degrades_and_residual_needs_review` | ✅ |
| 事件坐标落在沈阳城区 | `test_ft_sy_1800_events_have_map_coordinates`、`test_shenyang_anchors_are_in_city` | ✅ |
| 争议证据绑定 + 快照冻结 | `test_dispute_flow_binds_evidence_and_locks_snapshot` | ✅ |
| 端到端（创建→完成→账单→事件→争议） | `test_e2e_create_complete_bill_events_dispute` | ✅ |

## 4. 交付物清单（对照实验手册）

| 手册要求 | 交付物 | 状态 |
|---|---|---|
| 需求与可行性（TELOS + 乘客/司机/运营仲裁/监管方） | `docs/requirements-feasibility.md` | ✅ |
| 系统设计（模块、数据库 13 表、UI/截图说明、用例） | `docs/system-design.md` + `docs/use-cases.md` | ✅ |
| 哈佛引用（含沈阳计价规则来源） | `docs/references.md` | ✅（占位需核对） |
| 团队角色表 | `TEAM_ROLES.md` | ✅ |
| 假设说明 | `ASSUMPTIONS.md` | ✅ |
| README（含 curl 示例、种子命令） | `README.md` | ✅ |
| 展示脚本（20 分钟含 5 分钟问答） | `docs/presentation-script.md` | ✅（沈阳数字需按本清单更新） |

**截图待办**：启动服务后截取三张图放入 `docs/screenshots/`：① 订单列表；② FT-SY-1800 账单页（含 Leaflet 沈阳地图：等候标记与绕路高亮）；③ 争议提交页。

## 5. 遗留事项与局限

1. 参考文献为占位，提交前需核对沈阳运价通告的文号与年份。
2. 锚点坐标为 OSM 近似值；绕行点为"青年大街与三好街之间"的虚拟位置。
3. 偏离判定基于直线距离，不能区分合理绕行（封路/禁左）；归因结果定位为待复核证据。
4. 等候事件平均速度为 0（演示停驶）；蠕行场景金额按秒数计算，口径相同。
5. 地图瓦片需联网；离线时矢量图层仍显示并有降级提示。
