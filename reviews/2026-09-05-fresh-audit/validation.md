**本轮执行证据**

所有验证在临时源码副本和新建Python环境运行，源码SHA256与审查基线逐文件一致。没有使用真实行情、旧研究产物、真实账户、Discord投递或线上服务。网络仅用于获取本轮测试依赖。

| 检查 | 结果 |
|---|---|
| `compileall -q src scripts tests` | 通过 |
| 按requirements-dev.txt安装依赖并执行兼容性检查 | 73个包兼容 |
| 全量原仓库pytest | **661 passed，2 warnings，46.46秒** |
| 数据/PIT/因子原生复现 | 5项确认，包含真实DuckDB与Parquet发布 |
| 正式回测、回放、账户、SQLite、通知与Web模块复现 | 9项确认 |
| 真实Jinja模板及隔离Node VM | 1项确认 |
| 动量、历史事件、行业模块复现 | 8项确认 |
| 主审市场研究/运维复现 | 5项确认 |
| 打包后的独立复现入口 | 6个子进程全部退出0 |

全量测试使用Python3.12.14、pytest8.4.2、pandas3.0.5、numpy2.5.2、DuckDB1.5.5、PyArrow25.0.1、exchange-calendars4.13.2；完整版本见[依赖快照](/Users/huozhihong/Documents/Quant/reviews/2026-09-05-fresh-audit/evidence/requirements-resolved.txt)。两条warning来自Starlette/TestClient的弃用接口，未导致失败。代码未修改，因此测试通过不能解释为修复完成。

先前另有184个相关测试在较轻的临时环境通过；它们属于全量661项的子集，不能把两个通过数相加。

原始证据：[全量pytest日志](/Users/huozhihong/Documents/Quant/reviews/2026-09-05-fresh-audit/evidence/full_pytest.log)、[结构化验证摘要](/Users/huozhihong/Documents/Quant/reviews/2026-09-05-fresh-audit/evidence/validation_summary.json)、[打包复现结果](/Users/huozhihong/Documents/Quant/reviews/2026-09-05-fresh-audit/evidence/reproduced/run_summary.json)。每个模块的原生输出与子进程日志也保存在evidence目录中。

**重跑方法**

使用装有本项目requirements-dev.txt的Python，运行：

```bash
python reviews/2026-09-05-fresh-audit/reproductions/run_all.py --node /path/to/node
```

入口先核对审查过的源文件哈希，再创建仅含所需跟踪文件的临时源码副本，分别运行数据、动量、回测、主审和模板实验。它只替换原实验中本次会话的临时I/O路径，不修改业务函数。默认输出到报告的`evidence/reproduced/`；可用`--output-dir`指定新目录。

如果Node已在PATH中，可以省略`--node`；否则JavaScript执行部分会明确标为跳过。本轮交付验证使用了Node，没有跳过。源码发生变化后入口会拒绝声称正在复现原审查版本；这些是故障复现实验，后续修复时应另写期望正确行为的回归测试。

阅读覆盖和测试均不包括旧独立仓库`quant-factor-framework/`、真实缓存数据质量、供应商在线接口行为、线上systemd实际运行状态或跨浏览器端到端验收。报告没有把这些未执行范围写成通过。
