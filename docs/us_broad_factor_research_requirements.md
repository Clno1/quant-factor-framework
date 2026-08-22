# 全美宽基因子研究与因子数据浏览改造需求

更新日期：2026-08-12  
状态：需求已确认 v1.0；自 2026-08-12 起按第 16 节分阶段实施  
适用范围：全美证券主数据、`US_EQUITY_COVERAGE`、`US_LIQUID_5M`、宽基因子数据、正式宽基研究和因子数据浏览器

## 1. 文档目的

当前系统已经能够对 SP500、NASDAQ100 和 MAG7 发布版本绑定的 raw/clean 因子数据，并在网页中查询
日期截面和单股历史。但“哪些证券有数据”“哪些证券参加当日横截面比较”“哪些股票池用于证明因子
可信”仍被绑定在同一个 Research Universe 概念中，导致不属于当前三个池的股票无法正常浏览因子
数据。

本需求要解决以下问题：

1. MDB、AEVA 等不属于当前 SP500/NASDAQ100 成分的活跃美股也能查询因子数据；
2. 系统能对全美流动股票计算每日 clean、排名和百分位；
3. 系统可以在数据达到严格 PIT 门槛后，对宽基股票池计算 IC、ICIR、分组回测和置信评估；
4. SP500、NASDAQ100 继续承担分市场稳健性验证，不因引入宽基池而失去作用；
5. 当前名单不得倒灌到历史，退市、代码变更和 IPO 不得从历史研究中消失；
6. 页面必须清楚区分“有 raw”“当日进入宽基比较”“有正式统计研究结论”三种能力。

本文件是需求与数据合同，不是实施记录。获批后应另建 implementation 文档记录代码落地、测试、
数据回填和 SG 上线过程。

## 2. 决策摘要

目标架构采用五层模型：

```text
全美证券主表 Security Master
  -> 全美行情覆盖 US_EQUITY_COVERAGE
  -> 全美流动估计池 US_LIQUID_5M（PIT）
  -> SP500 / NASDAQ100 稳健性验证
  -> Watchlist 目标投资池与最终组合
```

核心决策如下：

| 编号 | 决策 | 本文推荐值 |
|---|---|---|
| D-001 | `US_ACTIVE` 的定位 | 仅表示 FMP 当前供应商快照，不是正式研究池 |
| D-002 | 全市场数据覆盖 ID | 新增 `US_EQUITY_COVERAGE` |
| D-003 | 宽基比较池 ID | 保留 `US_LIQUID_5M`，但重建为真正 PIT 动态池 |
| D-004 | 宽基流动性口径 | 月末按截至当日的 20 日平均成交额筛选，门槛 500 万美元 |
| D-005 | 最低价格 | 月末收盘价至少 1 美元 |
| D-006 | 正式回填起点 | 推荐 2019-01-01；研究评价区间仍可从 2020 或近 5 年开始 |
| D-007 | 因子数据与置信研究 | 分成两个原子 publication，不能互相冒充 |
| D-008 | 网页默认宽基排名 | `US_LIQUID_5M` 完整有效截面 |
| D-009 | 非宽基成员 | 可以显示 coverage raw；clean、排名显示不适用 |
| D-010 | SQLite 职责 | 不保存大规模行情、因子矩阵或排名 |
| D-011 | 历史行业不严格 PIT 时 | 可以发布带醒目标记的观察数据，不得发布正式宽基置信结论 |
| D-012 | SG 上线 | 至少五个不同交易日影子核验通过后再切换页面默认入口 |
| D-013 | SG 计算资源 | 固定使用现有 2 vCPU、1.9 GiB RAM 服务器，不以扩容为实施前提 |
| D-014 | 任务频率 | 因子数据每日增量；完整宽基研究按周在周末串行运行 |
| D-015 | 重建方式 | 首次回填和低频全量重建必须分片、断点续跑，不要求一次进程完成 |

第 18 节中的七项产品与数据选择已按推荐值确认，后续如真实数据门禁暴露新冲突，再单独形成决策项。

## 3. 当前系统审计结论

### 3.1 当前正式研究池只有三个

`configs/research_universes.yaml` 当前注册：

| universe | role | confidence | cross-universe |
|---|---|---:|---:|
| SP500 | PRIMARY | 是 | 是 |
| NASDAQ100 | SECONDARY | 是 | 是 |
| MAG7 | REFERENCE | 否 | 否 |

`scripts/run_factor_research.py::_configured_universes()` 直接返回该注册表的全部 ID，
`FactorObservationReader._normalize_universe()` 也要求查询股票池必须存在于同一注册表。因此当前网页
只能选择这三个池，是代码和 publication 合同的结果，不是因子公式本身的限制。

### 3.2 当前 `US_ACTIVE` 与 `US_LIQUID_5M`

当前链路为：

```text
FMP company-screener 当前快照
  -> US_ACTIVE
  -> 当天 price × volume >= 5,000,000
  -> US_LIQUID_5M
  -> 约 180 个日历日行情
  -> 动量突破扫描
```

`US_ACTIVE` 是当前快照。正式发布后使用 `US_LIQUID_5M` 名称。当前配置
`data.liquid_universe.initial_start = 180D`，其目的原本是支持短周期动量扫描，不足以支持完整的
MOM_6M、MOM_12M 和五年因子置信研究。

当前 `_versioned_membership()` 在没有历史版本时，会把本次当前名单写到 `initial_start` 作为基线。
这种做法可以让短周期当前扫描启动，但不能被描述成历史 PIT 研究。

### 3.3 2026-08-10 SG 实测基线

只读检查 SG 正式版本得到：

| 项目 | 实测值 |
|---|---:|
| `US_LIQUID_5M` target session | 2026-08-10 |
| version ID | `ff9c8527e3a54f3aaade58d8ef939f68` |
| ticker count | 2,953 |
| bar rows | 361,216 |
| 每票大致交易日 | 124 |
| membership snapshot dates | 2 |
| MDB 是否当前成员 | 是 |
| AEVA 是否当前成员 | 是 |
| MDB 当前单日成交额 | 约 7.06 亿美元 |
| AEVA 当前单日成交额 | 约 5,283 万美元 |

两个 membership 快照分别是 2026-02-11 和 2026-08-10，且都包含相同的 2,953 只证券。该版本
可以继续服务当前动量扫描，但不得用于声称 2026-02-11 当天已经知道 2026-08-10 的完整成员名单。

### 3.4 当前可以复用的能力

以下现有能力应保留并扩展：

- `MarketDataWriter` 单写者、增量摄取、质量门禁和不可变 Parquet；
- DuckDB `dataset_versions`、`quality_checks` 和 `published_versions`；
- `MarketDataReader` 强制哈希校验和禁止旧文件回退；
- `load_published_bundle()` 一次冻结 DatasetVersion；
- raw/clean 同 generation 原子发布；
- `research_publication.json` 绑定行情、membership、factor generation 和 confidence；
- `FactorObservationReader` 的 rank、percentile、PIT 和并发 publication 校验；
- 网页 fail-closed 状态和不调用 FMP 的读取约束；
- 现有 SP500/NASDAQ100 跨池稳健性结论。

### 3.5 当前不能直接复用的假设

下列假设对三千只以上动态宽基池不再成立：

1. 一个 DatasetVersion 同时等于行情覆盖和股票池 membership；
2. ticker 可以永远充当稳定证券主键；
3. 当前供应商行业可以无标记地回填全部历史；
4. factor browser 每次把整个宽矩阵读入 Pandas 内存仍然足够快；
5. 因子数据存在就等于 IC/ICIR 和置信研究已经正式通过；
6. 每日完整重算八个因子、分组回测和置信报告在 SG 当前规格上必然能按时完成；
7. 当前 screener 单日成交额可以代表稳定可投资性。

## 4. 新领域模型

### 4.1 Security Coverage Universe

`US_EQUITY_COVERAGE` 表示系统能够识别并保存历史行情的美国股票覆盖范围。

它回答：

> 系统是否认识这只证券，并且能否在指定日期计算公式级 raw 因子？

它不直接回答：

- 该股票是否适合交易；
- 是否进入当日 clean 横截面；
- 在全美流动股票中排名第几；
- 因子在全市场是否可信。

### 4.2 Broad Estimation Universe

`US_LIQUID_5M` 表示从 coverage 中按 point-in-time 流动性和基础可交易条件筛选出的动态宽基池。

它回答：

> 在当日收盘后可以获得的信息下，哪些股票进入同一横截面做去极值、中性化、Z-score、排名和后续研究？

该池应覆盖大盘、中盘和具有足够流动性的小盘股票。MDB、AEVA 在 2026-08-10 都应进入该池。

### 4.3 Validation Universes

SP500 和 NASDAQ100 继续作为正式验证池：

- SP500 检查美国大盘环境；
- NASDAQ100 检查大型成长/科技环境；
- 两者继续形成现有 `ROBUST/PRIMARY_ONLY/CONFLICT/...` 结论；
- 宽基结论不得简单替代或平均现有两池结论。

### 4.4 Reference Universe

MAG7 继续只作参考，不承担统计证明。

### 4.5 Target Universe

Watchlist 继续作为用户目标投资池：

- 可以消费已经发布的宽基或正式研究因子；
- 可以按自身截面重新标准化，但必须显式显示 `normalization_universe`；
- 不因包含某只股票就自动获得正式 IC/ICIR；
- 回测和模拟盘继续冻结 Watchlist revision、数据版本和因子 publication。

### 4.6 通用 universe 能力模型

现有单一 `role` 不足以表达 coverage、因子数据和正式研究能力。注册模型建议扩展为：

```yaml
schema_version: 2
universes:
  US_EQUITY_COVERAGE:
    purpose: COVERAGE
    membership_type: PIT
    factor_publication_mode: RAW_ONLY
    confidence_enabled: false
    cross_universe_enabled: false

  US_LIQUID_5M:
    purpose: ESTIMATION
    membership_type: PIT
    factor_publication_mode: FACTOR_DATA
    confidence_enabled: false
    cross_universe_enabled: false
    parent_data_universe: US_EQUITY_COVERAGE

  SP500:
    purpose: VALIDATION
    verdict_role: PRIMARY
    membership_type: PIT
    factor_publication_mode: FULL_RESEARCH
    confidence_enabled: true
    cross_universe_enabled: true
```

建议枚举：

| 字段 | 允许值 |
|---|---|
| `purpose` | `COVERAGE / ESTIMATION / VALIDATION / REFERENCE` |
| `factor_publication_mode` | `NONE / RAW_ONLY / FACTOR_DATA / FULL_RESEARCH` |
| `verdict_role` | `NONE / PRIMARY / SECONDARY / REFERENCE` |

用户 Watchlist 不进入版本控制注册表，继续存 SQLite。

## 5. 全美证券主表要求

### 5.1 稳定主键

ticker 只是某段时间内的交易代码，不能继续作为长期唯一身份。新增 `security_id` 作为内部稳定主键。

要求：

- 同一家公司更名或 ticker 变更后，`security_id` 不变；
- 同一 ticker 被另一家公司重新使用时，必须对应不同 `security_id`；
- 页面输入 ticker 时先按查询日期解析到 `security_id`；
- bars、membership、factor observation 新宽基链路均保存 `security_id`；
- 在旧模块完成迁移前仍保留 ticker 展示列，但遇到歧义必须 fail closed。

### 5.2 DuckDB 主表

建议新增：

```sql
security_master(
  security_id VARCHAR PRIMARY KEY,
  issuer_id VARCHAR,
  current_ticker VARCHAR,
  name VARCHAR,
  asset_type VARCHAR,
  primary_exchange VARCHAR,
  country VARCHAR,
  currency VARCHAR,
  cik VARCHAR,
  isin VARCHAR,
  cusip VARCHAR,
  listing_date DATE,
  delisting_date DATE,
  trading_status VARCHAR,
  source VARCHAR,
  source_asof DATE,
  updated_at TIMESTAMPTZ
)

security_symbol_history(
  security_id VARCHAR,
  ticker VARCHAR,
  exchange VARCHAR,
  effective_from DATE,
  effective_to DATE,
  is_primary BOOLEAN,
  event_type VARCHAR,
  source VARCHAR,
  source_asof DATE
)

security_classification_history(
  security_id VARCHAR,
  sector VARCHAR,
  sub_industry VARCHAR,
  effective_from DATE,
  effective_to DATE,
  knowledge_date DATE,
  classification_policy VARCHAR,
  source VARCHAR,
  source_asof DATE
)
```

SQLite 不保存这些表。它仍只保存策略、Watchlist、任务、模拟盘账户和业务账本。

### 5.3 示例数据

```text
security_master
security_id  current_ticker  name                    status  listing_date
sec_001      MDB             MongoDB, Inc.            ACTIVE  2017-10-19
sec_002      AEVA            Aeva Technologies, Inc. ACTIVE  2021-03-15

security_symbol_history
security_id  ticker  effective_from  effective_to  event_type
sec_001      MDB     2017-10-19      null          LISTING
```

### 5.4 资产类型规则

Coverage 默认保留：

- NYSE、NASDAQ、AMEX 的普通股；
- 正常上市、已退市和历史上曾经正常上市的普通股；
- REIT 普通股。

Coverage 可以保存但默认不进入 `US_LIQUID_5M`：

- ADR；
- preferred stock；
- SPAC unit；
- warrant、right；
- closed-end fund。

默认排除：

- ETF、mutual fund；
- OTC/Pink Sheet；
- 指数、期权、债券、加密货币；
- 无法确定资产类型的证券。

SPY、QQQ、IWM 等基准 ETF 可以作为收益比较和运行健康检查所需的辅助行情写入 coverage，但必须标记
为 `BENCHMARK_ONLY`，不得计入 `US_LIQUID_5M` membership、clean 截面或排名分母。

ADR 是否进入估计池是第 18 节待确认项。无论最终选择如何，都必须在数据中保留显式字段，不能只
根据 ticker 字符串猜测。

### 5.5 来源与时间语义

每一个证券属性都必须区分：

```text
effective_from / effective_to：该事实适用于哪个市场日期
knowledge_date：系统在什么时候能够知道该事实
source_asof：供应商数据截至什么时候
```

如果只能获得当前行业，必须记录：

```text
classification_policy = LATEST_KNOWN_BACKFILL_NOT_PIT
```

不得把它标成严格 PIT。未知行业统一为 `UNKNOWN`，不能无声删除证券。

### 5.6 主表质量门禁

| 检查 | 发布门槛 |
|---|---:|
| 当前宽基成员 `security_id` 覆盖率 | 100% |
| 当前宽基成员资产类型已知率 | 100% |
| 同日 ticker 映射冲突 | 0 |
| `effective_from > effective_to` | 0 |
| 已退市但状态仍 ACTIVE 的已知冲突 | 0 |
| 当前成员名称覆盖率 | >= 99% |
| 当前成员行业覆盖率 | >= 95% |
| 正式宽基置信研究的 PIT 行业覆盖率 | >= 95% |

## 6. `US_LIQUID_5M` PIT 规则

### 6.1 决策时间

每个 `observation_date = t` 的宽基 membership 只能使用截至 t 收盘已经知道的数据。该 membership
用于计算 t 收盘后的因子截面，并最早在下一交易日开盘执行。

允许使用：

- t 及以前的收盘价和成交量；
- t 收盘前已经生效的上市、退市和代码变更事实；
- t 当时可得的证券类型和分类。

禁止使用：

- t+1 的价格、成交量或成员信息；
- 今天的 `US_ACTIVE` 名单反向覆盖过去；
- 未来退市结果提前剔除历史证券；
- 未来行业分类或当前市值冒充历史数据。

### 6.2 默认重构频率

推荐 V1 每月最后一个 XNYS 交易日重构一次完整 membership，次月沿用最近一次正式快照。这样可以：

- 降低门槛附近股票的日常反复进出；
- 与现有月末因子分组回测更一致；
- 降低 membership 和因子 generation 的日常计算成本；
- 仍然保持完整 PIT 语义。

IPO、退市和无效证券可以产生月中强制事件，不必等到下个月末。

### 6.3 默认筛选规则

定义：

```text
ADV20(t) = mean(close(d) × volume(d)), d 为截至 t 的最近 20 个有效交易日

eligible_universe(t, security)
= listed_as_of(t)
  AND ordinary_equity
  AND primary_exchange IN {NYSE, NASDAQ, AMEX}
  AND close(t) >= 1 USD
  AND ADV20(t) >= 5,000,000 USD
  AND valid_price_volume_sessions_last_20 >= 15
  AND has_not_stopped_trading_before(t)
```

注意：`ADV20` 是平均成交额，不是当前代码中的单日 `price × volume`。

`has_not_stopped_trading_before(t)` 表示证券在 t 仍有合法交易资格：若 t 是供应商确认的最后可交易日，
它仍可属于 t 的截面；从下一个交易日起才退出。不能只比较一个含义不明的 `delisting_date` 字段。

### 6.4 因子 warm-up 与 membership 分离

股票可以进入 `US_LIQUID_5M`，但某个长周期因子仍可能因历史不足而暂时没有 raw。不得为满足
MOM_12M 而把所有新股从短周期因子中删除。

| 因子 | 当前代码所需历史窗口 |
|---|---:|
| MOM_1M | 21 个先前交易日 |
| MOM_3M | 84 个先前交易日 |
| MOM_6M | 147 个先前交易日 |
| MOM_12M | 273 个先前交易日 |
| VOL_20D | 20 日目标窗口 |
| VOL_60D | 60 日目标窗口 |
| REVERSAL | 5 日 |
| TURNOVER | 20 日 |

因子有效性使用独立状态：

```text
VALID
NOT_PIT_MEMBER
CALCULATION_WINDOW_INSUFFICIENT
RAW_MISSING
CLEAN_MISSING
CLASSIFICATION_MISSING
DATA_QUALITY_REJECTED
```

### 6.5 membership 快照

现有 `build_membership_mask()` 把每个日期视为完整快照，并对后续日期使用最近快照。新宽基发布应
继续兼容 `date,ticker,active`，同时增加稳定身份和审计字段：

```text
date
security_id
ticker
active
selection_price
adv20_usd
valid_sessions_20d
asset_type_pass
price_pass
liquidity_pass
reason_codes
source_data_version_id
```

正式 membership 文件只需包含完整快照及所有 active 成员；完整候选与失败原因另存
`eligibility_audit.parquet`，避免把所有失败行混成 active snapshot。

### 6.6 IPO、停牌和退市

- IPO 在拥有足够 ADV20 观测并通过下一次重构后进入；
- 因 factor warm-up 不足而无 raw 是正常状态；
- 临时停牌不自动回写历史 membership；
- 月中确认退市时生成明确退出事件；
- 回测继续使用现有 `next_open_or_last_close_to_cash` 退出政策；
- 已退市证券必须保留历史 bars、membership 和因子观测；
- 供应商没有可靠退市价格时必须标记，不得把收益默认记为 0。

### 6.7 禁止的快捷方式

- 不允许把当前 2,953 只名单回填到 2019 或 2020；
- 不允许根据今天仍有行情推断历史上一直 eligible；
- 不允许只保存当前成员而丢掉历史退出成员；
- 不允许用 SP500/NASDAQ100 membership 补全宽基 membership；
- 不允许 Web 查询触发临时重建；
- 不允许 PIT 不完整时把池标为 `PUBLISHED`。

## 7. 因子数值与排名口径

### 7.1 raw

`raw` 是公式级数值，只依赖该证券截至 observation date 的必要历史输入。对于当前八个因子，输入
主要是复权 OHLCV/returns。

原则：

- coverage 中的股票即使不在 `US_LIQUID_5M`，也可以有 raw；
- 同一数据版本、同一因子参数、同一证券和日期的 raw 不应因比较池不同而变化；
- SP500 与宽基链路中重叠股票的 raw 应做精确一致性校验；
- raw 缺失必须返回原因，不能填 0。

### 7.2 clean

`clean` 是在指定 `normalization_universe` 的当日完整有效截面上执行：

```text
raw
  -> 横截面去极值
  -> 行业中性化
  -> 可选 PIT 市值中性化
  -> 最终横截面 Z-score
  -> clean
```

因此 clean 必须带上：

```text
normalization_universe_id
universe_version_id
preprocessing_methodology_version
classification_policy
```

同一股票在 SP500 和 `US_LIQUID_5M` 中的 clean 不要求相同，因为比较截面不同。

### 7.3 oriented value

继续使用因子注册表预设方向：

```text
oriented_value = clean × direction
```

方向不允许根据宽基历史表现重新选择。

### 7.4 rank

```text
rank_eligible(t, security)
= PIT_member(t, security)
  AND finite(clean(t, security))

factor_rank
= rank(oriented_value, descending=True, ties="min")
```

强制口径：

- `rank=1` 永远表示按预设因子方向最优；
- rank 分母是该日期 `US_LIQUID_5M` 完整有效截面；
- 搜索、分页和只看某一行业不能改变 rank；
- 非成员即使有 raw，也没有该池 clean/rank；
- `eligible_count` 必须随每条结果返回；
- ticker 仅用于同 rank 的稳定展示顺序，不改变数学排名。

### 7.5 percentile 和 quintile

继续沿用现有因子浏览器口径：

```text
factor_percentile = percentile_rank(oriented_value, ties="average") × 100
```

- 最优接近或等于 100%；
- Q1 最弱，Q5 最强；
- percentile、rank 和 quintile 必须来自同一个 PIT 截面。

### 7.6 是否保存 rank

首选方案仍是由 `clean + direction + membership` 确定性计算，不写 SQLite。

宽基性能达不到门槛时，可以在同一不可变 factor generation 内增加派生 rank Parquet，但必须：

- manifest 标明它是 derived artifact；
- 随机抽样能从 clean 重新计算并精确一致；
- publication 与 clean/membership 同时切换；
- 不允许 rank 文件成为脱离 clean 的第二事实源。

## 8. 历史数据与供应商门槛

### 8.1 FMP 能力审计必须先做

开始批量回填前，必须使用 SG 实际 API key 对以下能力做一次只读 spike：

1. 当前 active common stocks 全量分页和去重；
2. 历史/已退市证券列表；
3. 已退市证券 EOD 历史是否可读；
4. ticker change/merger 映射；
5. IPO/listing date；
6. 复权 OHLCV 的拆股、分红语义；
7. 历史行业、资产类型和主上市地可用性；
8. 请求次数、带宽、分页和订阅权限；
9. survivorship-bias-free EOD 端点是否仍对当前套餐开放；
10. 同一证券跨 ticker 历史能否稳定拼接。

审计输出：

```text
outputs/data_audits/fmp_us_equity_coverage_<DATE>.json
```

至少包含 endpoint、HTTP 状态、字段、样本、覆盖率、账户权限、请求成本、结论和 SHA-256。API key
不得写入报告。

如果 FMP 无法提供足够的历史退市证券和身份映射，只允许两种选择：

1. 接入有授权的替代数据源后回填；
2. 从正式启用日开始前瞻积累 PIT，并把历史状态明确标为 `PROSPECTIVE_ONLY`。

禁止用当前名单伪造五年历史。

### 8.2 历史起点

推荐 coverage bars 从 2019-01-01 开始，原因：

- 为 2020 年开始的研究提供 MOM_12M 约 273 个交易日 warm-up；
- 支持近五年评价窗口；
- 保留足够的 IPO、退市和 liquidity regime 样本；
- 初始数据规模仍适合当前 DuckDB + Parquet 架构。

若用户只批准 2020-01-01 起点，正式评价开始日必须向后推到最长因子 warm-up 完成之后。

### 8.3 行情语义

至少保存：

```text
date, security_id, ticker,
open, high, low, close, adj_close, volume,
source, source_asof, ingestion_run_id
```

要求：

- `next_open` 继续强制使用 open；
- raw 动量使用当前代码约定的 `adj_close`；
- ADV20 的价格口径必须固定并写入 methodology；
- 复权规则变化必须产生新 dataset version；
- 每日 overlap 更新不能掩盖供应商全历史修订，需定期做完整重拉抽样或全量校验。

### 8.4 数据质量门禁

| 门禁 | 阈值 |
|---|---:|
| target session 当前宽基成员 EOD 覆盖 | >= 98% |
| 历史 PIT 成员每日行情覆盖 | >= 95% |
| duplicate `(date,security_id)` | 0 |
| 非 XNYS 日期 | 0 |
| 非法 OHLC 关系 | 0 |
| 非法负成交量 | 0 |
| 当前成员 open 覆盖 | >= 95% |
| membership baseline | 必须早于或等于研究起点 |
| membership 最新快照 | 必须覆盖 target session 所属重构期 |
| 历史 active 但 bars 完全缺失的证券 | 0，或明确 provider exception 后整版拒绝研究 |
| manifest/hash 缺失 | 0 |

实现约束：网页元数据请求可以只认证父 manifest 与不可变 partition index，并逐文件认证本次实际读取的
因子/行情分片；生产发布和每日 shadow 必须继续逐文件校验完整 coverage。禁止为了页面性能跳过 index
哈希、查询分片哈希或每日全量影子校验。

### 8.5 因子数据门禁

对每个因子分别以“满足该因子 warm-up 的 PIT 成员”为分母：

| 门禁 | 阈值 |
|---|---:|
| latest raw coverage | >= 95% |
| latest clean coverage | >= 95% |
| raw 非空但 clean 全空且无原因的证券 | 0 |
| zero-std 截面比例 | <= 2% 才可标为健康 |
| 因子方向与 manifest | 100% 一致 |
| 八因子 generation | 同一次 publication 必须齐全 |
| data/universe/factor target session | 完全一致 |

### 8.6 正式宽基研究附加门禁

`US_LIQUID_5M` 从 `FACTOR_DATA` 晋升到 `FULL_RESEARCH` 前还必须满足：

- 至少 756 个可评价交易日，推荐完整近五年；
- 单日 IC 有效截面不少于 500；
- PIT 行业覆盖率至少 95%；
- 历史退市证券和 ticker change 审计通过；
- 分组回测使用现有 next-open、手续费、滑点和 ADV 成交限制；
- 至少五个不同交易日 shadow publication 无版本混用；
- 全量研究在 SG 规定窗口内完成；
- 供应商修订重跑结果和上一 generation 的差异可解释。

## 9. 存储与版本模型

### 9.1 为什么需要拆开 data version 与 universe version

当前 `DatasetVersion` 同时绑定 bars、universe 和 membership。宽基架构中，同一份全美 coverage bars
可以派生多个比较池，不能为每个池无限复制相同行情。

目标关系：

```text
US_EQUITY_COVERAGE dataset_version
  ├─ US_LIQUID_5M universe_version
  ├─ 未来 US_LIQUID_20M universe_version
  └─ 未来用户批准的动态研究池
```

### 9.2 DuckDB 新表

在保留现有 `dataset_versions` 的前提下新增：

```sql
derived_universe_versions(
  universe_version_id VARCHAR PRIMARY KEY,
  universe VARCHAR,
  parent_dataset_version_id VARCHAR,
  target_session DATE,
  membership_path VARCHAR,
  membership_sha256 VARCHAR,
  eligibility_path VARCHAR,
  eligibility_sha256 VARCHAR,
  manifest_path VARCHAR,
  manifest_sha256 VARCHAR,
  status VARCHAR,
  created_at TIMESTAMPTZ
)

published_universe_versions(
  universe VARCHAR PRIMARY KEY,
  universe_version_id VARCHAR,
  published_at TIMESTAMPTZ
)
```

发布事务必须先注册 immutable version，最后原子更新 pointer。

### 9.3 Parquet 布局

建议：

```text
data/lake/curated/US_EQUITY_COVERAGE/version=<DATA_VERSION_ID>/
  bars_index.json
  bars/year=<YYYY>/month=<MM>/part-*.parquet
  security_universe.parquet
  manifest.json

data/lake/universes/US_LIQUID_5M/version=<UNIVERSE_VERSION_ID>/
  membership.parquet
  eligibility_audit.parquet
  manifest.json
```

Security Master 关系表存 DuckDB，同时每次 coverage publication 冻结一份版本快照 Parquet 和哈希，
避免数据库当前值改变后无法重放旧因子 generation。

### 9.4 因子数据 publication

新增 `factor_data_publication.json`，与现有 `research_publication.json` 分工：

| publication | 证明什么 | 是否要求 confidence |
|---|---|---:|
| `factor_data_publication.json` | raw/clean/PIT/排名数据可查询 | 否 |
| `research_publication.json` | IC、ICIR、分组、成本和置信研究完整 | 按 registry |

建议布局：

```text
outputs/universes/US_LIQUID_5M/factor_data/
  generation=<GENERATION_ID>/
    factor_id=MOM_1M/year=2026/month=08/part.parquet
    factor_id=MOM_3M/year=2026/month=08/part.parquet
    ...
    preprocessing_audit.parquet
    manifest.json
  factor_data_publication.json
```

宽基 observation 建议使用 long Parquet：

```text
date
security_id
ticker
factor_id
raw_value
clean_value
pit_member
status
```

rank/percentile 默认查询时通过 DuckDB window function 在完整截面计算。不得为一个 ticker 的查询只读
该 ticker 后再排名。

### 9.5 publication 必须绑定的身份

```json
{
  "schema_version": 1,
  "status": "PUBLISHED",
  "publication_id": "...",
  "publication_mode": "FACTOR_DATA",
  "universe": "US_LIQUID_5M",
  "target_session": "2026-08-10",
  "parent_dataset_version_id": "...",
  "parent_dataset_manifest_sha256": "...",
  "universe_version_id": "...",
  "membership_sha256": "...",
  "eligibility_sha256": "...",
  "security_master_generation_id": "...",
  "security_master_sha256": "...",
  "preprocessing_methodology_version": "...",
  "classification_policy": "...",
  "factors": {
    "MOM_6M": {
      "generation_id": "...",
      "manifest_sha256": "...",
      "date_start": "...",
      "date_end": "..."
    }
  }
}
```

任一身份不匹配时 fail closed。

### 9.6 不使用 SQLite 保存的内容

- bars；
- Security Master 历史大表；
- membership 大表；
- raw/clean；
- 每日 rank/percentile；
- IC 时序和大规模回测明细。

SQLite 只保存用户业务对象和运行账本。

## 10. 发布流程

### 10.1 首次历史回填

```text
供应商能力审计
  -> 构建 Security Master + symbol history
  -> 拉取 current + historical delisted coverage bars
  -> 发布 US_EQUITY_COVERAGE candidate
  -> 根据每个历史重构日的已知数据计算 US_LIQUID_5M PIT
  -> 质量门禁与人工抽样
  -> 发布 coverage version + universe version
  -> 计算八因子 raw/clean
  -> 发布 factor_data publication
  -> 页面影子验收
  -> 满足额外门槛后再跑正式宽基 confidence
```

### 10.2 每日收盘后增量

现有 07:15 `quant-us-daily-refresh` 继续服务动量扫描。为避免 2 GB SG 上任务争抢，新宽基链使用
独立的 11:30 SGT 窗口：

```text
1. 刷新 Security Master 增量、退市和 symbol changes
2. 增量摄取 US_EQUITY_COVERAGE EOD，保留 overlap
3. 发布 coverage DatasetVersion
4. 若到月末或有强制事件，生成新 US_LIQUID_5M universe version
5. 非重构日复用最近正式 membership，但绑定新的 parent data version
6. 计算受新数据影响的 factor rows
7. 原子发布 factor_data publication
8. 成功后清理进程内缓存并更新网页状态
```

### 10.3 systemd 依赖

建议新增：

```text
quant-us-equity-coverage.service
quant-broad-factor-data.service
quant-broad-research-readiness.service
quant-broad-shadow-observation.service
```

依赖规则：

- broad factor data 只能在 coverage 和 universe 同 target session 成功后启动；
- 使用 `OnSuccess=` 或显式 orchestrator 形成真实依赖，不能只依赖两个固定时钟碰巧先后完成；
- broad factor research 与每日 factor data 分离；严格 PIT 行业门槛未通过时只运行 readiness，不创建
  一个会绕过门槛的正式 research service；
- broad 失败不能污染 SP500/NASDAQ100 已发布结果；
- broad 失败时网页保留上一版并明确显示 stale，不得回退旧 raw 文件或临时 FMP。

### 10.4 增量与完整重建

每日：

- 摄取最新 EOD；
- 重算最长 warm-up 加供应商 overlap 涉及的日期；
- 对受影响日期重新做完整横截面 clean；
- 发布新 factor data generation。

定期完整任务：

- 抽查供应商历史复权修订；
- 完整重建五年 raw/clean、IC 和分组回测；
- 比较新旧 generation 差异；
- 只有差异通过阈值或有可解释 provider revision 才发布。

不得声称增量结果正确，却从未验证它与完整重建一致。

## 11. 因子数据浏览器改造

### 11.1 页面能力

“研究 -> 因子数据”继续保留：

- 日期截面；
- 单股历史。

股票池下拉框新增：

```text
全美流动股票（US_LIQUID_5M）
标普 500
纳斯达克 100
科技七巨头
```

搜索股票时应查询 Security Master，而不是只搜索当前 selected generation 的列名。

### 11.2 单股历史交互

推荐筛选顺序：

```text
股票代码
因子
比较/排名股票池
开始日期
结束日期
```

页面必须显示三层状态：

| 状态 | 页面含义 |
|---|---|
| coverage 有数据 | 可以展示 raw |
| 当日 comparison member | 可以参与 clean 和 rank |
| research ready | 可以跳转 IC/置信研究结论 |

例如 AEVA 在某天不属于 SP500，但属于 `US_LIQUID_5M`：

```text
全美 coverage raw：有
SP500 clean/rank：不适用，不是当日成员
US_LIQUID_5M clean/rank：有
```

### 11.3 页面文案

允许状态：

- `正式研究`：完整 research publication；
- `宽基因子数据`：factor data publication 已发布，但不代表 confidence PASS；
- `仅原始值`：coverage raw 可用，未进入 clean/rank；
- `研究数据不足`：PIT 或历史长度尚未达到正式置信门槛；
- `数据截至 YYYY-MM-DD`；
- `当日不在全美流动股票比较范围内`。

禁止文案：

- 把 `FACTOR_DATA` 写成“因子已通过”；
- 把 `US_ACTIVE` 写成历史研究池；
- 把无排名写成“股票因子很差”；
- 把其他股票池结果静默搬过来；
- 暴露 `generation` 等内部术语给普通用户，详细版本放可展开审计区。

### 11.4 当前页面兼容

现有 SP500/NASDAQ100/MAG7 查询仍通过原 `research_publication` 和已验证宽矩阵 adapter。
`US_LIQUID_5M` 使用新的 factor data publication 和 long-Parquet adapter。两个 adapter 必须返回同一
`FactorSnapshotResult/FactorHistoryResult` 领域对象，Web route 不分叉排名逻辑。

## 12. API 需求

### 12.1 Security Master 搜索

```text
GET /api/securities/search?q=MDB&asof=2026-08-10&limit=20
```

返回：

```text
security_id, ticker, name, exchange, asset_type,
listing_date, delisting_date, trading_status,
coverage_status, available_comparison_universes
```

### 12.2 因子元数据

扩展现有：

```text
GET /api/research/factor-data/meta
```

每个 universe 返回：

```text
purpose
publication_mode
target_session
factor_data_status
research_status
capabilities: raw / clean / rank / confidence
parent_dataset_version_id
universe_version_id
```

### 12.3 snapshot/history

继续使用现有 API 路径，并支持 `universe=US_LIQUID_5M`：

```text
GET /api/research/factor-data/snapshot
GET /api/research/factor-data/history
GET /api/research/factor-data/export
```

响应 contract 增加：

```text
security_id
publication_mode
parent_dataset_version_id
universe_version_id
normalization_universe_id
classification_policy
```

### 12.4 新错误码

| HTTP | 业务码 | 含义 |
|---|---|---|
| 404 | SECURITY_NOT_FOUND | Security Master 不认识该股票 |
| 409 | COVERAGE_NOT_PUBLISHED | 全美 coverage 尚未发布 |
| 409 | UNIVERSE_NOT_PUBLISHED | 宽基 PIT 尚未发布 |
| 409 | FACTOR_DATA_NOT_PUBLISHED | 有行情但无 factor data publication |
| 409 | FACTOR_DATA_STALE | 因子数据落后于预期 target session |
| 409 | SECURITY_ID_AMBIGUOUS | ticker 在该日期无法唯一解析 |
| 422 | NOT_IN_COMPARISON_UNIVERSE | 有 raw，但当天不能给出该池 clean/rank |
| 422 | CALCULATION_WINDOW_INSUFFICIENT | 该因子历史窗口不足 |

`NOT_IN_COMPARISON_UNIVERSE` 在单股历史中通常是行级状态，不必让整个历史请求失败。

## 13. 性能、容量与固定 SG 资源约束

### 13.1 2026-08-12 SG 实机基线

本项目不能依赖升级到 4 核 8 GB。2026-08-12 通过 SSH 只读核验得到：

| 项目 | 实测值 |
|---|---:|
| CPU | 2 vCPU，AMD EPYC 7K62，约 2.0 GHz |
| 物理内存 | 1.9 GiB |
| Swap | 0 |
| Web 当前/峰值内存 | 约 286 / 300 MB |
| 当时 MemAvailable | 约 1.0 GiB |
| 系统盘 | 80 GB |
| 已用/可用磁盘 | 13 / 68 GB |
| 项目 data/outputs | 约 167 / 159 MB |
| 2026-08-01 后内核 OOM 记录 | 0 |

同日现有任务实测：

| 任务 | 墙钟时间 | CPU 时间 | 说明 |
|---|---:|---:|---|
| `quant-us-daily-refresh` | 约 9 分 25 秒 | 约 2 分 39 秒 | 2,919 个当日候选，发布版本历史并集 3,087 票、380,283 行 |
| `quant-market-data` | 约 3 分 05 秒 | 约 47 秒 | 三个现有研究池行情 |
| `quant-factor-research` | 约 9 分 23 秒 | 约 9 分 22 秒 | SP500、NASDAQ100、MAG7 八因子和跨池发布 |
| `quant-group-analytics-eod` | 约 16 秒 | 约 16 秒 | 板块盘后产物 |
| `quant-intraday-momentum-monitor` | 一个完整会话 | 约 2 分 48 秒 | systemd 记录峰值内存约 69 MB |

当前服务器 CPU 和磁盘余量充足，最严格的约束是只有约 1 GiB 可供新增后台进程使用，并且没有
swap。所有宽基实现必须在这台机器上通过资源验收，不能把扩容写成完成条件。

### 13.2 四类负载必须分开

以当前约 9,747 只 FMP active securities、约 2,919 只流动宽基成员、8 个因子和 2019 年以来约
1,900 个交易日估算：

```text
coverage 历史日线理论上限       9,747 × 1,900      ≈ 1,850 万行
coverage 全历史 raw 因子观测    9,747 × 1,900 × 8  ≈ 1.48 亿个
宽基全历史 clean/rank           2,919 × 1,900 × 8  ≈ 4,400 万个
正常单日新增 raw                9,747 × 8          ≈ 7.8 万个
正常单日新增 clean/rank         2,919 × 8          ≈ 2.3 万个
```

这是容量上限估算，不代表所有证券每天都有有效历史。生产负载分成：

| 负载 | 频率 | 主要压力 | 是否每天全量运行 |
|---|---|---|---:|
| 首次 Security Master、行情和因子回填 | 上线一次，规则大改后重建 | FMP、磁盘写入、CPU、内存 | 否 |
| 行情与 factor data 增量 | 每个交易日 | FMP 请求、最近窗口读取、Parquet 写入 | 是，但只增量 |
| 最新 PIT membership | 普通日复用，月末重构 | 最近 20 日成交额和截面筛选 | 否 |
| IC、置信评估和逐票成本完整重放 | 每周末 | CPU、Pandas 临时矩阵、内存 | 否 |

首次回填是绝对工作量最大的一次，但不是永远只运行一次。因子公式、预处理方法、PIT 规则、证券身份
或供应商历史复权发生重大变化时，应启动新的低频全量重建。

“每周完整研究”是从同一份已发布 factor data 和行情版本重新计算 IC、置信统计与逐票成本回测，
默认不重新生成 2019 年以来的全部 raw/clean。全历史 raw/clean 重建属于独立的低频一致性任务，
只在方法变化、历史数据修复或抽样校验发现差异时触发。

### 13.3 每个交易日真正需要做什么

每日链路只允许处理新 target session 和 overlap 修订影响范围：

1. 增量更新上市、退市、ticker change、资产类型和分类；
2. 摄取 coverage 当日 EOD，并回看配置的 21 个日历日以吸收供应商修订；
3. 检查重复、OHLC、成交量、target-session 覆盖和哈希；
4. 普通日复用最近正式 PIT 快照，仅处理退市等强制事件；月末才计算新一期 ADV20 membership；
5. 每个因子分别读取必要历史窗口，计算新增或受修订日期的 raw；
6. 对受影响日期的完整约 2,900 票截面计算 clean、方向化值和 rank；
7. 只写新分区或受影响分区，原子更新 factor-data pointer；
8. 清理候选缓存，网页读取新 publication。

例如 MOM_12M 为计算最新一天需要读取约 273 个先前交易日，但这不表示重新输出 273 日或重新计算
七年历史。正常情况下每天新增约 7.8 万条 raw 和 2.3 万条 clean/rank，因子数学本身不是主要
压力；更可能成为日常瓶颈的是 FMP 是否能批量稳定提供近一万只证券的 EOD，以及 Parquet 是否做到
分区复用而不是每日复制全部历史。

### 13.4 为什么完整研究主要受内存限制

单张 `1,900 × 2,919` 的 float64 矩阵约为：

```text
1,900 × 2,919 × 8 bytes ≈ 44 MB
```

完整研究会同时或依次产生 open、close、adj_close、volume、returns、membership、tradability、
raw、clean、分组持仓、收益、逐票交易和成本矩阵。Pandas 的排序、中性化、对齐和回测还会创建临时
副本，因此峰值可能远高于一张矩阵的 44 MB。

coverage 若直接做成 `1,900 × 9,747` 的完整宽表，单个 float64 字段就约 148 MB；同时载入多个
字段会迅速超过当前服务器余量。因此禁止把现有 `run_mvp.py` 的“全行情宽表一次入内存”方式直接
放大到全 coverage。宽基研究必须按因子、日期分区和证券批次运行，截面 clean 只在单日或小日期块
上聚合。

### 13.5 24 小时运行窗口与生产时限

服务器 24 小时运行，不要求所有后台计算在上午固定时刻完成。XNYS 常规收盘到下一交易日开盘固定
约 17.5 小时；扣除当前 120 分钟 FMP 稳定等待后，仍约有 15.5 小时可用于生产计算。

| 时区场景 | 美股收盘（SG/北京时间） | FMP 最早稳定时间 | 下一次开盘 | 可计算窗口 |
|---|---:|---:|---:|---:|
| 美国夏令时 | 约 04:00 | 约 06:00 | 约 21:30 | 约 15.5 小时 |
| 美国冬令时 | 约 05:00 | 约 07:00 | 约 22:30 | 约 15.5 小时 |

周五收盘到周一开盘约 65.5 小时；扣除数据稳定等待后，周末仍约有 63.5 小时。这比提高并发更适合
当前 2 核 2 GB 服务器。

推荐调度采用依赖链而不是把所有任务固定在相邻时刻：

```text
每个交易日
  FMP 数据达到稳定时间
    -> Security Master + coverage 增量
    -> 现有指数池行情和研究
    -> 板块盘后任务
    -> 宽基 factor data 增量
    -> 依赖宽基数据的 paper decision
    -> 下一次盘前任务和开盘监控

每周六（对应周五美股收盘后的 SG 白天）
  当日增量链成功
    -> 因子 1 完整研究候选
    -> 因子 2 ... 因子 8，逐个串行并保存 checkpoint
    -> 全部同版本通过后一次性发布 broad research

首次回填或规则变化
  按年份 × 证券批次下载
    -> 按重构月份生成 PIT
    -> 按因子 × 日期分区计算
    -> 每个阶段断点续跑，可跨多个白天和周末
```

时间要求：

- 每日 factor data 的硬截止时间是“下一次开盘前 90 分钟”，不是固定 08:45；
- 在宽基因子尚未进入模拟盘策略前，可以安排在现有 10:30 paper task 之后运行；
- 一旦模拟盘依赖宽基因子，paper task 必须改成 `After/OnSuccess=quant-broad-factor-data`，不得继续
  假定 10:30 已有数据；
- 每周完整研究可以使用整个周末，但不得跨入周一盘前监控窗口；
- 每日关键链完成后的白天空闲窗口，也可以运行首次回填或低频重建的有限分片，到盘前保护窗口必须
  保存 checkpoint 并退出；
- 周末研究未完成时保留 checkpoint 和上一正式版本，下个安全窗口继续；不得为了赶时间与盘中任务
  并发或发布不完整结果；
- 非交易日不重复生成同一 target session，只运行明确安排的历史重建、校验或维护任务。

### 13.6 固定规格下的强制资源保护

- coverage 全历史不得整体 pivot 为 Pandas 宽表；
- 八个因子必须串行，任一时刻只保留当前因子的必要日期块；
- 完整研究一次只跑一个因子，产出 candidate checkpoint 后主动释放内存；
- DuckDB 查询必须下推日期、证券和列，并允许临时结果 spill 到磁盘；
- `OMP_NUM_THREADS/MKL_NUM_THREADS/OPENBLAS_NUM_THREADS` 固定为 1，避免库内部隐式抢占两核；
- 所有重型 writer/research unit 共享排他锁，不能并发执行；
- 后台 unit 建议以 `MemoryHigh=700M`、`MemoryMax=900M` 作为初始验收线，并根据实测向下收紧；
- 建议增加 2 GB swap、`vm.swappiness=10` 只作为瞬时 OOM 缓冲；持续 swap-in/out 判定为性能失败；
- 重任务使用较低 `Nice`/I/O 优先级，Web 不得为了完成研究而停止；
- 长计算不得持有 DuckDB 写事务；先写 immutable staging，最终只用短事务更新 pointer；
- MemAvailable 低于 350 MB、磁盘可用低于 15 GB或盘前保护窗口到达时，不启动新的重任务；
- 超时、OOM 或任一分片失败时保留上一正式 publication，不发布部分 generation，不静默降级；
- 每日完整复制多年 bars 或完整 factor generation 视为实现缺陷，不允许用磁盘换取编码便利。

增加 swap 不是服务器扩容，也不能代替内存优化。它只用于避免偶发峰值直接触发 OOM；若正常完整
研究必须依赖大量 swap 才能完成，该实现不满足本需求。

### 13.7 查询实现

三千只以上宽基不得继续依赖“每次请求把全部五年 raw/clean 宽矩阵加载进 Pandas”。要求：

- DuckDB/Parquet predicate pushdown；
- snapshot 按日期裁剪；
- history 按 `security_id + factor_id + date range` 裁剪；
- rank 必须在过滤 ticker 前完成完整截面 window；
- 缓存键包含 publication、data version、universe version 和 factor generation；
- publication 切换后缓存自动失效；
- Web worker 不持有全部八因子五年矩阵的永久副本。

### 13.8 首次容量基准

正式回填前分别跑：

```text
100 securities
500 securities
3,000 securities
历史退出证券并集
```

记录：

- FMP 请求量和带宽；
- writer 时长；
- Parquet 大小；
- DuckDB 查询 p50/p95；
- factor raw/clean 时长；
- peak RSS；
- SG 剩余磁盘；
- 完整 research 时长。

固定 SG 的验收还必须记录：

- 每阶段峰值 RSS 和 MemAvailable 最低值；
- swap 峰值和 major page faults；
- 是否与其他 Quant 重型 unit 重叠；
- 每日链距离下一开盘还剩多少安全时间；
- 周末八因子 checkpoint 数、失败重试和总完成时间；
- 失败后 Web、盘中监控和上一正式 publication 是否仍可用。

### 13.9 页面性能门槛

| 请求 | 冷缓存 p95 | 热缓存 p95 |
|---|---:|---:|
| 宽基单日截面首 100 行 | <= 3 秒 | <= 750 毫秒 |
| 单股五年历史 | <= 3 秒 | <= 750 毫秒 |
| Security Master 搜索 | <= 500 毫秒 | <= 200 毫秒 |
| CSV 生成开始响应 | <= 3 秒 | <= 1 秒 |

性能测试必须包含真实哈希/版本校验，不能只测裸 DataFrame。

## 14. 日志、指标与可观测性

每次 coverage、universe、factor data 和 research 运行至少记录：

```text
run_id
target_session
parent_dataset_version_id
universe_version_id
publication_id
security_count
membership_count
historical_union_count
bar_rows
raw/clean coverage by factor
provider failures
quality gate failures
duration by stage
peak memory when available
```

网页状态接口必须能回答：

- 今天 coverage 是否发布；
- 今天宽基 membership 是否发布；
- 今天 factor data 是否发布；
- 正式宽基研究最近一次是什么日期；
- 哪个阶段失败；
- 当前页面为什么仍显示上一版。

本需求只要求结构化日志和状态 API；统一运维监控页可以作为独立后续需求。

## 15. 代码改造地图

### 15.1 新增建议

```text
configs/universe_catalog.yaml

src/data/security_master_store.py
src/data/broad_coverage.py
src/data/derived_universe.py
src/data/universe_publication.py

src/factors/data_publication.py
src/factors/broad_pipeline.py
src/factors/observation_backends.py

scripts/audit_fmp_us_equity_coverage.py
scripts/backfill_us_equity_coverage.py
scripts/update_us_equity_coverage.py
scripts/build_us_liquid_pit.py
scripts/run_broad_factor_data.py
scripts/run_broad_daily_pipeline.py
scripts/check_broad_resources.py
scripts/check_broad_research_readiness.py
scripts/check_broad_shadow_observation.py

deploy/systemd/quant-us-equity-coverage-root.service
deploy/systemd/quant-broad-factor-data-root.service
deploy/systemd/quant-broad-research-readiness-root.service
deploy/systemd/quant-broad-shadow-observation-root.service
deploy/systemd/quant-us-equity-coverage.timer

tests/test_security_master_store.py
tests/test_broad_pit_universe.py
tests/test_factor_data_publication.py
tests/test_broad_factor_observations.py
tests/test_broad_factor_pipeline.py
```

文件名可以在实现前微调，但领域边界不得重新塞回 `routes_v2.py` 或一个超长脚本。

### 15.2 需要修改

```text
configs/default.yaml
src/data/foundation.py
src/data/security_master.py
src/data/pit.py
src/data/access.py
src/data/universe_ids.py
src/research_universes/models.py
src/research_universes/registry.py
src/factors/observations.py
src/webapp/research_routes.py
src/webapp/templates/factor_data.html
src/webapp/static/js/factor_data.js
scripts/refresh_us_active.py
scripts/run_factor_research.py
docs/data_foundation.md
docs/unified_data_storage.md
docs/sg_operations_overview.md
```

### 15.3 兼容迁移

- `US_ACTIVE` 的网页和命令别名可以短期映射到 `US_LIQUID_5M`，但 manifest 中禁止写成正式历史池；
- SP500/NASDAQ100 继续使用现有 embedded membership DatasetVersion；
- 新宽基先使用 parent dataset + derived universe model；
- 后续可以把指数池也迁为 derived universe，但不属于本期强制范围；
- 旧 `US_LIQUID_5M` 180D 版本只读归档，不覆盖、不删除；
- 新版本必须使用新的 methodology/schema version，避免网页误认旧数据已满足 PIT。

## 16. 实施阶段

### Phase 0：需求确认与供应商审计

- 用户确认第 18 节决策；
- 实现只读 FMP capability audit；
- 核对套餐、带宽、退市 EOD 和 symbol history；
- 做 100/500/3000 证券容量估算；
- 在现有 2 vCPU、1.9 GiB SG 上记录分片计算的峰值 RSS、swap、CPU、墙钟时间和磁盘增长；
- 形成 GO / ALTERNATE_PROVIDER / PROSPECTIVE_ONLY 结论。

验收：没有通过 Phase 0，不开始五年全量下载。GO 必须表示方案能在现有 SG 的 900 MB 后台进程
内存上限内分片运行，不得以未来扩容为前提。

### Phase 1：Security Master

- 新建稳定 `security_id`；
- 建 symbol history 和 classification history；
- 摄取 current、delisted、ticker change；
- 增加冲突与覆盖率门禁；
- 版本快照和哈希绑定。

验收：MDB、AEVA、一个退市证券和一个代码变更证券都能按日期解析唯一身份。

### Phase 2：Coverage 与 PIT 宽基池

- 回填 `US_EQUITY_COVERAGE`；
- 从历史已知数据确定 eligibility；
- 发布 `US_LIQUID_5M` membership 与 eligibility audit；
- 证明当前名单未倒灌历史；
- 证明历史退出证券有 bars。

验收：独立程序可从 bars + rules 重建任一抽样月末 membership，并与 publication 完全一致。

### Phase 3：宽基因子数据 publication

- 计算八因子 coverage raw；
- 在 `US_LIQUID_5M` 截面计算 clean；
- 发布 long Parquet generation；
- 增加宽基 observation backend；
- 完成正/负向排名、非成员和 warm-up 测试。
- 完成单因子、日期分区 checkpoint 与失败续跑；
- 证明正常每日任务只更新受影响分区，没有复制全部历史。

验收：MDB、AEVA 可以查询；重叠股票 raw 与现有研究链一致；clean 明确绑定宽基池；每日增量在
下一开盘前 90 分钟完成且峰值 RSS 不超过核准上限。

### Phase 4：网页与 API

- 股票池新增“全美流动股票”；
- Security Master 全局搜索；
- 展示 coverage/member/research 三层状态；
- CSV 和 URL 状态恢复；
- 桌面、移动端、长历史性能验收。

验收：用户无需先知道某股票属于哪个指数，也能搜索并得到可理解结果。

### Phase 5：正式宽基研究

- 完成 PIT 行业和退市门禁；
- 周末按因子串行运行宽基 IC、ICIR、置信评估和成本后分组回测；
- 所有 checkpoint 必须冻结同一 data、universe 和 factor-data publication；
- 发布独立 broad research 结论；
- 研究总览增加宽基证据，但不擅自改变现有跨池 verdict 规则。

验收：`FACTOR_DATA` 和 `FULL_RESEARCH` 状态不混用，宽基 FAIL 不会被页面写成数据缺失；完整八因子
任务在周末窗口内完成，运行期间 Web 可用且没有 OOM 或持续 swap。

### Phase 6：SG 影子与切换

- 备份代码、DuckDB pointer 和旧 `US_LIQUID_5M`；
- 部署新 services 并运行 `systemd-analyze verify`；
- 至少五个不同 target session 连续通过；
- 影子期 `data.broad_factor_data.web_default_enabled=false`，只能由用户显式选择宽基池；
- 核验发布耗时、内存、磁盘和页面 p95；
- 至少完成一次从周六候选计算到原子发布的完整周末研究演练；
- 切换页面默认宽基入口；
- 不删除旧版本文件。

## 17. 测试与验收场景

### 场景 A：MDB

输入：

```text
股票：MDB
因子：MOM_6M
比较池：US_LIQUID_5M
日期：2026-08-10
```

必须返回 coverage raw、宽基 clean、rank/eligible count、percentile、PIT member、data/universe/factor
publication 身份。不能要求 MDB 当前属于 NASDAQ100。

### 场景 B：AEVA

AEVA 不属于 SP500/NASDAQ100 时：

- 宽基查询成功；
- 指数池查询显示“当日不是该比较池成员”；
- 不把无指数排名解释为因子差；
- 不静默切换股票池。

### 场景 C：重叠股票

选择同时属于 SP500 和宽基池的股票：

- 同一底层行情版本和因子参数下 raw 精确一致；
- clean/rank 可以不同；
- 页面显示两个 normalization universe；
- 差异不是错误。

### 场景 D：低流动性股票

某 coverage 股票未通过 ADV20：

- raw 可以查询；
- `pit_member=false`；
- clean/rank 为空；
- status=`NOT_PIT_MEMBER`；
- 页面说明当日未进入全美流动比较范围。

### 场景 E：IPO

- 上市前无数据；
- 上市后可以逐步出现短因子 raw；
- MOM_12M 在 273 日 warm-up 前明确不足；
- 不填 0、不借用其他 ticker 历史；
- 通过下一重构后才进入宽基排名。

### 场景 F：退市股票

- 历史成员期仍可查询 raw/clean/rank；
- 退出后不进入分母；
- 退市收益和退出成交按明确政策处理；
- 当前搜索默认可以隐藏退市，但历史搜索可找到。

### 场景 G：ticker change

- 新旧 ticker 解析为同一 `security_id`；
- 单股历史连续；
- 页面按 observation date 显示当时 ticker；
- 同 ticker 被复用时不会串接不同公司。

### 场景 H：负向因子

VOL_20D/TURNOVER 中 clean 最低的有效股票 rank=1。不得因扩展宽基而反转方向口径。

### 场景 I：版本篡改

bars、Security Master、membership、eligibility、factor Parquet 或 manifest 任一哈希变化：

- Reader 拒绝；
- API 返回可理解 409；
- 页面不显示混合结果；
- 不回退旧文件或 FMP。

### 场景 J：供应商缺失

某日 coverage 更新失败：

- 不发布新宽基 factor data；
- 保留上一正式 pointer；
- 页面显示 stale 日期和失败阶段；
- SP500/NASDAQ100 独立任务不被错误覆盖。

## 18. 已确认的实施口径

以下默认值已于 2026-08-12 确认，实施代码和验收按这些口径执行：

| 编号 | 问题 | 推荐选择 | 影响 |
|---|---|---|---|
| R-001 | 首次历史起点 | 2019-01-01 | 支持 2020 起完整 MOM_12M warm-up，下载量更大 |
| R-002 | ADR 是否进入宽基 rank | Coverage 保留，估计池排除 | 避免重复上市和海外市场时差，覆盖 raw 仍可查 |
| R-003 | membership 重构频率 | 月末完整重构 + 月中确认退市强制退出；IPO 事件立即进入主表/coverage，满足门槛后在下一次月末重构入池 | 稳定、成本可控；不反映每日流动性边界变化 |
| R-004 | 流动性门槛 | ADV20 >= 500 万美元 | 与现有命名一致，约三千只规模 |
| R-005 | 最低价格 | 1 美元 | 与当前回测 tradability 默认值一致 |
| R-006 | 历史行业非 PIT 时 | 先上线 factor data，暂缓正式宽基 confidence | 保证页面可用，同时不夸大历史研究正确性 |
| R-007 | 正式宽基研究频率 | 每周末串行完整运行，因子数据每日增量 | 利用约 63.5 小时周末窗口，日常页面仍更新 |

## 19. 明确不在本期范围

- 不把所有活跃证券无过滤地放入同一交易组合；
- 不上线真实券商交易；
- 不自动把每个 Watchlist 晋升为正式研究池；
- 不在网页请求期间访问 FMP 或重算三千只股票；
- 不把实时分钟因子纳入本次日线宽基改造；
- 不引入基本面因子，当前仍以已注册八个 OHLCV 因子为范围；
- 不在本期强制把 SP500/NASDAQ100 迁移到共享 coverage bars；
- 不删除旧 `US_LIQUID_5M`、SP500、NASDAQ100 或 MAG7 历史版本；
- 不把最新行业回填包装成严格 PIT；
- 不因宽基样本更多就自动推翻现有跨池结论规则。

## 20. 完成定义

只有以下条件全部满足，才能宣布“全美宽基因子数据”完成：

- Security Master 使用稳定 `security_id`，代码变更和退市可追溯；
- `US_ACTIVE` 明确只作为当前供应商输入；
- `US_EQUITY_COVERAGE` 和 `US_LIQUID_5M` 职责分离；
- 宽基 membership 由历史当时可得数据确定，没有当前名单倒灌；
- MDB、AEVA 能查询 coverage raw 和宽基 clean/rank；
- 非成员股票仍能显示 raw 和准确原因；
- rank=1、percentile 和 Q1-Q5 继续遵守预设方向；
- data version、universe version、Security Master 和 factor generation 全部哈希绑定；
- factor data publication 与 research publication 明确分离；
- 正式宽基 confidence 只有在 PIT、行业、退市和历史长度门槛通过后才发布；
- 页面、API、CSV 对同一观测逐字段一致；
- 读取端不调用 FMP、不回退旧文件、不混合 latest；
- SG 五交易日 shadow、性能、日志和重启恢复验收通过；
- 每日关键链在下一开盘前 90 分钟完成，周末完整研究不跨入盘前保护窗口；
- 所有后台重任务在现有 2 vCPU、1.9 GiB SG 上满足内存上限，没有 OOM 或持续 swap；
- 数据基础、架构、运维和实现文档同步更新。

## 21. 参考依据

项目内部：

- `configs/default.yaml`
- `configs/research_universes.yaml`
- `scripts/refresh_us_active.py`
- `scripts/run_factor_research.py`
- `src/data/foundation.py`
- `src/data/security_master.py`
- `src/data/pit.py`
- `src/factors/publication.py`
- `src/factors/observations.py`
- `docs/research_universe_redesign_requirements.md`
- `docs/factor_data_explorer_requirements.md`
- `docs/data_foundation.md`
- `docs/unified_data_storage.md`

外部设计依据：

- MSCI Barra 对 coverage universe 与 estimation universe 的区分：
  <https://www.msci.com/documents/1296102/1336482/Introducing_MSCI_IndexMetrics.pdf/23cbc36f-cf2c-4bf0-96c3-206eecdfdf6d>
- Kenneth French Data Library 对 NYSE/AMEX/NASDAQ 宽市场和时变资格的处理：
  <https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library.html>
- QuantConnect 动态 universe 与历史 universe：
  <https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/universe-data>
- QuantConnect survivorship bias 说明：
  <https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide>
- QuantConnect US Equity Security Master：
  <https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/quantconnect/us-equity-security-master>
- Qlib 动态 instrument filter：
  <https://qlib.readthedocs.io/en/latest/component/data.html>
- FMP Company Screener、Delisted Companies 和 Symbol Change 文档：
  <https://site.financialmodelingprep.com/developer/docs/stock-screener-api/>
  <https://site.financialmodelingprep.com/developer/docs/delisted-companies-api>
  <https://site.financialmodelingprep.com/developer/docs/symbol-change-api>
