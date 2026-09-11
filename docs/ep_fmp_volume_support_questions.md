# FMP 扩展时段量能技术核验

这是待发送的技术问题草稿，**尚未代用户提交**。不是重新讨论会员授权，也不是建议升级套餐。
本项目需要知道字段统计范围，而不是仅确认接口有权访问。以下问题与模型无关。

## 可发给 FMP 支持的正文

Subject: Stable aftermarket volume semantics and intraday aggregation discrepancies

We use an existing paid subscription for private EP research and alerts. Please help us
confirm the data contract for the stable endpoints below. All requests succeeded without
authentication errors. No API key is included in these examples.

### 1. What exactly does aftermarket quote volume count?

Endpoints: `/stable/batch-aftermarket-quote` and `/stable/batch-aftermarket-trade`.
Observed at 2026-09-11 07:43 UTC (03:43 America/New_York), before the 04:00 premarket window:

| Symbol | Aftermarket quote volume | Ordinary quote volume | 2026-09-10 EOD volume |
| --- | ---: | ---: | ---: |
| AAPL | 70,011,913 | 69,820,744 | 69,820,744 |
| GTLB | 4,432,332 | 4,378,452 | 4,378,452 |
| NYAX | 13,640 | 13,639 | 13,639 |

The aftermarket timestamps still belonged to the prior postmarket session. These numbers
look consistent with a whole-day accumulator, but we have not treated that as confirmed.

A second snapshot at 2026-09-11 08:01 UTC (04:01 ET) showed:

- AAPL: quote `volume=49230.30225`, `timestamp=1789113679000`; last trade `tradeSize=118`,
  `timestamp=1789113676000`. What accounts for fractional volume in this field?
- GTLB: quote `volume=0`, `timestamp=1789113677000`; last trade `tradeSize=1`,
  `timestamp=1789113663000`. Is this expected because of odd-lot/venue filtering or asynchronous field updates?
- NYAX: quote `volume=0` with a current-day timestamp, but the last-trade timestamp remained on the prior day.
- Current-day 1-minute requests still returned empty arrays for all three symbols. Since this is shortly
  after 04:00, we have not assumed whether this is publication delay or unsupported extended-hours bars.
- The prior-day GTLB EOD volume changed from 4,378,452 to 4,432,377, and NYAX from 13,639 to 13,640
  between the two snapshots; ordinary quotes retained the older totals. When do EOD bars finalize,
  and how are late trades, corrections and adjustments applied?

- Is `volume` cumulative for the calendar day, regular session, postmarket only, or another window?
- At what timezone/time does it reset? What happens at 04:00, 09:30, 16:00 and 20:00 ET,
  on holidays, and on early-close days?
- Does `timestamp` describe the bid/ask update, last trade, or the entire record? Can `volume`
  retain an older session's value while the bid/ask timestamp is current?
- Is this consolidated U.S. volume or a venue/subset feed? Are odd lots included?
- Is `tradeSize` the size of one last trade, in shares or lots? What does `null` mean?
  We do not sum polled last-trade sizes as a substitute for the complete tape.

### 2. How can we obtain reliable extended-hours historical bars?

For `historical-chart/1min` and `historical-chart/5min`, with `from` and `to` both set to
the same event date, our GTLB (2026-09-02), VEEV (2026-08-27), and NYAX (2026-08-25)
responses contained no labels in the 04:00-09:29 ET window. Labels were timezone-naive.

- Are premarket/postmarket bars supported by these stable endpoints? If so, what documented
  endpoint/parameter/entitlement is required, and how many historical days are available?
- What timezone do bar labels use, and do they mark the start or end of the interval?
- Are absent minutes no-trade intervals, delayed data, provider filtering, or unavailable data?
- Is volume raw or split-adjusted? Why can share volume be fractional?
- Which session/trade conditions and venues are included in 1-minute, 5-minute, and EOD volume?

### 3. Please explain reproducible cross-interval differences

| Symbol / session | Sum of returned 1-minute volume | Sum of returned 5-minute volume | EOD volume |
| --- | ---: | ---: | ---: |
| GTLB / 2026-09-02 | 20,313,072.38812 | 4,028,240 | 28,993,600 |
| VEEV / 2026-08-27 | 2,711,258.43009 | 3,473,608 | 6,410,127 |
| NYAX / 2026-08-25 | 18,092.30883 | 33,557.82791 | 68,311 |

This is not just an EOD vs intraday total comparison: when we aggregated only complete
five-minute windows from the 1-minute bars, the volumes matched the corresponding native
5-minute bars in 10/77 windows for GTLB, 38/78 for VEEV, and 0/5 for NYAX (tolerance 1e-6).
We did not assume which feed is correct or scale either series to the EOD total.

Which interval's volume is suitable for same-time relative-volume calculations, and can
you provide a sample response with the intended contract? Is historical true dollar turnover
or bar VWAP available for extended-hours data? Multiplying a bar's closing price by volume
is not exact traded notional, so we do not label that approximation as actual turnover.

## 我们如何验收回复

- 保存支持回复日期、涉及的精确 endpoint、字段、时区、复权、交易所覆盖和历史范围。
- 对盘前有实际更新的股票交叉取样，检查原始时间戳和成交量，而不是仅看接收时间。
- 使用同口径、同一时刻的历史扩展时段量，才允许计算盘前 RVOL。
- 不自动以“高级会员”“返回 200”“字段叫 volume”代替技术口径核验。
- 若 FMP 不提供所需数据，再比较替代供应商；本轮不购买、不换 API、不开放量能评级。

官方文档目前只给出功能描述，没有在已查阅页面中明确以上统计口径：
[Batch Aftermarket Quote](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-quote)、
[Batch Aftermarket Trade](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-trade)、
[1 Min Interval](https://intelligence.financialmodelingprep.com/developer/docs/stable/intraday-1-min)。
