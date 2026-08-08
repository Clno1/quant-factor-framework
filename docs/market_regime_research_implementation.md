# 大盘顶底研究系统：第一阶段实现说明

> 实现日期：2026-08-01
> 当前状态：市场核心数据、标签和 P0 特征已经可运行；完整 PIT 横截面研究仍被数据质量门禁阻止；尚未接入页面或交易。

第二轮独立代码复审、修复项和剩余风险见：

[大盘顶底研究系统代码复审报告](market_regime_research_code_audit.md)

## 一、这一阶段交付了什么

第一阶段没有直接输出“顶部”或“底部”图标，而是建立了后续研究必须依赖的
可重复计算底座：

```text
长期且已完成的市场日线
    -> 明确 raw/total-return 价格语义
    -> 可审计来源清单与 SHA256
    -> 无前视的候选日条件
    -> 仅用于结果变量的 first-touch 标签
    -> P0 市场状态特征
    -> 不可变研究产物
```

对应代码：

| 文件 | 职责 |
| --- | --- |
| `src/market_regime_research/settings.py` | 参数和边界校验 |
| `src/market_regime_research/sources.py` | FMP、Cboe、FRED 数据契约 |
| `src/market_regime_research/pit.py` | 从 FMP 增删事件倒推完整 PIT 快照 |
| `src/market_regime_research/labels.py` | 5/20/60 日 first-touch 顶底标签 |
| `src/market_regime_research/features.py` | P0 价格、波动率、流动性、广度、动量压力特征 |
| `src/market_regime_research/artifacts.py` | 原子发布、哈希和不可变 run |
| `src/market_regime_research/pipeline.py` | 数据门禁和计算编排 |
| `scripts/run_market_regime_research.py` | prepare / pit / run / all 命令 |

这里的 `market_regime_research` 和
`src.breakouts.scanner.load_market_regime()` 不是同一个系统。后者只是动量告警使用的
MA10/MA20 市场过滤器。

## 二、数据源和价格语义

### 2.1 FMP 长期行情

当前使用 `^GSPC`、`^NDX`、SPY、QQQ、IWM、HYG 和 LQD。

FMP 单次 EOD 请求实测最多返回 5,000 行。直接请求 1990 年至今不会报错，但序列会
从约 2006 年才开始。`get_historical_ohlcv_complete()` 已改成每十年分段下载，并在
任何分段为空或达到 5,000 行时拒绝结果。

ETF 文件同时保存：

```text
open/high/low/close
    FMP full，拆股调整后的市场 OHLC

adj_open/adj_high/adj_low/adj_close
    FMP dividend-adjusted，总回报研究口径
```

指数没有分红调整，因此两组 OHLC 相同。

### 2.2 未完成日线防护

FMP 可能在纽约现金市场尚未收盘时就返回当天的“日线”。实测中，纽约仍处于
2026-07-31 盘中时，FMP 已返回该日数据，而 Cboe 只到 2026-07-30。

现在有两层保护：

1. `prepare` 的结束日期取“配置日期”和“最近已正式收盘的 XNYS session”中较早者；
2. 读取缓存时再次拒绝晚于最近已收盘 session 的任何行。

因此盘中临时 OHLC 不会进入标签和特征。

### 2.3 Cboe 波动率

直接使用 Cboe 官方 CSV：

- VIX：1990 年起；
- VIX9D：2011 年起；
- VIX3M：2009 年起。

每条数据的保守可用时间记录为观察日纽约时间 17:00。滚动分位按实际有效观察数
计算，某条期限指数偶发缺一天时不会使随后 252 天分位全部变成空值。

### 2.4 信用数据

代码已支持 FRED `BAMLH0A0HYM2`，并把可用时间保守设成下一个工作日 18:00。
但 2026-08-01 验收时，FRED graph CSV 在当前网络连续返回 504，静态下载地址又返回
错误重定向。因此：

- 正式 `prepare` 默认仍要求 HY OAS；
- 可以用 `--skip-credit` 运行市场核心研究；
- HYG/LQD 相对价格代理已经可用；
- HY OAS 没有成功下载时不会伪造、回填或改用不明来源。

## 三、PIT 股票池当前结论

FMP `historical-sp500-constituent` 返回的是增删事件，不是完整快照。代码从当前
503 个成分股开始，按日期倒序执行：

```text
事件日变更后快照
    -> 撤销当天 additions
    -> 恢复当天 removals
    -> 得到变更前状态
```

关键字段规则是：

```text
addedSecurity 非空时，symbol 明确表示新增股票。
addedSecurity 为空但 symbol != removedTicker 时，
可以保留为“缺新增公司名的明确替换”，同时记录 WARNING。
```

在 removal-only 行中，FMP 会把被移除代码重复放在 `symbol`。如果无条件把
`symbol` 当新增，会系统性污染全部历史快照。WARNING 负责保留字段缺失的来源事实，
真正无法分类或无法和后续状态闭合的事件仍然是 ERROR。

2026-08-01 的真实诊断结果：

| 项目 | 结果 |
| --- | ---: |
| 当前成分 | 503 |
| 1990 年以来事件行 | 868 |
| 完整候选快照 | 696 |
| 快照最少/最多成员 | 490 / 508 |
| 可确定但缺公司名的 WARNING 事件 | 35 |
| 无法闭合的不一致组 | 23 |
| 质量状态 | FAIL |
| 是否发布到生产 PIT 路径 | 否 |

23 组不一致可以进一步拆成：

| 类型 | 数量 | 含义 |
| --- | ---: | --- |
| 无新增/移除代码的未分类事件 | 2 | 只有 “Annual Re-ranking” 等文字，无法证明是否改变成分 |
| 新增代码不存在于后续状态 | 9 | 可能缺少后续移除、临时代码或 ticker 转换 |
| 已移除代码仍存在于后续状态 | 12 | 多数涉及代码复用、公司继承或缺少后续加入 |

诊断中的 SATS、SOLSV、AGN、UA、TT、T、S、WB、C、ITT 和 X 等事件需要
稳定证券实体 ID 或第二个可审计来源才能消歧。仅凭 ticker 和公司名自动猜补会把
两个不同历史证券合并成一个，因此当前实现选择失败关闭，而不是人工修到约 500
只后放行。

这说明“每个快照大约 500 只”不是充分校验。候选文件和诊断会写到：

```text
data/raw/market_regime/pit/SP500_candidate.parquet
data/raw/market_regime/pit/SP500_events.parquet
data/raw/market_regime/pit/SP500_diagnostics.json
```

只有 `quality_status=PASS` 才允许发布到：

```text
data/pit_universes/SP500.parquet
```

## 四、顶底标签

### 4.1 波动率屏障

日波动率不年化：

```text
daily_vol_20(t) = std(return[t-19:t])
barrier_h(t) = max(
    daily_vol_20(t) * sqrt(h) * barrier_vol_multiplier,
    minimum_barrier_pct
)
```

默认 `h = 5, 20, 60`、`barrier_vol_multiplier = 1.0`、
`minimum_barrier_pct = 2%`。

### 4.2 顶部候选

```text
drawdown_252 = close / rolling_max_252(close) - 1
top_eligible = drawdown_252 >= -3%
```

未来 `h` 日内，先触及负屏障且尚未先触及正屏障时，`top_label_h = 1`。
先向上延续或期限内没有发生足够下跌时为 0。

### 4.3 底部候选

底部必须同时满足固定回撤和历史极端性：

```text
fixed_threshold = -10%
adaptive_threshold =
    trailing_1260d_quantile_20%(drawdown_252).shift(1)

bottom_threshold = min(fixed_threshold, adaptive_threshold)
bottom_eligible = drawdown_252 <= bottom_threshold
```

`shift(1)` 保证今天是否极端的阈值不包含今天自身。未来 `h` 日内，先触及正屏障且
尚未先触及负屏障时，`bottom_label_h = 1`。

### 4.4 日线无法判断盘中顺序

如果同一根未来日线的 high 和 low 同时穿越正负屏障，OHLC 无法知道先后顺序。
系统输出：

```text
label = null
first_touch = ambiguous
```

不会假设对模型更有利的一边先发生。

## 五、已经实现的 P0 特征

市场核心模式已经有：

- SPX、NDX、SPY、QQQ、IWM、HYG、LQD 的 1/5/20/60 日收益；
- 252 日回撤；
- MA20/50/200 距离；
- RV20/RV60；
- ATR14、隔夜跳空、收盘位置；
- ETF Amihud、成交量冲击、20 日 down-volume share；
- VIX、VIX9D、VIX3M 的水平、变化和滚动分位；
- VIX/VIX3M、VIX9D/VIX3M；
- IWM/SPY、QQQ/SPY、HYG/LQD 相对状态。

指数 vendor volume 没有可交易“股数”单位，因此 SPX/NDX 不计算 Amihud；仍保留
成交量冲击和 down-volume 作为活动度代理。

完整 PIT 模式另外实现了：

- Advance%、Decline%、BreadthNet；
- % above MA20/50/200；
- 252 日新高/新低比例；
- PIT 等权收益、EW-CW spread；
- 横截面 STD/MAD；
- 60 日平均成分相关性；
- 12-1 动量 winner/loser 两腿；
- 动量多空收益、回撤、20 日波动率；
- “winner 下跌且 loser 上涨”的 two-sided loss。

动量组合用 T-1 排名解释 T 日收益，禁止用当天收盘形成组合后再获得当天收益。

## 六、运行方法

### 6.1 下载并验证来源

```bash
cd /Users/huozhihong/Documents/Quant
python scripts/run_market_regime_research.py prepare
```

FRED 暂时不可用时：

```bash
python scripts/run_market_regime_research.py prepare --skip-credit
```

### 6.2 生成 PIT 候选并执行发布门禁

```bash
python scripts/run_market_regime_research.py pit
```

当前 FMP 事件会返回退出码 2，并写诊断，但不会发布生产 PIT。

### 6.3 运行市场核心研究

```bash
python scripts/run_market_regime_research.py run --core-only --skip-credit
```

### 6.4 运行完整 PIT 研究

```bash
python scripts/run_market_regime_research.py run
```

只有以下条件全部满足才会成功：

1. 长期市场、Cboe、信用数据齐全；
2. `high/low` 宽表已重建；
3. PIT membership 已通过事件一致性；
4. 历史成分并集的行情全部存在。

## 七、研究产物

每个成功 run 位于：

```text
outputs/market_regime_research/runs/<RUN_ID>/
```

当前固定产物：

```text
features.parquet
labels.parquet
feature_registry.parquet
data_manifest.json
diagnostics.json
run.json
```

`data_manifest.json` 记录输入来源、输入文件 SHA256 和三个核心 Parquet 的 SHA256。
`latest.json` 只在完整 run 原子发布成功后更新。

2026-08-01 市场核心 smoke run：

```text
run_id: stage_a_audited_20260801_v6
algorithm_version: 0.1.1
日期: 1990-01-02 -> 2026-07-30
交易日: 9,211
特征: 139
标签/路径字段: 36
最新日有效特征: 139 / 139
```

## 八、测试和防错范围

新增测试覆盖：

- FMP 长历史分段和 5,000 行截断；
- raw/total-return OHLC 日历一致性；
- 未完成未来日线拒绝；
- Cboe/FRED 字段和 available_at；
- removal-only 事件不能误判为 addition；
- PIT 倒推方向和严格失败；
- first-touch 顺序和同日歧义；
- 标签条件无前视；
- P0 价格特征无前视；
- 非成员不能进入 breadth；
- 动量使用前一日排名；
- 特征注册表与矩阵一一对应；
- 不可变产物和 latest 指针。

## 九、尚未完成

以下内容故意没有提前接入生产：

1. 修复/替换 FMP PIT 事件源；
2. 下载历史成分并集和退市股票行情；
3. 正式接通 HY OAS，并补 EBP；
4. G7 参数扰动与 G8 增量模型比较；
5. G9 含 next-open、滑点和费用的经济价值；
6. 60 个交易日影子运行；
7. 最终封存集评估；
8. 研究页面 K 线、顶底标记和解释面板。

市场核心特征的第一轮 univariate event study、walk-forward、FDR 和 G1-G6
scorecard 已于 2026-08-02 完成。834 个测试中只有
`bottom_spx_return_5d__5d` 阶段性通过，生产批准仍为 0。详细口径和结果见：

[大盘顶底信号：第一轮有效性筛选报告](market_regime_effectiveness_screening.md)

PIT 修复和市场核心候选的 G7-G10 可以并行推进；页面仍应排在最终 scorecard 和
影子验证之后。
