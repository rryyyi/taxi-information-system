# 贡献指南

欢迎提交缺陷反馈、文档修正和有明确验收条件的改进。当前项目为合成数据业务原型，范围与已知局限见 [README](README.md#工程边界)。项目自有代码尚未选择开源许可证；涉及许可和再分发的问题请先与维护者确认。

## 本地开发

使用 Python 3.14，在 `faretrace/` 创建独立虚拟环境并安装依赖：

```bash
cd faretrace
python -m venv .venv
# 按操作系统激活虚拟环境，见根目录 README
python -m pip install -r requirements.txt
python -m pytest
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

从最新 `main` 建立小范围分支，例如 `docs/quickstart` 或 `fix/trajectory-gap`。提交说明突出问题和结果；请将应用改动与无关的格式化、演讲材料和生成物分开。

## 业务变更约束

- 保持七项归因输出和金额守恒；金额使用 `Decimal`，不要用二进制浮点数累加账务金额。
- 证据不足时保留质量标记与残差，不把差额补到等候或绕路费用中。
- 修改计费口径时同步检查 `config_version`、`algorithm_version`、计算证据和技术假设。
- 归因重算不能改变已冻结的争议快照；争议状态变化必须经过状态机。
- 新增规则 / 算法行为应提供有业务意义的测试；缺陷修复先提供可复现样例或回归测试。
- 页面文案以用户能理解为准，金额、规则和证据继续由服务层提供。

## 验证与 PR

```bash
# faretrace/ 下
python -m pytest
python -m pip check

# 金额 / 归因改动
python -m pytest tests/test_shenyang.py tests/test_attribution.py tests/test_api.py

# 需要比较热路径性能时，固定同一环境和参数
python scripts/bench_attribution.py 500
```

PR 应描述触发条件、改动后的行为、实际验证结果和已知风险。界面调整附桌面 / 移动端截图；依赖升级需重新运行全套测试。性能报告需注明机器、Python 版本、负载、缓存状态和测量方法，不能把热路径微基准当作生产指标。

GitHub Actions 从干净环境安装依赖并执行测试。测试使用独立临时 SQLite 库；本地演示库不会作为测试输入。

## 数据和第三方资源

不要提交 `.env`、凭据、数据库、运行日志、真实乘客 / 司机数据和真实轨迹。截图与复现材料请使用合成订单。

新增第三方资源需保留许可与来源，更新 [第三方说明](THIRD_PARTY_NOTICES.md)。当前不通过新增许可证文件替原作者授予项目许可。
