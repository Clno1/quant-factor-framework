# 研究股票池分层与 NASDAQ100 改造实施记录

更新日期：2026-08-09  
状态：代码实现与本地 379 项回归完成，正式数据重发和 SG 部署验收待执行

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

但截至 2026-08-09，不能把本机或 SG 宣称为新版正式发布完成：

- 本机 SP500 和 MAG7 最新版本仍是 2026-07-31 的旧 catalog schema，缺
  `universe_sha256` 和 `manifest_sha256`，新版 Reader 将其标为 `INVALID`；
- NASDAQ100 当前成分严格比对发现 FMP 独有 `EA`、Nasdaq 官方独有 `HONA`，候选发布按设计
  fail closed；
- NASDAQ100 尚无正式行情和 8 因子研究发布；
- 当前跨池 generation `20260809T103820_bccb53fc05` 的 8 个因子均为
  `INSUFFICIENT`；
- 本轮改造尚未部署到 `/home/projects/quant`，SG 仍以服务器实机版本为准。

## 2. 分阶段状态

| 阶段 | 代码状态 | 正式数据/生产状态 |
|---|---|---|
| Phase 0 正确性 | 完成 | 必须重发 SP500/MAG7 v2 版本并重跑研究 |
| Phase 1 领域模型 | 完成 | 待随代码部署 SG |
| Phase 2 NASDAQ100 数据 | 构建器、10 组官方事件门禁、调度完成 | 当前成分不一致，禁止发布 |
| Phase 3 跨池研究 | 评估、不可变 generation、原子 pointer 完成 | NASDAQ100 缺失，结论为 `INSUFFICIENT` |
| Phase 4 网页 | 页面、API、导航、冻结证据和成本账本完成 | 本地验收后再部署 SG |
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
- `scripts/run_data_pipeline.py pit`：依次运行所有 PIT 研究池，单池失败不覆盖旧 publication；
- `quant-market-data.service`：行情阶段包含 SP500、NASDAQ100 和 MAG7。

正式发布必须同时通过：

1. FMP endpoint 结构和 `dateAdded` 生效日契约；
2. 至少 10 组官方历史事件逐组匹配；
3. FMP 当前 ticker set 与 Nasdaq 官方 ticker set 完全一致；
4. PIT 快照规模、事件和历史并集检查；
5. 行情、PIT、行业覆盖和四文件哈希检查。

当前失败证据位于：

```text
data/raw/pit/NASDAQ100/asof=2026-08-07/
  run=pit_20260809T093001_8244a7bc/diagnostics.json
```

该失败不应通过手写无来源 alias 绕过。先等待供应商跟进；若持续不一致，再依据正式指数公告增加
精确日期、精确证券和可审计来源的规则。

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

## 9. SG 正式上线顺序

先完成代码审阅、测试和 commit，再执行以下步骤；本轮尚未执行这些服务器动作。

1. 一致性备份 SG 代码、`/etc/quant`、systemd、DuckDB、SQLite、`data/lake` 和 `outputs`。
2. 部署精确 commit，不覆盖 SG 的 `data/`、`outputs/`、`logs/` 和密钥文件。
3. 安装 root units，运行 `systemd-analyze verify`。
4. 先运行 NASDAQ100 candidate；不通过时停止正式 NASDAQ100 发布。
5. 两个 PIT 池通过后，强制重发 SP500、NASDAQ100、MAG7 的当日 v2 行情版本。
6. 使用 Reader 验证四类文件哈希，再运行 08:45 研究链。
7. 要求 SP500/NASDAQ100 同一 target session，跨池不再为 `INSUFFICIENT`。
8. 重启 Web，验收研究、排名、回测、模拟盘和 API。
9. 保留旧版本和备份，不删除历史 Parquet、SQLite 或 DuckDB。

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

只有以下条件全部成立，才把本改造标为生产完成：

- SP500、NASDAQ100 最新 DatasetVersion 都有四类完整哈希并通过 Reader；
- 两池 PIT 历史活跃 ticker 的 raw/clean 和行业覆盖审计通过；
- 两池 8 因子研究绑定同一 target session；
- 跨池 manifest 的所有 source binding 均为 `AVAILABLE`；
- SG `systemd-analyze verify`、storage check、Web/API 和 journal 验收通过；
- SG 重启后只读取已完成的原子 publication。
