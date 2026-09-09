# 因子数据浏览器需求文档

更新日期：2026-08-09  
状态：需求基线 v1.0；Phase 1 至 Phase 4 已实现，正式数据重发与 SG 上线待完成  
适用范围：正式研究股票池中的单因子截面与单股历史查询

## 1. 文档目的

本需求用于补齐当前量化研究平台缺失的“基础因子数据查询层”，准确回答两类问题：

1. 在指定交易日、指定研究股票池中，某只股票的因子原始值、清洗后因子值和因子排名是多少？
2. 在指定开始日至结束日之间，某只股票每天的因子原始值、清洗后因子值和因子排名如何变化？

本功能不是策略排行的另一种展示，也不是回测决策回放的替代品。完成后，平台的三个层次必须保持清晰：

```text
因子数据
  回答：单个因子在某天、某只股票上的值和池内位置是什么？

策略股票排名
  回答：多个因子按策略权重融合后，股票的综合分和综合排名是什么？

决策回放
  回答：某次回测或模拟盘运行当时冻结了什么数据，并据此做了什么决策？
```

## 2. 最新页面现状审阅

### 2.1 已完成的信息架构

2026-08-09 对本地最新版网页和代码进行了检查。左侧导航已经重组为：

```text
研究
  研究总览
  研究股票池
  因子库
  跨池稳健性

策略
  目标股票池
  策略库
  股票排名

交易验证
  回测任务
  模拟盘
  决策回放

市场监控
  动量交易
  行业/主题（按配置启用）
```

对应实现主要位于：

- `src/webapp/templates/base.html`
- `src/webapp/research_routes.py`
- `src/webapp/templates/research_overview.html`
- `src/webapp/templates/research_factor.html`
- `src/webapp/templates/strategy_ranking.html`
- `src/webapp/templates/decision_replay.html`

这套结构已经正确区分“研究股票池、目标股票池和最终交易验证”。本需求不推翻现有结构，只在“研究”组中增加一个正式入口。

### 2.2 现有页面的职责

| 页面 | 当前回答的问题 | 不应承担的职责 |
|---|---|---|
| 研究总览 | 一个因子总体上是否可靠、跨池是否稳健 | 查询某只股票每天的因子值 |
| 因子研究详情 | 因子定义、预处理、IC、稳定性和经济意义 | 展示全部股票的逐日观测明细 |
| 研究股票池 | 股票池角色、版本、PIT 成分和质量门禁 | 多因子策略综合排名 |
| 股票排名 | 多因子策略的综合分、综合排名和目标权重 | 把综合排名描述成单因子排名 |
| 决策回放 | 回测/模拟盘冻结的 raw、clean、综合分、成交和成本 | 充当当前最新研究数据浏览器 |
| 旧单股诊断 | 最新 raw 值、旧口径排名和单股公式时序 | 继续作为正式研究查询入口 |

### 2.3 当前底层已经具备的数据

正式研究流水线会为每个 `universe × factor` 原子发布：

```text
outputs/universes/<UNIVERSE>/factors/<FACTOR_ID>/
  factor_raw_values.parquet
  factor_values.parquet
  factor_matrix_manifest.json
  preprocessing_audit.json
```

其中：

- `factor_raw_values.parquet`：因子公式直接计算的 raw 矩阵；
- `factor_values.parquet`：去极值、中性化、截面 Z-score 后的 clean 矩阵；
- 两张矩阵均为 `date × ticker` 宽表；
- `factor_matrix_manifest.json` 将 raw、clean、因子参数、预处理配置、行情版本和 SHA-256 绑定为同一 generation；
- `research_publication.json` 将完整因子集合绑定到同一个正式行情版本。

当前本地实际存在 SP500 和 MAG7 的 8 个因子 raw/clean 制品。SP500 单因子矩阵当前形状约为
`1255 个交易日 × 591 个历史证券`，范围为 2021-08-02 至 2026-07-31。NASDAQ100 尚未形成正式研究发布，因此新页面必须显示明确的“研究尚未发布”状态，不能临时重算或回退。

### 2.4 当前能力缺口

1. 研究 API 只有因子结论、股票池和跨池结论，没有 raw/clean 观测查询。
2. 旧 `/stock/{ticker}` 只给最新一天做单因子 raw 排名，历史图是单股自身时间序列 Z-score，不是每日池内 clean 排名。
3. 旧单股页仍可能从 FMP 临时拉取并重算，不能绑定正式 `dataset_version_id`。
4. 旧单股页排名为“raw 值升序，1 表示最小”；策略排名为“综合分降序，1 表示最好”，语义冲突。
5. 决策回放已保存 raw/clean 历史，但其中的 `rank` 是策略综合排名，不是单因子排名。
6. 当前正式数据版本无效时，旧单股页会抛出 HTTP 500；新版策略排行已经能用可理解的缺数状态 fail closed。
7. 当前页面无法从“因子研究详情”直接进入该因子的每日截面或某只股票历史。

## 3. 产品定位

### 3.1 新页面名称

中文名称：`因子数据`  
内部名称：`Factor Data Explorer`  
页面路由：`GET /research/factor-data`

左侧导航调整为：

```text
研究
  研究总览
  研究股票池
  因子库
  因子数据          <- 新增
  跨池稳健性
```

研究大组继续默认展开，不增加序号。

### 3.2 支持范围

V1 支持已经注册并正式发布因子研究的股票池：

- SP500；
- NASDAQ100；
- MAG7（参考池，可以查询，但不参与跨池总判断）。

用户 Watchlist 属于目标股票池，当前没有独立正式 raw/clean 研究发布，不纳入 V1 的单因子正式查询。
Watchlist 的多因子结果继续通过“股票排名”和“决策回放”查看，不能临时套用 SP500 的 clean 值或排名。

### 3.3 明确不做的事情

- 不在请求过程中调用 FMP；
- 不针对单只股票单独计算排名；
- 不把策略综合排名伪装成单因子排名；
- 不将 raw/clean 再复制到 SQLite；
- 不在 V1 支持任意用户 Watchlist 的正式 IC 或单因子排名；
- 不声称当前 publication 中的历史观测等于“当年当天页面实际看到的旧 publication”；
- 不替代回测和模拟盘的冻结决策回放。

## 4. 统一术语和计算口径

### 4.1 原始值 raw

`raw_value` 是因子公式直接计算的结果。

以 12 个月动量为例：

```text
raw_value(t, ticker) = P(t-21) / P(t-273) - 1
```

raw 尚未去极值、标准化或中性化，适合解释因子公式本身，不直接用于最终池内比较。

### 4.2 清洗后因子值 clean

`clean_value` 是正式研究使用的因子值，来自 `factor_values.parquet`，已经执行该 generation 中记录的：

- 去极值；
- 行业中性化；
- 市值中性化（仅在严格 PIT 市值可用且配置开启时）；
- 每日横截面 Z-score。

页面主列名称必须写成“清洗后因子值”，不能只写含糊的“因子值”。

### 4.3 方向调整后的信号值

每个正式因子必须预先声明 `direction ∈ {+1, -1}`：

```text
oriented_value = clean_value × direction
```

- 正向动量因子：`direction = +1`，clean 越高越好；
- 负向波动率因子：`direction = -1`，clean 越低越好；
- 方向来自因子注册表和 generation manifest，查询时不得根据历史收益重新选择。

### 4.4 因子有效截面

指定日期的排名样本必须满足：

```text
eligible(t, ticker)
= PIT_member(t, ticker)
  AND is_finite(clean_value(t, ticker))
```

只有 `eligible = true` 的股票进入排名分母。以下股票不排名：

- 当天不属于该股票池的股票；
- raw 因回看窗口不足而为空；
- clean 因数据质量或预处理门禁而为空；
- 制品中不存在该证券。

页面必须同时返回 `eligible_count`，排名显示为 `rank / eligible_count`。

### 4.5 因子排名

正式页面中的 `factor_rank` 统一定义为：

```text
factor_rank(t)
= rank(oriented_value(t), descending=True, ties="min")
```

强制语义：

- `rank = 1` 永远表示按该因子预设方向看最优；
- 精确相同的 oriented value 使用相同 rank；
- 同 rank 的表格显示顺序按 ticker 升序，不用 ticker 偷偷改变数学排名；
- 不再沿用旧单股页“1 表示最小 raw 值”的口径。

### 4.6 排名百分位与分组

`factor_percentile` 使用方向调整后的完整有效截面计算：

```text
factor_percentile = percentile_rank(oriented_value, ties="average") × 100
```

- 最优接近或等于 100%；
- 最差接近 0%；
- 只有一个有效证券时定义为 100%；
- `Q1` 表示最弱 20%，`Q5` 表示最强 20%；
- rank、percentile、quintile 必须来自同一个有效截面。

### 4.7 观测日期与发布日期

页面必须区分：

- `observation_date`：用户正在查看哪一个历史交易日；
- `publication_target_session`：当前正式研究发布截止到哪一天；
- `publication_id`：本次查询使用哪一次研究发布；
- `factor_generation_id`：raw/clean 来自哪一代因子矩阵；
- `dataset_version_id`：因子使用哪一版正式行情和 PIT 股票池。

V1 查询的是“当前正式 publication 对历史日期的重建结果”。若用户要还原某次回测当时冻结的事实，应进入决策回放。

## 5. 页面总体结构

### 5.1 顶部结构

页面采用现有研究页的视觉语言，不建立新的设计体系：

```text
因子数据
当前研究发布状态 / 数据截止日

[日期截面] [单股历史]       <- 两段式切换控件

筛选栏
结果摘要
图表或表格
数据契约（可展开）
```

要求：

- 默认进入“日期截面”；
- 两种模式共享 URL 查询参数，可以复制链接恢复相同查询；
- 桌面端适配当前 260px 左侧导航和 1280px 主内容宽度；
- 移动端筛选器换行，表格允许横向滚动；
- 页面区块采用全宽布局，不使用卡片套卡片；
- 数值使用等宽字体；
- 所有状态使用当前 `research_label` 中文映射，不直接把内部错误码作为主文案。

### 5.2 URL 状态

日期截面示例：

```text
/research/factor-data
  ?mode=snapshot
  &universe=SP500
  &factor=MOM_12M
  &date=2026-07-31
  &ticker=AAPL
```

单股历史示例：

```text
/research/factor-data
  ?mode=history
  &universe=SP500
  &factor=MOM_12M
  &ticker=AAPL
  &start=2021-08-02
  &end=2026-07-31
```

刷新页面、前进后退和分享 URL 后，筛选状态必须保留。

## 6. 视图一：日期截面

### 6.1 用户流程

1. 选择研究股票池；
2. 选择因子；
3. 选择有效交易日；
4. 查看当天所有有效证券排名；
5. 可搜索 ticker；
6. 点击股票进入同一因子的“单股历史”视图。

### 6.2 筛选器

必须包含：

- 研究股票池：SP500 / NASDAQ100 / MAG7；
- 因子：来自正式因子目录；
- 日期：只允许当前 publication 矩阵中的日期；
- 股票搜索：仅过滤结果展示，不改变排名分母；
- 状态筛选：全部 / 有效 / 非当日成分 / raw 缺失 / clean 缺失。

日期不允许静默回退：

- 用户输入非交易日时返回“该日期没有正式观测”；
- 页面可以提供最近前一交易日和后一交易日按钮；
- 只有显式选择 `latest` 时才能解析到最新可用交易日，响应必须返回实际解析日期。

### 6.3 结果摘要

显示：

- 观测日期；
- 当前股票池；
- 因子及方向；
- 当日 PIT 成分数；
- raw 有效数；
- clean 有效数；
- 实际排名分母；
- clean 覆盖率；
- 数据截止日；
- publication 状态。

### 6.4 截面表格

默认按 `factor_rank` 升序，字段为：

| 字段 | 说明 |
|---|---|
| 因子排名 | `rank / eligible_count`，1 表示最优 |
| 股票 | ticker、公司名称 |
| 行业 | 当日可用的正式行业分类 |
| 原始值 raw | 因子公式直接结果 |
| 清洗后因子值 clean | 正式研究使用值 |
| 方向调整值 | `clean × direction`，默认可隐藏 |
| 排名百分位 | 最优为 100% |
| 分位组 | Q1 至 Q5 |
| 当日成分 | PIT member 是/否 |
| 数据状态 | VALID 或明确缺失原因 |

交互要求：

- 点击 ticker 切换至该股票的历史视图；
- 表头支持对 raw、clean、percentile 排序；
- 搜索和分页不得重新计算局部排名；
- 默认每页 100 行；
- 表格顶部显示“当前展示 N / 有效截面 M”；
- CSV 下载列与当前 API 口径一致，并带 publication 元数据。

### 6.5 单票快速定位

如果 URL 带 `ticker=AAPL`：

- 表格仍按完整截面计算；
- 页面自动定位或高亮 AAPL；
- 不得只加载 AAPL 后把它排名为 1/1；
- 如果 AAPL 当天不在 PIT 股票池，显示原因，不给伪排名。

## 7. 视图二：单股历史

### 7.1 用户流程

1. 选择研究股票池；
2. 输入或选择 ticker；
3. 选择一个因子；
4. 选择开始日和结束日；
5. 查看 raw、clean、rank、percentile 的完整历史；
6. 点击某天返回该日完整截面。

### 7.2 查询条件

必须包含：

- 股票代码；
- 研究股票池；
- 因子；
- 开始日；
- 结束日；
- 快捷区间：近 3 月、近 1 年、近 3 年、全量。

“全量”使用当前 factor generation 的 `date_start` 和 `date_end`，不是应用配置中的模糊日期。

### 7.3 最新快照摘要

显示区间最后一个有效观测日的：

- raw；
- clean；
- factor rank / eligible count；
- percentile；
- quintile；
- 当日 PIT 成分状态；
- 因子方向。

如果区间末日无有效 clean，应显示最近有效日，并明确写出“请求结束日”和“最近有效观测日”，不能暗中替换。

### 7.4 历史图

采用一个指标切换控件，避免把不同量纲硬塞进同一 Y 轴：

```text
[raw] [clean] [排名] [排名百分位]
```

要求：

- X 轴为交易日；
- raw/clean/排名/百分位分别查看；
- rank 图的 Y 轴视觉上 `1` 位于顶部；
- percentile 图固定 0% 至 100%；
- 非 PIT 成分区间显示为空白，不做前向填充；
- hover 同时显示日期、值、排名分母和观测状态；
- 图表切换不能重新请求全部页面 HTML，只请求 JSON 数据或使用已加载数据。

### 7.5 历史明细表

字段为：

| 字段 | 说明 |
|---|---|
| 日期 | 精确交易日 |
| raw | 原始值 |
| clean | 清洗后因子值 |
| 因子排名 | `rank / eligible_count` |
| 百分位 | 0% 至 100% |
| 分位组 | Q1 至 Q5 |
| PIT 成分 | 当天是否属于该池 |
| 状态 | 有效或缺失原因 |

点击日期跳转到同因子、同股票池的日期截面，并高亮该 ticker。

## 8. 查询服务与数据流

### 8.1 新增统一 Reader

建议新增：

```text
src/factors/observations.py
```

核心对象建议为：

```text
FactorObservationReader
FactorObservationContract
FactorSnapshotResult
FactorHistoryResult
```

Web 路由不得直接拼接 Parquet 路径或自行计算排名。页面、API 和未来导出功能必须共用同一个 Reader。

### 8.2 严格读取顺序

每次查询必须按以下顺序：

```text
1. 解析 universe 和 factor
2. validate_factor_research_publication(universe)
3. 从 research_publication.json 冻结 publication_id
4. 读取该 factor 的 generation_id 和 manifest SHA-256
5. load_factor_matrix_bundle() 同时校验 raw/clean
6. 根据 publication.data_foundation.version_id 读取显式 DatasetVersion
7. MarketDataReader 校验 bars/universe/membership/manifest 四类哈希
8. 加载该版本的 PIT membership 和证券元数据
9. 对齐 raw、clean、membership 的日期和 ticker
10. 在完整有效截面上计算 oriented value、rank、percentile、quintile
11. 返回结果和完整数据契约
```

任意一步失败都不得：

- 改读 latest 的另一个版本；
- 回退到旧 `data/raw/ohlcv`；
- 调 FMP；
- 使用当前成分替代历史 PIT 成分；
- 只对查询股票计算 z-score 或 rank。

### 8.3 不新增重复数据库

V1 数据职责保持：

```text
DuckDB catalog
  保存行情版本、摄取任务和正式版本索引

不可变行情 Parquet
  保存 bars、universe、membership

因子 Parquet + manifest
  保存 raw/clean 矩阵和 generation 契约

SQLite
  继续只保存策略、Watchlist、回测任务、模拟盘账户和业务账本
```

rank 和 percentile 是由 `clean + direction + PIT membership` 确定性得到的派生结果，V1 不额外复制一整套 SQLite 表或长期 rank 文件。

### 8.4 缓存

允许进程内只读缓存，但缓存键必须至少包含：

```text
universe_id
publication_id
factor_id
factor_generation_id
factor_manifest_sha256
dataset_version_id
```

publication 或 generation 改变后必须自动失效。不能仅以 `factor_id` 缓存，否则每日发布后可能继续展示旧值。

### 8.5 并发发布一致性

查询开始和返回前必须确认 publication 没有切换。若发布恰好发生在查询过程中：

- 重试一次完整读取；或
- 返回 `409 PUBLICATION_CHANGED`，提示用户刷新。

不得返回一半来自旧 generation、一半来自新 generation 的结果。

## 9. API 需求

### 9.1 元数据

```text
GET /api/research/factor-data/meta
```

返回：

- 可查询研究股票池及状态；
- 每个股票池已正式发布的因子；
- publication target session；
- factor date range；
- 可选日期；
- 因子方向和中文名。

### 9.2 日期截面 API

```text
GET /api/research/factor-data/snapshot
  ?universe=SP500
  &factor=MOM_12M
  &date=2026-07-31
  &ticker=AAPL
  &status=all
  &sort=rank
  &order=asc
  &offset=0
  &limit=100
```

响应结构示例：

```json
{
  "contract": {
    "universe": "SP500",
    "factor_id": "MOM_12M",
    "direction": 1,
    "observation_date": "2026-07-31",
    "publication_id": "...",
    "publication_target_session": "2026-07-31",
    "factor_generation_id": "...",
    "dataset_version_id": "..."
  },
  "summary": {
    "pit_member_count": 503,
    "raw_valid_count": 500,
    "clean_valid_count": 498,
    "eligible_count": 498,
    "coverage": 0.9901
  },
  "rows": [
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "sector": "Information Technology",
      "raw_value": 0.2841,
      "clean_value": 1.1732,
      "oriented_value": 1.1732,
      "factor_rank": 37,
      "eligible_count": 498,
      "factor_percentile": 92.7,
      "quintile": "Q5",
      "pit_member": true,
      "status": "VALID"
    }
  ],
  "total_rows": 591
}
```

### 9.3 单股历史 API

```text
GET /api/research/factor-data/history
  ?universe=SP500
  &factor=MOM_12M
  &ticker=AAPL
  &start=2021-08-02
  &end=2026-07-31
```

响应必须包含：

- 与 snapshot 相同的 contract；
- 请求区间和实际矩阵区间；
- 每日 raw、clean、rank、eligible_count、percentile、quintile、PIT member、status；
- 最新有效观测摘要；
- 区间有效天数和覆盖率。

同一个 `date × ticker` 在 snapshot 和 history 两个 API 中必须完全一致。

### 9.4 CSV 导出

```text
GET /api/research/factor-data/export
```

导出必须复用同一查询服务和筛选口径。文件名包含：

```text
factor_data_<universe>_<factor>_<mode>_<date-or-range>_<publication-prefix>.csv
```

CSV 顶部不能丢失版本身份；至少在列中包含 publication、generation 和 dataset version。

## 10. 状态与错误处理

### 10.1 状态码

| HTTP | 业务码 | 场景 |
|---|---|---|
| 400 | INVALID_QUERY | 日期范围反向、limit 非法等 |
| 404 | FACTOR_NOT_FOUND | 因子不存在 |
| 404 | TICKER_NOT_IN_GENERATION | 股票在该 generation 从未出现 |
| 409 | RESEARCH_NOT_PUBLISHED | 股票池尚无正式研究发布 |
| 409 | RESEARCH_STALE | 研究发布与预期交易日不一致 |
| 409 | RESEARCH_INVALID | publication、manifest 或哈希无效 |
| 409 | PUBLICATION_CHANGED | 查询期间 publication 切换 |
| 422 | DATE_NOT_AVAILABLE | 日期不在当前 generation 中 |
| 500 | INTERNAL_ERROR | 仅保留给未预期程序错误 |

### 10.2 页面空状态

必须提供可操作文案：

- NASDAQ100 未发布：说明等待正式 NASDAQ100 PIT、行情和研究发布；
- 研究已过期：显示当前截止日和预期截止日；
- 数据版本无效：说明需要重发完整 integrity contract；
- 日期不可用：给出前后可用日期；
- 股票当天不在 PIT 池：仍可显示 raw/clean 缺失原因，但不排名；
- 回看窗口不足：显示“计算窗口不足”，不是通用“无数据”。

禁止把可预期的数据状态变成 HTTP 500 页面。

## 11. 与现有页面的集成

### 11.1 因子研究详情

在 `/research/factors/{factor_id}` 顶部增加“查看因子数据”按钮：

```text
/research/factor-data?factor=MOM_12M&universe=SP500
```

不把几百只股票的表格直接塞进现有因子详情长页面。

### 11.2 研究股票池详情

“当前成分证券”表中的 ticker 可以进入单股历史模式，并预填股票池和 ticker。因子未指定时显示因子选择器，不擅自默认成某一个投资因子。

### 11.3 策略股票排名

策略排行继续保留综合分和综合 rank。页面必须明确标注“策略综合排名”。

- 对正式研究股票池，可从股票行进入因子数据页；
- 页面带入 ticker 和 universe，但需要用户选择要看的单因子；
- 对 Watchlist，不允许伪装成正式研究池排名；应继续查看该策略运行时的因子拆解或决策回放。

### 11.4 决策回放

决策回放继续展示冻结运行中的 raw、clean、strategy input、contribution 和综合排名。

若增加跳转到因子数据页，必须提示：

```text
当前研究 publication 可能与本次运行冻结的 publication 不同。
```

不能用当前研究数据覆盖历史任务快照。

### 11.5 旧单股页迁移

当前已确认不需要旧页面兼容。完成新页面后执行：

1. 将所有 `/stock/{ticker}` 链接迁至 `/research/factor-data?mode=history...`；
2. 删除旧页面中的 FMP 直接回退；
3. 删除旧 raw 升序排名口径；
4. 删除或归档 `src/analysis/single_stock.py` 和 `src/webapp/templates/stock.html`；
5. 删除旧 `/api/stock/{ticker}`；
6. 在一个短版本周期内可保留 308 重定向，之后彻底删除旧 route。

迁移不能删除正式因子 Parquet、历史行情或决策回放快照。

## 12. 建议代码改造

### 12.1 新增文件

```text
src/factors/observations.py
src/webapp/templates/factor_data.html
src/webapp/static/js/factor_data.js
tests/test_factor_observations.py
tests/test_factor_data_api.py
tests/test_factor_data_web.py
```

### 12.2 修改文件

```text
src/webapp/research_routes.py
src/webapp/templates/base.html
src/webapp/templates/research_factor.html
src/webapp/templates/research_universe_detail.html
src/webapp/templates/strategy_ranking.html
src/webapp/static/css/style.css
src/factors/artifacts.py（仅在需要补充只读列裁剪接口时）
```

### 12.3 删除候选

新页面验收后再删除：

```text
src/analysis/single_stock.py
src/webapp/templates/stock.html
routes.py 中 /stock 和 /api/stock 路由
旧单股页专属 CSS
旧单股页测试
```

## 13. 性能要求

### 13.1 查询约束

- snapshot 必须先计算完整截面，再分页；
- history 默认最多返回当前 generation 全量交易日，硬上限 3000 个交易日；
- ticker 历史读取应尽量利用 Parquet 列裁剪；
- snapshot 可使用受 generation 约束的矩阵缓存；
- 不允许每一行股票重复打开 Parquet 文件；
- 不允许为一个股票的全历史逐日循环加载完整文件。

### 13.2 性能门槛

在 SG 当前部署规格和 SP500 约 600 个历史证券、约 5 年日线规模下：

| 请求 | 冷缓存 p95 | 热缓存 p95 |
|---|---:|---:|
| 单日完整截面 | 2 秒以内 | 500 毫秒以内 |
| 单股 5 年历史 | 2 秒以内 | 500 毫秒以内 |
| 页面首屏可交互 | 3 秒以内 | 1 秒以内 |

性能测试必须包含哈希校验和业务查询路径，不能只测已经加载好的 DataFrame 切片。

## 14. 测试要求

### 14.1 排名正确性

至少覆盖：

1. 正向因子最高 clean 为 rank 1；
2. 负向因子最低 clean 为 rank 1；
3. 相同值共享 rank；
4. ticker 只影响同 rank 显示顺序；
5. 非 PIT 成分不进入分母；
6. raw 有值但 clean 为空时不排名；
7. 单证券有效截面 percentile 为 100%；
8. rank、percentile 和 quintile 方向一致；
9. 搜索和分页不改变排名；
10. snapshot 与 history 同一观测完全一致。

### 14.2 PIT 正确性

使用至少一个加入事件和一个退出事件验证：

- 加入日前没有 rank；
- 生效日开始进入分母；
- 退出日之后没有 rank；
- 当前成分不能反向覆盖历史成分；
- 历史退出股票仍可查询其曾经有效的历史区间。

### 14.3 版本正确性

至少覆盖：

- publication 与 factor generation 不匹配时拒绝；
- raw/clean 任一哈希变化时拒绝；
- dataset version 与 membership 哈希不匹配时拒绝；
- publication 切换后缓存失效；
- 查询中切换 publication 不返回混合结果；
- 无 NASDAQ100 正式发布时返回 409 而非空成功；
- 旧 v1 数据版本缺完整哈希时返回可理解状态而非 500。

### 14.4 API 和页面

至少覆盖：

- URL 参数可恢复页面状态；
- 非交易日不静默回退；
- 日期、ticker 和因子输入校验；
- 研究状态条与查询 contract 一致；
- 桌面和移动视口无重叠、无文字溢出；
- 宽表横向滚动不会遮住筛选器；
- Plotly 空数据、单点数据和长区间均能正确显示；
- 浏览器控制台无未处理错误。

## 15. 实施顺序

### Phase 0：正式数据前置门槛

1. 重发带四类完整哈希的 SP500 正式行情版本；
2. 重跑并发布 SP500 的 8 因子 raw/clean generation；
3. 完成 NASDAQ100 PIT 门禁、正式行情和因子研究发布；
4. 确认 `research_publication.json` 与 raw/clean manifest 一致。

没有完成 Phase 0 时可以开发页面，但只能显示 fail-closed 状态，不能把旧数据描述成正式可用。

### Phase 1：查询领域层

1. 实现 `FactorObservationReader`；
2. 固化 rank 和 percentile 口径；
3. 实现 PIT 对齐和状态原因；
4. 实现 generation 级缓存；
5. 完成单元测试。

### Phase 2：只读 API

1. meta；
2. snapshot；
3. history；
4. export；
5. API 合同和错误状态测试。

### Phase 3：网页

1. 新增左侧“因子数据”；
2. 实现日期截面；
3. 实现单股历史；
4. 增加因子详情、股票池详情跳转；
5. 完成桌面与移动端视觉验收。

### Phase 4：旧入口收敛

1. 迁移策略排行 ticker 链接；
2. 删除旧单股 FMP 回退；
3. 删除旧单股 route、API、模板和专属代码；
4. 全量搜索 `/stock/` 和 `/api/stock/`，确认无残留依赖。

### Phase 5：SG 上线

1. 部署精确 commit；
2. 重启 `quant-web.service`；
3. 验收 SP500、NASDAQ100、MAG7 三池状态；
4. 抽查一个正向因子和一个负向因子；
5. 抽查 PIT 加入/退出证券；
6. 检查 Web 错误日志和响应性能。

## 16. 验收场景

### 场景 A：某日某股票

输入：

```text
股票池：SP500
因子：MOM_12M
日期：2026-07-31
股票：AAPL
```

必须得到：

- AAPL 的 raw；
- AAPL 的 clean；
- 因子方向；
- oriented value；
- rank / eligible count；
- percentile 和 quintile；
- PIT member；
- publication、generation 和 dataset version。

结果必须与对应 raw/clean Parquet 和完整 PIT 截面的独立计算一致。

### 场景 B：单股完整历史

输入：

```text
股票池：SP500
因子：MOM_12M
股票：AAPL
区间：全量
```

必须展示该 generation 从开始到结束的每日：

```text
date, raw, clean, rank, eligible_count,
percentile, quintile, pit_member, status
```

任意选择一天跳转回日期截面后，数值和排名必须完全一致。

### 场景 C：负向因子

输入：`VOL_20D`。

要求：clean 最低且有效的股票为 rank 1；页面方向说明为“因子值越低，预期收益越高”。不能沿用动量因子的高值优先逻辑。

### 场景 D：历史退出股票

选择一个曾属于 SP500、后来退出的股票：

- 在成员期间显示 raw、clean 和 rank；
- 退出后显示 `NOT_PIT_MEMBER`；
- 退出后的股票不进入排名分母；
- 页面不能因为它不是当前成分就删除其全部历史。

### 场景 E：研究未发布

NASDAQ100 没有正式 publication 时：

- 页面显示“纳斯达克100研究尚未发布”；
- API 返回 409 `RESEARCH_NOT_PUBLISHED`；
- 不调用 FMP；
- 不回退到 SP500、MAG7 或旧文件；
- Web 日志中没有未处理异常。

## 17. 完成定义

只有以下条件全部满足，才能宣布功能完成：

- “研究”左侧导航存在“因子数据”；
- 日期截面和单股历史两种模式均可使用；
- raw、clean、单因子 rank 定义明确且页面不混用策略 rank；
- rank=1 对所有正负向因子都表示预设方向下最优；
- 所有排名使用指定日期的完整 PIT 有效截面；
- snapshot 与 history API 同一观测逐字段一致；
- 查询严格绑定 research publication、factor generation 和 dataset version；
- 数据未发布、陈旧或无效时 fail closed 且不返回 500；
- 不存在 FMP 或旧文件静默回退；
- 旧单股页和旧排名口径已移除；
- 正向因子、负向因子、PIT 加入/退出和版本切换测试通过；
- 本地与 SG 页面、API、日志和性能验收通过；
- 运维和项目架构文档同步更新。

## 18. 2026-08-09 实施记录

当前状态严格拆成两部分：

- 已完成：查询领域层、只读 API、日期截面、单股历史、CSV、入口迁移、旧单股页删除、本地单元/API/浏览器验收；
- 未完成：Phase 0 的 SP500/NASDAQ100 正式数据重发，以及 Phase 5 的 SG 部署和生产性能验收。

本地完整测试为 `399 passed`。本地真实 SP500 因旧 `research_publication.json` 和旧行情版本缺少
当前要求的完整哈希而显示“完整性校验失败”；NASDAQ100 显示“尚未发布”。这是预期的
fail-closed 结果，不代表查询代码失败，也不能据此宣布正式数据已就绪。

实现明细和上线门槛见
[`factor_data_explorer_implementation.md`](factor_data_explorer_implementation.md)。
