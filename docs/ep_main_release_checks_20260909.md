# EP main 发布前验证

日期：2026-09-09。发布范围为 EP 源码、配置模板、文档、离线测试及验收脚本；模型调用和通知保持原有显式门禁，不因代码发布自动开启。

## 本地验证

- 相关回归：380 passed，25 subtests passed。
- 全量 `pytest tests -q`：980 passed，4 failed，59 subtests passed。
- 对本轮修改前的 `a185394` 导出干净源码，在同一临时 Python 环境运行上述四项，全部复现相同失败，因此不是本轮 EP 修改新增的回归。
- 三项 `test_broad_pit_universe.py` 失败来自既有代码对 categorical 列执行 `fillna("")`；一项 `test_market_regime_sources.py` 失败来自 `datetime64[ns, UTC]` 与期望 `datetime64[us, UTC]` 的精度差异。本轮不修改这两个独立业务模块或放宽测试。
- 暂存文件的密钥模式扫描和禁止数据文件检查无发现；`git diff --cached --check` 通过。

本地原临时环境缺 Web 测试依赖，已仅在 `/tmp/quant-ep-v1a-test` 补齐与 SG 同版的 httpx、websockets 后完成全量测试，未修改 SG 依赖。

## 同步约束

GitHub 不包含新增的原始行情、新闻正文、EP SQLite 数据库或生成的 JSON 报告。服务器同步以提交中的文件为边界，先备份 Git 状态与差异文件，保留 `.env`、`/etc/quant`、`.venv`、生产数据、独立 LLM 试验目录及其他服务器专有文件。不得以重新创建试验数据库重置累计预算。

发布完成以 GitHub main、工作机 HEAD、服务器代码摘要的实际核对为准；本文件不以计划代替部署成功。代码同步不涉及 systemd 安装、服务重启或自动付费测试。
