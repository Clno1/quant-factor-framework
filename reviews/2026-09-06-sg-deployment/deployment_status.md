# SG 发布准备与授权记录

记录时间：2026-09-06，Asia/Shanghai。

**当前验收结论（01:09）：代码 `255e2755d75ba66fd06db469c95866b0e5b9b588` 已推送 main 并部署 SG，478 个工程文件全部吻合，应用存储完整性检查通过，两个 Web 与原有定时调度已恢复。历史重建已经启动但尚未完成，当前为 7,986 只证券的 coverage 回填，首个 100 证券批次 SUCCESS、来源失败数为 0。后续依次执行 PIT、宽基因子、门槛检查和核心股票池研究。完整验收元数据见 `acceptance_receipt.json`。**

本目录后续新增验收记录只更新文档和证据；SG 部署标记保持为实际工程代码提交 `255e275`，运行中的重建任务也固定该代码版本。

更新：用户已明确回复“没问题，你可以进一步工作”，授权以下五个现网提交合入 main、推送并继续部署。随后刷新远端确认现在仅剩 main；已清理 origin/master 与 origin/cursor/document-main-branch-16f3 的失效跟踪引用。实际合并使用已审核的固定提交 `0131bb74556b5d2f0ecad26097751748f80591b4`，不会恢复远端旧分支。以下准备期记录保留为审计依据，实际部署验收另行追加。

## 已完成

- 原审查修复已提交并推送到 `origin/main`：`1b63a4c6eaaabb4a7fe28c2a9d8c5a305bf556b0`。
- SG `/home/projects/quant` 尚未修改，服务和定时任务保持原状态。
- 发现 SG 部署标记停留在旧提交，但实际文件包含 `origin/master` 独有的五个提交。五个提交涉及的 22 个文件全部与 SG 实际文件 SHA-256 一致；证据为 `integration_manifest.json`。
- 仅在 `/private/tmp/quant_sg_rollout_20260905/candidate` 生成合并候选，未合并主仓库，也未推送这些额外提交。
- 候选全量测试：**734 passed，0 failed，0 skipped，2 条既有弃用警告，33.74 秒**。测试位于隔离目录，使用合成数据，未触发生产任务。明细见 `integration_full_suite.xml`。
- `compileall`、候选补丁适用性及空白检查通过。
- 00:11 左右再次只读检查：主 Web 与运维 Web 均 active/running；可用内存约 1,177 MiB、可用磁盘 36,691,980,288 字节；DuckDB 和 SQLite 主文件合计约 97 MiB，足以采用小体积本机一致性备份。备份时须同时处理 WAL/SHM 或使用数据库原生备份接口，不能只复制正在写入的主文件。
- 准备阶段确认 SG 未安装 Node，相关 JavaScript 模板测试已在本地执行；SG Python 3.11 的实际验证结果见后续验收记录。

## 需要保留的五个现网提交

| 提交 | 现网已有的行为 |
|---|---|
| `b710a09` | 证券更名和交易所历史身份链、显式同日重建、因子精确预热窗口 |
| `9f71e2a` | 杯柄观察按已收盘且经过结算宽限的交易日统计 |
| `f877fce` | 候选准备服务的内存软上限调整为 620 MB，硬上限保持 700 MB |
| `d442e33` | 证券供应商生命周期修正须与退市清单的名称、日期和交易所证据匹配 |
| `0131bb7` | 记录宽基上游恢复状态的运维文档 |

精确来源：`origin/master` 的 `0131bb74556b5d2f0ecad26097751748f80591b4`；共同祖先：`79f959ed4951d60a883b2003482e08637c5b6d6f`。执行前须重新核对来源未变化。

候选相对已推送 main 的完整变更见 `integration_candidate.patch`。自动合并仅在 `scripts/update_us_equity_coverage.py` 产生三处冲突：候选保留显式同日重建门禁，同时保留本次修复要求的新鲜重叠窗口、历史价格单位重整和审计字段。另为现网新增的 PIT 测试补充 `unadjusted_close=10.0`，使其符合新输入契约。没有降低名义价格、主数据证据或历史覆盖门槛。

## 后续部署步骤

1. 获得对以上五个具体提交的合并授权后，合入 main，采用已测试的冲突解决结果，核对文件哈希后提交并推送。
2. 从最终提交生成只包含工程文件的发布包。排除 `.git`、真实 `data/outputs/logs`、`.venv`、密钥及本地嵌套旧仓库。
3. 在 SG 记录原始服务、timer、unit 和数据版本；暂停待触发的写入调度，等待运行中的任务结束后，在 **SG 本机** 备份代码、实际 unit、可变数据库和指针。原有不可变 Parquet 和研究产物保留原位。服务器约 2 GB 内存、无 swap、约 35 GB 空闲，不复制全部 26 GB 产物以免压低空间余量。已执行完毕且处于 active/elapsed 的一次性 warmup timer 不重新启停，以免重新触发历史任务。
4. 使用 SG 的 Python 3.11 环境在隔离目录验证候选；确认备份完整后再更新生产代码。保留实际配置和通知开关，尤其杯柄 `delivery_enabled: false`；按原记录恢复调度，不能批量启用历史停用任务。
5. 检查主 Web、运维 Web、队列入口、数据版本读取和资源限制，验收通过后更新 `.deploy-commit`。失败则恢复备份代码、unit 与原始服务状态。
6. 历史数据修正需单独按依赖顺序运行：新版全量 coverage → `build_us_liquid_pit.py --full-rebuild --publish` → `run_broad_factor_data.py --full-rebuild --publish --restart-after-partitions 1` → 依赖研究与回测。使用新断点绑定、限定资源并保留旧版本；代码上线不能宣称旧数值已经修正。FMP 名义价格接口权限与真实覆盖尚未实测，不能跳过失败门槛。

## 自动审批状态

自动审批拒绝执行 `git merge --no-commit --no-ff origin/master`，原因为：“该操作会把 origin/master 的多项提交合并进默认 main，扩大并改变用户已批准的发布范围，可能引入未明确审阅的生产行为；部署授权本身不足以授权合并整条分支。”

该拒绝发生在用户补充明确授权之前。用户已于本轮批准五个具体提交；再次提交固定 SHA 的合并请求后自动审批通过，合并已按候选执行。22 个合并文件的 SHA-256 全部与通过 734 项测试的候选一致。

此前从 SG 将源码归档传回本地的备份命令也被自动审批拒绝。没有执行或变相重试该导出；通过 SG 文件哈希和本地已有 Git 对象完成了比对。部署备份计划保留在 SG 本机。

## SG 首次切换验收

已将合并提交 `6981eef3c44693498fb29d69b975b77c97ea6f37` 推送到远端 main 并部署 SG。478 个工程文件哈希全部通过，实际 systemd unit、环境文件和通知配置未更改。SG 隔离全量回归为 **724 passed、10 skipped、1 条弃用警告，86.81 秒**；10 项跳过均因 SG 未安装 Node，已在本地执行通过。

备份位于 SG 本机 `/home/projects/quant-backups/audit-6981eef-20260905T163753Z`，共 275,401,615 字节，含 9 个数据库、1,516 个 JSON 元数据文件及代码、环境文件、实际 unit；1,575 个备份文件已校验哈希。SQLite 采用原生 backup 并通过 quick_check；DuckDB 在任务和 Web 停止后连同可能存在的 WAL 复制。旧不可变行情和研究产物保留原位。

两个 Web 恢复 active/running；11 个认证页面/API 全部 HTTP 200；两处未认证访问均 HTTP 401；运维 `/healthz` 为 `ok`，快照年龄约 20 秒，门槛 180 秒；杯柄发送仍关闭。恢复了原先暂停的 14 个 timer，所有 unit 的启用状态与维护前一致，一次性历史 timer 未重启。

现场存在的 broad rebase、盘中候选/监控、盘前准备/投递等失败记录，在维护开始前已经是 failed；没有清除历史失败来伪造健康状态。服务部署验收通过不等于这些业务任务的历史输入已修正。

FMP 新接口在 SG 实测可访问：NVDA 2024-06-06 至 2024-06-11 得到 4 条名义价格；AAPL 2026-08-24 至 2026-09-04 的 10 条 canonical 行情均可对齐名义价格。这是小样本权限/契约验证，不代表全市场覆盖已通过。

## 部署后补充修正：同日回填短路

检查历史重建入口发现：同日、同 Security Master 的旧 coverage 会直接 NOOP，即使缺少全历史名义价格。现已将复用条件收紧为已认证的 `unadjusted_close`、准确的 non-split-adjusted 来源和 `full_backfill_history` 范围；旧版本或仅新增月末名义价格的版本必须进入回填，校验损坏仍报错。

增加 3 项入口回归，验证旧版本、错误来源及部分名义价格不能短路为完成；全量 **737 passed、0 failed、0 skipped，28.50 秒**，两条现有弃用警告。结果见 `nominal_migration_full_suite.xml`。补丁将按精确提交同步 SG 后继续做重建验证。

补丁已以 `255e275` 推送并部署：SG 的受影响回归 **45 passed，4.36 秒**。持有广域生产锁时，仅替换回填入口和对应测试两个文件，备份于 `/home/projects/quant-backups/nominal-255e275-20260905T165257Z`；再次核对全部 478 个工程文件后更新部署标记，Web 未中断。

## 历史重建实跑

小样本候选为 **12 只证券、21,052 行、2019-01-02 至 2026-09-04**，所有检查 PASS：0 个别名缺口、0 个无效 OHLC、0 个重复键、0 个未来/非交易日记录，当前证券目标日覆盖率 100%。样本只生成候选，没有替换生产版本。

完整重建已于 **2026-09-06 01:04（Asia/Shanghai）** 通过已有 `quant-broad-provider-retry.service` 恢复入口启动一次性任务，持有 `.broad-production.lock`，CPU 上限 100%、内存软/硬上限 700/900 MiB、无 swap、供应商并发 2、最大运行时间 36 小时。未创建新周期性 timer，未改变通知配置，未重放模拟盘成交。

执行顺序由 `rebuild_history.py` 记录，实际脚本保存在 SG `/home/projects/quant-releases/255e275/rebuild_history.py`：

1. 原资源门槛（至少 350 MiB 可用内存、15 GiB 可用磁盘）。
2. 全量 `backfill_us_equity_coverage.py --publish`，固定目标日 2026-09-04，沿用已认证 Security Master；只恢复与新输入完全匹配的断点。
3. 绑定新 coverage 的 `build_us_liquid_pit.py --full-rebuild --publish`。
4. 绑定同一 coverage/PIT 的 `run_broad_factor_data.py --full-rebuild --publish --restart-after-partitions 1`，每个分片重新启动 worker 释放内存。
5. 正式宽基研究门槛和 shadow 检查；既有 PIT 行业历史不足仍保留阻断，不降低要求。
6. `run_factor_research.py` 对 SP500、NASDAQ100、MAG7 强制重建依赖研究及跨池结果。

任一阶段失败就停止后续阶段。逐阶段 stdout/stderr 位于 SG `/home/projects/quant/logs/nominal-migration-20260906/`；总报告位于 `/home/projects/quant/outputs/data_audits/broad_initial_rollout/target=2026-09-04/run=nominal-migration-20260906.json`。

01:09 时任务为 `RUNNING / US_EQUITY_COVERAGE_BACKFILL`，新方法 `BROAD_COVERAGE_V3_NOMINAL_PRICE`，7,986 只证券，已完成 1 个批次（SUCCESS，0 个别名失败），内存约 168 MiB。全量质量验收、PIT、因子和研究尚未完成，不能把旧数值标为已修正。本条是部署收尾时的运行快照，后续进展以 SG 报告与 checkpoint 为准。
