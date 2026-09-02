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
