# 大盘顶部与底部信号：研究、筛选与验证规范

> 状态：第二阶段，G1-G6 第一轮有效性筛选已完成
> 更新日期：2026-08-02
> 研究对象：标普 500、纳斯达克 100，以及必要的市场内部和跨资产数据
> 本文是该主题的唯一主文档。视频指标核验完整保留在第十三节。

## 一、研究目标

我们不把系统设计成一个声称能够精确预言最高点和最低点的“神奇指标”。
最终应该输出的是在不同时间尺度上的条件概率和可审计状态：

```text
top_risk_5d              未来 5 个交易日进入显著下跌的概率
top_risk_20d             未来 20 个交易日进入显著下跌的概率
bottom_reversal_5d       当前压力状态在 5 日内发生可交易反转的概率
bottom_reversal_20d      当前压力状态在 20 日内发生可交易反转的概率
market_regime            TREND / STRESS / REVERSAL / RECOVERY / RANGE
signal_confidence        LOW / MEDIUM / HIGH
```

页面上的顶部和底部标记必须区分三件事：

1. **预警**：风险条件正在积累，但反转尚未发生；
2. **候选**：价格和市场内部出现反转迹象；
3. **确认**：只有等待后续价格才能确认，天然会比实际极值晚。

精确最低价只能在未来数据出现后识别。生产系统可以实时预测“附近可能形成底部”，
不能在当天使用未来数据把最低点伪装成实时信号。

## 二、研究原则

### 2.1 顶部和底部分开建模

顶部通常可能经历较长时间的趋势减速、宽度收窄或信用恶化；底部更容易与高波动、
流动性冲击和被迫减仓同时出现。这种不对称必须由样本验证，不能强迫一个分数同时
解释两边。

### 2.2 不同预测期限分开

5 日、20 日和 60 日预测的是不同问题。4 小时双底适合短期确认，估值和信用环境
更适合中长期状态过滤，不能放进同一个窗口后任意加权。

### 2.3 标签可以看未来，特征绝对不能

训练标签必须用未来收益判断事后是否发生顶部或底部，但输入特征只能使用当时已经
发布的数据。每个外部数据还必须保存 `available_at`，而不只是经济数据所属日期。

### 2.4 先验证单项，再建立组合

每个指标先独立做事件研究和样本外验证。只有确认它在已有模型之外还能增加信息，
才进入组合。不能先给 VIX、Fear & Greed、双底等指标各分配一个主观权重。

### 2.5 数据基础不合格时拒绝产出

市场宽度必须使用当日真实成分股；周频和月频数据必须按实际发布日期对齐；
缺失值不能填成中性。无法证明 point-in-time 的历史数据只能用于探索，不能用于
最终结论。

## 三、项目当前数据审计

以下是 2026-07-31 对本地项目文件的实际检查结果：

| 项目 | 当前状态 | 对顶底研究的影响 |
| --- | --- | --- |
| SP500 `close/returns` 宽表 | 2021-05-07 至 2026-05-08，1257 日、502 列 | 只有约 5 年，重大周期样本过少 |
| 当前 SP500 成分表 | 503 只当前成分 | 不能代替历史成分 |
| PIT 代码和强制校验 | `src/data/pit.py` 已实现 | 基础设施存在 |
| PIT 成分文件 | 本地没有 `data/pit_universes/SP500.parquet` | 目前无法做可信的历史宽度回测 |
| SPY 日线原始数据 | 2026-01-12 至 2026-07-10，共 124 日 | 不足以训练 |
| QQQ 日线原始数据 | 2025-11-03 至 2026-07-10，共 171 日 | 不足以训练 |
| IWM 日线原始数据 | 2026-01-12 至 2026-07-10，共 124 日 | 不足以训练 |
| VIX、VIX3M、VVIX、SKEW | 本地未建立正式历史表 | 波动率和期权层尚缺 |
| 4 小时指数历史 | 本地没有连续 SPY/QQQ 4 小时历史 | 暂不能验证双底 |
| 市场宽度函数 | 已有 `compute_breadth()` | PIT 数据完成后可以复用 |

现有 `compute_breadth()` 已严格计算上涨数、下跌数、`breadth_net` 和
`ad_ratio`，但计算公式正确不代表输入历史成分正确。当前最大风险不是公式，
而是样本过短和幸存者偏差。

FMP 官方文档提供指数完整日线、1 小时指数数据和历史 S&P 500 变更端点，可作为
原始输入：

- [FMP Historical Index Full Chart](https://site.financialmodelingprep.com/developer/docs/stable/index-historical-price-eod-full)
- [FMP Stable API Indexes](https://site.financialmodelingprep.com/developer/docs/stable)

历史成分变更必须重建成“每个有效日期的完整快照”，再与若干已知调仓日交叉核验。
FMP 的 additions/removals 记录不能直接交给当前 PIT loader，因为 loader 期待完整
snapshot，而不是增量事件。

## 四、候选信号体系

### 4.1 价格、趋势和结构

第一批日频价格特征应保持简单、连续并避免过多参数：

```text
return_5d(t)       = P(t) / P(t-5) - 1
return_20d(t)      = P(t) / P(t-20) - 1
drawdown_63d(t)    = P(t) / max(P[t-62:t]) - 1
distance_ma20(t)   = P(t) / mean(P[t-19:t]) - 1
distance_ma200(t)  = P(t) / mean(P[t-199:t]) - 1
realized_vol20(t)  = std(daily_return[t-19:t]) × sqrt(252)
atr_pct14(t)       = ATR14(t) / P(t)
close_location(t)  = (close-low) / (high-low)
```

这些变量描述当前位置和市场状态，本身不应该被硬解释为顶或底。比如深度回撤既可能
意味着超卖，也可能意味着熊市趋势仍在延续。

#### 4 小时双底

双底必须从视觉名称改成确定性算法。一个候选定义是：

```text
L1、L2 是仅使用当时已完成 K 线确认的局部低点
abs(L2-L1) / ATR <= tolerance
两低点间隔位于 [min_bars, max_bars]
neckline = 两低点之间的最高价
确认 = 4h 收盘突破 neckline
失效 = 收盘跌破 min(L1,L2) - invalidation_buffer
```

系统最早只能在第二个低点被后续 K 线确认后发出“候选”，在突破颈线后发出“确认”。
它不可能合法地在 L2 最低价那根 K 线上直接宣称双底成立。

Lo、Mamaysky 和 Wang 使用自动化模式识别研究了 double-bottom 等技术形态，发现
部分形态对后续收益分布提供增量信息，但同时强调视觉技术分析的主观性。
[Foundations of Technical Analysis](https://www.nber.org/papers/w7613)

因此，4 小时双底属于需要严格参数化和样本外验证的 P1 确认信号，而不是核心底部
标签。

### 4.2 市场宽度和内部结构

指数可能由少数大市值股票推动，市场宽度负责回答“有多少股票真正参与”：

```text
breadth_net(t) =
    (advance_count - decline_count) / valid_member_count

pct_above_ma_k(t) =
    count(close_i(t) > MA_k_i(t)) / valid_member_count

new_high_low_net(t) =
    (new_52w_highs - new_52w_lows) / valid_member_count

equal_cap_spread(t) =
    equal_weight_return(t) - cap_weight_return(t)

dispersion(t) =
    std(cross_sectional_stock_returns(t))
```

还应计算成分股平均相关性或由第一主成分解释的方差，判断市场是否进入系统性
同涨同跌状态。

顶部候选假设可以是“指数创新高但宽度持续恶化”；底部候选假设可以是“宽度先
极端恶化，随后出现广泛修复”。这两个假设都不能直接写死阈值。

跨 64 个国家和 1973-2018 样本的研究发现，市场宽度对后续组合收益具有信息，
但这不是“宽度背离必然抓到指数顶部”的证明。
[Herding for Profits: Market Breadth and Global Equity Returns](https://www.sciencedirect.com/science/article/pii/S0264999319312982)

**必要条件：** 每一天的分母必须来自当日 PIT 成分，且历史退市股票行情必须存在。

### 4.3 波动率和期权市场

VIX 是由 SPX 期权价格推导的约 30 天预期波动率，不是直接的“恐慌概率”。候选
特征包括：

```text
vix_level
vix_change_1d / 5d
vix_rolling_percentile_5y
vix_zscore_5y
vix9d_vix3m_ratio = VIX9D / VIX3M
vix_vix3m_ratio   = VIX / VIX3M
vvix_level
skew_level
put_call_ratio_5d
```

Cboe 同时发布 9 日、30 日、3 月、6 月和 1 年预期波动率期限结构。
[Cboe VIX Term Structure](https://www.cboe.com/tradable-products/vix/term-structure)

期限结构倒挂代表近期波动定价高于远期，但仍是压力状态，不是自动买入信号。
VIX 高位可以继续升高，市场也可能在 VIX 尚未回落时反弹。

更有理论意义的变量是方差风险溢价：

```text
VRP(t) = option_implied_variance(t) - expected_realized_variance(t)
```

美联储研究发现 VRP 对未来 2-4 个月市场收益具有预测信息，但结果依赖正确构造的
model-free implied variance 和高频 realized variance。
[Expected Stock Returns and Variance Risk Premia](https://www.federalreserve.gov/econres/feds/expected-stock-returns-and-variance-risk-premia.htm)

所以第一版可以测试 VIX 与期限结构，VRP 放到数据条件更高的 P1 阶段，不能简单用
`VIX² - 日线波动率²` 冒充论文口径。

### 4.4 CNN Fear & Greed 与情绪

CNN Fear & Greed 当前由七项等权指标组成：

1. S&P 500 相对 125 日均线；
2. NYSE 52 周新高减新低；
3. McClellan Volume Summation Index；
4. 5 日 Put/Call Ratio；
5. VIX 相对 50 日均线；
6. 股票与国债 20 日相对收益；
7. 高收益债相对安全债券的利差。

[CNN Fear & Greed 方法说明](https://www.cnn.com/markets/fear-and-greed)

这个总分不应直接作为核心模型输入，原因是：

- 七项底层指标大多可以自行构造并分别检验；
- 等权没有证明最适合我们的顶底标签；
- CNN 页面已经披露过个别组件的计算调整，存在历史版本问题；
- 历史完整序列和每次修改时点不如交易所、监管和 FRED 数据容易审计；
- “Extreme Fear” 可能持续很久，不能等同于最低点。

正确做法是把七个原始组件放入候选池，CNN 总分仅作为外部对照和页面解释项。

### 4.5 因子压力、拥挤交易和仓位解除

视频带来的主要增量是机构仓位层：

```text
momentum_factor_return
momentum_factor_drawdown
momentum_factor_shock_z
momentum_factor_realized_vol
winner_leg_return
loser_leg_return
two_sided_loss
sector_concentration
CFTC_leveraged_funds_position
short_interest / days_to_cover
gross_and_net_exposure_proxy
```

动量 crash 常发生在市场先下跌、高波动、随后快速反弹的环境，但研究描述的是
共现关系，不能把动量 crash 单独当成领先底部信号。
[Momentum Crashes](https://www.nber.org/papers/w20439)

高盛 Prime 的 Gross/Net 和客户流量适合解释市场，但完整历史属于专有数据。
本项目可先构造公开、可复现的因子压力代理。详细定义、视频来源和数据限制见
第十三节。

### 4.6 流动性、成交和被迫卖出

仅使用 OHLCV 可以构造以下流动性代理：

```text
amihud_i(t) = abs(return_i(t)) / dollar_volume_i(t)
market_amihud(t) = PIT 截面中位数或稳健均值
volume_shock(t) = volume(t) / median(volume[t-20:t-1])
down_volume_ratio(t) = declining_volume / total_volume
gap_return(t) = open(t) / close(t-1) - 1
intraday_reversal(t) = close_to_close_return - gap_return
```

高波动、流动性供给撤退和融资约束可以共同形成去杠杆反馈。
[Market Liquidity and Funding Liquidity](https://www.nber.org/papers/w12939)

短期反转策略的预期收益在 VIX 高企的压力环境中显著提高，支持“强制价格压力后
可能出现反转补偿”的机制，但依然不能确定最低点。
[Evaporating Liquidity](https://www.nber.org/papers/w17653)

### 4.7 信用、宏观和跨资产状态

这些变量更适合做市场状态过滤，而不是精确到某一天：

```text
high_yield_OAS_level_and_change
excess_bond_premium
10Y_minus_3M_or_2Y_term_spread
financial_conditions_index
HYG_relative_to_LQD
HYG_relative_to_SPY
IWM_relative_to_SPY
equal_weight_relative_to_cap_weight
Treasury_relative_to_equity
```

美联储研究显示，Excess Bond Premium 能反映信用市场风险偏好，并对未来经济
活动和风险状态提供信息。
[The Transmission of Global Risk](https://www.federalreserve.gov/econres/notes/feds-notes/the-transmission-of-global-risk-20230627.html)

信用利差走阔可能提前提示风险，也可能只是与股票下跌同时发生。它必须与价格、
宽度和流动性联合检验，不能硬编码成“利差超过某值就是顶部”。

### 4.8 估值和基本面

CAPE、远期 P/E、股权风险溢价、盈利修正和指数集中度可以作为 60 日以上的慢速
先验，但不适合预测下周的最高点。估值高可以持续多年，估值低也可能在盈利快速
下修时继续下降。

这一层只调整长期风险基线，不参与第一版 5 日底部确认。

## 五、候选指标优先级

| 指标组 | 代表指标 | 主要用途 | 优先级 | 当前可行性 |
| --- | --- | --- | --- | --- |
| 指数价格 | 回撤、均线距离、趋势斜率 | 状态和前置条件 | P0 | 需重建长期指数日线 |
| 指数波动 | RV20、ATR%、跳空、收盘位置 | 压力和反转 | P0 | 可由 OHLCV 构造 |
| 日度宽度 | A/D、BreadthNet、上涨比例 | 市场参与度 | P0 | 先补 PIT 成分 |
| 中期宽度 | % above MA20/50/200 | 趋势扩散 | P0 | 先补 PIT 成分和退市股 |
| 截面状态 | 离散度、相关性、EW-CW | 拥挤和系统性压力 | P0 | 需 PIT 和历史市值 |
| VIX | 水平、变化、长期分位 | 期权压力状态 | P0 | 需接入 Cboe 历史 |
| VIX 期限 | VIX9D/VIX3M、VIX/VIX3M | 近期压力相对远期 | P0 | 需接入期限指数 |
| 动量压力 | 因子收益、回撤、波动、两腿损益 | 去杠杆代理 | P0 | 可基于 PIT 股票池构造 |
| 流动性 | Amihud、成交冲击、Down Volume | 被迫卖出代理 | P0 | PIT 完成后可构造 |
| 信用 | HY OAS、EBP、HYG/LQD | 风险状态过滤 | P0/P1 | FRED/Fed 与市场价格 |
| 4h 形态 | 双底、双顶、颈线突破 | 短期确认 | P1 | 需长期连续 4h 数据 |
| VRP | 隐含方差减预期实现方差 | 风险溢价 | P1 | 需要更严格期权/高频数据 |
| 期权情绪 | Put/Call、SKEW、VVIX | 尾部需求和情绪 | P1 | 需 Cboe 历史 |
| CFTC 仓位 | Leveraged Funds 净仓和变化 | 期货仓位 | P1 | 免费周频、有发布滞后 |
| FINRA 空头 | SI/Float、Days to Cover | 空头拥挤 | P1 | 每月两次 |
| CNN 总分 | Fear & Greed | 外部对照 | P2 | 不作为核心训练特征 |
| 高盛 Prime | Gross/Net、客户流量 | 机构解释 | P2 | 完整历史专有 |
| 估值 | CAPE、P/E、ERP | 长期风险先验 | P2 | 不做短期触发 |

P0 表示第一轮必须验证，并不表示它已经可靠。P1 等 P0 基线建立后再判断是否增加
信息；P2 主要用于解释、对照或未来数据升级。

## 六、如何定义顶部和底部标签

### 6.1 不使用“肉眼选点”

不能手工在 2020 年 3 月、2022 年 10 月等著名日期上标注后再调参数。标签算法
必须一次定义，应用到全部历史。

### 6.2 主标签：波动率缩放的 first-touch barrier

对指数价格 `P(t)` 和未来窗口 `h`：

```text
forward_return(t, i) = P(t+i) / P(t) - 1
MFE_h(t) = max(forward_return(t, 1:h))
MAE_h(t) = min(forward_return(t, 1:h))
daily_vol20(t) = std(daily_return[t-19:t])
barrier_h(t) = daily_vol20(t) × sqrt(h)
```

这里的 `daily_vol20` 是未年化的日波动率；如果错误使用已经乘过 `sqrt(252)` 的
年化波动率，屏障会被重复缩放。

#### 底部反转标签

先要求当前处在有意义的回撤状态，然后判断未来上涨屏障是否先于继续下跌屏障触达：

```text
precondition:
    drawdown_63d(t) <= training_quantile

Bottom_h(t) = 1:
    +k_up × barrier_h 先被触达
    且 -k_fail × barrier_h 尚未先被触达
```

#### 顶部风险标签

先要求价格位于阶段高位附近，然后判断未来下跌屏障是否先于继续上涨屏障触达：

```text
precondition:
    distance_to_63d_high(t) >= -epsilon

Top_h(t) = 1:
    -k_down × barrier_h 先被触达
    且 +k_continue × barrier_h 尚未先被触达
```

`k_up`、`k_fail`、`k_down`、`k_continue` 和 `epsilon` 只能在训练集确定，随后冻结。
波动率缩放比固定 5% 或 10% 更能适应高低波动时期。

第一轮分别建立 `h = 5、20、60` 日标签，不把三个窗口混合。

### 6.3 辅助标签：有显著度的局部极值

局部峰谷标签只用于事后事件分析和页面画点：

```text
PivotLow(t):
    P(t) 是 [t-w, t+w] 的最低点
    且随后反弹 prominence >= k × ATR

PivotHigh(t):
    P(t) 是 [t-w, t+w] 的最高点
    且随后下跌 prominence >= k × ATR
```

因为它使用 `t+w` 的未来价格，所以不能当作实时特征。评估实时信号时，可允许
信号落在真实极值前后 `±2` 或 `±3` 个交易日的事件窗口内，但每个事件只能记一次
命中。

## 七、研究数据规范

### 7.1 建立分层历史，而不是强行取公共最短区间

建议建立三个可独立验证的数据面板：

| 面板 | 目标历史 | 用途 |
| --- | --- | --- |
| Core Index | 尽量从 1990 年开始 | 指数价格、VIX、信用和宏观 |
| PIT Breadth | 至少覆盖多个牛熊周期 | SP500/NDX 成分宽度和截面特征 |
| Intraday Structure | 在供应商允许范围内尽量延长 | 1h/4h 双底、双顶和日内反转 |

如果某指标只有 5 年历史，它只能参加 5 年共同样本的增量测试，不能迫使所有基础
指标都放弃更长历史。

### 7.2 每个值保存可用时点

统一研究表至少需要：

```text
observation_date
available_at
instrument_or_universe
feature_name
feature_value
source
source_version
quality_status
age
```

周频 CFTC 或月频信用数据可以在发布后持有上一个已知值，但必须同时保存 `age`。
禁止把未来修订值回填到原发布日期之前。

### 7.3 PIT 宽度要求

- 当日 active membership 来自最近一个有效完整 snapshot；
- 历史成分并集的退市股票行情必须存在；
- 分母使用当日应有成分数，不使用当前 503 只；
- 价格缺失和未上市必须区分；
- 日度 coverage 低于门槛时，宽度信号返回 `null`；
- 所有输入文件保留 SHA256 和来源版本。

## 八、信号筛选流程

### 8.1 阶段 0：候选登记

每个候选指标先登记以下内容：

```text
名称和唯一版本
经济或行为机制
完整公式
预期对 Top/Bottom 的方向
预测期限
数据源和 available_at
允许测试的参数范围
与已有信号可能重复的原因
```

登记后再看结果，防止发现某个漂亮年份后修改故事。

### 8.2 阶段 1：数据质量门

先检查覆盖率、重复日期、时区、公司行动、成分股 PIT、发布滞后和异常值。数据门
不通过时不产生 IC、命中率或图表。

### 8.3 阶段 2：单变量事件研究

每个信号先按训练期滚动分位分箱，而不是马上指定阈值。例如将 VIX percentile
分成五组，比较各组未来：

```text
Top/Bottom 事件发生率
未来 5/20/60 日收益
MFE 和 MAE
最大回撤
信号到真实极值的提前/滞后天数
每年误报数量
```

需要观察关系是否单调、是否只由一两个危机年份贡献。

### 8.4 阶段 3：严格时间顺序的 walk-forward

采用 expanding 或 rolling window：

```text
训练历史 -> 下一段验证
训练历史扩展 -> 再下一段验证
...
最后保留一段完全封存的 final test
```

由于 20 日和 60 日标签相互重叠，训练与验证边界要留出至少一个最大标签窗口的
embargo，避免相邻样本共享未来收益。

随机打乱日期的 K-Fold 不适用于本任务。

聚合市场收益预测尤其容易出现样本内漂亮、样本外失效。相关研究发现，很多传统
预测变量离开训练样本后不能稳定战胜历史均值，即使真正存在的预测能力通常也较小。
[Predicting the Equity Premium Out of Sample](https://www.nber.org/papers/w11468)

### 8.5 阶段 4：多重检验控制

测试几十个指标、窗口和阈值后，普通 `p < 0.05` 很容易选中偶然结果。研究显示，
金融因子在大规模数据挖掘下需要比传统 t=2 更高的门槛。
[Harvey, Liu and Zhu, Multiple Testing](https://www.nber.org/papers/w20592)

本项目必须：

- 记录所有尝试过的特征和参数，不能只记录赢家；
- 对同一候选族使用 Benjamini-Hochberg FDR；
- 对大量策略变体报告 Deflated Sharpe Ratio 或 PBO；
- 不使用 final test 选择阈值。

回测过拟合可通过组合对称交叉验证估计。
[The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)

### 8.6 阶段 5：增量信息

两个信号可能只是同一压力状态的不同表达。例如 VIX、VIX 相对均线和 CNN
Fear & Greed 的 volatility 组件高度重叠。

候选指标只有在加入基础模型后仍能改善样本外 Brier Score、PR-AUC、校准或经济
结果，才保留。不能因为单变量图漂亮就重复计权。

### 8.7 阶段 6：经济价值

统计显著不等于可交易。通过统计门的信号需要接入现有回测执行层，检验：

- 降低风险暴露能否改善最大回撤和 Calmar；
- 恢复风险暴露是否错过过多反弹；
- 交易次数、换手、滑点和费用；
- 相比固定持有和简单均线规则是否真的增加价值；
- SPX、NDX 和 IWM 上是否具有合理迁移性。

交易成本使用项目已有的统一费用与滑点模型，不能为市场信号另设更有利的口径。

### 8.8 阶段 7：稳健性和影子运行

最终候选还要经过：

- 参数上下浮动约 20%；
- 不同训练起点；
- 去掉 2008、2020 等单个危机后的结果；
- 牛市、熊市、高波动和低波动分层；
- SPX 与 NDX 交叉验证；
- 至少 60 个交易日 shadow，不驱动真实仓位。

## 九、暂定录取门槛

这些门槛在正式 final test 前冻结；如需修改，只能在开发集阶段修改并留下版本：

| Gate | 暂定要求 |
| --- | --- |
| G0 可复现 | 公式、源文件、版本、`available_at` 和代码 hash 完整 |
| G1 数据 | 指数日线无缺口；PIT breadth 日度 coverage 至少 98% |
| G2 样本 | 开发期至少包含 3 种显著市场状态；每类事件不足 30 个只标记探索性 |
| G3 方向 | 预期方向在至少 75% walk-forward folds 一致 |
| G4 预测 | OOS Brier Skill 大于 0，PR-AUC 高于同期基准发生率 |
| G5 置信 | 95% block-bootstrap 区间的 OOS 增量下界大于 0，并通过 leave-one-crisis-out |
| G6 多测 | 候选族 Benjamini-Hochberg FDR `q <= 0.10`，所有失败尝试保留 |
| G7 稳健 | 参数上下扰动约 20% 后方向不变，并保留至少 70% 的中位效果 |
| G8 增量 | 加入基础模型后，在超过一半 OOS folds 改善概率或经济指标 |
| G9 经济 | 统一成本后改善预先指定的风险目标，而非只挑最好看的指标 |
| G10 运行 | 60 个交易日 shadow 无数据时点、缺失或状态机错误 |

由于顶部和底部是稀有类别，不能用普通 Accuracy。主要统计指标为：

```text
event precision / recall
PR-AUC
Brier Score / Brier Skill Score
probability calibration
false alarms per year
median lead/lag
MFE / MAE after signal
```

## 十、模型路线

### 10.1 基础模型

第一版使用可解释模型：

1. 单变量滚动分位和事件率；
2. Logistic Regression；
3. 带 Elastic Net 的 Logistic；
4. 必要时使用离散时间 hazard model。

顶部、底部和各预测期限使用独立模型。

### 10.2 挑战模型

只有基础模型完成后，才增加 Gradient Boosting 等非线性模型。挑战模型必须使用
相同 walk-forward 划分和封存测试集，并进行概率校准。它只有在稳定提高样本外
指标时才能替代基础模型。

### 10.3 输出不是一个神秘总分

页面至少展示：

```text
Top Risk Probability
Bottom Reversal Probability
Regime
各信号组贡献
数据截至时间和质量
触发、确认和失效原因
历史相似事件
```

模型不确定时应该显示低置信，而不是强制选择顶部或底部。

## 十一、第一批实施范围

### 阶段 A：研究数据集

1. 重建 `^GSPC`、`^NDX`、SPY、QQQ、IWM 长期日线；
2. 接入 VIX、VIX9D、VIX3M 的可审计历史；
3. 接入 HY OAS / EBP 等信用状态；
4. 从历史 additions/removals 重建完整 SP500 PIT snapshots；
5. 下载历史成分并集，包括已退出和退市股票；
6. 宽表增加 `high`、`low`，保留 raw/adjusted 两种价格语义；
7. 为所有外部数据记录 `available_at`、版本和质量状态。

### 阶段 B：标签与 P0 特征

先实现：

- Top/Bottom 的 5、20、60 日 first-touch labels；
- 指数回撤、趋势、均线距离、RV、ATR、跳空和收盘位置；
- A/D、BreadthNet、% above MA20/50/200；
- 截面离散度、相关性和 EW-CW spread；
- VIX 水平、变化、分位和期限结构；
- 动量因子收益、回撤、波动和两腿损益；
- Amihud、成交量冲击和 Down Volume；
- HY OAS 与 HYG/LQD 风险状态。

### 阶段 C：自动研究报告

每次研究运行固定产出：

```text
data_manifest.json
feature_registry.parquet
labels.parquet
univariate_event_studies.parquet
walk_forward_predictions.parquet
candidate_scorecard.parquet
research_report.html
```

先通过报告筛选，随后才把获准信号接入研究页面。

### 阶段 D：P1 和 4 小时形态

在日频基线冻结后，再加入 4 小时双底/双顶、Put/Call、SKEW、VVIX、CFTC 和
FINRA。这样可以准确衡量每一层真正增加了多少信息。

## 十二、下一步决策

下一项代码工作应是“市场状态研究数据集与标签引擎”，而不是先开发页面上的红绿
顶底图标。正确顺序是：

```text
长期 PIT 数据
    -> 确定性标签
    -> P0 特征
    -> walk-forward 评估器
    -> 候选信号 scorecard
    -> 审核通过
    -> 页面和每日计算
```

只有 scorecard 通过录取门槛的指标，才能进入最终组合。

### 12.1 第一阶段实施状态

第一阶段代码已经开始落地，完整实现、数据语义、运行命令、真实样本验收和当前
阻断项见：

[大盘顶底研究系统：第一阶段实现说明](market_regime_research_implementation.md)

[大盘顶底研究系统：代码复审报告](market_regime_research_code_audit.md)

截至 2026-08-02：

- 已建立 1990 年起的 SPX/NDX、ETF 和 Cboe 长期市场数据；
- 已实现 first-touch 标签和 139 个市场核心 P0 特征；
- 已识别并阻止 FMP 未收盘日线进入研究；
- FMP PIT 事件在 1990 年以来仍有 23 组无法闭合（2 组无法分类、9 组
  新增后缺失、12 组 ticker 复用/继承冲突），因此没有发布生产 PIT；
- 已对 139 个核心特征的 834 个 `feature × side × horizon` 组合完成
  event study、walk-forward、FDR 和 G1-G6 scorecard；
- 事前登记的 62 个测试中只有 `bottom_spx_return_5d__5d` 阶段性通过，
  但 G7-G10 尚未完成，因此生产批准仍为 0；
- 2022 年起的封存集未参与筛选；
- 尚未进入组合模型、影子运行和页面开发。

第一轮方法、真实结果、产物和后续门槛见：

[大盘顶底信号：第一轮有效性筛选报告](market_regime_effectiveness_screening.md)

## 十三、视频指标专项核验

> 视频：`stodownload.mp4`，约 3 分 18 秒
> 核验日期：2026-07-31
> 本节负责还原、定义和核验视频观点，不把视频叙事直接当作生产信号。

### 13.1 先说结论

这段视频真正提供的不是传统 K 线顶底形态，而是一套观察“拥挤交易解除”
的机构资金视角：

1. 动量因子大幅反转；
2. 动量因子的波动率异常上升；
3. 多空基金同时削减多头和空头，即 `de-grossing`；
4. Gross / Net Exposure 从极端高位下降；
5. 拥挤的 AI、半导体和高动量股票遭到集中减仓；
6. 原来的弱势股因空头回补出现急涨；
7. 流动性和融资约束放大上述价格运动。

这些现象适合判断“市场内部是否正在发生仓位清洗”，但不能直接等同于
“标普 500 或纳斯达克已经见底”。尤其值得注意的是，高盛对同一轮行情的公开
说明把它称为一次健康的仓位重置，同时明确说仓位尚未达到彻底出清状态。

### 13.2 视频时间轴

| 时间 | 视频内容 |
| --- | --- |
| 00:00-00:20 | 科技股调整被解释为动量、CTA 和多空基金共同降低风险 |
| 00:20-00:40 | 风格快速反转会触发模型和风险规则，并放大波动 |
| 00:40-01:00 | 强势多头下跌、弱势空头上涨，市场中性组合出现“两边亏损” |
| 01:00-01:20 | 损失触发杠杆、保证金和风险约束，基金被迫减仓 |
| 01:20-01:40 | 建议跟踪 Long/Short 基金仓位和杠杆，并提出“第一、第二阶段” |
| 01:40-02:00 | 把集中抛售和所谓 `capitulation` 视为调整尾声候选信号 |
| 02:00-02:20 | 展示动量因子相对收益和 `GSTMTMOM Index` 单日变化图 |
| 02:20-02:40 | 展示 `US Fundamental L/S: Gross vs Net Leverage` 图 |
| 02:40-03:00 | 软件等原弱势方向出现空头回补和 short squeeze |
| 03:00-03:18 | 强调通胀、利率预期、波动率和夏季流动性仍会影响后续方向 |

视频没有内嵌字幕轨。上表来自逐帧查看画面和音频内容，而不是自动字幕摘要。

### 13.3 原始来源追踪

视频里的数据、行业顺序和结论与高盛 2026 年 7 月 24 日发布的 Prime Services
访谈高度一致。高盛公开材料确认了以下事实：

- High-beta momentum basket 从高点回撤 32%；
- TMT momentum long/short pair 从高点回撤接近 40%，为五年内最深；
- 科技方向累计卖盘为其十年记录中的最大值，接近 2024 年夏季事件；
- 全球半导体在对冲基金净股票配置中的占比从年初 10% 升到 6 月最高 24%，
  随后回落到 18%；
- 近三个月动量因子的已实现波动率达到约 45 年最高水平，衰退期除外；
- 六周前 Gross Exposure 在五年高位，Net Exposure 在四年高位；减仓后两者
  约处于过去三年的 60%-65% 分位；
- 高盛将同时削减 AI 多头与宏观对冲称为典型 `de-grossing`。

来源：[Goldman Sachs, Are Hedge Funds Still Bullish on AI Stocks?](https://www.goldmansachs.com/insights/the-markets/are-hedge-funds-still-bullish-on-ai-stocks)

这里有一个重要边界：高盛看到的是其 Prime Brokerage 客户的聚合持仓和交易，
不是整个美国市场或全部对冲基金。视频显示的 `GSTMTMOM Index` 具体成分、权重、
调仓和历史序列也没有公开方法文档，不能直接把图中数值当作可复刻因子。

高盛公开原文没有确认视频后来增加的几项判断：

- 没有把这次行情称为 `capitulation`；
- 没有采用“第一阶段 / 第二阶段”的分类；
- 没有在公开访谈中给出“八周中六周净卖出科技股”这一口径；
- 没有说极端动量反转可以确认大盘底部；
- 反而明确说当前仓位并未达到彻底出清状态。

因此，视频中的原始数据描述和作者对数据的二次推断必须分开评价。

### 13.4 逐项定义和证据

#### 13.4.1 股票横截面动量

视频所说的“买过去最强、卖过去最弱”是股票横截面动量：

```text
MomentumReturn(t) = WinnerPortfolioReturn(t) - LoserPortfolioReturn(t)
```

一个公开、可复现的标准口径是 Kenneth French 的日频 `Mom`：

```text
Mom = 1/2 × (SmallHigh + BigHigh) - 1/2 × (SmallLow + BigLow)
```

其中 High/Low 根据股票此前第 2 至第 12 个月的累计收益划分，跳过最近一个月。
[Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor_daily.html)

学术研究确认动量策略偶尔会出现剧烈、持续的亏损。这类 crash 常出现在大盘先
经历下跌、波动率较高、随后市场快速反弹的环境中。
[Daniel and Moskowitz, Momentum Crashes](https://www.nber.org/papers/w20439)

**证据判断：强。** 动量反转是真实、可计算的因子事件。但论文说的是它与市场
反弹“同时发生”，不代表它能提前确认指数底部。

#### 13.4.2 动量回撤、单日冲击和已实现波动率

不能只看某一天的动量收益，应同时计算：

```text
FactorIndex(t) = FactorIndex(t-1) × (1 + MomentumReturn(t))
Drawdown(t) = FactorIndex(t) / rolling_max(FactorIndex) - 1
ShockZ(t) = (MomentumReturn(t) - rolling_mean) / rolling_std
RealizedVol63(t) = std(last 63 daily returns) × sqrt(252)
```

视频中的“历史级别反转”实际上混合了三个不同概念：单日极端收益、累计回撤和
三个月已实现波动率。正式系统必须分开保存和展示，不能只保留一个“动量崩溃”
布尔值。

**证据判断：强。** 数据可以由本项目的 point-in-time 股票池和日线行情自行
构造，也可以用 French 日频 Mom 做外部基准。

#### 13.4.3 CTA 趋势策略

CTA 常见核心是时间序列动量，而不是股票横截面动量：

```text
过去收益为正 -> 做多该市场
过去收益为负 -> 做空该市场
```

其交易对象通常横跨股指、债券、商品和外汇期货。
[AQR, A Century of Evidence on Trend-Following Investing](https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing)

美联储指出，动量和波动率敏感的模型策略，包括 CTA 和 risk parity，可能在压力
期同时卖出同类资产并放大市场运动。
[Federal Reserve Financial Stability Report, May 2020](https://www.federalreserve.gov/publications/2020-may-financial-stability-report-asset-valuation.htm)

**证据判断：机制强，实时仓位证据有限。** 视频把 CTA、股票动量和市场中性
放在一起描述，方向上可以理解，但三者不是同一个策略，后续不能共用一个指标。

#### 13.4.4 市场中性与“两边亏损”

市场中性的准确表述是把组合对市场的 Beta 或净方向暴露控制在接近零，而不是
“无论市场涨跌都赚钱”。一个 Long/Short 组合的简化损益是：

```text
PnL(t) = LongExposure × LongBookReturn
       - ShortExposure × ShortBasketReturn
       - trading_and_financing_costs
```

当 `LongBookReturn < 0` 且 `ShortBasketReturn > 0` 时，多头亏损，空头也因被
做空股票上涨而亏损，这才是视频所说的“两边亏损”。只有采用“强者做多、弱者
做空”的市场中性动量组合才符合视频的具体例子；价值、质量或统计套利型市场
中性策略未必如此。

2007 年量化基金事件提供了直接历史证据：相似的 Long/Short 组合协调去杠杆，
导致多个量化基金异常亏损和短暂市场错位。
[Khandani and Lo, What Happened to the Quants in August 2007?](https://www.nber.org/papers/w14465)

**证据判断：强。** 但它用于诊断因子组合压力，不是单独的大盘底部指标。

#### 13.4.5 Gross Exposure、Net Exposure 与 de-grossing

对股票多空组合，可采用以下清晰口径：

```text
LongExposure  = sum(abs(long position market values)) / NAV
ShortExposure = sum(abs(short position market values)) / NAV
GrossExposure = LongExposure + ShortExposure
NetExposure   = LongExposure - ShortExposure
```

`Gross` 表示总风险规模，`Net` 表示方向偏置。比如 130% 多头和 70% 空头对应
200% Gross、60% Net。

`De-grossing` 不是简单看 Net 下降，而是多头仓位被卖出、空头仓位被买回，
使 Long 和 Short 的绝对规模同时下降。高盛对本次事件的公开定义正是减少 AI
多头，同时减少宏观空头对冲。

SEC 的 Form PF 口径同样要求 Gross 类指标先取每个头寸绝对值再求和。
[SEC Form PF FAQ](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/form-pf-faq)
这个原则可用于校验计算方向，但 Form PF 的 GRFACV/GNE 与高盛 Prime 的
Gross Leverage 仍是不同监管或业务口径，不能直接比较数值。

**证据判断：强，但高频数据私有。** Gross/Net 是有效的仓位状态指标；高盛
Prime 的周频或近实时完整历史并非公开数据。

#### 13.4.6 拥挤度和集中度

视频把 AI、半导体和高动量股票的集中持仓视为后续去杠杆的燃料。可量化候选
包括：

```text
行业净配置占比
前 N 大持仓占比 / HHI
因子收益与基金收益的共同暴露
股票两两相关性与因子相关性
多空两侧的成交拥挤和换手
证券借贷利用率与借券费率
```

研究明确指出，市场没有一个公认的单一 `crowding` 指标；可以用基金对共同
交易风格的暴露来估计拥挤程度。
[Pojarliev and Levich, Detecting Crowded Trades](https://www.nber.org/papers/w15698)

**证据判断：概念强，口径需组合。** 不应把“半导体涨得多”直接等同于拥挤，
至少还要结合集中度、估值、持仓或流量证据。

#### 13.4.7 融资、保证金和流动性螺旋

视频关于“亏损触发风险限额、保证金和被迫减仓”的机制有坚实理论基础。融资
能力下降会迫使杠杆投资者卖出；价格下跌、波动上升和流动性变差又可能提高
保证金，形成反馈循环。
[Brunnermeier and Pedersen, Market Liquidity and Funding Liquidity](https://www.nber.org/papers/w12939)

美联储也指出，杠杆基金在流动性变差时被迫向同一市场卖出，可能造成流动性快速
瓦解和价格运动放大。
[Federal Reserve Financial Stability Report, May 2020](https://www.federalreserve.gov/publications/2020-may-financial-stability-report-asset-valuation.htm)

**证据判断：机制强，触发阈值不可见。** 单靠 OHLCV 无法知道某只基金何时碰到
margin call 或 VaR limit，只能通过波动、相关性、成交冲击和仓位代理量间接识别。

#### 13.4.8 空头回补与 short squeeze

空头回补是买入股票以关闭空头。若价格上涨或借券困难迫使很多空头同时回补，
回补买盘又推动价格继续上涨，就可能形成 short squeeze。
[SEC Regulation SHO 说明](https://www.sec.gov/investor/pubs/regsho.htm)

可收集的代理变量包括：

```text
ShortInterest / Float
DaysToCover = ShortInterest / AverageDailyVolume
ShortInterest 的两期变化
借券费率、可借数量和 utilization
弱势/高空头股票的异常正收益和异常成交量
```

必须避免一个常见错误：FINRA 的日度 Short Sale Volume 是当日被标记为空头
卖出的成交量，不是尚未平仓的 Short Interest，也不能单独证明当日发生了回补。
[FINRA, Short Interest: What It Is, What It Is Not](https://www.finra.org/investors/insights/short-interest)

**证据判断：机制强，免费数据滞后。** FINRA Short Interest 每月两次；真正
高频的 borrow fee、utilization 和 buy-to-cover flow 通常需要付费数据。

#### 13.4.9 Capitulation

`Capitulation` 是交易叙事中常用的“恐慌性集中抛售”，但没有 SEC、交易所或
主流资产定价研究共同采用的唯一公式。视频也没有给出明确阈值。

如果后续纳入系统，必须先把它定义成可审计的复合状态，例如同时观察：

```text
指数收益处于历史低分位
下跌成交量和全市场成交量异常放大
上涨股票占比、创新低数量和市场宽度极端恶化
VIX 或已实现波动率急升
收盘位置、跳空和日内反转
短期反转因子随后转强
```

研究发现，短期反转收益会在 VIX 高企、流动性供给撤退时显著上升，这支持
“压力卖盘后存在反转补偿”的机制，但仍不等于精确择底。
[Nagel, Evaporating Liquidity](https://www.nber.org/papers/w17653)

**证据判断：名称弱，组成变量可研究。** 在定义、校准和样本外验证完成前，
不能在页面上把它显示成已确认的“机构投降”。

#### 13.4.10 “第一阶段 / 第二阶段”和调整尾声

视频提出“主动再平衡进入集中被动去杠杆”的两阶段说法，但没有给出公开模型、
阈值或历史样本。这不是目前可找到的学术或监管标准分类。

更重要的是，历史研究发现 2007 年量化组合的解除从 7 月开始，并持续到当年末，
说明一次极端因子反转并不能证明仓位清洗当天结束。高盛对 2026 年 7 月行情也
明确表示减仓后 Gross/Net 只是回到中等偏上分位，尚未 `washed out`。

**证据判断：弱。** 可以保留为待检验假设，不能直接实现为生产信号。

#### 13.4.11 夏季流动性和宏观条件

视频最后把夏季流动性、通胀波动和利率预期称为后续方向的条件。BIS 对 2024 年
8 月波动事件的研究确实提到，杠杆解除在 8 月常见的较薄市场中会被放大。
[BIS Bulletin 90](https://www.bis.org/publ/bisbull90.pdf)

这只能支持“薄流动性可能放大冲击”，不能推出“整个第三季度必然弱势”。
通胀、利率和宏观波动属于独立的市场状态层，也不能由 de-grossing 指标替代。

**证据判断：放大机制中等，单独择时能力弱。**

### 13.5 数据可得性

| 数据 | 频率 | 是否公开 | 可以回答什么 | 主要限制 |
| --- | --- | --- | --- | --- |
| 本项目 PIT 股票池 + OHLCV | 日频 | 已有 | 自建动量收益、回撤、波动、宽度、相关性 | 不能直接看到机构仓位 |
| Kenneth French Mom | 日频 | 免费 | 标准美股动量外部基准 | 更新有延迟，不是高盛 TMT 篮子 |
| CFTC TFF / COT | 周频 | 免费 | 股指期货中 Leveraged Funds 的多空仓位 | 只覆盖期货，不能等同 CTA 全仓 |
| FINRA Short Interest | 每月两次 | 免费 | 个股空头存量、Days to Cover | 低频且发布滞后 |
| FINRA Short Sale Volume | 日频 | 免费 | 部分场所的空头卖出成交 | 不是空头存量，也不是回补量 |
| OFR Hedge Fund Monitor | 季频 | 免费 API | 行业聚合 GNE、杠杆和风险 | Form PF 滞后，不能做日内/日频转折 |
| Goldman Prime Services | 近实时/周频 | 公开内容仅快照 | PB 客户 Gross/Net、流量和行业配置 | 完整历史和方法属于专有数据 |
| 证券借贷数据 | 日内/日频 | 多为付费 | borrow fee、utilization、recall | FMP OHLCV 无法替代 |

公开入口：

- [CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- [OFR Hedge Fund Monitor API](https://www.financialresearch.gov/hedge-fund-monitor/api/)
- [OFR Form PF 数据说明](https://www.financialresearch.gov/hedge-fund-monitor/datasets/fpf/)
- [FINRA Short Interest](https://www.finra.org/investors/insights/short-interest)
- [Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library.html)

### 13.6 当前候选指标清单

#### 可以由本项目可靠自建

- 股票横截面动量 Long/Short 日收益；
- 动量因子累计指数和峰值回撤；
- 动量因子单日收益历史分位与 z-score；
- 21/63 日动量已实现波动率及历史分位；
- Winner、Loser 两条腿的单独损益和“两边亏损”状态；
- 行业中性与非行业中性动量的差异；
- 股票相关性、横截面离散度和行业集中度；
- 市场宽度、异常成交和短期反转；
- 以公开 Short Interest 构造的低频 short-crowding 特征。

#### 可以接入公开外部数据，但频率较低

- CFTC Leveraged Funds 股指期货净仓位和周变化；
- FINRA Short Interest、Days to Cover 和半月变化；
- OFR / Form PF 行业总杠杆和 Gross Notional Exposure。

#### 暂时不能原样复刻

- 高盛 `GSTMTMOM Index`；
- 高盛 Prime 客户的近实时 Gross/Net Exposure；
- “科技股过去八周有六周净卖出”一类 PB 客户流量；
- 全市场实时 buy-to-cover；
- 实时证券借贷 utilization、recall 和完整 borrow fee。

### 13.7 对视频可靠性的初步评级

| 视频命题 | 初步评级 | 原因 |
| --- | --- | --- |
| 拥挤 Long/Short 组合会因风格反转而两边亏损 | 高 | 公式清楚，历史研究支持 |
| 融资和风险约束会形成被迫去杠杆 | 高 | 论文和监管研究均支持 |
| 动量 crash 常伴随市场快速反弹 | 中高 | 有跨市场研究，但主要是同时关系 |
| Gross/Net 下降能识别 de-grossing | 高 | 定义明确，但完整高频数据私有 |
| 空头回补会推高原弱势股票 | 高 | 市场机制明确，实时识别较难 |
| 极端动量反转说明调整已到尾声 | 低至中 | 可能共现，不能单独确认底部 |
| 大规模 capitulation 已经发生 | 低 | 视频未定义阈值，原始高盛材料也未如此表述 |
| 已从“第一阶段”进入“第二阶段” | 低 | 非标准分类，无公开验证 |
| 夏季流动性弱意味着 Q3 继续震荡 | 低至中 | 可放大冲击，但不是稳定方向信号 |

### 13.8 与主筛选流程的衔接

下一阶段不能问“哪个指标听起来像底部”，而应先规定待预测事件，例如：

```text
Bottom_20d(t):
未来 20 个交易日最大上涨 >= X%
且未来 5 个交易日最大下跌 <= Y%

Top_20d(t):
未来 20 个交易日最大下跌 <= -X%
且未来 5 个交易日最大上涨 <= Y%
```

然后只用 `t` 日当时能够获得的数据，分别检验每个候选指标的：

- 命中率、漏报率和误报率；
- 提前或滞后天数；
- 在牛市、熊市、高波动和低波动状态下的稳定性；
- 加入交易成本后的实际价值；
- walk-forward 和样本外表现；
- 数据发布滞后是否造成 point-in-time 污染。

这样才能区分“对某次行情讲得通的解释”和“能够长期进入系统的量化信号”。
