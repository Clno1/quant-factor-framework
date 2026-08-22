# 研究股票池分层与 NASDAQ100 改造实施记录

更新日期：2026-08-11
状态：SG 双核心研究池、跨池研究和网页功能已正式发布；因子数据冷缓存性能仍待优化

本文件记录
[`research_universe_redesign_requirements.md`](research_universe_redesign_requirements.md)
的实际落地情况。需求文档是口径基线，本文件是实现、上线门槛和运维事实。

## 1. 当前结论

代码已经具备以下完整链路：

```text
Research Universe registry
  -> SP500 / NASDAQ100 PIT 构建与质量门禁
  -> DuckDB + 不可变 Parquet 行情版本
  -> 单池 raw/clean factor 与置信评估
  -> 跨池结论原子发布
  -> 策略研究证据
  -> Target Universe 排名
  -> 回测 / 模拟盘冻结快照与逐票成本账本
```

截至 2026-08-11，SG 已完成双核心池正式发布：

- SP500 DatasetVersion `fcd51776db3b4266be12926cbe0d57b3`，目标交易日 `2026-08-10`；
- NASDAQ100 DatasetVersion `9c5abc4b58a5414e911153cdda6a429c`，165 个历史/当前证券、
  248,893 行、目标日覆盖率 100%；
- NASDAQ100 research publication `763f89c3-3b62-4fd2-9d6b-968f3bf4b4b2`，8 个因子均完成
  raw/clean、IC、置信评估和 next-open 分组回测；
- MAG7 DatasetVersion `e4c7ad541f3d4480a0b15844adb59195`；
- 三个研究池目标交易日一致，跨池结论为 `ROBUST=3`、`SEGMENT_SPECIFIC=1`、
  `INSUFFICIENT=4`；
- Web、研究 API、因子数据日期截面和真实 systemd unit 已通过生产验收。

## 2. 分阶段状态

| 阶段 | 代码状态 | 正式数据/生产状态 |
|---|---|---|
| Phase 0 正确性 | 完成 | SG 三池均为 2026-08-10 正式版本 |
| Phase 1 领域模型 | 完成 | SG 已部署并由网页/API 消费 |
| Phase 2 NASDAQ100 数据 | 完成 | PIT、行情、事件账本和四哈希已发布 |
| Phase 3 跨池研究 | 完成 | 双池 8 因子与跨池 generation 已发布 |
| Phase 4 网页 | 完成 | 页面/API 功能通过；冷缓存性能待优化 |
| Phase 5 用户池晋升 | 不在本期范围 | 未实施 |

## 3. 数据正确性改造

### 3.1 单版本 Reader

`load_published_bundle()` 一次解析显式 `DatasetVersion`。行情、universe、membership 和因子发布
都绑定该版本；回测等待数据恢复后也先冻结契约，再进入 `running`。

Reader 现在校验四类文件：

```text
bars_sha256
universe_sha256
membership_sha256（PIT 池必需）
manifest_sha256
```

旧 catalog 行可以读取状态，但缺 v2 哈希时不能被正式消费。任一不可变文件被修改，
`require_version()` 立即失败。

### 3.2 因子 generation

因子 publication 绑定每个因子的 generation ID。组合器加载 raw/clean bundle 后再次比较冻结的
generation ID；即使发布恰好在组合过程中切换，也会拒绝混合版本运行。

### 3.3 历史证券与中性化

历史 PIT ticker 通过 security master 补全名称和行业。无法确认的分类写成显式 `UNKNOWN`，不再用
`None` 让股票静默退出。预处理审计保存每日行业覆盖、回归样本、跳过原因和
`raw_non_null_clean_all_null` 清单；正式研究低于行业覆盖门槛或出现活跃股票被无声清空时失败。

## 4. 三层股票池

唯一预设注册表是 [`../configs/research_universes.yaml`](../configs/research_universes.yaml)：

| 股票池 | 角色 | Membership | 进入跨池总体结论 |
|---|---|---|---|
| SP500 | `PRIMARY` | PIT | 是 |
| NASDAQ100 | `SECONDARY` | PIT | 是 |
| MAG7 | `REFERENCE` | STATIC | 否 |

旧 `configs/default.yaml:universes.enabled` 已删除，脚本、网页、回测和模拟盘不再维护第二份预设池
名单。

用户 Watchlist 是 `TARGET`。每次修订保存 canonical ticker set 的 SHA-256；普通 Target
Universe 不发布正式 IC PASS。

## 5. NASDAQ100 发布门禁

实现入口：

- `src/data/nasdaq100_pit.py`：指数专用规范化、候选重建、官方当前成分比对和发布；
- `configs/nasdaq100_pit_verification.yaml`：10 组官方加入/退出事件和来源；
- `configs/nasdaq100_pit_corrections.yaml`：精确事件修正、官方来源和逐条审计；
- `scripts/run_data_pipeline.py pit`：依次运行所有 PIT 研究池，单池失败不覆盖旧 publication；
- `quant-market-data.service`：行情阶段包含 SP500、NASDAQ100 和 MAG7。

正式发布必须同时通过：

1. FMP endpoint 结构和 `dateAdded` 生效日契约；
2. 至少 10 组官方历史事件逐组匹配；
3. FMP 当前 ticker set 与 Nasdaq 官方 ticker set 完全一致；
4. PIT 快照规模、事件和历史并集检查；
5. 行情、PIT、行业覆盖和四文件哈希检查。

FMP 在 2026-08-10 的当前名单和历史事件中存在六类已确认差异。系统保留原始 payload，不做
宽泛 alias，只在日期、加入代码、删除代码、证券名和原因同时匹配时应用审核规则：

| 事件 | 处理 |
|---|---|
| HONA 2026-06-29 | 补充 spin-off 加入事件，并以 Nasdaq 当前名单交叉确认 |
| EA 2026-08-05 | 补充私有化删除事件 |
| TTWO/SGEN 2023-12-18 | 修复 FMP 有 `symbol` 但缺 `addedSecurity` 导致的加入丢失 |
| SOLS 2025-11-06 | 把 FMP 同票加入/删除修正为删除事件 |
| XLNX 2022-02-22 | 把错误的 `Annual Re-ranking` 原因修正为 AMD 并购 |
| ANSS 2025-07-28 | 把错误的 `Annual Re-ranking` 原因修正为 Synopsys 并购 |

正式 PIT run `pit_20260811T050906_58c2a182` 在 systemd 环境中再次通过：40 个快照、0 条不一致。
供应商以后修改任一被审核字段时，规则不会模糊匹配，而会重新 fail closed。

## 6. 跨池结论

领域包 `src/research_universes/` 提供 typed registry、单池证据、规则评估和原子发布。结论为：

```text
ROBUST
PRIMARY_ONLY
SEGMENT_SPECIFIC
CONFLICT
INSUFFICIENT
REJECT
```

跨池算法不平均 IC。它分别保留方向、PASS/WATCH/FAIL、样本、显著性、经济收益和成本，并把
每个来源池的 dataset version、research publication、factor generation 和 checksum 写进
manifest。MAG7 不参与总体结论。

产物：

```text
outputs/research/cross_universe/generation=<ID>/
  factor_assessments.parquet
  manifest.json
outputs/research/cross_universe/publication.json
```

## 7. 网页与 API

左侧导航分为“研究、策略、交易验证、市场监控”，四组默认展开且无序号。主要页面：

```text
/research
/research/universes
/research/universes/{universe}
/research/factors/{factor_id}
/watchlists
/backtests/{task_id}
/paper/{account_id}
```

研究总览支持跨池结论、单池结论和类别筛选。重复的“跨池稳健性”导航已合并到研究总览，
旧 `/research/cross-universe` 地址跳转到 `/research`。

独立“股票排名”网页入口已移除，旧 `/rankings` 和
`/strategies/{strategy_id}/ranking` 地址分别跳转到策略列表和策略详情。底层策略评分、目标权重
计算以及只读排名 API 保留，继续供回测、模拟盘和审计程序复用。

只读 API：

```text
GET /api/research/status
GET /api/research/universes
GET /api/research/universes/{universe}
GET /api/research/factors
GET /api/research/factors/{factor_id}
GET /api/research/factors/{factor_id}/cross-universe
```

## 8. Portfolio Run 冻结

新建回测和模拟盘账户保存：

```text
strategy_snapshot
research_evidence_snapshot
target_universe_snapshot
dataset_version_id + 四类 checksum
factor_publication_id / runtime_factor_id
execution_config
risk_config
```

创建边界会把 execution 补全为完整配置并深拷贝所有快照。之后修改
`configs/default.yaml`、策略或 Watchlist，不会改变已创建记录。旧记录若没有完整冻结配置，页面
明确标为 `INVALID`，不再描述成可比较的“无摩擦 close-to-close”。

回测和模拟盘详情都显示逐票开盘原价、成交价、参与率、动态滑点，以及券商佣金、SEC、FINRA
TAF/CAT、清算、转付、交易所和总摩擦成本。

## 9. SG 正式上线记录

2026-08-11 的变更前备份位于：

```text
/home/projects/quant-backups/nasdaq100-pit-fix-20260811T124759CST/
```

两个临时屏蔽 NASDAQ100 的 systemd drop-in 已移入该备份目录，没有删除。验收结果：

1. NASDAQ100 candidate 和正式 PIT 均为 PASS；
2. DatasetVersion `9c5abc4b58a5414e911153cdda6a429c` 通过 Reader 哈希校验；
3. 8 因子研究和跨池 generation 正式发布；
4. `systemd-analyze verify` 通过，唯一提示是腾讯云 `tat_agent` 的旧 `/var/run` 路径；
5. 实际启动 `quant-market-data.service`：SP500/NASDAQ100 PIT 均发布，三池行情均为 NOOP，
   退出码 0；
6. 实际启动 `quant-factor-research.service`：三池研究均为 NOOP，跨池重新发布，退出码 0；
7. `quant-web.service` active，认证后的研究页、因子数据页和 API 均返回 200。
8. 修复五个旧 unit 对已删除 `runlog/` 的强制挂载后，US_LIQUID_5M 与板块 EOD 依赖链也完成
   实跑，最终没有失败的 Quant unit。

关键命令：

```bash
.venv/bin/python scripts/run_data_pipeline.py pit \
  --universe NASDAQ100 --candidate-only --json

.venv/bin/python scripts/run_data_pipeline.py pit --json

.venv/bin/python scripts/run_data_pipeline.py update \
  --universe SP500 --universe NASDAQ100 --universe MAG7 \
  --force --workers 6 --json

.venv/bin/python scripts/run_factor_research.py \
  --universe SP500 --universe NASDAQ100 --universe MAG7 --json

.venv/bin/python scripts/run_data_pipeline.py status --json
.venv/bin/python scripts/check_app_storage.py
```

页面验收至少包括：

```text
/research
/research/universes/SP500
/research/universes/NASDAQ100
/research/factors/MOM_12M
/strategies
/watchlists
/backtests
/paper
```

## 10. 完成门槛

以下数据正确性和生产功能门槛已经满足：

- SP500、NASDAQ100 最新 DatasetVersion 都有四类完整哈希并通过 Reader；
- 两池 PIT 历史活跃 ticker 的 raw/clean 和行业覆盖审计通过；
- 两池 8 因子研究绑定同一 target session；
- 跨池 manifest 的所有 source binding 均为 `AVAILABLE`；
- SG `systemd-analyze verify`、Reader、Web/API 和 journal 验收通过；
- SG 重启后只读取已完成的原子 publication。

独立性能门槛尚未完全满足：SG 因子截面冷缓存实测 2.56 秒，高于目标 2 秒；热缓存约 0.077 秒、
单股历史约 0.20 秒。该项不阻止数据发布，但在性能优化完成前不得标记为“性能验收完成”。
