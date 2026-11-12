# TEAM_ROLES.md — 团队角色表

> 课程要求：报告末尾附团队角色表，明确不同成员角色。展示环节可按手册建议进行商务会议式角色扮演（20 分钟含 5 分钟问答）。
> 本表为**分工模板**，请把"姓名"列替换为本组成员后随报告提交。

| 角色 | 对应手册角色 | 姓名 | 主要职责 | 交付物 | 工作量占比 |
|---|---|---|---|---|---|
| 项目经理（PM） | 项目经理 | （待填） | 范围与里程碑管理、阶段验收（每阶段先跑测试）、风险与假设登记、报告统稿 | `docs/PLAN.md`、进度表、`docs/acceptance-checklist.md` | 15% |
| 首席信息官（CIO） | CIO / CSIRO | （待填） | 信息系统架构与选型、模块划分、接口与库表设计、合规与数据治理 | `docs/system-design.md`、`ASSUMPTIONS.md`、架构图 | 15% |
| 后端工程师 | — | （待填） | FastAPI 接口、SQLAlchemy 模型、订单接入/完单编排、统一错误结构、演示脚本 | `app/routers/*`、`app/services/ingest.py`、`seed.py` | 20% |
| 数据/算法工程师 | — | （待填） | 轨迹预处理、拟合度、三维偏差归因、证据 JSON、守恒与场景测试 | `app/services/{geo,trajectory,pricing,attribution}.py`、`tests/*` | 25% |
| 前端工程师 | — | （待填） | 三个页面（订单列表/透明账单/争议提交）、双视角切换、证据展开、降级提示 | `app/templates/*`、`app/static/style.css` | 10% |
| 文档与展示负责人 | CFO（经济可行性）/ 展示主持 | （待填） | 需求与可行性（TELOS）、用例描述、哈佛引用、答辩脚本与彩排 | `docs/requirements-feasibility.md`、`docs/use-cases.md`、`docs/presentation-script.md`、`docs/references.md` | 15% |

## 展示分工建议（20 分钟：15 分钟讲解 + 5 分钟问答）

| 时间 | 环节 | 主讲 |
|---|---|---|
| 0–2 min | 业务痛点：预估价 18 元、实付 27 元，双方都拿不到可举证说明 | CEO 角色 |
| 2–5 min | 需求与可行性（TELOS 五维 + 利益相关者） | CFO / 文档负责人 |
| 5–9 min | 系统架构、数据库设计、接口 | CIO |
| 9–14 min | 归因算法演示：FT-1800 透明账单、证据展开、金额守恒 | 数据/算法工程师 |
| 14–15 min | 争议流程与运营复核价值 | 项目经理 |
| 15–20 min | Q&A（预答脚本见 `docs/presentation-script.md`） | 全体 |
