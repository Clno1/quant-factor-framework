# 大盘顶底研究 v2：市场宽度与 COR1M

> 代码版本：`market_regime_research` 0.2.0  
> 候选注册版本：2.0.0  
> 冻结时间：2026-08-20 17:24:45 +08:00  
> 当前状态：代码和研究协议已完成；完整 PIT 数据门禁尚未通过，因此没有有效性结论。

## 一、这次新增了什么

本轮把两类互补信息接入既有 Stage A / Stage B 流程：

1. PIT 成分股中收盘价高于自身 MA20/50/60/120/200 的比例；
2. Cboe 一个月期隐含相关性指数 `COR1M`。

它们不是同一个指标。市场宽度描述当前有多少股票参与趋势；`COR1M` 使用 SPX 和
标普最大 50 只成分股的期权，描述市场对未来一个月股票共同波动程度的预期。

本轮没有创建主观加权的“顶底总分”，也没有设置固定买卖阈值。每项特征先作为独立
候选进入现有 walk-forward、block bootstrap 和 FDR 检验。

## 二、数据与时间契约

### 2.1 COR1M

官方数据：

```text
https://cdn.cboe.com/api/global/us_indices/daily_prices/COR1M_History.csv
```

当前官方文件从 2006-01-03 开始。系统将观察日数值保守标记为纽约时间 17:00 后可用，
与 VIX 系列使用相同的下载、OHLC 校验、SHA256 和 source manifest。当天收盘后的
`COR1M` 特征只能用于下一交易时点，不能回填成当天盘中已知。

官方历史文件目前有 4 天的盘中 `HIGH` 超出相关性指数的理论区间（最大为 526.02），
但这些日期的 `CLOSE` 正常。研究只使用 `CLOSE`：系统严格拒绝超出 `[-100, 100]` 的
收盘值，原样保留非信号 OHLC，并把异常天数写入 source manifest，避免静默清洗或误用。

Cboe 指出，一月期隐含相关性存在财报季带来的季度季节性。因此本轮：

- 保存原始 level，供完整探索扫描留痕；
- 正式候选优先使用 5 日变化和 252 个有效观察滚动分位；
- 不写死绝对阈值；
- 暂不自行发明无法复现 Cboe 方法的“去季节化”序列。

### 2.2 PIT 均线宽度

对窗口 `L`：

```text
B_L(t) =
  sum_i( PIT_member_i(t) * valid_i,L(t) * 1[AdjClose_i(t) > SMA_i,L(t)] )
  / sum_i( PIT_member_i(t) * valid_i,L(t) )
```

其中 `valid_i,L(t)` 要求当天调整收盘价和完整 L 日均线都存在。股票当天是否属于股票池
只看当天 PIT membership；均线可以使用该股票加入指数之前已经公开的历史价格。

分母同时满足：

```text
有效股票数 >= min_cross_section_members
有效股票数 / 当天 PIT 成分数 >= 95%
```

达不到门槛时结果为 `null`，不能把缺失股票当作低于均线，也不能用少量幸存股票代替
完整市场。新高、新低、日度涨跌宽度和平均相关性也使用相同的覆盖率约束。

## 三、Stage A 新特征

### 3.1 COR1M

```text
cor1m_level
cor1m_change_1d
cor1m_change_5d
cor1m_change_20d
cor1m_percentile_252d
```

### 3.2 均线宽度

每个 `L in [20, 50, 60, 120, 200]` 生成：

```text
breadth_above_maL_pct
breadth_above_maL_change_5d
breadth_above_maL_change_20d
```

保留 MA50/200 是因为它们是更常见的市场研究基线。MA60/120 不替换基线：MA60 很可能
和 MA50 高度共线，MA120 则可能提供介于季度和年度趋势之间的增量信息。是否保留要由
开发样本中的相关性、样本外概率改善和增量检验决定。

## 四、冻结的 v2 假设

v1 的 28 个基础假设没有改写。v2 在
`configs/market_regime_screening_candidates_v2.yaml` 新增 9 个基础假设：

| 方向 | 特征 | 登记方向 | 研究含义 |
| --- | --- | --- | --- |
| Top | `cor1m_change_5d` | higher | 指数高位附近系统性共振快速上升 |
| Top | `breadth_above_ma60_pct` | lower | 季度趋势参与度收窄 |
| Top | `breadth_above_ma120_pct` | lower | 半年趋势参与度收窄 |
| Top | `breadth_above_ma20_change_20d` | lower | 短期宽度快速恶化 |
| Bottom | `cor1m_percentile_252d` | higher | 回撤中系统性压力处于高分位 |
| Bottom | `cor1m_change_5d` | lower | 系统性压力开始消退 |
| Bottom | `breadth_above_ma20_pct` | lower | 短期参与度极弱 |
| Bottom | `breadth_above_ma20_change_5d` | higher | 卖压后出现广泛修复 |
| Bottom | `breadth_above_ma120_pct` | lower | 回撤已经造成中期市场面损伤 |

未登记的 MA50/60/120/200 变化组合仍会进入全量探索扫描和多重检验，但只能标记为
`EXPLORATORY_ONLY`。

## 五、封存规则

Stage B 仍只允许读取 2022-01-01 之前的开发数据，并在边界前保留 60 个交易日 embargo。

但 v2 假设是在 2026-08-20 才冻结，所以需要区分：

- `2022-01-01` 起：代码封存的历史 holdout，打开后可做一次锁定规则的回放验证；
- `2026-08-21` 起：冻结假设之后产生的数据，才是真正的 prospective shadow 样本。

不得把 2022-2026 的结果同时用于调整 v2 参数和宣称最终样本外有效。任何阈值调整都会
生成新注册版本，并重新开始 prospective shadow 计时。

## 六、运行方法与当前阻断

刷新 FMP、Cboe 和 FRED 来源：

```bash
cd /Users/huozhihong/Documents/Quant
python scripts/run_market_regime_research.py prepare
```

FRED 自 2026 年 4 月起只公开该 ICE BofA 序列最近 3 年，当前返回文件无法通过完整历史
门禁。在取得许可数据前，使用 `prepare --skip-credit` 和 `run --skip-credit`；系统仍保留
HYG/LQD 信用风险代理，但不会把三年 HY OAS 冒充长历史。

完整 PIT Stage A：

```bash
python scripts/run_market_regime_research.py run --run-id stage_a_breadth_cor1m_v2
```

使用 v2 注册表执行 Stage B：

```bash
python scripts/run_market_regime_research.py screen \
  --research-run-id stage_a_breadth_cor1m_v2 \
  --candidate-registry configs/market_regime_screening_candidates_v2.yaml \
  --screening-id stage_b_breadth_cor1m_v2
```

复现旧 v1 时显式指定原文件：

```bash
python scripts/run_market_regime_research.py screen \
  --research-run-id stage_a_audited_20260801_v6 \
  --candidate-registry configs/market_regime_screening_candidates.yaml
```

截至 2026-08-20 的复核，专属 `SP500_MARKET_REGIME` PIT 发布仍因 25 个来源事件不一致而失败，
DuckDB 也没有对应的完整历史 DatasetVersion。因此可以完成 COR1M 的 core Stage A，
但不能运行包含市场宽度的正式 v2 Stage A/B；系统应继续失败关闭，不能借用当前成分股
或主因子 SP500 股票池绕过。

## 七、主要来源

- [Cboe Implied Correlation](https://www.cboe.com/us/indices/implied/)
- [Cboe Implied Correlation Methodology](https://cdn.cboe.com/resources/indices/documents/Implied_Correlation-WhitePaper.pdf)
- [FRED BAMLH0A0HYM2 data-availability note](https://fred.stlouisfed.org/series/BAMLH0A0HYM2)
- [The Use of Index-Specific Market Breadth and Index-Over-Moving-Average Indicators](https://researchwith.montclair.edu/en/publications/the-use-of-index-specific-market-breadth-and-index-over-moving-av/)
- [Herding for Profits: Market Breadth and Global Equity Returns](https://www.sciencedirect.com/science/article/pii/S0264999319312982)
- [Expected Correlation and Future Market Returns](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3134411)
