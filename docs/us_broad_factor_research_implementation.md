# 全美宽基因子研究实施记录

更新日期：2026-08-26
当前状态：首次正式链与人工验收已通过，`quant-us-equity-coverage.timer` 已启用。2026-08-20、21、24、25 四个连续 XNYS 交易日已通过精确影子核验，当前进度 4/5。`web_default_enabled=false` 继续保持，正式宽基置信研究仍只被 PIT 历史行业分类门槛阻断。

本文件记录 [`us_broad_factor_research_requirements.md`](us_broad_factor_research_requirements.md)
的实际落地和上线步骤。需求口径以需求文档为准。

## 1. 结论先行

| 阶段 | 代码状态 | 数据/生产状态 |
|---|---|---|
| Phase 0 供应商与容量 | 完成 | FMP 能力审计和 SG 合成容量实测完成 |
| Phase 1 Security Master | 已重新发布 | `PROSPECTIVE_ONLY` 台账、身份唯一性和质量门禁通过 |
| Phase 2 Coverage 与 PIT | 已正式发布 | 2026-08-25 coverage 与 PIT 版本严格绑定，全历史行情覆盖门禁通过 |
| Phase 3 宽基因子数据 | 已正式发布 | 8 因子、640 个月分片及 raw/clean/rank/percentile 哈希验证通过 |
| Phase 4 Web/API | 完成 | 全局证券搜索和宽基 adapter 已接入；默认切换开关保持关闭 |
| Phase 5 正式宽基研究 | 门禁完成、研究暂缓 | 当前行业为 latest-known，不满足严格 PIT，readiness 必须返回 `BLOCKED` |
| Phase 6 SG 影子 | 观察中 | 2026-08-20、21、24、25 连续通过，当前 4/5；timer 已启用，网页默认开关仍关闭 |

因此当前可以宣称“全美宽基基础数据链已进入生产影子观察”，但在 5/5 前不得打开网页默认开关，也不得发布宽基 IC、ICIR 或置信结论。第五个不同交易日只能由下一次正式日常链产生，不能重复登记 2026-08-25 或手工推进台账。

## 2. 已实现的数据链

```text
FMP current/delisted/profile/symbol-change
  -> Security Master generation
  -> US_EQUITY_COVERAGE DatasetVersion（月 Parquet）
  -> US_LIQUID_5M DerivedUniverseVersion（PIT membership + eligibility）
  -> factor_data publication（8 因子 x 月份 long Parquet）
  -> DuckDB 窗口函数计算完整截面 rank/percentile
  -> 因子数据页面与 CSV
```

四个身份必须同时一致：

```text
coverage version + Security Master generation
  + PIT universe version + factor-data generation
```

任一 manifest、membership、eligibility、Security Master 或因子分片哈希变化，Reader、API 和影子检查
都会 fail closed，不访问 FMP、不回退旧文件，也不把其他股票池的排名搬过来。

## 3. 关键实现

### 3.1 Security Master

- `src/data/security_master_store.py` 保存稳定 `security_id`、ticker 有效区间、分类历史和身份键；
- `scripts/build_security_master.py` 默认只生成候选，只有 `--publish` 才推进 DuckDB pointer；
- 同一 target session 重试会完整复读哈希后返回 `NOOP`；只有人工修复可用 `--force-publish`；
- MDB、AEVA、退市证券和 ticker change 都能按日期唯一解析；歧义直接拒绝。

真实候选曾得到 11,174 条证券、6,088 只当前普通股和 12,027 条 symbol history，峰值 RSS 约
302 MB。当前分类政策明确写为 `LATEST_KNOWN_BACKFILL_NOT_PIT`。

### 3.2 Coverage 与 PIT

- `src/data/broad_coverage.py` 将行情覆盖与比较池 membership 分离；
- 首次下载按证券批次/年份 checkpoint，正式读取模型压实为日历月分区；
- 日常 21 日 overlap 只重写受影响月份，其余月文件通过硬链接复用；
- 跨月时新月份即使在父版本中不存在，也会强制进入重建集合；
- `src/data/derived_universe.py` 按月末 ADV20、价格和资产类型构建 PIT 池，月中处理明确退出事件；
- PIT 发布会把每个 XNYS 交易日映射到最近完整快照，并用 DuckDB 校验当日成员行情覆盖率不低于
  95%；该检查不在 Pandas 中展开完整日频 membership；
- `scripts/build_us_liquid_pit.py` 同输入重试返回 `NOOP`，不同输入不得静默复用。

月分片是固定 SG 能承受每日增量的关键。若继续使用年度分区，8 月的一次供应商修订会改变整个
2026 年文件哈希，进而迫使八个因子重算年初至今；月分片把正常影响限制在 overlap 涉及的月份。

### 3.3 宽基因子数据

- `src/factors/broad_pipeline.py` 每次只计算一个因子、一个月份；
- coverage 内证券可保留 raw；只有当日 PIT 成员进入 clean 和排名分母；
- clean 继续使用既有去极值、行业/规模中性化和 z-score 合同；
- `src/factors/data_publication.py` 原子发布八因子完整集合及 preprocessing audit；
- 每个分片保存输入等价指纹。行情、membership 或分类没有变化的月份直接复用旧分片；
- rank 不复制进 SQLite，而是在查询时用 DuckDB 对完整有效截面计算；
- 正向和负向因子都保证 `rank=1` 表示按因子预设方向最优。

`scripts/run_broad_factor_data.py` 支持 checkpoint、`--generation-id` 恢复和 `--full-rebuild`。正式
同输入再次执行只做全分片哈希验证并返回 `NOOP`。

### 3.4 Web 与 API

- `FactorObservationReader` 对 SP500/NASDAQ100/MAG7 保留原 adapter，对 `US_LIQUID_5M` 使用 long
  Parquet adapter，返回相同领域对象；
- `/api/securities/search` 查询 Security Master，所以不属于三个指数的 MDB、AEVA 也能被找到；
- snapshot/history/export 均返回 data、universe、Security Master 和 factor generation 身份；
- 元数据页面认证 coverage manifest 和月分片 index，但不在每次 HTTP 请求中重新哈希五年所有行情分片；
  查询实际使用的因子分片仍逐文件校验，生产发布与每日 shadow 仍全量校验所有 coverage 子分片；
- 页面区分“有 raw”“进入当日 clean/rank”“有正式置信研究”三层能力；
- `data.broad_factor_data.web_default_enabled=false` 是灰度开关。影子期可以显式选择宽基池，但默认仍
  保持 SP500；五日验收前不得改成 `true`。

### 3.5 正式研究门槛

`src/factors/broad_research_gate.py` 和 `scripts/check_broad_research_readiness.py` 检查：

- 八因子完整且 target session 对齐；
- 每个历史截面达到最小样本；
- 至少 756 个可评价交易日；
- PIT 行业覆盖每天至少 95%；
- 分类政策必须属于 `PIT_EFFECTIVE_DATED`。

当前 FMP profile 只有最新行业快照，因此正确结果是退出码 2、状态 `BLOCKED`、原因
`PIT_CLASSIFICATION_POLICY`。这不是任务故障。读取基础损坏等基础设施异常使用退出码 3，systemd
仍会判定失败。尚未实现或启用绕过该门槛的宽基 confidence service。

## 4. 固定 SG 资源方案

现有 SG 是 2 vCPU、1.9 GiB RAM、无 swap。本方案不依赖扩容：

| 任务 | 运行方式 | systemd 上限 |
|---|---|---:|
| Security Master + coverage + PIT | 串行，月分片，11:30 SGT | `MemoryHigh=700M`、`MemoryMax=900M` |
| 八因子数据 | 一个因子一个月串行，复用未变化分片 | `MemoryHigh=700M`、`MemoryMax=900M` |
| readiness / shadow | 只读哈希和单次真实排名查询 | `MemoryHigh=450M`、`MemoryMax=600M` |

所有 BLAS/OpenMP 线程固定为 1；重任务使用同一个 `flock`；启动前要求至少 350 MB 可用内存和
15 GB 空闲磁盘。资源不足时保留上一正式 pointer，任务失败，不抢占 Web。

服务器 24 小时运行，所以不把任务挤在 07:15-10:30 的既有生产窗口。新链安排在 Tue-Sat 11:30
SGT，先运行 coverage 三阶段，成功后由 `OnSuccess=` 依次启动 factor data、readiness 和 shadow
检查。首次全量回填和方法级重建应在周末分批运行；日常只处理新交易日及 overlap 月份。

## 5. 运行与影子证据

新增结构化证据：

- `outputs/data_audits/broad_daily_pipeline/target=<DATE>/run=<ID>.json`：三阶段状态、耗时和峰值；
- `generation=<ID>/run_report.json`：因子计算/复用分片数、耗时和峰值；
- `outputs/data_audits/broad_shadow_observation.json`：按 target session 去重的影子台账；
- `outputs/universes/US_LIQUID_5M/research/broad_research_readiness.json`：正式研究门槛。

一次影子 PASS 同时要求：

1. 当日三阶段 pipeline 全部成功；
2. coverage 全部月分片逐文件 SHA-256 通过；
3. membership、eligibility 和 Security Master 哈希通过；
4. 八因子的 manifest、全部 Parquet 和输入指纹通过；
5. 实际执行一次完整截面的排名查询且有效样本达到门槛；
6. factor run report 与当前 publication ID 一致。

失败尝试写入 failures，但绝不计数。只有最近预期交易日也是 PASS，且尾部连续五个 XNYS session
都通过时，`ready_for_web_default=true`。检查器只报告资格，不修改配置。

## 6. SG 首次上线顺序

### 6.1 先备份和部署代码

备份代码、`data/catalog/quant.duckdb`、`data/lake`、`outputs/universes`、配置和 systemd unit。部署
时继续排除服务器上的 `data/outputs/logs/.venv`，不要用本地空目录覆盖生产状态。rsync 规则必须
使用根锚定的 `/data/`、`/outputs/`、`/logs/`；写成非锚定 `data/` 会把 `src/data/` 一并排除，
导致部署缺模块。

### 6.2 首次正式回填

在 `/home/projects/quant` 中依次运行：

```bash
.venv/bin/python scripts/check_broad_resources.py --json

.venv/bin/python scripts/build_security_master.py \
  --env-file /etc/quant/market-data.env --publish --json

.venv/bin/python scripts/backfill_us_equity_coverage.py \
  --env-file /etc/quant/market-data.env --publish --json

.venv/bin/python scripts/build_us_liquid_pit.py \
  --full-rebuild --publish --json

.venv/bin/python scripts/run_broad_factor_data.py --publish --json
```

回填中断后从输出的 run 目录恢复：

```bash
.venv/bin/python scripts/backfill_us_equity_coverage.py \
  --env-file /etc/quant/market-data.env \
  --resume-run-dir <RUN_DIR> --publish --json
```

不要并行启动两个 writer。首次回填失败时修复并恢复 checkpoint，不删除旧正式数据。

### 6.3 安装 root unit 模板

仓库中的 `-root.service` 是模板，安装时必须去掉文件名中的 `-root`：

```bash
install -m 0644 deploy/systemd/quant-us-equity-coverage-root.service \
  /etc/systemd/system/quant-us-equity-coverage.service
install -m 0644 deploy/systemd/quant-broad-factor-data-root.service \
  /etc/systemd/system/quant-broad-factor-data.service
install -m 0644 deploy/systemd/quant-broad-research-readiness-root.service \
  /etc/systemd/system/quant-broad-research-readiness.service
install -m 0644 deploy/systemd/quant-broad-shadow-observation-root.service \
  /etc/systemd/system/quant-broad-shadow-observation.service
install -m 0644 deploy/systemd/quant-us-equity-coverage.timer \
  /etc/systemd/system/quant-us-equity-coverage.timer

systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/quant-us-equity-coverage.service \
  /etc/systemd/system/quant-broad-factor-data.service \
  /etc/systemd/system/quant-broad-research-readiness.service \
  /etc/systemd/system/quant-broad-shadow-observation.service \
  /etc/systemd/system/quant-us-equity-coverage.timer
```

先手工启动一次并检查完整链，确认后才启用 timer：

```bash
systemctl start quant-us-equity-coverage.service
journalctl -u quant-us-equity-coverage.service -n 200 --no-pager
journalctl -u quant-broad-factor-data.service -n 200 --no-pager
journalctl -u quant-broad-research-readiness.service -n 100 --no-pager
journalctl -u quant-broad-shadow-observation.service -n 100 --no-pager
systemctl enable --now quant-us-equity-coverage.timer
```

### 6.4 五日切换

每天检查：

```bash
.venv/bin/python scripts/check_broad_shadow_observation.py --json --require-ready
```

前四天退出码 2 是正常的“仍在观察”。达到 5/5 后还要人工检查耗时、峰值内存、磁盘增长、Web
日志和 MDB/AEVA 页面。随后先备份 `configs/default.yaml`，再把：

```yaml
data:
  broad_factor_data:
    web_default_enabled: true
```

改为 `true`，校验 YAML 并重启 `quant-web.service`。不得删除旧 `US_LIQUID_5M`、JSON 或 Parquet。

## 7. 本地验收记录

2026-08-12 在 macOS 项目工作区完成：

- `python -m pytest -q`：`488 passed`；
- `python -m compileall -q src scripts`：通过；
- `configs/default.yaml`、`configs/research_universes.yaml` 解析：通过；
- `node --check src/webapp/static/js/factor_data.js`：通过；
- `git diff --check`：通过；
- 本地 `/research/factor-data`：旧正式池保持默认，宽基未发布状态明确、无静默回退、无控制台错误。

SG 同日验收：

- 备份为 `/home/projects/quant-backups/broad-migration-20260812T230832CST`；
- 5 个新 unit 已安装，`systemd-analyze verify` 通过，timer 保持 `disabled`；
- SG 完整测试 `488 passed`，资源预检和 DuckDB/SQLite 核验通过；
- 第一次 Security Master 发布 10,200 个证券，耗时 4 分 46 秒、峰值 526 MB；
- coverage 回填完成 4/79 批后发现 OCCIP 优先股误分类和身份键候选自检漏洞，已在美股盘中主动
  暂停，未发布 coverage；
- 修复增加紧凑优先股及 warrant/unit 分类、严格同 issue 归并、歧义键隔离、候选自身唯一性和
  跨 generation 幂等测试；修复后本地及 SG 测试均通过；
- Web 页面与 MDB/AEVA 搜索通过，宽基仍明确为 MISSING，没有静默回退。

因此 Linux unit 语义和代码部署已完成，但 SG 的正式 FMP 回填、完整资源峰值、磁盘增长和五个交易
日 shadow 仍未完成。

## 8. 当前门槛与下一步

截至 2026-08-14，Security Master 曾完成修复和正式发布，当时下一步是 coverage、PIT、八因子和
首日 shadow。2026-08-16 的更新审计又发现 ticker 区间冲突，当前实际门禁以第 11 节为准。
所有前置问题完成前保持：

- `web_default_enabled=false`；
- 正式 broad confidence 不发布；
- 新 timer 不在未完成首次回填时启用；绑定第一次错误主表的 coverage checkpoint 只保留审计，
  不得恢复或发布；
- 旧 07:15 动量用 `US_LIQUID_5M` DatasetVersion 继续独立运行；
- SP500/NASDAQ100/MAG7 原研究链继续按原时间表运行。

Phase 5 要继续，必须先获得可审计的历史 PIT 行业数据或形成新的明确数据源决策，不能通过关闭行业
中性化、降低覆盖率或把 latest-known 改名来放行。

## 9. 2026-08-13 至 2026-08-14 Security Master 修复

`2026-08-13 11:35 SGT` 进入安全窗口后，SG 先确认盘中动量进程已经退出、宽基 timer 仍为
`disabled`、coverage writer 未运行、文件锁可用；资源预检为 `PASS`，可用内存约 1,076 MB、可用
磁盘约 66.7 GB。旧检查点
`run=20260812T152208Z_57bca7cb` 仍原样保留 4 个成功批次，并未恢复。

随后在单线程 BLAS、`MemoryHigh=700M`、`MemoryMax=900M` 和独占 `flock` 下，从 FMP 重新抓取并
冻结 `target_session=2026-08-12` 的 provider source。候选运行
`run=20260813T033828Z_c7c84071` 得到 10,186 个证券、5,401 个活跃普通股，耗时约 319 秒，
systemd cgroup 峰值 444.9 MB，进程报告峰值 517.2 MB。OCCIP 在冻结源中已正确识别为
`PREFERRED`，没有进入普通股候选；`-WT/-WTS` 普通股误入数为 0；候选自身没有重复
`security_id`、剩余 identity-key 冲突或 ticker 区间冲突。

最初候选按设计 **失败关闭**，唯一失败项为 `security identity-key coverage below 100%`：覆盖率
`99.980365%`，10,186 个证券中有 2 个没有保留下来的可用身份键。进一步核查 SEC 原始披露后，
确认这不是两个需要拆分的巧合冲突，而是 FMP symbol-change feed 漏掉了两笔真实并购换码事件：

| 旧证券 | 新证券 | FMP 复用的键 | 处理结果 |
|---|---|---|---|
| `HSPT` | `SLBT` | `ISIN=KYG8191L1169`、`CUSIP=G8191L116` | SEC 披露合并完成，SLBT 于 2026-06-15 开始交易；作为同一证券的连续 ticker 区间处理 |
| `VACH` | `VRXA` | `ISIN=CH1476899161` | SEC Form 8-A 与新闻稿披露合并完成，VRXA 于 2026-06-11 开始交易；作为同一证券的连续 ticker 区间处理 |

修复没有降低 100% 门槛，也没有添加宽泛 fallback。`configs/security_master_corrections.yaml` 只登记
这两条经审阅事件，绑定精确 ticker、日期、名称、CUSIP/ISIN、活跃状态和 SEC 来源；任一供应商字段
漂移即拒绝构建。主要证据为 [HSPT/SLBT SEC 披露](https://www.sec.gov/Archives/edgar/data/2070534/000110465926073797/tm2617928d1_ex99-1.htm)、
[VACH/VRXA Form 8-A](https://www.sec.gov/Archives/edgar/data/2079109/000182912626006302/veraxabiotech_8-a12ba.htm)
和 [VACH/VRXA 合并新闻稿](https://www.sec.gov/Archives/edgar/data/2079109/000182912626006329/veraxabiotech_ex99-1.htm)。

修复后使用同一冻结源连续构建：

- `run=20260813T155959Z_7f000e3e` 与 `run=20260813T160315Z_9401eb5a` 均为 `PASS`；
- 10,184 个证券均有可用身份键，覆盖率为 100%，活跃普通股为 5,401；
- HSPT/SLBT 共享一个稳定 `security_id`，有效区间在 2026-06-15 切换；VACH/VRXA 在
  2026-06-11 切换；
- `master`、`symbols`、`classifications`、`identity_keys` 四张表内容与 Parquet SHA-256 完全一致；
- 为满足不可变合同，`master.updated_at` 改为由 target session 决定，真实运行时间继续记录在
  `audit.generated_at`；两次峰值内存约 292 MB。

发布前备份位于
`/home/projects/quant-backups/security-master-publish-20260814T000922CST`。正式 generation
`231b5b53d46a47d9a3a463cab6b06766` 已发布，target 为 2026-08-12，manifest SHA-256 为
`281e33a3d0351e6c87cc354a777836f56790e3022af54b2f732b31b7853db48d`。旧 generation 和绑定旧
generation 的 4 批 coverage checkpoint 均原样保留，未恢复、未删除。

## 10. 可恢复首次链与已安排窗口

新增 `scripts/run_broad_initial_rollout.py` 和 `quant-broad-initial-rollout.service`，与日常增量链分离：

1. 确认盘中动量、核心行情、研究和模拟盘服务均未运行，并执行资源门禁；
2. 发布最新 target 的 Security Master；
3. 从 2019 起回填 coverage，只恢复 target、主表 generation/manifest、方法版本、证券集合、ticker
   区间哈希和批大小完全一致的唯一 checkpoint；部分失败批次标记为 `PARTIAL` 并重试；
4. 发布或认证同输入的 PIT；
5. 生成兼容日常 shadow 的 pipeline 报告；
6. 八因子只恢复 coverage/PIT/Security Master/因子集合和全部输入哈希一致的唯一 generation；
7. readiness 只允许已知 `PIT_CLASSIFICATION_POLICY`/`PIT_INDUSTRY_COVERAGE` 阻断；
8. 完成全量子文件哈希、真实排名查询和首日 shadow。

服务使用单线程 BLAS、`flock`、`MemoryHigh=700M`、`MemoryMax=900M`，失败后 30 分钟重试，最多受
`StartLimitBurst=3` 约束。coverage 和因子 checkpoint 都显示真实完成数。运维站读取首次报告、
coverage 批次与因子分片 checkpoint，显示当前阶段和进度。

SG 最终回归为 `498 passed`，`compileall`、YAML 和 `systemd-analyze verify` 均通过；唯一 systemd
提示仍是腾讯云 `tat_agent` 的旧 `/var/run`，与 Quant unit 无关。持久 timer
`quant-broad-initial-rollout-scheduled.timer` 已安排在 **2026-08-15 11:35 SGT** 启动，服务器重启
后仍会补跑。该窗口使用 2026-08-14 周五完整收盘数据并拥有整个周末，不与盘中动量重叠。

首次链不会自行开启 `quant-us-equity-coverage.timer`。只有首日 shadow、MDB/AEVA、资源、日志和页面
全部验收后才人工启用日常 timer；`web_default_enabled` 仍须等待连续 5 个不同 XNYS 交易日通过。

## 11. 2026-08-15 至 2026-08-16 首次回填事故与当前门禁

首次链推进到 coverage 检查点 `47/78` 后不再更新。现网取证确认这不是正常慢任务：进程已运行约
13 小时，检查点约 10 小时没有推进；进程处于内存回收压力下的不可中断等待，DuckDB 正在对全部
分片执行一次大型全局校验。cgroup 长时间高于 `MemoryHigh=700M`，在 2 GB 主机上持续直接回收，
因此任务几乎没有有效进展。

服务已安全停止，没有推进任何正式 coverage pointer。旧 staging 完整保留在：

```text
/home/projects/quant/data/lake/staging/us_equity_coverage/asof=2026-08-14/run=20260815T034026Z_498e2876
```

检查点中的 78 个批次有 47 个成功、31 个 `PARTIAL`；共记录 75 条别名区间或供应商无行情失败，
涉及 40 个证券。它们包含两类不同问题：一类是优先股、票据等非普通股误入；另一类是 FMP 对部分
历史普通股/ADR 不提供完整退市历史。后者不能用空数据或当前 ticker 静默补造。

已部署的执行安全修复包括：

- 在任何全局 DuckDB 校验前先检查 alias/provider 失败，存在失败即 `ALIAS_INTERVAL_COVERAGE` 失败关闭；
- DuckDB 校验固定单线程、420 MB 内存上限和受控临时落盘，不再无界占用内存；
- checkpoint 记录当前阶段和逐批进度，首次编排把 stderr 直接送入 journal；
- 加强 `/`、`.` ticker 归一化、特殊证券识别、CIK 误合并防护、传递换码边处理和同一证券区间重叠检查。

使用同一冻结供应商源重新构建后，最新候选仍正确失败关闭。身份键覆盖率为 100%，但审计发现
30 组同一证券 ticker 区间重叠以及 1 条无效 `VAPE` 区间。典型冲突包括 `VIACA/PARAA`、
`UCBI/UCB`、`SPHA/AIFE/PGAC`、`COG/CTRA` 等；其中有些能由 SEC 文件证明是连续换码，另一些
仍缺可靠生效日期。正式 generation `4668478d8a9b4d64bb15317da90119cd` 作为既有生产证据保留，
但被更新的同日候选审计否决，不能继续驱动 coverage、PIT 或因子发布。

当前生产保护状态为：

- `quant-broad-initial-rollout.service=inactive`；
- `quant-us-equity-coverage.service=inactive`；
- 首次 one-shot timer 与日常 coverage timer 均为 `disabled`；
- `data.broad_factor_data.web_default_enabled=false`；
- shadow 为 `0/5`，失败日不计数；
- 主业务 Web、独立运维站和既有 SP500/NASDAQ100/动量任务继续运行。

继续历史宽基上线前必须做一个明确的数据决策：为缺失退市历史和换码生效日引入可审计的第二历史
供应商，或者把无法证明的证券标记为 `PROSPECTIVE_ONLY`、从 2019 历史宽基研究中排除。不得猜测
换码日期、降低区间唯一性门槛或恢复旧 `47/78` 检查点。

## 12. 2026-08-16 PROSPECTIVE_ONLY 决策与恢复上线

项目负责人批准采用 `PROSPECTIVE_ONLY`：FMP 无法证明激活日前历史的证券不得进入 2019 起的历史
宽基研究；仍在交易的证券只从 2026-08-14 起向未来摄取，已停牌或退市且历史不可验证的证券从
coverage 研究范围排除。未来接入第二历史数据供应商后，必须通过新版本和新审计重新评估，不能原地
改写本次结论。

实现增加第五份不可变 Security Master 产物 `research_history_policy.parquet`，与 `master`、
`symbols`、`classifications` 和 `identity_keys` 一起写入 manifest 并校验 SHA-256。公开配置
`configs/research_history_policy.yaml` 绑定精确 `security_id`、ticker、名称、状态、策略、生效日和
原因；任一供应商身份字段漂移都会失败关闭。第一轮批准台账共 62 条：

- 30 条 `PROSPECTIVE_ONLY`，从 2026-08-14 起参与未来行情摄取；
- 32 条 `EXCLUDED_UNVERIFIABLE_HISTORY`，不进入历史 coverage；
- 5 个旧回填失败身份已不属于最新宽基候选，只留在旧 checkpoint 审计，不污染当前台账。

`scripts/propose_research_history_policy.py` 只读取冻结候选、coverage checkpoint 和现有批准台账，
输出待审 YAML 到标准输出；它不能改配置、发布 generation 或移动指针。再次发现供应商历史缺失时，
脚本先应用现有政策得到与生产 Reader 相同的证券范围，再保留旧条目、合并新证据和原因码，避免把
已批准的排除项静默丢失。

冻结源候选 `run=20260815T182429Z_6a156cfc` 与
`run=20260815T182755Z_dfbd633e` 均为 `PASS`。两次构建的五张 Parquet 表行数和文件 SHA-256
逐项完全一致：10,512 条 master、10,849 条 symbol interval、10,512 条 classification、26,850 条
identity key 和 62 条 history policy；ticker interval conflict 为 0。40 票生产试跑覆盖全部 30 只
前瞻证券及 MDB/AEVA，返回 40/40、0 failure。

发布前备份位于：

```text
/home/projects/quant-backups/prospective-policy-final-20260816T022251CST
/home/projects/quant-backups/prospective-policy-publish-20260816T023411CST
```

正式 Security Master generation 为 `c61df53691f24bb6917a0776df4759a0`，manifest SHA-256 为
`f593b1d39d929ba09fec87d48f456f18367604ab79f19a8b70dfa0733937e304`。首次链随后创建全新 coverage
run `20260815T183958Z_e30c3c27`，选择 7,956 个证券、80 批。自动恢复审计明确拒绝旧 `47/78`
checkpoint：generation、manifest、证券数、universe 和 alias 哈希均不一致。旧 staging 未删除、未
改状态、未恢复。首次 coverage、PIT、八因子和首日 shadow 全部完成前，日常 timer 与网页默认开关
继续保持关闭。

第一次全范围扫描使用 run `20260815T183958Z_e30c3c27`，耗时 10,604 秒，写入 10,370,668 行、
640 个批次/年份分片，进程峰值约 334 MB、cgroup 峰值约 540 MB。80 批中 76 批成功，4 批因
THCB、RTPY、DMYI、RMRM 的历史接口稳定返回 0 行而 `FETCH_FAILED`；coverage 正式指针没有推进。
旧 staging 显示供应商曾把 MVST/AUR/IONQ/SEVN 的后继行情倒灌到这些旧身份，不能据此把旧身份与
后继证券重新静默合并。

首次精确恢复还暴露 `backfill_us_equity_coverage.py` 恢复分支在构造 progress 前引用未初始化
`batches` 的错误。修复把批次定义提前，并将严格 checkpoint 校验与进度恢复收敛为
`_prepare_resumed_checkpoint`，增加回归测试。部署后只重试 4 个 partial 批次，540 秒内四只再次
全部可重复失败，峰值约 331 MB。最终批准台账因此扩展到 **66 条**：30 条
`PROSPECTIVE_ONLY`、36 条 `EXCLUDED_UNVERIFIABLE_HISTORY`；配置 SHA-256 为
`11339e66b4d8d6ff9ad6eaaf4b15c97d4b40c0792aed90908c5e298b735166cd`。本地完整回归为
`523 passed`。

最终 66 条台账再次使用同一冻结 provider source 构建候选
`run=20260815T215704Z_506fb253` 和 `run=20260815T220128Z_09ccce60`。两次均为 `PASS`，五张
Parquet 表逐字节相同：10,512 条 master、10,845 条 symbol interval、10,512 条 classification、
26,850 条 identity key 和 66 条 history policy。发布前完整备份位于
`/home/projects/quant-backups/prospective-policy-final66-publish-20260816T063000CST`。

最终正式 Security Master generation 为 `fb434632cd434b9289b71453e774c68e`，manifest SHA-256 为
`31a39d2f3c2215eef434c5f1f1662ba0926f22f8d9908717a60447f54f06447e`。DuckDB 当前指针、manifest
登记值和五个发布文件已反向核验；五个文件还与冻结候选逐文件同哈希。新 coverage run
`20260815T221208Z_b1d33eaf` 选择 7,952 个证券、80 批，明确拒绝旧 47/78、40 票 pilot 和上一轮
7,956 票 staging，拒绝原因为 generation、manifest、universe、alias 及证券数合同不一致。

政策落地抽查显示：36 个 `EXCLUDED_UNVERIFIABLE_HISTORY` 身份在新 universe 和 alias interval 中
均为 0；30 个 `PROSPECTIVE_ONLY` 身份全部保留，`fetch_start` 均为 2026-08-14，早于生效日的区间
为 0。首次运行快照达到 8/80 时全部为 `SUCCESS`、alias failure 为 0，运维站已显示新 run；完整
coverage、PIT、八因子和首日 shadow 结论仍以最终审计报告为准。

## 13. 2026-08-16 Coverage 正式发布与 PIT 性能处置

最终源回填 `run=20260815T221208Z_b1d33eaf` 完成 80/80 批并写入 10,370,668 条原始行情。源数据
校验发现 1,108 条非正价格和 337 条 OHLC 边界不一致，因此该原始 run 按合同失败关闭，未直接
推进正式指针。修复过程没有改写源分片，而是逐一验证 640 个源文件哈希，从不可变源派生一份
可审计的坏条台账和精确补集。

正式 coverage 版本为 `ad5de5cfd10d47e2ae21364f1808248d`，target 为 2026-08-14，共
10,369,223 条有效行情、1,445 条隔离记录和 92 个自然月分片。隔离率为 0.0139335%，低于 0.05%
上限；目标交易日隔离证券为 0，正式行情中无无效数值或 OHLC 边界错误。关键哈希如下：

```text
bar_quarantine_sha256 = 081f7e715620f7e71a52102a96451d25843b91113b65fe2ed2a6f24b7b719255
bars_index_sha256     = 97234b6ac1855b099a58bd9990671724226eef94d8c646d81f035eb675413f3d
manifest_sha256       = 6fbe3bc28ac4e477b782fa9cc337a3618a75875b4c3f31bf6676d9b481c8b7c0
```

首次 PIT 构建暴露两类只影响执行时间、不允许降低正确性门槛的问题：全历史日级覆盖核验一次性展开
大连接，以及 Pandas 3/Arrow 字符串集合查找在 7 年月末资格审计中反复构造哈希表。代码现改为
按自然月做有界 DuckDB 覆盖连接、只读取 `date/security_id/ticker/close/volume` 五列，并在资格审计
前把身份字段固定为普通对象字典编码。专项测试和本地完整回归分别为 `15 passed`、`525 passed`。

SG 部署备份包括：

```text
/home/projects/quant-backups/provider-bar-quarantine-20260816T092821CST
/home/projects/quant-backups/bounded-monthly-compaction-20260816T102947CST
/home/projects/quant-backups/bounded-pit-coverage-20260816T122834CST
/home/projects/quant-backups/pit-dictionary-encoding-20260816T142917CST
/home/projects/quant-backups/pit-column-projection-20260816T145819CST
/home/projects/quant-backups/pit-object-string-hotspot-20260816T1522CST
```

截至 2026-08-16 15:28 CST，修复后的 `quant-broad-pit-continuation-v5.service` 正以单线程、
`MemoryHigh=700M`、`MemoryMax=900M` 和独占锁运行。现场调用栈已确认旧 Arrow set-lookup 热点消失，
进程保持约 98% CPU；但正式 membership、eligibility、历史逐日行情覆盖检查和 publication JSON
尚未产出，所以本阶段仍记为“运行中”，不得启用日常 coverage timer、不得计算首日 shadow。

## 14. 2026-08-20 身份增量、日更恢复合同与 FMP 阻断

PIT V2 已完成 target 2026-08-14 的正式发布，universe version 为
`3db1ed595a9a4dca98bf85fb9cad6797`。随后日更到 target 2026-08-19 时，Security Master 新发现一只
活跃普通股 NUR：FMP 不能证明 2026-08-14 以前的历史，只能观察到政策生效日前后极短行情。项目按
既有 `PROSPECTIVE_ONLY` 决策把它作为第 31 条前瞻证券，从 2026-08-14 起摄取，没有补猜历史。
公开台账现为 67 条，其中 31 条前瞻、36 条历史排除，配置 SHA-256 为
`308b8714d10f3ec84bc8232dcdd0405d0753004ae090b309d72d4b2eaaa211c4`。

同一冻结 FMP source 连续构建的两份候选五表文件逐字节一致，全部质量门禁通过。当前正式 Security
Master generation 为 `559f310170984b67bcee18d0f12c44dc`，manifest SHA-256 为
`a329cb8ec5583433686b5805bf5448a203d44ce385e435d67dea51742703c0d7`；共有 10,577 个证券、
5,351 个活跃普通股，身份覆盖率 100%。发布前备份位于：

```text
/home/projects/quant-backups/nur-policy-publish-20260820T1405CST
```

增量 coverage 首先补齐了新 Security Master 相比正式 coverage 多出的身份历史。34 个 identity delta
共取得 29,829 条历史记录，`alias_failures=[]`、`alias_fallbacks=[]`。旧实现随后每次重抓 21 个自然日
内的 19 份 EOD bulk，触发 FMP 429 和长时间读取超时。FMP 官方把 EOD bulk 定义为按交易日调用一次、
按 `(symbol,date)` 入库，因此生产实现已改为：

1. 身份增量用 canonical 单票历史接口补到父 coverage target；
2. EOD bulk 只抓父版本之后尚未发布的 XNYS session；
3. 两个来源日期不重叠，冲突不能被 `drop_duplicates` 掩盖；
4. 身份历史和每个 EOD session 都写入绑定 parent version、Security Master generation/manifest、
   target 和 session 列表的 provider cache；
5. 每个缓存目录原子发布，并在恢复时复验合同、日期、行数和 Parquet SHA-256；
6. bulk 请求间隔固定为 10 秒，单请求最长 60 秒、最多 6 次；任何部分失败仍使整次发布失败。

当前精确缓存合同为 `b4a378e25ac74347964f11cccc777d164673295e72261caa41c216a1c171c6fd`。其
`identity_delta/frame.parquet` 已成功保存 29,829 行并有独立 manifest；下一次只剩
2026-08-17、2026-08-18、2026-08-19 三份 bulk，失败后不再重复 34 组身份历史请求。旧 cache、
失败报告和 staging 均保留，没有删除或改写。

2026-08-20 的 v8、v9、v10 均在 `US_EQUITY_COVERAGE` 阶段失败关闭。v10 报告为：

```text
outputs/data_audits/broad_daily_pipeline/target=2026-08-19/
  run=20260820T063540Z_c122abe8.json
```

SG 日志对 2026-08-17 EOD bulk 记录了五次 60 秒读取超时和一次 FMP `502 Bad Gateway`；开发机用
同一 stable endpoint 的单次只读请求也返回 502，确认是供应商端异常，不是 SG 内存、CPU、磁盘或
锁死。v10 峰值内存约 316 MB，远低于 `700M/900M` 合同。此次恢复机制部署备份为：

```text
/home/projects/quant-backups/coverage-monthly-validation-20260820T1327CST
/home/projects/quant-backups/coverage-identity-delta-20260820T1340CST
/home/projects/quant-backups/coverage-source-boundary-20260820T1350CST
/home/projects/quant-backups/nur-prospective-policy-20260820T1400CST
/home/projects/quant-backups/eod-bulk-resume-20260820T1418CST
/home/projects/quant-backups/provider-cache-v2-20260820T1430CST
/home/projects/quant-backups/append-only-bulk-20260820T1435CST
```

本地完整回归为 `533 passed`；SG 针对 provider cache、coverage 和 FMP 的回归为 `38 passed`。
供应商恢复前不得发布 coverage、不得手工推进 PIT/八因子或计入 shadow。当前正式 coverage 仍为
`ad5de5cfd10d47e2ae21364f1808248d`，日常 timer 仍 disabled，`web_default_enabled=false`，
shadow 为 0/5。

供应商冷却后的本地探测仍未在 30 秒内成功。SG 已创建一次性 transient timer
`quant-broad-provider-retry.timer`，计划于 **2026-08-20 15:53 CST/SGT** 运行同一 target 和 exact
cache。它不等同于正式日常 timer；成功后仍须完成人工 coverage/PIT 哈希验收，才能继续八因子、
readiness 和首日 shadow。

同日 14:57 CST 再次验收既有业务链。核心 `US_LIQUID_5M` 已独立发布 target 2026-08-19、版本
`839aa104e09249a988c40afcb6949254`，目标日覆盖率 100%。生产盘中候选入口的无发送预检读取
2,940 只合格股票，2,940 只均有 2026-08-19 完整日线，并生成 375 个候选；盘前动量入口也成功
绑定同一版本与同一 source session。Discord 三路路由检查为 `ok`。因此清除了盘中监控和盘前摘要
服务的旧 systemd failed 标志，但没有立即启动或补发；两者继续等待 21:20 timer。该恢复不改变
宽基 FMP blocker、正式 coverage 指针、0/5 shadow 或任何上线开关。

15:54 CST 的一次性恢复任务成功取得 2026-08-17、18、19 三份 EOD bulk，并发布 target
2026-08-19 的正式 coverage 版本 `74ab17464aff4156becdc0416580c018`：10,389,366 条有效行情、
7,960 个证券，target coverage 为 99.686247%。34 组 identity delta 命中精确缓存；供应商三份文件
各有 1 条无 symbol 记录被审计排除，另有 2 条坏行情进入 quarantine，没有静默写入。任务随后进入
基于该 coverage 的全量 PIT 构建，FMP blocker 已解除，但整条链尚未结束，shadow 仍为 0/5。

## 15. 2026-08-21 跨代次 PIT 中断修复与最新交易日续跑

`quant-broad-provider-retry.service` 并非被 FMP、OOM 或磁盘故障中断。coverage 已在 77 秒内发布，
随后 PIT 子进程持续运行，最终被 unit 的 `TimeoutStartSec=2h` 发送 `SIGTERM`；服务峰值内存
748.8 MB，低于 `MemoryMax=900M`，内核日志没有 OOM 证据。根因是旧 PIT 绑定 Security Master
`fb434632cd43...`，新 coverage 绑定 `559f31017098...`，旧代码仅按目标日期判断增量资格，错误地
允许跨 Security Master 代次滚动旧 membership/eligibility。

PIT 构建器现要求 Security Master generation 和 manifest SHA-256 同时精确相等才允许增量；身份
权威发生变化时自动执行全量 PIT 重建，不复用旧资格状态。脚本同时输出 `PIT_STAGE` 阶段日志，明确
区分输入认证、候选生成、全历史逐日行情覆盖门禁和不可变发布。修复部署备份为：

```text
/home/projects/quant-backups/pit-rollforward-fix-20260821T122415CST
```

修复后的 PIT-only 重建绑定 coverage `74ab17464aff4156becdc0416580c018`，85.377 秒完成并发布
universe `3f719706b26545a9b841500569cce066`，八项质量检查和三个文件哈希全部通过。随后正式日更链
推进到最新可发布交易日 2026-08-20：coverage `b12824a4bcba41aeb6e122208de860a8`、PIT
`b3fd075787524b38ad21751408642585`、Security Master `787de11c214844b18a7f81ea7e0aa5e3`。
整条日更约 8 分钟，峰值 701.7 MB、无 swap；PIT 全量重建本身为 76.43 秒。

八因子正式 generation `bab021a29e7547f0a95e2963d96bd067` 已启动，共 640 个因子/月分片。
systemd unit 现固定传入 `--auto-resume`：只恢复 coverage、PIT、Security Master、因子列表、开始
日期和旧 publication 全部不可变输入都精确一致的唯一 checkpoint；不允许猜测恢复或跨版本复用。
日常 coverage timer 和 `web_default_enabled` 在首次发布及 5/5 shadow 完成前继续保持关闭。

## 16. 2026-08-21 FMP 非交易日行情隔离与首日影子验收

八因子 generation `bab021a29e7547f0a95e2963d96bd067` 完成 640/640 后被发布门槛主动拒绝，
不是进程卡死或服务器 OOM。现场交叉检查发现 FMP 单票历史接口在正式 coverage 中混入 424 条
非 XNYS 交易日记录，分布在 278 个自然日和 7 个证券身份：WLL 188 条、JDZG 88 条、UOKA 73 条、
AVDL 72 条，AHL、BEP、QVCG 各 1 条。尤其 QVCG 的 2026-08-09 周日记录在宽表中插入一行只有
单票有值、其余股票均为空的日期，令后续 20 行换手率窗口只剩 19 个有效交易日，最终造成
TURNOVER 最新 raw/clean 覆盖率为 0。其余周末记录与动量、波动率、反转因子中少数证券的
`unexplained_clean_disappearance` 完全重合。

修复没有删除、覆盖或改写任何正式版本、失败 generation、原始 staging 或隔离台账。生产合同新增
三道门槛：摄取质量拆分把非交易日记录标记为 `NON_XNYS_SESSION`；coverage publication 必须通过
`xnys_session_calendar`；因子输入必须只包含 XNYS session，输入指纹升级为
`BROAD_FACTOR_INPUT_V2_XNYS_ONLY`，checkpoint 身份也绑定该方法。这样旧 V1 的 640 个失败分片不能
静默复用。日常增量、首次回填和从已发布版本派生修复都使用同一日期合同。

修复候选逐一验证 92 个正式月分片。原 coverage 10,400,409 行中隔离 424 行，保留 10,399,985 行；
同时完整继承首次回填的 1,445 条、8 月 19 日的 2 条和 8 月 20 日的 10 条既有隔离记录。累计隔离
台账为 1,881 行，账目满足 `10,399,985 + 1,881 = 10,401,866` 条完整供应商血缘，隔离率
0.0180833%，目标交易日隔离率为 0。正式修复版本如下：

```text
coverage_version       = 5ed0bc1f4b104e4f8b85256f15efba45
coverage_manifest_sha  = 6b791bfc95f8199d4909c114e4aa3cde570f71b980965646dc135cc2826a33ad
bar_quarantine_sha     = c5048e83f49dc14c12c2e657a8a54ab4adf22cbd638fe5d3f074e0c5a35d5d21
accepted_rows          = 10,399,985
cumulative_quarantine  = 1,881
off_xnys_session_rows  = 0
```

绑定新 coverage 的 PIT 做了全历史重建，76.617 秒后发布 universe
`8b37e3ec99eb46d8b2d52a1a54808690`；membership 223,085 行、91 个快照、当前成员 2,778，
全历史逐日行情覆盖门槛和所有文件哈希均通过。随后八因子从 V2 输入重新计算 640 个分片并发布：

```text
factor_generation      = 844e6a7a8bd642a0a0466bfb137529cf
factor_publication_id  = de5b2119-afc6-412f-b9a1-e9d3fe5833a2
factor_manifest_sha    = da16f7becfda1d9f94fec70b77e96c977c855f90889ca8402eeef82c4e143ae2
factor_count           = 8
partition_count        = 640
```

因子服务历时约 1 小时 6 分，systemd 峰值 702.2 MB、swap 0；每个分片后重新执行进程，避免 2 GB
主机长期持有 Pandas/Arrow 分配页。MDB、AEVA 的真实 `MOM_6M` 查询均返回 159 个交易日、151 个
有效排名日，最新有效日为 2026-08-20。完整分片哈希、coverage/PIT/Security Master 绑定和资源检查
均通过。本地完整回归为 `556 passed`，SG 完整回归为 `526 passed`。

readiness 当前只保留已知的 `PIT_CLASSIFICATION_POLICY` 和 `PIT_INDUSTRY_COVERAGE`，不含基础设施
或版本 blocker。2026-08-20 已记为首个通过的 shadow，进度 1/5、剩余 4 个不同且连续的交易日；
`web_default_enabled=false`。当时持久 `quant-us-equity-coverage.timer` 仍为 disabled。事后复盘确认，
把 5/5 门槛用于阻止日常 timer 是错误的控制状态：5/5 只约束网页默认开关；首日完整链和人工验收
通过后，日常 timer 必须立即启用，否则后续四个交易日根本不会产生。

本次部署与发布备份：

```text
/home/projects/quant-backups/xnys-calendar-contract-20260821T1450CST
/home/projects/quant-backups/xnys-calendar-publication-20260821T1452CST
```

## 17. 2026-08-24 第二交易日影子与资源事件

首日完整链和人工验收通过后，正式启用
`quant-us-equity-coverage.timer`；时间表为 Tue-Sat 11:30 SGT，
`Persistent=true`。由于该 timer 以前从未启用，systemd 没有旧的漏跑时间戳，
本次在非盘中窗口一次性启动相同的受限 service 补跑 2026-08-21，此后交由 timer。

2026-08-21 完整生产绑定为：

```text
security_master_generation = b02c753c82674e8daee356871368efe6
security_master_manifest   = 05dbdd87cf3a26212f4642871e2d215b566e8d251db6a36327d7e32225d08476
coverage_version           = a5e598dd50fa454d88b9d0764924346c
coverage_manifest          = b21448fbbc0273000b0f1b6aa90c32f98a58f5a33c8776fd9ac5b23d63035a58
universe_version           = 8312749ec0164208b2dd630588acd068
membership_sha             = 25f609e2a50504bcf46a7c9ad0b273ab31a733f48444024c60fa86a5cd7af614
eligibility_sha            = 8c518751de665ed550d4b6e5cfc849e7b19ea5d010cd4d2b267040656e8bc3ee
factor_generation          = 2ff7721bcd814b66abd71248454d1583
factor_manifest            = e6f876ad3705064df66aae03b461c8f0faf5f80bcd2e435fb9517e15dc6a473a
factor_publication_id      = 11923d7f-853e-4ca6-b6cd-05f2400126ff
```

daily pipeline 历时 495.26 秒，报告峰值 665.051 MB；systemd 记录该 service 峰值
701.8 MB、swap 0。FMP EOD bulk 返回 23,905 条源记录，其中 1 条没有 symbol，
已按 provider invalid identity 隔离，没有进入正式 coverage。PIT 全历史日行情覆盖
门禁通过，当前成员 2,778。

由于 Security Master 正式版本变化，因子输入指纹要求 640 个月分片重算。因子
service 总 CPU 时间 1 小时 8 分，systemd 峰值 706.2 MB、swap 0；8 因子、640 分片
和最终 publication 均通过。readiness 只保留预期的
`PIT_CLASSIFICATION_POLICY`/`PIT_INDUSTRY_COVERAGE`，shadow 将 2026-08-21 记为 PASS。
现在连续通过日为 2026-08-20、2026-08-21，进度 2/5，剩余 3 日；
`web_default_enabled=false`。

完整链落盘后执行 HTTP 页面验收时，SG 开始无法及时响应 SSH banner、主站和
独立运维站。重启后读取上一启动周期 kernel journal，已经确认根因是全局 OOM，而不是
Parquet 写回或磁盘 I/O：2026-08-24 12:09:51 CST，`quant-web.service` 中的 Python 进程
达到约 1.66 GB anonymous RSS，在 1.96 GB、无 swap 的主机上触发 OOM killer；当时
`dirty=0`、`writeback=0`，没有 hung task 或块设备错误。

触发请求是宽基单股历史 API。旧实现先对所选因子的全部 80 个月分片、约 928 万行执行按日
窗口排名，最后才筛选 MDB/AEVA；DuckDB 无查询内存上限，主 Web 也没有 cgroup 内存上限，
因此一条请求占满整机。修复后单股历史逐月计算同一套横截面排名，DuckDB 查询上限为 192 MB；
主 Web 增加 `MemoryHigh=420M`、`MemoryMax=600M`、`MemorySwapMax=0` 和
`OOMPolicy=stop`。即使以后再出现异常查询，也应只让 Web 被 systemd 重启，不能阻断 SSH、
运维站和定时任务。

影子观察停在 1/5 的根因也已通过 systemd 时间线确认：timer 启用软链接直到
2026-08-24 09:32:10 CST 才创建，此前没有 timer journal。首次脚本明确不会自动启用日常 timer，
而当时 `configs/operations.yaml` 又设置 `enabled_expected=false`，使 watchdog 主动忽略 timer
disabled/inactive 告警；文档同时混淆“5/5 后打开网页默认开关”和“首日后启用每日生产”两个门槛。
现已将运维期望改为启用，后续 timer 关闭或 inactive 会直接产生 systemd 运维告警。

生产部署后的回归结果：SG 完整测试 `527 passed`；MDB 全历史 `MOM_6M` 返回 1668 行，
耗时 14.26 秒，AEVA 返回 1630 行，耗时 14.37 秒。重复请求后主 Web 的 cgroup 峰值固定在
440,950,784 bytes（约 420.5 MiB），`NRestarts=0`，没有逐次增长。MDB 和 AEVA 最新历史行的
排名/百分位分别与 2026-08-21 日期截面逐值一致，证明分片执行没有改变横截面口径。修复备份：

```text
/home/projects/quant-backups/web-oom-shadow-root-cause-20260824T123159CST
```

当前影子台账仍为 2026-08-20/21 两日通过、2/5、剩余 3 日；`--require-ready` 退出码 2 是正确的
“仍在观察”。下一次 `quant-us-equity-coverage.timer` 计划于 2026-08-25 11:31 SGT 左右运行，
`web_default_enabled=false` 未改变。

## 18. 2026-08-25 第三日因 Security Master 身份漂移阻断

11:31 SGT 的 target `2026-08-24` 日常链在第一阶段 Security Master fail-closed，30 分钟后的
systemd 自动重试以同一错误再次失败。两次都没有进入 coverage、PIT、八因子或 shadow，
因此 2026-08-24 不计入观察，台账仍为 2/5。

失败对象是 `sec_5cba73738dbb59188a27c25dbaedf178`。研究历史政策预期它为活跃的 GRML / Greenland
Mines Ltd.，新候选却把该旧 security_id 解析为不活跃的 KLTO / Klotho Neurosciences, Inc.。
不可变 FMP 源同时给出同一 CIK `0001907223` 的两条普通股 profile：GRML 活跃、CUSIP
`49876K202`；KLTO 不活跃、CUSIP `49876K103`，并给出 2026-03-12 的 KLTO -> GRML 事件。

SEC 官方 CIK 提交索引确认 Greenland Mines Ltd. 的 former name 是 Klotho Neurosciences, Inc.；
2026-03-16 8-K 又明确说明这只是公司名称和代码变更、GRML 自 2026-03-12 开始交易，而且普通股
CUSIP 保持不变。FMP 的两个不同 CUSIP 因而与官方连续性证据冲突。当前 selector drift 门禁正确地
阻止了自动改写身份，不能直接把政策中的名字或 security_id 改成候选值。

本次冻结源必须保留：

```text
outputs/data_audits/security_master_candidates/asof=2026-08-24/run=20260825T033103Z_5dfd58eb/provider_sources
outputs/data_audits/security_master_candidates/asof=2026-08-24/run=20260825T040524Z_cf6bd73a/provider_sources
```

下一步需扩展严格 corrections 合同，允许在 SEC 明确证明证券连续、但 FMP issue identifier 漂移时，
登记带来源的 provider identifier override；随后用同一冻结源双重构建，验证四表精确幂等、旧
security_id 连续、无并行 share-class 误合并，再原子更新研究历史政策并补跑 target 2026-08-24。
在这些门槛完成前不得降低校验、不得计入失败日，也不得打开网页默认开关。

## 19. 2026-08-26 标识纠正、第三日恢复与下游统一读链

GRML/KLTO 事故已经按 `SAME_LISTED_ISSUE` 解决，不是通过修改历史政策、放宽身份覆盖率或猜测
CUSIP 完成。`configs/security_master_corrections.yaml` 的 schema v2 新增
`reviewed_provider_identifier_conflicts`：规则必须精确匹配生效日、前后 ticker、名称、资产类型、
交易所、CIK、FMP 原始 ISIN/CUSIP、挂牌日和活跃状态，并附 SEC 一手来源。构建器只授权这一条
证券连续性边；FMP 返回的两个冲突标识仍原样保存在 profile/alias 审计中，不会被改写成虚假的
一致值。

同一份冻结 provider source 连续构建两次后，Security Master 四张表和 manifest 哈希精确一致，
随后正式发布 target 2026-08-24：

```text
security_master_generation = 1e5e249c62424fc1ad679f3d70f179fc
security_master_manifest   = 8e132f61028493bdfc35efb4db9fb54fc61e254ddd4313942a5bbce537f3fe2c
coverage_version           = 77cfefacab4a417cbec8d681bed6e201
coverage_manifest          = 634107dfaf2ca5d50cb809b4519f0951a75edb27f2fc11d0af76ae4fad881b48
universe_version           = 19fd8dc8fee24d11bd1869b4276505b2
factor_generation          = 0fd93177f78444fc981c448d603fb437
factor_publication_id      = 49d02d27-8dcc-401d-b9e8-c5f06b184487
factor_manifest            = 87a10b2c4e407ba2768d95e68429b870ac38128aa4418d46bbe8e23e67e8b46b
```

8 因子 640 个分片、全 coverage child hash、PIT membership/eligibility、MDB/AEVA 查询及版本绑定
均通过；readiness 只保留预期的 PIT 行业历史 blocker。2026-08-24 因此成为第三个连续 PASS，
shadow 为 3/5，`web_default_enabled=false`。

本次还完成了动量数据消费者迁移。旧 `refresh_us_active.py` 发布的短历史
`US_LIQUID_5M` 不再是当前数据源；新的只读适配器把 `US_ACTIVE` 解析为：

1. `US_EQUITY_COVERAGE` 的不可变父行情和认证价格语义；
2. 同一 target 的 `US_LIQUID_5M` PIT membership/eligibility；
3. 精确绑定的 Security Master generation/manifest；
4. membership、eligibility 和 PIT manifest 三个 SHA-256。

动量候选只从 PIT 当日成员读取股票，SPY/QQQ 等 ETF 只从父 coverage 作为市场基准读取，不进入
股票排名。盘前板块轮动的 benchmark 也改读 `US_EQUITY_COVERAGE`。旧短历史 publication、原始
文件和运行记录保留只读，日常 timer 在新读链真实验收后关闭。

核心 SP500/NASDAQ100/MAG7 日更遇到 `non-uniform adj_close/volume revision` 时，严格语义门禁
要求完整历史重建。这不是网络重试问题。新增 `scripts/run_core_market_data.py` 只在**全部失败池**
都明确属于该语义漂移且错误要求 full rebuild 时，才对失败池执行 `--force --full-rebuild`；FMP
timeout、PIT/hash 错误或混合故障继续失败关闭。子任务日志实时进入 journal，报告只保留有界尾部；
日常 unit 使用 4 workers、单线程 BLAS、`700M/900M` 内存边界和禁止 swap。

## 20. 2026-08-26 第四日、业务链恢复与 FMP 精度边界

target `2026-08-25` 的正式宽基链已通过，当前绑定如下：

```text
security_master_generation = 1953abeff75c402a9d363413f6c7978b
security_master_manifest   = 8e243a7e70493590366ff9389501c7ea82d471647b7e39e6d2059d899c65fc1e
coverage_version           = b91499501659453abedf008290e95fea
coverage_bars_index_sha    = b82c7130179fc27f2c6fc3d03235e4349fac0653e9a08af9515b86b9acee4b18
universe_version           = ef508d571b76485c86eef744e6696a35
factor_generation          = 9eadbfad7bc54150a738b5d4a4b5c9c1
factor_publication_id      = 059bdcd1-ffc4-4ca7-9c0f-a55af6931924
```

8 因子、640 个分片、coverage child hash、PIT membership/eligibility 和真实排名查询均通过。
影子日期为 2026-08-20、21、24、25，当前连续 4/5、剩余 1 日。资源门禁为 PASS，可用内存
1,156.3 MB、可用磁盘 46.8 GB。下一次 11:30 SGT 日常链才有资格产生 target 2026-08-26 的
第五条观察；`web_default_enabled=false` 未修改。

同日补齐两个与宽基共用数据基础设施的生产边界：

- 自定义 Watchlist 增量遇到 `non-uniform revision` 时，缺数 worker 只在错误同时明确要求
  `full rebuild` 时重建该专属股票池。FMP 超时、身份冲突、PIT/hash 或质量门禁不得进入恢复分支。
- FMP 的 `adj_close` 在部分证券上以美分精度发布，而执行 `close` 可有更细小数。直接反推现金分红
  会产生约正负 0.005 美元的伪事件。模拟盘改为按半美分输入量化误差做区间运算：零落在区间内即
  归零；整个区间仍为负则继续硬失败。6 股全历史 483 个负点和 498 个正点均属于量化噪声，未生成
  虚假现金事件。

Watchlist 正式重建版本为 `93eb4878bc4b4e0b9829fbf690bc39f4`，6/6 股票、7,496 行、目标日覆盖
100%。模拟盘随后绑定该版本成功运行；相同决策日二次执行没有重复订单、成交或现金事件。部署备份：

当前生产代码已在 SG 以 `MemoryHigh=550M`、`MemoryMax=700M`、`CPUQuota=50%` 的临时验证单元
完成全量回归：`608 passed, 1 warning in 108.42s`，峰值内存 281.1 MB、未使用 swap。唯一警告为
FastAPI TestClient 弃用提示，不影响生产行为。

```text
/home/projects/quant-backups/request-worker-semantic-recovery-20260826T214500CST
/home/projects/quant-backups/paper-dividend-precision-20260826T234000CST
```

## 21. 2026-08-27 五日完成与网页正式启用

第五个不同 XNYS 交易日 `2026-08-26` 已通过完整生产链与影子核验。连续 PASS 日期为
2026-08-20、21、24、25、26，台账状态为 `READY`、`5/5`、剩余 0。当前不可变绑定为：

```text
security_master_generation = 6706c172a3f04d9bb1b92cbb8c76fdcf
security_master_manifest   = 6c586ae8635678c64f060e01b16b379b6b6593696a1f4aebbbabe78402cdc9d6
coverage_version           = e4963942c52a4031bf31fba475753e63
coverage_bars_index_sha    = 21da6c0a8f9bcc6d4167a8e6ecdd0965cd50823a416388948f48b589996625ec
universe_version           = ded547cbef6b446399a7a74cf39c482c
membership_sha             = 203b97d91255e5a1b5ce76f32958fc50259db766e870fec003411b56c5ede262
eligibility_sha            = ff941757c2e482ac5ff2da0b99f6a1fef74c902a51e83de7480d911de9596254
factor_generation          = 1a60b302fa474a589d4a73fd9fab2555
factor_publication_id      = 1e6d8c6a-e8ff-47e7-922f-9be03bd3e84a
factor_manifest_sha        = 6257ac84e13c842b2a08283f610e79d9e181ea2dd2c0cc9ff145dbfecf5a0332
```

当日 coverage/PIT 于 14:06 SGT 完成后，已排队的板块研究和八因子任务同时启动。板块研究先以
read-only 连接持有 DuckDB 共享锁，而八因子的 `published_generation()` 在只读查询前错误调用
`initialize()` 尝试获取写锁，导致两次失败。修复包括：Security Master 发布读取不再初始化或写库；
八因子在板块研究之前执行；核心行情、宽基生产和板块研究共享 `.broad-production.lock`。备份为：

```text
/home/projects/quant-backups/duckdb-scheduling-fix-20260827T1530CST
```

修复后八因子从已认证的 1/640 checkpoint 继续，最终发布 8 因子、640 分片；systemd 峰值内存
714.5 MB、无 swap。readiness 仅以预期的 PIT 行业历史 blocker 返回退出码 2，shadow 自动 PASS。
配置备份位于 `/home/projects/quant-backups/broad-web-enable-20260827T1715CST`，随后将
`data.broad_factor_data.web_default_enabled` 设为 `true`。SG 完整回归为
`609 passed, 1 warning`；MDB 与 AEVA 的 MOM_12M 历史查询均返回 5/5 有效交易日并精确绑定上述版本。

因此全美宽基因子数据浏览现已正式启用；宽基 IC、ICIR 和置信结论仍被
`PIT_CLASSIFICATION_POLICY`/`PIT_INDUSTRY_COVERAGE` 阻断，二者不是同一个上线门槛。

## 22. 2026-08-28 核心全量重建内存退化修复

target `2026-08-27` 的核心三池增量认证分别在 A 的 open、ABNB 的 volume 和 AMZN 的 volume
发现非均匀历史修订，严格门禁要求 full rebuild。MAG7 与 NASDAQ100 已于 08:25、08:28 SGT
发布；SP500 的 621 只证券在 08:37 已完成 FMP 抓取，raw ingestion
`8557e15a063843c7ba09e2bba789b761` 无失败证券，但随后长时间停留在本地数据整理。

根因不是 FMP、磁盘或 DuckDB 锁。full rebuild 使用无类型空 parent 与 fetched bars 执行
`pd.concat`，把约 98 万行的数值列从 `float64` 转成 Python `object`。现网原生栈停在
`array_astype -> PyFloat_FromDouble -> PyObject_Malloc`，服务内存约 815 MB，cgroup
`memory.events.high` 达 80,863 次，形成持续对象分配和内存回收。修复后空 parent 直接复制
fetched frame，只有 parent 和 fetched 都非空时才 concat，因此保留原始数值 dtype；回归测试明确
验证 OHLCV 六列仍为 numeric。

该修复不降低历史语义门禁，也不接受有差异的旧数据。A/ABNB/AMZN 仍必须走正式 full rebuild；
改变的只是重建内部的内存表示。部署前备份位于
`/home/projects/quant-backups/core-rebuild-observability-fix-20260828T1205CST`。截至本节记录时，
旧进程尚在等待自然完成或 systemd 超时，修复后的重试和 target `2026-08-27` 四层发布仍需以正式
publication、哈希和质量门禁验收，不能手工标记成功。

## 23. 2026-08-28 target 2026-08-27 恢复完成

修复后的核心 SP500 full rebuild 已正式完成，而不是通过修改状态或复用旧 publication 绕过门禁。
SP500 版本为 `e151b46c1d814d93a9d631dafc730ab1`，共 980,613 行、621 只证券，目标日覆盖率
100%，FMP 失败证券为 0；运行 CPU 时间约 2 分 03 秒，systemd 峰值内存 688.2 MB、无 swap。
MAG7 和 NASDAQ100 也已发布到 target `2026-08-27`。

宽基日更随后暴露出第二个独立问题：日更脚本已要求把认证价格语义及父版本写入 publication，
但 `BroadCoverageStore.publish_partitions()` 尚未接收这两个参数。该接口合同已补齐并将 manifest
schema 升为 v5；增量发布必须绑定与质量 lineage 相同的已认证父版本，full rebuild 则禁止伪造
父版本。覆盖、PIT 和八因子的最终不可变绑定为：

```text
security_master_generation = be02e2fff93d4ccf93b4d2c237c0f8b5
security_master_manifest   = e553f1ebef5d271dae94b616dca0a3c19b3f35a55ab2d3d1fa01e5c2ec71d357
coverage_version           = 378d1f3fae8944af863d6f67704b0313
coverage_manifest          = ee138aa657f6b2acb7a2fc63c071396afefee1a3bf9dc7c6a866e9c54295829c
coverage_bars_index_sha    = 66c560e7e2bed0fa6780fb4f7c9e68d774ca21d7d391c1ca189f08394db5b8e3
universe_version           = 8f19d47b45b64305b15c091cf959f5d4
membership_sha             = 88c0cbd95ac42b68372b1c44d9a8388261f2621d6f446e382ca43c06996c928b
eligibility_sha            = 8501f6a3cdbcb692f092e4c7018eb5ec3fd7fcc7e24c496857de1c57a0ace950
factor_generation          = 11247203be72468c9c72d592d72b5332
factor_publication_id      = d4b69444-bbc4-4106-85d1-aea511fb0573
factor_manifest            = db4cc5b6a7db6edffeaa5cc6e8f34a92693b92aa7077cd73fcf8f9e36487644b
```

coverage 共 10,431,001 行、7,975 只证券；17,118 条当日供应商记录中 17,117 条成功映射，1 条缺少
身份的记录进入隔离台账，未静默进入正式数据。PIT 共 223,235 条 membership、91 个快照、当前
2,780 个成员，`historical_pit_daily_bar_coverage` 门禁通过。宽基日更全链耗时约 120 秒，峰值
719.8 MB、无 swap。

Security Master 变化要求八因子重算 640/640 个 factor-month 分片。任务从认证 checkpoint 完成，
耗时 1 小时 08 分 43 秒，systemd 峰值 703.7 MB、无 OOM 或 swap。MDB、AEVA 的 MOM_12M
真实历史查询均返回 2026-08-20 至 2026-08-27 的 6 个交易日，并精确绑定上述 coverage、PIT 和
因子 publication。readiness 仅保留预期的 `PIT_CLASSIFICATION_POLICY`、
`PIT_INDUSTRY_COVERAGE`；shadow 台账新增 2026-08-27，当前连续通过日期为 2026-08-20、21、
24、25、26、27，即 `6/5`。

恢复期间还确认板块研究曾运行满 2 小时后超时并自动重试，占用 `.broad-production.lock`，使宽基
日更首次人工重跑等待 60 秒后退出。为优先恢复主数据链，只停止了该次板块重试，timer 未删除；
板块研究的性能和超时是独立待办，不能标记为宽基失败。核心 SP500/NASDAQ100 正式因子研究仍因
缺少 PIT 行业历史而 fail closed，MAG7 已发布；这也不影响已经完成的宽基因子数据浏览上线。

## 24. 2026-08-28 宽基消费者有界读取与盘前事故

板块研究和盘前预计算的长耗时不是因子公式本身变慢。例行消费者在读取少量股票、少量日期前，
`MarketDataReader` 会重复校验 coverage 的 92 个历史月分片；板块研究还加载了 SP500 全历史，实际
只需要当前日、上一交易日和 ADV60。低内存 SG 上这些无界读取持续触发 cgroup 高水位回收，板块
任务最终达到 3 小时超时，旧盘前预计算运行超过 4 小时仍未完成。

现已拆分“发布级全量审计”和“消费级有界认证”：

1. shadow、publication 和人工完整核验仍逐个哈希全部 child partition；
2. 普通读取先认证 manifest 和 partition index，再只哈希实际读取的月分片；
3. 板块研究将行情窗口限制在 as-of 前 120 个日历日，足以覆盖 60 个交易日 ADV；
4. 价格语义、父版本、PIT membership 和实际分片哈希门禁均未降低。

部署后板块与子行业研究在 target `2026-08-27` 上成功发布，CPU 9.609 秒，而不是再次触发 3 小时
超时。盘前预计算重新运行后约 11 分钟完成，CPU 8 分 42.79 秒、峰值 537.1 MB、无 swap，生成
target `2026-08-28` 的 94 个动量候选和两个不可变 payload。由于完成时间已晚于 09:29 ET 投递
截止时间，两个 payload 只保留审计，没有迟到补发；当天盘前投递属于真实 `MISSED`。

运维适配器同步改为按 SLA 推导状态：截止后仍为 `PENDING/SENDING` 的源记录显示 `MISSED`，完整
但晚于预计算截止时间的 payload 显示 `DEGRADED`。原始 SQLite 状态、截止时间和 `past_deadline`
继续写入元数据，页面结论不覆盖源证据。部署备份位于：

```text
/home/projects/quant-backups/consumer-bounded-read-20260828T1905CST
/home/projects/quant-backups/operations-deadline-state-20260828T2340CST
```

本地完整回归为 `586 passed`；SG 宽基与运维定向回归为 `28 passed`。宽基数据链保持 target
`2026-08-27`、连续 shadow `6/5`，本次消费者性能修复没有改写 publication 或历史观察台账。

## 25. 2026-09-02 身份漂移纠正与同日显式重绑

FMP 冻结源缺少 `UGRO -> FLZH` 与 `SVII -> NUCL` 的可靠换码历史，并把后继证券资料呈现为 OTC。
生产继续 fail closed，没有按 ticker、名称或上市地猜测身份。SEC 证据确认后，精确纠正规则写入
`configs/security_master_corrections.yaml`；两次使用相同冻结源构建均为 PASS，五张 Parquet 表
哈希完全一致。正式 Security Master generation 为
`b99fc58963604831b9534af9600e75f2`，manifest SHA-256 为
`545875e2b0e591295103221a11a0b33c34e29db00512937367388c2285aa652a`。

PIT 资格判断不再用当前 ticker 的交易所回填整段历史，而是读取查询日生效 symbol interval 的
交易所。由此 `UGRO` 的历史 Nasdaq 区间可以参与研究，`FLZH` 的 OTC 区间仍被严格排除；研究范围
没有扩展到 OTC。

同一个 target session 的 coverage 已绑定旧主表时，新增显式
`--force-security-master-rebase` 修复入口。该入口只允许真实主表绑定发生变化的同日重建，并把
rebase、身份差异、父版本和输入哈希写入审计；普通流程仍拒绝静默复用。首次运行还暴露并修复了空
历史 frame 与新数据 concat 时的日期 dtype 不一致，失败运行没有发布任何版本。

最终 coverage `a8c3814e7fd444e9b5f0a12cb047aa7f` 含 10,447,745 行、7,976 只证券、
93 个分片，target 覆盖率 99.4951%，完整 child hash 验证通过。全量 PIT
`bbe1288de3684cc3ab6849954cbd9507` 含 226,095 条 membership、92 个快照、5,558 个历史成员和
2,849 个当前成员；`historical_pit_daily_bar_coverage` 通过。membership SHA-256 为
`9fad1f5794f8333a3b10e87399b296c3ff54099b70ac94ac064993074fd6b78c`，eligibility SHA-256 为
`8e369a667823d9d613fca8a990e177ab7053c4531e7223ae9d671468c3ae9ae0`。

八因子使用 generation `2db3832266ed462cb6d47a49777a6b4c` 从认证 checkpoint 重建 648 个
factor-month 分片。正式发布前不得把运行中状态写成完成；readiness 仍只允许既有的
`PIT_CLASSIFICATION_POLICY` 与 `PIT_INDUSTRY_COVERAGE` 预期阻断，任何新增 blocker 都必须停止。

## 26. 2026-09-02 因子暖机窗口 off-by-one 修复

generation `2db3832266ed462cb6d47a49777a6b4c` 实际完成了 648/648 个计算分片，但发布门禁拒绝
了结果。2026-09 只有一个输出交易日时，MOM_1M、MOM_3M、MOM_6M、MOM_12M 和 REVERSAL
的最新 raw/clean 覆盖率均为 0%；VOL_20D、VOL_60D 和 TURNOVER 为 100%。失败代次及其
checkpoint 保留在 `.staging_2db3832266ed462cb6d47a49777a6b4c`，不得删除、改写或标记成功。

根因不是 coverage 缺数，而是 `exchange_calendars.sessions_window()` 会把锚定输出日计入返回
窗口。旧代码请求 `-N` 时只得到 `N-1` 个输出日前交易日，精确动量和反转公式因此永远少一个价格
观察；宽松 80% 暖机的波动率及只需要 N 个成交量观察的 TURNOVER 没有暴露该问题。修复后统一加载
“N 个输出日前交易日 + 输出日”，并把输入指纹合同升级为
`BROAD_FACTOR_INPUT_V3_EXACT_WARMUP_XNYS`。V2 checkpoint 和 V2 publication 均不能被新任务
复用，因此 648 个分片必须重新计算。

本地因子测试为 9 passed，SG 定向回归为 53 passed，SG 正式 `tests/` 完整回归为 651 passed。
完整回归还发现 9 个 macOS AppleDouble `._*.py` 元数据文件污染源码扫描；它们已原样移动到
`/home/projects/quant-backups/appledouble-quarantine-20260902T203127CST`，没有删除内容。
代码部署备份为 `/home/projects/quant-backups/factor-exact-warmup-20260902T202847CST`。

为避免与 21:20 SGT 至收盘后的茶杯柄盘中监控争抢 2 GiB 内存，修正后的正式重建没有立即启动。
一次性 persistent timer 已通过 `systemd-analyze verify`，将在 2026-09-03 04:20 SGT 触发现有
`quant-broad-factor-data.service`。服务继续使用单线程 BLAS、700 MiB soft high、900 MiB hard max、
flock 和原 OnSuccess readiness 链；成功前不得手工发布或绕过覆盖率门禁。

## 27. 2026-09-05 FMP 退市状态漂移的严格处理

2026-09-03 目标日的两次宽基日更都在 Security Master 阶段 fail closed。FMP 不再把 FLZH 标记为
活跃，同时仍返回 `UGRO -> FLZH`、一致的 CUSIP/ISIN，以及 FLZH 在 2026-08-26 从 OTC 退市的
记录。旧规则只描述换码后的活跃 profile，因此把这次变化识别为未审阅漂移是正确行为；错误不能通过
删除规则、接受任意 active 状态或复用旧 Security Master 解决。

纠正规则现增加有界 `provider_lifecycle`：仅当目标日不早于 2026-08-26，且同一不可变 provider
source 中存在 FLZH、精确退市日、OTC、公司名四项完全匹配的退市记录时，才把预期 profile 改为
INACTIVE。早于该日期仍要求活跃基线，缺字段、多行、名称、交易所或日期漂移均继续失败。SEC 换码
证据、供应商 CUSIP/ISIN 和原始冻结源都被保留，未猜测历史或降低 100% 身份门槛。

生产前验证使用 2026-09-03 的真实失败冻结源。同源两次构建均为 PASS，五张 Parquet 的 SHA-256
逐表一致；第二份独立冻结源也 PASS，两组活跃普通股数量均为 5,350。部署备份为
`/home/projects/quant-backups/flzh-lifecycle-20260905T004551CST`，SG 完整回归为 `655 passed`。
2026-09-05 11:31 SGT 正常日更已完成正式恢复验收。目标 2026-09-04 的 Security Master 为
`3ea8a269a67a4797be8bfcbfb2d7ae78`，manifest SHA-256 为
`1d9b99dfab9ed398f8e67ddb3ff25cf43f4d3c107f182e0b95ff4d4226744e36`；coverage
`2f31ea50b7484e038ca977b252679f43` 含 10,467,468 行、7,986 只证券，PIT
`25cec81b68304b3a85e7829b31313567` 含 225,982 条 membership、当前成员 2,848，并通过全历史日线
覆盖门禁。全链耗时 544.825 秒、systemd 峰值 701.9 MiB、无 swap。八因子随后从认证 checkpoint
重建，核查时为 483/648；八因子发布完成前仍不能把整个后续链标为 SUCCESS。

## 28. 2026-09-09 RML 增量历史缺失与下游茶杯柄阻断

SG 23:17 SGT 只读复核确认 target 2026-09-08 日更失败，而不是盘中进程 OOM：11:31 初次运行
11:37 失败，12:07 重试在 12:08 再失败。初次峰值 595.1 MiB，重试 229.6 MiB，均无 swap。
重试报告为 `outputs/data_audits/broad_daily_pipeline/target=2026-09-08/run=20260909T040719Z_dc569da5.json`，
全链耗时 52.075 秒，coverage 阶段 51.119 秒。

证券主表已正式发布 target 09-08 的 `6ff24226200643b8a4f9b7a999037458`，manifest SHA-256
`6a0b6eadb70faa3a7b1f3371f1e7149caafc07b791b31e84b79cfee6c3aa8a7a`。随后 coverage identity delta
需要补齐 16 个身份，成功获取的历史共 13,126 行，但以下一条失败导致整次发布 fail closed：

| 字段 | 冻结主表/审计中的值 |
| --- | --- |
| security_id | sec_d396285ca3c35145a5b3b250472e40d0 |
| ticker / name | RML / Resolution Minerals Ltd. Sponsored ADR |
| 类型/交易所 | ADR / NASDAQ |
| CUSIP / ISIN | 76091K105 / US76091K1051 |
| FMP listing_date / alias effective_from | 2003-11-28 |
| 请求历史区间 | 2019-01-02 至 2026-09-04 |
| alias_failures | 1，RML；alias_fallbacks 为空 |

当前代码仅在历史 fetcher 返回 None/空表时写入此 alias_failure，因此已确认的是“该请求区间没有
取得有效行情”，不是已确认 RML 没有真实历史、也不是已证明上市日期错误。审计没有保留底层原始
HTTP 响应，不能仅凭这条记录断言供应商超时或确认真实上市日。主表 listing_date 来自
FMP_PROFILE_BULK，CIK 为空；尚需核实美国 ADR 的真实上市/身份和供应商行情覆盖。

审计文件：
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-08/run=20260909T040722Z_af98b9dd/identity_delta_audit.json`。
失败父版本为 coverage `562967c01bb54e2ab39454804cc4ac73`、Security Master
`3ea8a269a67a4797be8bfcbfb2d7ae78`。正式 coverage/PIT 仍在 09-04；新主表发布成功不应导致旧
coverage/PIT 自动改绑。09-08 茶杯柄候选的旧不可变合同本次重新校验通过，但用于 09-09 候选时
新鲜度门槛明确失败，18:30 候选及开盘后的盘中监控因此均退出，未产生新评估。

恢复建议及边界：先保存有日期和原始响应证据的 RML 定点历史查询，核实 ADR 与其他市场同名证券
的身份及美国上市时间。如果供应商补齐则按现有合同重跑；如果上市/别名元数据错误则以可靠证据
做有界纠正规则并同冻结源双重幂等验证；如果历史仍无法证明，则需正式审阅排除/向未来摄取政策，
不能擅自追加到原 66 条 PROSPECTIVE_ONLY 台账。新 coverage、PIT 发布并核验后才可重建候选。
本次没有修改身份规则、历史排除、门槛、生产数据或服务开关，也没有把失败日补记为 shadow 通过。

## 29. 2026-09-10 RML 证据化向未来摄取与恢复

第 28 节记录的是排查当时状态；项目负责人随后授权核实 RML 并执行有证据的修正/历史排除。
本次没有把 FMP 的 listing_date 直接改成推测日期，也没有拼接其他市场行情。

证据链：

- [发行人 2026-09-09 公告](https://resolutionminerals.com/investor-centre/nasdaq-trading-to-commence/)
  明确将 Nasdaq RML ADS 交易开始日定为 2026-09-09。
- [Citi 存托凭证目录](https://depositaryreceipts.citi.com/adr/notices/pgm_dispCA.aspx?cusip=76091K105&pageId=15&subpageID=112)
  将 CUSIP 76091K105、ISIN US76091K1051 对应到 OTC ADR RSMIY，比例 200 普通股:1 ADR。
- [Nasdaq 状态目录](https://www.nasdaqtrader.com/Trader.aspx?id=nasdaq-security-status-updates)
  的 09-08 RML 行是 Anticipated Security Additions，不能当成历史首次成交日。
- SG 有界请求 RML/RSMIY/RLMLF 的 full 与 dividend-adjusted 两端点，区间 2019-01-02..2026-09-08，
  六次均 HTTP 200。RML 两端点各仅一条 09-07 记录（XNYS 休市日），另两个代码均空数组。
  因此不是本次请求超时，且响应不能证明可用的上市前美国交易历史。09-04 截止日原任务无数据
  与此一致。缺失不允许由 ASX 普通股、OTC 普通股或无证据 ADR 历史替代。

原始响应及 SHA-256 在 `outputs/data_audits/rml_identity/20260909T155422Z/manifest.json`；
三个主来源网页均 HTTP 200，另存 `primary_sources_manifest.json`，没有保存 API 密钥。

实现：对精确 security_id `sec_d396285ca3c35145a5b3b250472e40d0`、ticker、name、ACTIVE 状态
增加 PROSPECTIVE_ONLY，research effective_from=2026-09-09，附来源、原因和逐票 decision_basis。
原主表 listing_date=2003-11-28 及 CUSIP/ISIN 保持原样；这是研究准入边界，不宣称法律发行日。
原 67 条政策原样保留，现共 68 条（32 prospective、36 excluded），没有移除任何排除台账。

代码允许已批准的 PROSPECTIVE_ONLY 起始日晚于构建 target；别名仍从未来生效日开始，coverage
选择器在生效日前排除该身份。未来 EXCLUDED 策略仍拒绝。新增测试覆盖生效日前不纳入、不请求
历史、生效日裁剪、原字段保留、同输入五表精确幂等。身份覆盖率 100%、历史覆盖、价格与哈希门禁
均未降低。不能通过把 effective_from 提前到 09-08 来绕过校验。

本地完整测试 1,048 passed、6 skipped；SG 定向测试 57 passed。部署前四个文件逐个与本地
基线哈希一致，备份为 `/home/projects/quant-backups/rml-prospective-20260909T235646CST`。
冻结源为 `outputs/data_audits/security_master_candidates/asof=2026-09-08/run=20260909T033104Z_d458b9f3/provider_sources`。
恢复执行采用单线程 BLAS、flock、MemoryHigh=700M、MemoryMax=900M，先双候选校验，再发布、
重跑 coverage/PIT 并核验候选。最终生产结果见本节后续验收记录；未完成步骤不得据此视为已恢复。

### 本轮真实验收结果

两次冻结源候选分别位于 `outputs/data_audits/rml_recovery/security_master_candidates/asof=2026-09-08/`
下的 `run=20260909T155749Z_73937ecf`、`run=20260909T160144Z_fdcfa93c`；五表精确比较报告为
`outputs/data_audits/rml_recovery/double_frozen_verification.json`，PASS。均为 10,646 个身份、
5,353 活跃普通股，身份覆盖 100%，零身份冲突。不是放松校验或手工改成功标志。

正式 Security Master 已发布 `65fc7779e7524461810d10633db63dd4`，target 09-08，manifest SHA-256
`ea1c07820fa3ccf922300451599fadc2f9f77629dd8b98c3a24b5080375e7fd1`。正式五表哈希再次核验，
与两份候选完全一致。发布构建耗时 196.445 秒，systemd 峰值 247.0 MiB、swap 0（进程报告的
peak RSS 为 365.961 MiB，和 cgroup 计量口径不同，不混用）。

随后 `quant-rml-data-recovery.service` 执行真实日更，报告为
`outputs/data_audits/broad_daily_pipeline/target=2026-09-08/run=20260909T161013Z_04583bcb.json`。
RML 阻断已消除：增量身份 16 -> 15，取得 13,126 行历史，alias_failures=[]、alias_fallbacks=[]。
17 个近期 XNYS 交易日批量响应均完成并保存有哈希缓存，未恢复旧输入 checkpoint。
但在后续重叠窗口认证时出现新的独立 blocker，整链最终 FAILED，耗时 282.362 秒，systemd
峰值 444.9 MiB、swap 0。PIT 和候选没有启动，不能宣称恢复成功。

新 blocker 是 CALC（`sec_00603104b02855dca5bb210569c75e16`）的 volume 非统一比例差异，
max_relative_deviation=1.93013e-05，超过现有 1e-5 门槛。定点对比发现单股 full 端点当前仍返回
与旧版本相同的整数成交量，而 eod-bulk 返回小数，例如：

| 日期 | 旧版本及当前 full | 新 eod-bulk |
| --- | ---: | ---: |
| 2026-08-18 | 20724 | 20723.6 |
| 2026-08-19 | 84730 | 84729.6 |
| 2026-08-24 | 50239 | 50238.8 |
| 2026-08-25 | 44153 | 44153.2 |
| 2026-08-28 | 135205 | 135205.2 |

已证实端点成交量精度不一致，不能把该错误简单称为公司行动、网络故障或已确定的真实历史修订。
也不能仅凭差异小就调大容差或四舍五入全部行情。逐日证据为
`outputs/data_audits/rml_recovery/calc_overlap_diagnostic.json`；当前 full 原始响应为
`calc_current_full_response.json`，SHA-256
`fe1422169540143ba3c87883306a42a2ed681b76538ed1cb25397c6787e4fe27`。

现有宽基 writer 对这类差异只能严格停止，尚无逐证券完整历史重建与原子替换路径。下一步需先在
冻结重叠窗口审计所有受影响身份，再实现有界、版本绑定的规范单股全历史重取、与批量数据的冲突
认证和不可变分片替换；不能只覆盖短期窗口留下历史口径断点，也不能假设修复 CALC 后无其他冲突。
完成回归和候选验证后才可再次发布 coverage/PIT，随后恢复候选及盘中评估。此次保留全部响应、
旧发布与失败记录，没有放宽容差、强行启动依赖过期行情的候选或补记 shadow 日。

## 30. 2026-09-10 全证券范围审计与整票历史恢复

对 2026-09-08 的冻结 EOD 缓存和父版本 `562967c01bb54e2ab39454804cc4ac73`
进行完整重叠认证，5,437 只通过，321 只失败。按每票首先触发的失败字段统计：volume 194、
adj_close 56、open 18、high 17、low 8、close 4、零成交量不一致 24。
这是首次失败字段分类，不代表同一证券只有一个字段不同，也不能将全部 321 只解释为精度误差。
正式审计：SG `outputs/data_audits/rml_recovery/full_overlap_scope_audit.json`；
本地副本 `outputs/data_audits/full_security_history_repair/sg_frozen_overlap_scope_20260910.json`。

新增 `src/data/broad_history_repair.py`，方法 `FULL_SECURITY_CANONICAL_REPLACEMENT_V1`。
恢复是显式运维操作，普通日更仍严格拒绝无法认证的增量，不会自动开启大量全历史下载。

1. `update_us_equity_coverage.py --audit-overlap-only` 扫描所有继续存在的身份，输出
   `overlap_scope_audit.json`，不构建或发布分片。认证容差仍为 `1e-5`。
2. `--repair-full-history` 对审计确认的语义漂移逐票重取整个允许历史区间，遵守正式
   Security Master 的历史 ticker 区间和 PROSPECTIVE_ONLY 起点，不做当前 ticker 回退。
3. 全历史使用 canonical full OHLCV、dividend-adjusted adj_close、独立 non-split-adjusted
   nominal close。旧前缀、旧 nominal close、新 bulk 重叠和新增日都不能混入被修复证券。
   所有 bulk/full 字段冲突计数入审计；选择 full 是明确的整票来源合同，不是数值容差豁免。
4. 每票缓存绑定父版本及 manifest、Security Master 及 manifest、目标日、方法、全范围审计、
   历史区间和别名。逐别名接口返回帧、验证后帧和 manifest 各有哈希；只有完整成功缓存可以复用。
   失败 attempt 保留返回帧与 failure.json。这些 Parquet 是 FMP 适配器的返回帧，不等同于三路
   HTTP 原始响应字节。返回空、日期越界、重复、坏价格、非法成交量、
   nominal close 缺失或已有有效日期丢失，一律不允许替换；不会填充未证明的交易日。
5. 按月重建所有涉及历史的分片，先移除被修复证券的全部旧行/bulk 行，再插入完整新历史。
   新写入的每个月从磁盘读回，与对应 canonical 数据逐行精确比对；其他身份继续走原有认证。
6. 全池质量门禁通过后，原子登记新 immutable version 并更新指针。发布前在 catalog 写锁内
   检查父指针未被其他 writer 推进；失败不切换指针，不删除旧版本或 staging。

进度证据为本次 run 下的 `full_history_repair.json`（total/completed/validated/errors）及
`journalctl -u quant-full-history-repair.service`；成功缓存存于原 provider cache 的
`full_security_repair/<security_id>/<binding>/`。该临时恢复服务不等于日常 timer 已修复。

部署前备份：`/home/projects/quant-backups/full-security-history-repair-20260910T004154CST`。
本地 `python -m pytest -q tests`：1,077 passed、6 skipped；SG 恢复/覆盖/运维专项测试：60 passed。
直接在根目录执行 pytest 会收集嵌套 `quant-factor-framework/tests` 同名模块而冲突，
本项目回归明确指定 `tests`，未删除用户的嵌套目录。
00:45 SGT 已启动真实恢复，MemoryHigh=700M、MemoryMax=900M、单线程 BLAS、90 分钟超时、
生产 flock 互斥；该时刻尚未发布新 coverage，不代表 PIT、候选或 shadow 已恢复。

### 30.1 首次全范围执行结果与尚未接入的特殊情况

2026-09-10 00:45:13 至 01:11:05 SGT，321/321 全部处理完毕，318 只验证通过，累计
323,492 行完整历史进入已认证缓存；3 只拒绝。历时 25 分 52 秒，CPU 6 分 8.354 秒，
cgroup 峰值 702.0 MiB、swap 0。服务以数据校验失败退出 1；SSH 等待连接曾断开，
但服务已独立完成，journal 和终态 JSON 证明这不是服务器中断。

| 证券 | 完整历史校验问题 | 核验后的性质 |
| --- | --- | --- |
| STRR | 缺少 2025-08-22 至 09-04 的 9 个有效交易日 | FMP 查询代码与真实交易代码的切换窗口不一致 |
| ATCX | 2019-11-22、2020-04-03 的 open > high | 与原始正式隔离台账的坏行完全相同，父有效行情中不存在 |
| BGLC | 2022-07-26、2023-04-06 的 open > high | 与原始正式隔离台账的坏行完全相同，父有效行情中不存在 |

STRR 的正式身份仍是 HSON 至 2025-09-04、STRR 自 09-05；
[发行人更名公告](https://www.starequity.com/node/19281)及
[SEC 8-K](https://www.sec.gov/Archives/edgar/data/1210708/000121070825000081/hson-20250902.htm)
支持该边界。合并在 08-22 完成，但
[合并公告](https://www.sec.gov/Archives/edgar/data/1210708/000119312525185799/d931756dex991.htm)
明确 HSON 继续交易，不能将这 9 天伪称停牌或把真实换码日改成 08-22。
本轮另取 08-15 至 09-10 的三类原始 HTTP 响应：HSON 的三类接口均不含这 9 日，
STRR 的 full/dividend-adjusted/non-split-adjusted 均包含全部 9 日。
证据保存为 `outputs/data_audits/full_security_history_repair/strr_window_probe_20260910/manifest.json`。
这证明需要专门的、有身份/日期/证据约束的 provider 查询映射，不能对整个旧历史盲用 STRR。
本轮只做诊断，没有将这些响应直接拼入正式数据或修改身份登记。

四条坏行已与版本 `ad5de5cfd10d47e2ae21364f1808248d` 的正式隔离文件逐值核对，原 manifest
SHA 为 `6fbe3bc28ac4e477b782fa9cc337a3618a75875b4c3f31bf6676d9b481c8b7c0`，隔离文件
SHA 为 `081f7e715620f7e71a52102a96451d25843b91113b65fe2ed2a6f24b7b719255`，均验证一致。
证据为 `outputs/data_audits/full_security_history_repair/prior_quarantine_evidence.json`。
**ATCX/BGLC 不是新增的历史丢失：是新整票恢复分支尚未接入已认证隔离台账，当前默认零坏条而拒绝。**
下一步应只让与认证台账精确匹配的坏行沿用有审计的隔离，保持全池隔离比例/目标日门槛，
且不能因此删除父版本原本有效的日期；不能简单改 open/high、放宽价格规则或吞掉新坏行。

本轮恢复主干已实现，但上述两类特殊接入仍未实现，不能宣称生产恢复完成。未发布新 coverage，
旧版本仍 `562967c01bb54e2ab39454804cc4ac73`（09-04），PIT 仍
`c1329fcd14dd4521911976b21fa6be22`。未运行后续 PIT/候选，未补记 shadow 或打开发送。
318 份成功缓存可在完全相同输入合同下复用；变更特殊票的查询/隔离合同必须重新认证对应缓存。

终态及明细：原 run 的 `full_history_repair.json`、`failure_diagnostics.json`；本地同名副本在
`outputs/data_audits/full_security_history_repair/`。后续又部署了按月查询 threads=1/memory_limit=320MB、
供应商 ValueError 留存为数据合同失败、缓存采集时间记录，以及发现当前 coverage 整票修复时
PIT 禁止增量沿用旧月度资格的保护。对应备份
`/home/projects/quant-backups/full-history-repair-hardening-20260910T012835CST` 保留首次实跑代码。
最终本地全套 1,079 passed / 6 skipped；SG 覆盖/恢复/运维/PIT 专项 73 passed。
这些补强通过代码测试，但并未把首次失败运行改写成成功；PIT 生产验收仍需等待修复完成。

### 30.2 2026-09-10 日更的新 target 审计

11:32 至 11:43:55 SGT 自动日更 target=2026-09-09 失败。主表已正式发布
`c8f5a40065914b83b68a87ecb0012b59`，manifest SHA-256
`bee69d816c3eb7ae882208de961e9c20813d51c7967c0c12f13e8bfea985d739`。
覆盖审计 5,433 只通过、324 只失败，首例 CALC 非均匀 volume 差异 1.93013e-05。
审计路径 `data/lake/staging/us_equity_coverage_incremental/asof=2026-09-09/run=20260910T033726Z_b076440e/overlap_scope_audit.json`，
provider_cache_binding=`50bfbac91b6d6543862afc3e50d5c756826a860a21ba9247419c9c0d4d7a98a0`。
其 target、主表和源绑定不同于 30.1 的 321 只，不能直接使用旧恢复结果作为本日发布证明，
也不能仅凭计数差断言“新增恰好三只问题证券”。未执行本日完整历史恢复。

正式行情 `562967c01bb54e2ab39454804cc4ac73`、PIT `c1329fcd14dd4521911976b21fa6be22`
和因子 `037d9d2783f0467b90679e0df1af0c3a` 仍截至 09-04。先完成 30.1 两类适配，
再按选定完整冻结合同恢复和全链验收；不得因日更重试自动绕过显式恢复授权与数据门槛。
茶杯柄无新 v3 合格日，0/5、发送关闭。资源与运行报告见 SG 运维 46。

### 30.3 2026-09-11 新阻断：候选主表历史别名重叠

target=09-10的主表构建11:36:28 SGT失败，未发布。audit位于
`outputs/data_audits/security_master_candidates/asof=2026-09-10/run=20260911T033059Z_2f6b28ee/audit.json`，
SHA-256=`da8715370ae339dfae52b24f0c3a51ffda85896ebffa69fefe32a246bf929953`；
冻结provider_sources manifest SHA-256=`a50ba0b289b4f240113f62d3c0a66fe031fb94a1997e17c6c68cb662572b89d1`。

读取冻结profiles与候选symbols确认：
- HYMC/HYMCZ均被供应商标为STOCK，同CIK=0001718405、CUSIP=44862P208、ISIN=US44862P2083。
  候选映射到sec_788b0b1924f55d6ea8127b916052e703：HYMC从2018-03-12至今，
  HYMCZ从2017-02-17至2022-10-21，ISSUE_ID_ALIAS区间重叠。
- BDX/BDXA均被标为STOCK，同CIK=0000010795、CUSIP=075887109、ISIN=US0758871091。
  候选映射到sec_dc934bb1b7ce51c395720f963524c8df：BDX从1973-02-21至今，
  BDXA从2017-05-11至2020-04-30，ISSUE_ID_ALIAS区间重叠。

这证明当前冻结源相同标识触发合并后违反区间合同，不证明它们真实是同一证券或应直接删除。
须核实原始证券类别、历史发行及标识，确认是供应商错误归类/标识污染还是合并逻辑问题，
再做证据绑定的纠正和同冻结源双重幂等核验；禁止只调整日期来让门禁通过。
本次仅定位，不改身份政策，不发布候选。本日行情/PIT未执行，旧324只历史恢复阻断仍未解决。
正式coverage/PIT/因子截至09-04，茶杯柄v3仍0/5；完整运行和监控信息见SG运维47与茶杯柄29。

### 30.4 2026-09-11 受控纠正与完整历史恢复

本轮执行已改变30.3的“仅定位”状态，不能继续将其当作最新修复结论。

1. SEC证据确认HYMCZ是认股权证，BDXA是优先股存托股份，不是HYMC/BDX普通股。
   见[Hycroft 2021 10-K封面](https://www.sec.gov/Archives/edgar/data/1718405/000171840522000016/hymc-20211231.htm)
   和[BD 2019 8-K证券类别](https://www.sec.gov/Archives/edgar/data/10795/000114036119009406/nc10002034x1_8k.htm)。
   `configs/security_master_corrections.yaml`新增两条精确源记录纠正；验证ticker、名称、类别、
   交易所、CIK、CUSIP、ISIN、上市日及活跃标记后，在身份合并前排除这两个非普通股工具。
   原始冻结源不改写，审计保留源记录和证据；不按ticker后缀猜测，不移动历史日期。
2. `configs/full_history_repair_rules.yaml`限定STRR查询映射：只对
   `sec_9227ef0c29095fddbf7b0a2e9d60d9e0`、2025-08-22至09-04使用STRR向FMP查询，
   存储的历史ticker仍为HSON，09-05才为STRR。旧STRR属于其他身份，不受该规则影响。
   依据[2025-09-02 8-K](https://www.sec.gov/Archives/edgar/data/1210708/000121070825000081/hson-20250902.htm)
   与[合并公告](https://www.sec.gov/Archives/edgar/data/1210708/000119312525185799/d931756dex991.htm)。
3. ATCX两行、BGLC两行只在身份、日期、ticker、OHLC、adj_close、volume和坏行原因与旧隔离台账
   完全一致时继承隔离。旧版本、manifest及quarantine哈希均固定；任何新坏行或数值变化仍拒绝。
   旧台账没有新价格语义合同，所以仅作为“已隔离原始证据”认证，绝不授权旧有效价格参与新计算。
   新有效数据仍必须满足当前canonical合同；不允许移除上一正式版本中有效的交易日。
4. 完整恢复方法升级为`FULL_SECURITY_CANONICAL_REPLACEMENT_V2`。修复过的security_id存入
   发布manifest的`quality_lineage.canonical_history_security_ids`，以后每天用同一单股canonical
   来源取近期重叠窗口并再次认证，避免次日bulk小数成交量重新污染整数历史。不是每天重取全历史；
   真正的非一致历史修订仍须显式完整恢复。近期源证明保存在`canonical_overlap_refresh`。

12:23:47 SGT新主表正式发布，target=2026-09-10，quality=PASS：
generation=`496db448b54e4ae49701512b3acfd64c`，manifest SHA-256=
`0afd6b0156d2efbf6a41537c0cfad1459c4ee9dc762ff36d6d91815d17c64a49`。
同一冻结源先构建两次，master/symbols/classifications/identity_keys/history_policy五表精确一致，
再第三次发布；运行报告在`outputs/data_audits/cup_upstream_repair_20260911/`。

12:26:48启动target=09-10完整行情恢复，run=
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-10/run=20260911T042651Z_291b3f89`。
身份增量43只、36,219行、别名失败0；正在取bulk重叠源与执行整票认证。
这只是运行进度，不表示coverage/PIT已发布，也不产生茶杯柄合格观察日。
最终状态见本节后续记录及SG运维48；旧staging、checkpoint、版本和隔离记录均保留。

本次target=09-10重叠审计实际需恢复421只，不能沿用旧target的321/324计数。
运行发现PROP的2019-01-04、2023-10-16两行坏数据，逐项比对确认与同一旧隔离版本完全一致。
全421只与旧台账交集共6行、3只证券，已将PROP两行加入精确清单；并非允许任意新坏行。
12:57的原始取数运行已验证CALC(1478行)、STRR(1927行、零缺日)、ATCX(1861有效行、2隔离行)，
该轮已记录PROP失败，不会改写该失败为成功。

新增显式`--reuse-frozen-repair-inputs`，只可配合`--repair-full-history`：在审阅隔离政策改变后，
可重新读取完整且哈希正确的原始响应，创建新规则绑定的验证产物，而不是复用旧PASS/FAIL。
target、主表、父行情、scope、方法、身份、历史窗口及具体查询映射必须相同；只允许隔离审阅规则
不同。原始响应不全不复用，哈希变化或多份不同响应均拒绝；每一行重新执行身份、日期、OHLCV、
完整性和隔离精确匹配。新产物记录原始证据路径/哈希和验证时间，不把重验时间冒充取数时间。
不同target/主表/coverage的旧checkpoint仍不得恢复。规则变更前另行备份见SG运维48。

#### 本轮生产终态（2026-09-11 13:32 SGT）

- 重验421/421通过、失败0，421份完整原始返回均重新验证，继承隔离6行。
  `run=20260911T051205Z_f84e5438/audit.json`的所有质量检查通过；93个月分片重建并精确核验。
- 13:22:38正式coverage发布为`76e68448ccea48f5b5e1dbf871c9f6c9`，target=09-10，
  10,502,974行、8,012只历史证券，manifest SHA-256=
  `31d54687942c40bd6f6e15cf2f870afd4c8b9180aad192e10d833713d2a2c97c`。
  隔离记录共7行：上述6条历史坏行，另有09-03 LPSN bulk非正价格按既有门槛隔离；
  未把坏价格修成合法价格，target坏行仍为0。quarantine SHA-256=
  `8a244717ee6309a418c91a5be5997d258b3ac08db7b10aa10546de147ea06d29`。
- 13:25:28完整PIT重建发布`857031854a554d9bbfee942a6bfc3919`，
  manifest=`e77623377b5c12a86f1e9d691d1d93415e360a1da78b23d432d6a8b17fd9c68e`，
  membership=`5407240c1f65ac0eba576583c1b5cb997539993c88760381a395b068d0e075e4`，
  eligibility=`20999886ffdb35b92237e17735b4548c165145f450c1bf6e637008d21ea44a96`。
  绑定同一coverage/主表；1681个历史交易日无门槛失败，最差覆盖率99.8484%。
- STRR映射的九个交易日在新完整历史中仍标为HSON；真实股票代码历史没有被改写。
- 常态daily pipeline 13:28:25 SUCCESS，三个阶段均认证NOOP复核已发布版本；报告
  `outputs/data_audits/broad_daily_pipeline/target=2026-09-10/run=20260911T052821Z_4d4b3896.json`。
- 八因子由既定OnSuccess恢复，generation=`b6108d673ac447dab5e7be88e7f76ca1`，
  截至13:32为42/648月因子分片，仍在计算，不能标成八因子已发布。
  茶杯柄候选已经成功，不以八因子完成为前置条件；完整验收见茶杯柄30及SG运维48。

### 30.5 2026-09-11 15:54至16:05 SGT：八因子正式发布验收

本节替代30.4最后的“八因子仍在计算”状态。`quant-broad-factor-data.service`实际于
13:28:25启动、15:17:46成功退出，历时1小时49分21秒，CPU时间1小时14分10秒，cgroup峰值
709.1MiB、swap峰值0。run_report的31.708秒是最后一个受控执行段，不能当成整轮耗时。

- generation=`b6108d673ac447dab5e7be88e7f76ca1`，publication_id=
  `30cfe535-0330-4c09-a5c2-7bdcaee45b11`，manifest SHA-256=
  `f3e640167f3ab60e7a25116c0307b50f62cf24baf98f923fdcb93ea714059f7b`。
- target=2026-09-10，648/648月因子分片已发布；绑定30.4修复后的coverage、PIT和Security Master。
- 15:18:01自动shadow核验成功，检查93个行情子分片、648个因子分片、主表、membership/eligibility
  哈希及版本绑定，并执行真实截面查询。该日记录是宽基数据验收，不是茶杯柄盘中通过日。
- 独立HTTP验收覆盖八个因子的完整有效截面：MOM_12M/1M/3M/6M分别2726/2846/2811/2778只，
  REVERSAL/TURNOVER/VOL_20D/VOL_60D分别2846/2846/2846/2834只。
  用返回clean乘预设direction，通过pandas重新计算并列最小排名和平均百分位，全部一致；
  完整截面请求约0.91至0.99秒。此项验证排名口径，不冒充重新计算全部历史raw公式。
- MDB和AEVA近月历史接口均200、约0.91/0.99秒；全历史分别1681/1643行、14.056/13.790秒，
  均结束于09-10并绑定新版本。全历史查询仍较慢，是性能余量，不应描述成亚秒全历史体验。
- readiness仅`PIT_CLASSIFICATION_POLICY`、`PIT_INDUSTRY_COVERAGE`阻断，退出码2为预期。
  不影响本次FACTOR_DATA发布或茶杯柄候选，不代表正式宽基置信研究已获准。

数据shadow最近连续日期仅09-10（1/5），历史通过日不拼接；原已上线的web默认开关未变。
茶杯柄v3独立观察仍0/5、发送false。运维旧候选覆盖新正式发布的修复、备份和后续检查时间见SG运维49。

### 30.6 2026-09-12 10:41 SGT：目标推进，等待当日日更

当前正式主表、coverage、PIT、八因子仍是30.5的09-10版本，generation/parent/PIT/主表绑定
均未变化，09-11茶杯柄候选及70批盘中合同匹配该正式输入。昨晚盘中正常结束，失败来自
IBTA/UAN/WBI分钟证据不足，不是09-10日线合同过期或八因子任务中断，详见茶杯柄34。

新可发布目标已推进到09-11，10:41运维专项前四阶段因此显示STALE，正式置信研究仍被PIT行业
历史门槛阻断。`quant-us-equity-coverage.timer`仍active，下次11:30:19 SGT，检查时尚未到时。
这里是“旧版本等待常态日更”，不是新一轮日更FAILED，也不能继续描述为已覆盖09-11。
未抢跑、未重复重建648分片、未修改Web默认开关。11:45巡检应读取常态任务真实结果和新正式绑定，
不能因为旧factor service的exit0就认为新目标已发布。
宽基data shadow保存的09-10连续1/5记录不等于新目标验收通过，更不能与茶杯柄v3的0/5混用。

### 30.7 2026-09-12 下午：退市历史分页预算不足及受控恢复

本节更新30.6的“等待日更”：11:30初跑和12:06重试现已真实FAILED，12:41达到StartLimit。
两个运行均在SECURITY_MASTER阶段报`delisted history does not reach history_start`，未运行
后续coverage/PIT，不是尚未调度，也不是内存不足。重试证据为
`outputs/data_audits/broad_daily_pipeline/target=2026-09-11/run=20260912T040601Z_5ad8b665.json`。
主表审计为`outputs/data_audits/security_master_candidates/asof=2026-09-11/run=20260912T040602Z_01d52901/audit.json`：
100页、stop_reason=max_pages、oldest_loaded=2019-12-05、history_boundary_reached=false，
未达要求的2019-01-01；峰值RSS572.754MiB。质量失败时正式版本未被该候选替换。

对更后页的有限探测确认page=100已有2018年真实记录。不同查询时刻的分页内容可能变化，
不将事后探测拼接进中午冻结源。本轮仅把`configs/default.yaml`中的
`data.security_master.delisted_max_pages`从100增至200；保留历史起点、身份校验和发布门槛。
SG部署前核对配置SHA-256并确认YAML唯一差异，备份到
`outputs/deploy_backups/cup_shadow_20260912_pagination/default.yaml.before`。
新配置SHA-256=`928b391ecf519a4edf4dd6b892e0a29b79c7e1ee654d7523f7bed1f340d22325`。

16:56:28启动独立`quant-cup-upstream-recheck-20260912.service`，运行现有正式pipeline，
target=09-11，保留`.broad-production.lock`及700M/900M内存软硬限制、TasksMax=64、Nice=10。
此独立unit不触发原coverage service的八因子OnSuccess，不将旧八因子exit0说成09-11发布。
17:02:02主表阶段已PASS并正式发布：

- generation=`5c738854ad504f1c863c47cf15bb4a63`，target=09-11，10791证券、5354活跃普通股。
- manifest SHA-256=`d9d00cf2b7db5c47bbf9ef00c28aea06507414c7bfa0f89a5dac9672531fc3ed`。
- 新鲜抓取102页，oldest_loaded=2018-07-31，stop_reason=history_start_reached，
  history_boundary_reached=true；边界外数据不混入要求范围内的正式记录。
- 主表审计`outputs/data_audits/security_master_candidates/asof=2026-09-11/run=20260912T085629Z_1e820d52/audit.json`，
  SHA-256=`1f22da60a7d62c170c96becb29dccb7e41119749039fc4918c74591799e4758a`，峰值RSS553.652MiB。

这修复的是本次上游分页截断，不解决茶杯柄UAN/WBI/IBTA分钟证据不足，也不追认09-08/11失败或
09-04/09/10缺跑。候选必须另行验证新coverage/PIT，不能仅凭主表PASS就宣称下周已准备完成。
茶杯柄仍严格v3 0/5、delivery=false；全链路恢复终态见下方记录。

#### 17:30至17:38终态：主表恢复，行情认证仍失败

受控任务于17:30:56退出1，不是超时或OOM。正式pipeline报告
`outputs/data_audits/broad_daily_pipeline/target=2026-09-11/run=20260912T085628Z_f6572f89.json`
状态FAILED，SHA-256=`2398cc9551d6f6bc5813c69cd63438cfe04b040676393d6f4fc68fbb92bd2985`。
主表阶段333.455秒SUCCESS；coverage阶段1734.567秒FAILED，PIT未执行。总耗时34分28秒，
CPU349.723秒、cgroup峰值701.9MiB、swap0，报告进程峰值RSS629.566MiB，两种峰值口径不可混用。
中途仅将独立恢复unit的临时时限延长到一小时，内存限制和生产锁未变，进程没有重启。

真实阻断为5295只证券未通过重叠窗口认证，只有463只认证通过；不存在新coverage/PIT发布。
审计位于`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260912T090204Z_59396a5f/overlap_scope_audit.json`，
SHA-256=`0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`。
按每只证券首先触发的失败字段：open2674、high1118、volume863、low587、close43、adj_close10；
均标为recoverable，但该标记只是进入显式全历史修复的资格，不表示已经修复或可以放行。

进一步只读对比这5295只的正式父版本和本轮冻结bulk：父版本79274行，按已审计当前ticker
匹配79262行，另12行未匹配，不能当成一致；共6767个匹配行有变化。
其中4680只在匹配范围内仅09-10发生变化，615只还涉及更早日期。此分析按绝对差1e-9定位变化，
不是新的生产容差，不替代身份映射、价格语义或正式认证。不能将全部5295只简单归类为单日修订。

三个有界逐票历史重查样本支持“已发布末日后来发生源修订”，而非CPU故障或必须统一缩放：

| 股票 / 09-10字段 | 正式父版本 | 本轮冻结bulk及当前逐票历史源 |
| --- | ---: | ---: |
| BRBS low | 4.035 | 4.03 |
| UNCY open | 5.06 | 5.07 |
| NKE volume | 29559153 | 29961018 |

三个样本的更早重叠日期一致，当前bulk与逐票历史源一致；这只是三个样本的交叉核对，
不证明所有5295只的修订原因或最终性。现有认证路径只允许可证明的统一尺度调整，遇到非统一
修订要求显式全证券历史重建，因此供应商后续修订会持续阻断日更。
冻结样本及全范围日期分布保存在`reviews/2026-09-12-cup-shadow-investigation/`。

正式coverage仍`76e68448ccea48f5b5e1dbf871c9f6c9`、target=09-10；PIT仍
`857031854a554d9bbfee942a6bfc3919`，绑定旧主表`496db448b54e4ae49701512b3acfd64c`，
不能虚假拼接到新主表。八因子仍09-10的`b6108d673ac447dab5e7be88e7f76ca1`，本轮没有触发重算。
17:32纯构建函数隔离检查09-14/source=09-11候选，明确BLOCKED：
`[US_EQUITY_COVERAGE] target 2026-09-10 is stale; expected 2026-09-11`。
候选表及四张cup表总行数前后完全一致，没有持久化未来候选或补记观察。

后续修复建议：先按冻结差异、身份与公司行动证据分流，设计可审计的逐日源修订认证路径，
验证允许替换的真实源行、修订边界和不可变父版本；对更早日期变化及未匹配行保留更严格调查，
必要时按现有显式整票历史恢复机制重建。不得将三个样本外推为整批通过，不能放宽容差、
无证据覆盖历史、跳过认证，或直接盲跑5295只全历史重建。本轮在真实FAIL处停止，没有新成功日。

### 30.8 2026-09-12 18:13：严格末日修订审计与完整前缀认证

已实现并部署`src/data/coverage_revisions.py`和`scripts/audit_coverage_revisions.py`，仅审计，
没有`--publish`入口。现有统一尺度容差、全历史修复和发布门槛均未改变；历史修复辅助函数
只新增默认关闭的`cache_only`参数，审计读取421份canonical缓存时禁止抓取、创建或覆盖缓存。
先验证失败scope SHA、不可变父版本manifest、正式主表、历史身份映射、完整XNYS日期列表和
逐日冻结artifact SHA，再按完全相等逐字段分类；不按1e-9过滤差异，也不按当前ticker直连。

| 本轮5295只失败范围的分类 | 数量 | 证据边界 |
| --- | ---: | --- |
| LOCAL_REVISION_CANDIDATE | 4844 | 重叠范围仅父版本末日09-10变化，仍须逐票完整历史核验 |
| FULL_HISTORY_REQUIRED | 450 | 更早日期也有差异，不能按末日修订替换 |
| BLOCKED | 1 | CEPS父版本缺09-10，而冻结源新增该日；09-09 volume另由52026变52033 |

该口径复现生产历史身份映射和421份canonical覆盖，与30.7初步current-ticker bulk连接的
4680/615/12行未匹配不同；后者保留为历史诊断，不再用于认证结论。450只更早差异可能涉及
不同修订机制，不能未经逐票证据就全部解释为公司行动或精度变化。

三个指定样本两次真实源回查均通过，第二次使用实际部署模块：

| 股票 | 全历史行数 | 逐字段相同的保留前缀行数 | 09-10真实修订 |
| --- | ---: | ---: | --- |
| BRBS | 1934 | 1932 | low4.035到4.03，volume1095746到1095796 |
| UNCY | 1299 | 1297 | open5.06到5.07，high5.61到5.62，volume1241799到1241816 |
| NKE | 1934 | 1932 | volume29559153到29961018 |

全历史核验范围分别始于2019-01-02、2021-07-12、2019-01-02，止于2026-09-11。要求所有
父版本日期保留、历史别名一致、六个行情字段完全相同（09-10允许已绑定的真实修订），
已有名义收盘价前缀也必须相同；canonical与冻结源的修订及新日逐字段一致，缺失/额外日期、
无效行情、窗口外变化、源冲突、绑定变更均阻断。三只共5161行保留前缀零差异，零缺失、零
隔离、零源冲突，状态VERIFIED_LOCAL_REVISION。该标签是认证证据，不是发布许可；publishable=false。
剩余4841只候选尚未全前缀核验。不能把三只通过当成4844只或5295只通过。

最终报告：`outputs/data_audits/coverage_revisions/target=2026-09-11/run=20260912T101242Z_7fc766f6/audit.json`，
SHA-256=`343ac33a37b5c1cf7f937c912604f60e35975644c3ac5b0a59fe26a29dcb4615`。
绑定原scope SHA=`0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`，
父版本`76e68448ccea48f5b5e1dbf871c9f6c9`、主表`5c738854ad504f1c863c47cf15bb4a63`，
同时保留输入缓存、主表/父manifest、覆盖政策和三个代码文件的SHA及完整逐票证明路径。
本地摘要及部署证据：`reviews/2026-09-12-coverage-revision-authentication/`。

SG单次审计18:10:13到18:13:06成功结束，2分53.099秒，CPU157.209秒、峰值571.5MiB、swap0；
使用MemoryHigh=650M、MemoryMax=750M、TasksMax=64、RuntimeMaxSec=900和Nice=10。
退出0/AUDITED仅表示审计完成，不表示coverage发布成功。71项测试覆盖历史修复、coverage、
修订前缀、日期/身份/源冲突、缓存只读和哈希篡改、禁止发布和有界CLI；SG隔离模块10.92秒全过，
实际部署模块9.90秒再次全过，本轮没有在本地项目venv执行。

正式coverage/PIT仍09-10，PIT仍`857031854a554d9bbfee942a6bfc3919`；没有重算八因子、
新候选或生产日结。本次部署备份`outputs/deploy_backups/coverage_revision_audit_20260912/`，
不包含并行EP修改，也未改动任何发送开关。

后续恢复需完成逐票认证和可断点的受控取证；更早修订/CEPS按全历史真实源、日期与身份合同
单独处理。再审查带expected-parent校验的不可变发布接入、验证PIT重建及09-14候选。
当前工具刻意不承担发布，不能因样本认证成功跳过这些步骤。分钟缺口仍是独立阻断，详见茶杯柄36。

### 30.9 2026-09-12 20:15：有界全量认证与受控发布准备

用户批准继续完整逐票认证、受控发布及PIT/候选验证。本轮复用原有整票canonical替换路径，
不把修订价拼入旧前缀。writer新增`--repair-only`、只准备时允许的offset/limit、1或2个worker、
`--expected-scope-sha256`及`--repair-cache-only`。部分范围不能发布；缓存模式拒绝所有新增
provider请求，包括bulk、identity delta、canonical refresh、替换历史及月末名义价格。
每批最多25只父历史，报告每10只/错误落盘；某票失败不掩盖其他票，但整批不得发布。

绑定固定target=09-11，父coverage=`76e68448ccea48f5b5e1dbf871c9f6c9`、当前主表=
`5c738854ad504f1c863c47cf15bb4a63`，scope SHA=
`0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`。
固定范围5295只，不能仅认证4844候选后跳过450更早修订或CEPS。已审核隔离与查询映射规则未改。

先50只试点全过，报告`run=20260912T114207Z_b0506f0b/full_history_repair.json`，SHA=
`227bb699260d0707914e25e82b5e3deae3f8c6dac3950fd95727b5792999ee94`，208.489秒、701.9MiB峰值，
未发布。19:46完整任务启动，报告目录为
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260912T114630Z_0da7917a/`。
20:14检查点740完成、738通过、TEAD/SNYR失败，仍RUNNING；此处不是终态或发布许可。

TEAD的OB历史到2025-06-09缺88个父版本日期；按同窗口查询TEAD，full/dividend-adjusted/
non-split-adjusted三个端点均只返回2025-05-14..06-09的18日，OB三端点均空。更名映射不能
补齐余下70日。SEC文件证明收购与正式换代码时间，但不证明缺失OHLCV；没有加新映射、拼旧
前缀或删日期。SNYR新源无效bar不与已审核隔离一致，也不能直接加入白名单。原始响应和来源
链接保存在本轮review，必须取得完整真实源证明才能解除发布阻断。

130项coverage/整票修复/基础数据/PIT/适配器回归在SG实际部署模块通过（21.52秒）。缺失父身份
反例先失败，再将批量父读收紧为精确身份集合；更严格helper于19:51部署，准备进程此前已加载
旧代码。任何后续发布必须用新helper重新验证全部缓存，不信任早期报告的PASS标签。writer、
前后helper、报告及备份SHA见`reviews/2026-09-12-coverage-controlled-recovery/README.md`。

正式指针仍为09-10 coverage/PIT，八因子没有重算。20:11不发布的PIT检查实际报当前主表代际
不一致；纯候选构建09-14/source=09-11报日线stale，未写候选或cup表。全范围认证成功后才可
执行cache-only完整发布，再绑定新coverage重建PIT并验证候选；当前存在明确失败，不能仅等
五天、声称PIT就绪或自动开消息。分钟缺口与v3后验代理另见茶杯柄37，不受日线认证通过替代。

### 30.10 2026-09-12 23:36：5295只终态、31项修复及四项发布阻断

原策略全量认证23:08:04结束：5295全部检查，5260通过、35失败，未中断。原报告SHA=
`7cee92f0f7051ed95c2d456926de2ef8fd65f734d3d1298be3db5bcd6f752d3a`；完整失败清单SHA=
`c0a7317280e63de081e2c5c6debf2a145ad99d4fe5d52c1e2c6fb7ec8c8b9b3b`。原报告不覆盖。

逐项复查后部署candidate3修复规则，保持现有校验器和所有门槛不变：16条查询映射含原HSON，
新增15只为INEO、BCIC、GXAI、HYPD、IMDX、NXH、TONX、GDYN、CHAI、VRXA、SMRT、
FGNX、LUXE、WLY、PAMT。仅改变已证明的provider查询键，保留历史ticker/security_id和
正式有效期，重新下载并验证整票历史。NXH限定原Overstock同一发行人链，不混入旧破产BBBY；
GDYN/SMRT保持原SPAC身份，WLY只用Class A。来源及各窗口见review的映射文件和README。
WLY换代码日期由发行人公告/OCC确认，SEC年报仅确认身份，证据角色分开，未伪称SEC提供日期。

既有隔离来源切换至09-04版本`562967c01bb54e2ab39454804cc4ac73`，manifest SHA=
`7388933abb12306b88ebe05bf51c2eb9fa15b29e1ac51c3af273e29af1326579`、quarantine SHA=
`e5ea49cc797694e27cc5c02c21f1e54449cf917a8f1a30cde6cca6dc3b2cba4f`。只选120个精确键：
原6行加16只失败股票的114行，逐字段匹配旧隔离且移除有效父日期0。不是全账本白名单，也不是
新增坏数据豁免。SNYR/QVCG等早期“未在已审核规则中”已经此真实账本证明解决，旧失败仍保留。

policy SHA=`050371ca4001ad8c6e050fc5db118b055e8728cd704d8348fb8e0b92d6c1057f`，
23:13:50生产锁下旧SHA校验、备份、原子部署；23:14:12至23:35:35以严格父身份helper
`09c2256ba03a95ebdffd2f3f818344f2042b375977068793b68d296f58407f1b`重新认证5295只。
最终5291通过、4失败，原失败消除31项（15条别名修复、16只既有隔离继承）；5276只通过项重用
冻结原始字节并重新校验，15只查询映射变化重新抓取整票，绝非借用旧PASS。CEPS 151/151、
MDB 1934/1934日期真实认证通过，不能再把CEPS列为未处理，也不能据此宣布MDB回放通过。

复验报告`run=20260912T151414Z_5c8711d0/full_history_repair.json`，SHA=
`8f65155994b3f813c888745d01840cc4de81af88ef13cda3edc97de36b7e9042`。scope仍完整固定5295只，
SHA=`0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`；更早修订没有跳过。

| 剩余股票 | 当前原始失败 | 独立证据与修复条件 |
| --- | --- | --- |
| TEAD | OB缺88个父日期 | TEAD只能返回其中18日，仍缺70日；要求供应商恢复三类完整真实价格序列 |
| BGMS | CYCC缺190日，2024-12-06..2025-09-11 | BGMS三个端点同窗口全空；换代码身份已证明但行情缺失未解 |
| STEX | BSGM缺313日，2024-06-12..2025-09-11 | STEX三个端点同窗口全空；不拼旧前缀或删除日期 |
| XMAX | NVFY缺2025-11-06/07两日 | XWIN真实数据已存在，但尚缺充分独立的11-10交易代码切换证据，映射未批准 |

因此正式coverage发布为BLOCKED_NOT_ATTEMPTED，不用5291只部分发布。正式coverage仍
`76e68448ccea48f5b5e1dbf871c9f6c9`（09-10），当前主表仍`5c738854ad504f1c863c47cf15bb4a63`，
PIT仍`857031854a554d9bbfee942a6bfc3919`，绑定旧代际。23:36实际PIT检查仍报代际不一致，
09-14/source09-11候选仍报日线stale，MDB实际回放仍报PIT代际不一致；没有生成正式候选。

后续先解决上述源/身份阻断，按当时正式父版本/主表重新核对冻结合同与scope；若合同漂移必须
重新准备，不盲用此次缓存。完整范围全部通过后才可cache-only受控发布并保留expected-parent
CAS及全局质量门槛，再绑定精确新coverage重建PIT、验候选、重跑MDB。八因子没有重算，旧失败
交易日不补记，分钟缺口独立处理。数据链135项、运维/cup/后验41项回归通过；资源见SG55。
本地和SG审计产物：`reviews/2026-09-12-coverage-controlled-recovery/`，包含完整两轮报告、
逐票修复证明、原始源响应和未发送的供应商修复请求草稿。发送仍false、v3仍0/5。
