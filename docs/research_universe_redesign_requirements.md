# 多层股票池、跨池因子研究与 NASDAQ100 改版需求

状态：需求基线（实现进度见
[`research_universe_redesign_implementation.md`](research_universe_redesign_implementation.md)）  
版本：1.0  
日期：2026-08-09

## 1. 文档目的

本次改版重新定义“因子研究、用户股票池、策略、回测和模拟盘”的边界，并新增严格的
NASDAQ100 point-in-time（PIT）研究池。

目标数据流为：

```text
广泛研究池
  -> 判断单因子是否具有统计预测力、稳定性、经济意义和可交易性
  -> 在多个研究池之间检查结论是否稳健

目标投资池
  -> 将已研究因子应用到用户实际关心的股票范围
  -> 计算池内 clean factor、综合分、排名和目标权重

最终组合
  -> 加入调仓、持仓、费用、滑点、容量、风险和成交约束
  -> 进入回测、模拟盘和决策回放
```

本次改版不是让所有 Watchlist 自动变成正式研究池，也不是用 NASDAQ100 替代 SP500。

## 2. 调研结论与设计依据

“核心研究池”是本项目采用的产品术语。行业中与之相近的术语包括 estimation universe、
research universe、parent universe 和 starting universe；用户可交易范围通常称为 investable
universe 或 trading universe。

- MSCI Barra 区分 coverage universe 和 estimation universe，并把代表性、流动性和稳定性列为
  估计池的主要目标：
  <https://www.msci.com/documents/1296102/1336482/Introducing_MSCI_IndexMetrics.pdf/23cbc36f-cf2c-4bf0-96c3-206eecdfdf6d>
- FTSE Russell 从 starting universe 出发应用因子得分、缩窄和容量/行业约束：
  <https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ftse-global-factor-index-series-methodology-overview.pdf>
- Fama/French 使用规则化、广泛且满足数据条件的 NYSE/AMEX/NASDAQ 股票构造因子，而非用户事后
  挑选的固定名单：
  <https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library/f-f_factors.html>
- QuantConnect 明确提示，按回测期表现手工选择固定股票池会产生前视偏差：
  <https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/universe-selection/manual-universes>
- Nasdaq 官方将 NASDAQ-100 定义为 Nasdaq 上市的约 100 家最大非金融公司，并采用年度重构、
  季度再平衡和持续维护规则：
  <https://indexes.nasdaqomx.com/docs/methodology_NDX.pdf>

结论：研究池与交易池分层不是伪需求，但不能误解为“只在广泛池研究，完全不在目标池验证”。
成熟流程应同时保留广泛有效性、目标市场稳健性和最终组合可实现性。

## 3. 当前系统的主要问题

### 3.1 产品概念混在一起

当前侧栏以“因子、回测、股票池”组织页面，但用户难以回答：

1. 哪些股票池用于证明因子可信？
2. 哪些股票池只是用户希望交易的范围？
3. 某个因子只在 SP500 有效，还是在 NASDAQ100 也有效？
4. 策略排名使用的是哪一个股票池和哪一版数据？
5. 回测结论来自因子研究，还是来自用户池内按需计算？

“因子回测概览”也把研究层的单因子分组检验和业务层的策略回测混用了同一个“回测”称呼。

### 3.2 研究池角色没有显式模型

`configs/default.yaml` 当前只有 `universes.enabled: [SP500, MAG7]`，不能表达：

- SP500 是主要统计研究池；
- NASDAQ100 是大型成长/科技稳健性池；
- MAG7 只能作为小样本参考；
- Watchlist 是目标投资池，不应自动获得“正式因子可信”标签。

### 3.3 缺少跨股票池结论

当前每个股票池独立输出 IC、ICIR、q-value 和置信等级，没有汇总：

```text
同一个 MOM_12M：
SP500       PASS / IC 0.031
NASDAQ100   WATCH / IC 0.014
MAG7        仅参考
跨池结论    主要市场有效，成长股池偏弱
```

### 3.4 当前正式 SP500 研究存在上线阻断问题

本次需求审阅确认以下问题必须先修复：

1. `scripts/run_mvp.py` 最初取得一个 `DatasetVersion`，但 bars/universe 读取仍可能再次跟随最新
   pointer；并发手工发布时存在混版窗口。
2. bars 和 membership 在 Reader 中强制校验 SHA-256，但 universe 的哈希只写入 manifest，读取时
   没有强制验证。
3. 本机 SP500 版本的 88 只历史退出成员全部缺失 sector；行业中性化会把这些股票的 clean factor
   置空。实测 AAL 的 MOM_1M raw 有 769 个非空观察，clean 为 0。

在这些问题解决前，不得宣称当前 SP500 clean factor 和置信结论已经完全 PIT 正确，也不得复制同一
缺陷到 NASDAQ100。

## 4. 新领域模型

### 4.1 Research Universe（研究股票池）

用于评估因子的统计可信度，不等同于最终交易范围。

建议配置结构：

```yaml
research_universes:
  SP500:
    role: PRIMARY
    membership_type: PIT
    benchmark: SPY
    confidence_enabled: true
    cross_universe_enabled: true
    minimum_cross_section: 100

  NASDAQ100:
    role: SECONDARY
    membership_type: PIT
    benchmark: QQQ
    confidence_enabled: true
    cross_universe_enabled: true
    minimum_cross_section: 60

  MAG7:
    role: REFERENCE
    membership_type: STATIC
    benchmark: QQQ
    confidence_enabled: false
    cross_universe_enabled: false
    minimum_cross_section: 3
```

角色定义：

| role | 含义 | 是否进入总体因子结论 |
|---|---|---:|
| `PRIMARY` | 主要统计研究池 | 是 |
| `SECONDARY` | 风格/市场稳健性研究池 | 是 |
| `REFERENCE` | 小样本展示或重点股票观察 | 否 |

### 4.2 Target Universe（目标投资池）

用户创建的 Watchlist 默认属于 Target Universe，职责是：

- 限定策略可以选择的证券；
- 触发专属 OHLCV 版本发布；
- 计算该池内的 raw/clean factor 和策略综合分；
- 生成股票排名、目标仓位、回测和模拟盘订单；
- 冻结 Watchlist revision、ticker hash 和数据版本，支持重放。

Target Universe 默认不发布 IC/ICIR/q-value，也不显示“因子正式通过”结论。

### 4.3 Promoted Research Universe（晋升研究池，后续能力）

用户可以显式申请将某个 Watchlist 晋升为研究池，但本期只预留模型和 UI 状态，不强制实现完整
自动晋升。

建议门槛：

- 至少 30 只股票；高可信研究建议至少 100 只；
- 明确 `FIXED_BASKET` 或 `PIT_DYNAMIC` 语义；
- 冻结 revision 和 `valid_from`，禁止事后名单冒充历史 PIT；
- 有足够因子 warm-up、前向收益和数据覆盖；
- 有行业覆盖和可交易性覆盖报告；
- 设置每日研究配额，不能让无限 Watchlist 占满 worker。

### 4.4 Portfolio Run（最终组合运行）

回测和模拟盘必须同时冻结：

```text
strategy_snapshot
target_universe_snapshot
dataset_version_id
factor_publication_id 或 runtime_factor_id
execution_config
risk_config
```

## 5. 因子可信度的新口径

### 5.1 单池结论

PRIMARY/SECONDARY 池继续独立计算：

- raw/clean factor；
- Rank IC、ICIR、t/p/q-value；
- 月度、滚动和子区间稳定性；
- 分组单调性和扣费后多空收益；
- Rank 自相关、换手和年化成本；
- 数据质量与覆盖率。

REFERENCE 池可以计算排名和分组图，但不能给出与 PRIMARY 同等级的统计 PASS。

### 5.2 跨池结论

新增 `CrossUniverseFactorAssessment`，至少包含：

```json
{
  "factor_id": "MOM_12M",
  "target_session": "2026-08-07",
  "universes": {
    "SP500": {"role": "PRIMARY", "verdict": "PASS", "ic_mean": 0.031},
    "NASDAQ100": {"role": "SECONDARY", "verdict": "WATCH", "ic_mean": 0.014}
  },
  "direction_consistent": true,
  "verdict": "PRIMARY_ONLY",
  "summary": "SP500 有效，NASDAQ100 同方向但证据偏弱"
}
```

第一版跨池状态：

| 状态 | 建议规则 |
|---|---|
| `ROBUST` | PRIMARY 与 SECONDARY 都 PASS，方向一致 |
| `PRIMARY_ONLY` | PRIMARY PASS，SECONDARY WATCH/样本不足，方向不冲突 |
| `SEGMENT_SPECIFIC` | SECONDARY PASS，但 PRIMARY 未通过 |
| `CONFLICT` | 两个池显著方向相反，或一个 PASS、另一个 FAIL 且方向冲突 |
| `INSUFFICIENT` | 任一必要研究发布缺失或不可比 |
| `REJECT` | PRIMARY 与 SECONDARY 均 FAIL |

跨池结论不能简单平均两个 IC。必须同时展示样本数、方向、统计显著性、经济表现和交易成本。

### 5.3 策略使用规则

策略组件选择因子时显示研究证据，但第一版不自动禁止使用 `WATCH/FAIL` 因子。创建者必须能看到：

- SP500 结论；
- NASDAQ100 结论；
- 跨池结论；
- 最近研究日期；
- 是否存在方向冲突；
- 因子方向和策略权重是否一致。

后续可增加策略级 policy，例如只允许 `ROBUST` 或 `PRIMARY_ONLY` 因子进入正式模拟盘。

## 6. NASDAQ100 数据和研究需求

### 6.1 命名要求

系统内部统一使用 `NASDAQ100`，禁止使用含义不明确的 `NASDAQ`。

基准使用 QQQ，但 QQQ 本身不是 membership 的普通 constituent。

### 6.2 数据源验证

FMP 提供 `nasdaq-constituent` 和 `historical-nasdaq-constituent`，但编码前必须验证它们确实对应
NASDAQ-100，而不是 Nasdaq Composite 或其他集合。

上线门槛：

1. 当前成分与 Nasdaq 官方名单逐项核对；
2. 最近至少 10 组加入/退出事件与官方公告核对；
3. 验证多股类、ADR、改名、并购、spin-off、fast entry 和临时超过 100 只的情况；
4. 验证事件日期是公告日还是生效日；
5. 输出差异报告，存在未解释差异时 fail closed。

### 6.3 PIT 构建

新增正式产物：

```text
configs/nasdaq100_pit_corrections.yaml
data/raw/pit/NASDAQ100/asof=<DATE>/run=<RUN_ID>/
data/pit_universes/NASDAQ100.parquet
data/pit_universes/NASDAQ100.metadata.json
```

Raw 审计目录至少保存：

- current constituents；
- provider historical changes；
- normalized events；
- corrections audit；
- candidate membership；
- diagnostics。

PIT 文件继续使用完整快照语义：`date,ticker,active`。

### 6.4 行情发布

Curated 版本结构：

```text
data/lake/curated/equity_daily/
  universe=NASDAQ100/
    version=<VERSION_ID>/
      bars.parquet
      universe.parquet
      membership.parquet
      manifest.json
```

质量门禁至少包括现有 schema/OHLC/XNYS/coverage 检查，以及：

- PIT 当前快照与当前 universe 完全一致；
- 历史 PIT ticker 并集全部进入 bars/universe；
- 每个 session 的 PIT 行情覆盖达到配置门槛；
- 行业分类覆盖达到正式中性化要求；
- 当前成分数量、多股类数量和异常事件在合理区间；
- bars、universe、membership、manifest 均可校验哈希。

### 6.5 每日调度

建议在现有 08:15 数据任务中依次执行：

```text
SP500 PIT
NASDAQ100 PIT
SP500 market publication
NASDAQ100 market publication
MAG7 market publication
```

08:45 研究任务运行：

```text
SP500 factor publication
NASDAQ100 factor publication
MAG7 reference publication
cross-universe assessment
```

任一 PRIMARY/SECONDARY 池数据失败时：

- 不得使用旧日期冒充最新研究；
- 其他池可以独立发布，但跨池状态必须为 `INSUFFICIENT`；
- 页面明确显示失败池、目标 session 和错误原因。

## 7. 数据正确性前置改造

### DATA-001：一次解析并冻结 DatasetVersion

研究、回测、排名和模拟盘统一使用 `load_published_bundle()`；一次运行开始后，bars、universe、
membership 和 factor publication 都必须来自同一个显式 `dataset_version_id`。

不得在同一次运行中再次隐式 `require_latest()`。

### DATA-002：完整文件完整性验证

`DatasetVersion` 或版本 manifest 必须提供并强制校验：

- `bars_sha256`；
- `universe_sha256`；
- `membership_sha256`；
- `manifest_sha256` 或等价签名。

### DATA-003：历史证券主数据

不能再为历史退出 ticker 只创建 `sector=None` 的占位行。至少建立：

```text
ticker
name
issuer_id（可为空但需预留）
sector
sub_industry
effective_from
effective_to
source
source_asof
```

如果暂时无法获得严格 PIT 行业历史，必须：

1. 明确使用何种回填政策；
2. 将未知行业作为显式 `UNKNOWN`，不能无声丢弃股票；
3. 发布行业覆盖率；
4. 低于门槛时禁止发布“行业中性化已完成”的正式结论。

### DATA-004：中性化审计

每个因子研究必须输出：

- 每日中性化是否执行；
- 每日进入回归的股票数；
- 因缺行业/市值被排除的 ticker 数；
- `applied/skipped` 比例；
- 行业覆盖率；
- raw 非空但 clean 全空的 ticker 清单。

存在“PIT 活跃且 raw 有值，但因缺分类导致 clean 全空”的股票时，正式研究默认失败。

## 8. 网页信息架构改版

### 8.1 左侧大导航

所有大组默认展开，不显示序号。

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
  行业/主题
```

原“因子回测概览”更名为“研究总览”。“回测”只用于策略或组合回测，避免把单因子研究与策略回测
混为一谈。

### 8.2 全局发布状态

页面顶部提供紧凑状态条：

```text
市场数据：2026-08-07
SP500 研究：已发布
NASDAQ100 研究：已发布
跨池结论：已发布
数据延迟：0 个交易日
```

状态条只显示状态，不在每个页面重复大段说明。

### 8.3 研究总览

第一屏直接显示因子比较表，而不是营销式卡片：

| 因子 | 跨池结论 | SP500 | NASDAQ100 | IC | ICIR | q-value | 扣费后 LS Sharpe | 最新日期 |
|---|---|---|---|---:|---:|---:|---:|---|

要求：

- 支持按跨池结论、单池结论、因子类别筛选；
- 显示方向冲突；
- MAG7 不进入主表默认列，可作为参考展开；
- 点击因子进入统一因子详情；
- 未完成跨池发布时显示 `INSUFFICIENT`，不能沿用昨天绿色状态。

### 8.4 研究股票池页

每行一个 Research Universe：

| 股票池 | 角色 | 当前成员 | 历史并集 | PIT | 行业覆盖 | 数据版本 | 研究发布 | 基准 |
|---|---|---:|---:|---|---:|---|---|---|

详情页提供：

- 当前成员与历史变更；
- membership 起止日期和快照数；
- bars/universe/membership 哈希；
- 数据质量门禁；
- 行业分类覆盖；
- 本池所有因子结论；
- 最近发布和失败历史。

### 8.5 因子详情页

使用页面内标签页：

```text
概览 | 跨池比较 | 预测力 | 稳定性 | 经济意义 | 可交易性 | 数据质量
```

概览顶部明确：

- 因子公式、方向和预处理；
- SP500/NASDAQ100 单池状态；
- 跨池状态；
- 适合/不适合的市场范围；
- 数据和研究 publication ID。

### 8.6 目标股票池页

原 Watchlist 页面改名为“目标股票池”，保留用户熟悉的“自选股票池”文案作为对象类型。

每行显示：

- 名称和股票数；
- ticker revision hash；
- 行情状态和最新 session；
- 最近策略排名时间；
- 关联回测和模拟盘数量；
- 类型：`TARGET` 或未来的 `PROMOTED_RESEARCH`。

不得在普通目标池页面显示正式 IC PASS，因为该池没有正式研究 publication。

### 8.7 策略详情与股票排名

策略详情先展示“研究证据”，再展示“应用结果”：

```text
研究证据：每个组件因子的 SP500/NASDAQ100/跨池状态
应用范围：当前选择的目标股票池
应用结果：池内 raw、clean、贡献、综合分、排名、目标权重
```

排名页必须显示：

- `requested_universe`；
- `data_universe`；
- `dataset_version_id`；
- Watchlist revision hash；
- 评分日期；
- 标准化发生在哪个截面。

同一只股票在 SP500、NASDAQ100 和十只股票 Watchlist 中的 z-score 可能不同，页面不得只显示分数而
隐藏标准化股票池。

### 8.8 回测与模拟盘

创建页按以下顺序组织：

```text
选择策略
选择目标股票池
查看研究证据
选择组合与风险约束
确认成交模型和费用
提交
```

详情页分开显示：

- 研究证据：因子在研究池中的结论；
- 目标池证据：本次目标池内的排名和暴露；
- 组合结果：持仓、收益、风险；
- 执行结果：逐票成交、手续费、滑点和未成交；
- 数据契约：所有冻结 ID 和 checksum。

## 9. API 与存储要求

建议新增只读 API：

```text
GET /api/research/universes
GET /api/research/universes/{universe}
GET /api/research/factors
GET /api/research/factors/{factor_id}
GET /api/research/factors/{factor_id}/cross-universe
GET /api/research/status
```

跨池产物建议保存为不可变 Parquet/JSON generation，并用原子 pointer 发布：

```text
outputs/research/cross_universe/generation=<ID>/
  factor_assessments.parquet
  manifest.json

outputs/research/cross_universe/publication.json
```

manifest 必须绑定每个参与股票池的：

- `dataset_version_id`；
- `research_publication_id`；
- `target_session`；
- factor generation；
- bars/membership checksum。

Research Universe 注册表属于受版本控制的配置；用户 Target Universe 继续存 SQLite。

## 10. 代码改造建议

建议增加明确领域包，避免继续扩大 `routes_v2.py`：

```text
src/research_universes/
  models.py              # role、membership type、registry model
  registry.py            # YAML -> typed registry
  cross_universe.py      # 跨池结论
  publication.py         # 跨池原子发布/校验
  service.py             # 编排

src/webapp/
  research_routes.py
  strategy_routes.py
  trading_routes.py
```

数据层建议将 SP500 专用 PIT 编排逐步抽象成共享骨架，但保留指数特定的规范化和修正规则：

```text
shared PIT publication contract
  + SP500 event adapter/corrections
  + NASDAQ100 event adapter/corrections
```

不要把两个指数的特殊事件处理硬塞进一个大函数。

## 11. 迁移阶段

### Phase 0：正确性阻断修复

- DATA-001 单版本 bundle；
- DATA-002 完整哈希；
- DATA-003 历史证券/行业；
- DATA-004 中性化审计；
- 重跑 SP500 并证明历史退出成员进入 clean factor。

### Phase 1：领域模型与文案

- 引入 Research/Target/Portfolio 三层术语；
- `universes.enabled` 迁为带 role 的 registry；
- MAG7 标为 REFERENCE；
- 原“因子回测概览”改名。

### Phase 2：NASDAQ100 数据基础

- 验证 FMP endpoint 语义；
- 实现 NASDAQ100 PIT 和修正注册表；
- 发布并影子校验行情版本；
- 与官方当前成分和历史事件对账。

### Phase 3：NASDAQ100 研究与跨池结论

- 发布 8 因子完整研究；
- 新增 CrossUniverseFactorAssessment；
- 同 target session 原子发布；
- MAG7 从总体结论中排除。

### Phase 4：网页信息架构

- 上线新左侧导航；
- 研究总览、研究股票池和跨池稳健性页面；
- 策略页增加研究证据；
- 回测/模拟盘分离研究证据、目标池结果和执行结果。

### Phase 5：可选的用户池晋升

- 申请和审批状态；
- 最小样本、PIT 语义、配额和发布规则；
- 不满足门槛时继续作为普通 Target Universe。

## 12. 验收标准

### 数据正确性

- 同一次研究的 bars/universe/membership 都能证明属于同一个 version ID；
- 任一文件哈希不符时研究 fail closed；
- SP500/NASDAQ100 历史 PIT 活跃 ticker 不能因缺行业无声退出 clean factor；
- 当前成分、历史事件、membership 和行情覆盖均有机器可读诊断；
- NASDAQ100 未通过来源语义验证时不得发布。

### 统计研究

- SP500 与 NASDAQ100 分别产出完整单池报告；
- 跨池报告不简单平均 IC；
- MAG7 不参与总体 PASS；
- 最新五日因前向收益未实现而缺 IC 时，页面正确说明而非记为失败；
- 每个结论可追溯到 factor/data publication。

### 产品页面

- 用户能明确区分研究股票池和目标股票池；
- 用户能在两次点击内看到因子的跨池结论和单池证据；
- 策略排名明确显示标准化股票池和数据版本；
- 回测/模拟盘明确区分研究证据、目标池计算和执行结果；
- 所有大导航默认展开，移动端不重叠。

### 运维

- 新增 NASDAQ100 后每日任务仍按同一 target session 发布；
- 任一研究池失败不污染其他池，但跨池结论必须降为 `INSUFFICIENT`；
- systemd 日志能按 universe、run ID、version ID 和 publication ID 检索；
- SG 重启后 Web 只读取已经完成的原子 publication。

## 13. 明确不在本期范围

- 不自动把所有 Watchlist 变成研究池；
- 不让网页请求直接调用 FMP；
- 不让 NASDAQ100 替代 SP500；
- 不使用 MAG7 的小样本结果证明因子统计可靠；
- 不在跨池结论中简单平均 IC 或总分；
- 不在没有严格 PIT 或明确 fixed-basket 标签时回测历史用户名单并宣称无前视偏差；
- 不在本期上线真实券商交易。

## 14. 完成定义

本次改版完成时，系统应该能够让用户清楚回答：

1. 这个因子在 SP500 是否可靠？
2. 它在 NASDAQ100 是否同方向、同样稳定？
3. 跨池结论是稳健、局部有效还是冲突？
4. 我的目标股票池使用了哪些因子和哪一版数据？
5. 目标池内为什么是这些股票排名靠前？
6. 最终组合加入成本、风险和成交约束后表现如何？
7. 每个结论能否追溯到不可变的数据和研究版本？

只有这七个问题都能从页面直接得到答案，新的研究、策略和交易链路才算真正闭环。
