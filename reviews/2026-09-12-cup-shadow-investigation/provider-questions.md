# FMP intraday data investigation: 2026-09-11

Draft for provider review; not submitted. All bucket times below are New York time.
The requery was performed on 2026-09-12 at 08:47:36 UTC. API credentials are omitted.

## Requests

- `/stable/historical-chart/1min`, symbols IBTA, UAN, WBI, from/to 2026-09-11.
- `/stable/historical-chart/5min`, the same symbols and date.
- Normalized responses and SHA-256 values are in `provider-requery.json`.
- Live gap evidence and query-time neighboring bars are in `requery-summary.json`.

## Reproducible differences

| Symbol / bucket | 1min response within bucket | Native 5min response |
| --- | --- | --- |
| WBI 09:35-09:40 | One 09:35 row, OHLC 32.77, volume 120 | O 32.77, H 33.12, L 32.77, C 33.12, volume 453 |
| WBI 09:40-09:45 | No rows | O/H 33.09, L/C 33.08, volume 497 |
| IBTA 15:05-15:10 | No rows | No rows |
| UAN 09:50-09:55 | No rows | No rows |
| UAN 10:15, 10:20, 10:30, 10:35, 10:40, 10:55, 11:00, 11:10 | No rows in each five-minute bucket | No rows in each five-minute bucket |

Regular-session volume totals also differ: IBTA 108683.49971 vs 116537.78601;
UAN 13041 vs 15377.94583; WBI 401626 vs 425555 (1min sum vs native 5min sum).
These totals are evidence of inconsistent responses, not corrected trading volumes.

## Questions requiring source evidence

1. Do both endpoints include the same exchanges, trade conditions, odd lots,
   corrections and volume units? What are the exact timestamp/bucket conventions?
2. Why does native WBI 09:40 contain positive volume while every 1min constituent
   is absent? Can the missing source records and their publication/correction times
   be supplied?
3. For the ten IBTA/UAN gaps, was there no qualifying trade, or is source coverage
   incomplete? Please provide trade-level evidence or a feed-completeness watermark.
4. Does batch-quote timestamp represent last trade time or a completeness watermark?
   An unchanged cumulative-volume quote with an old timestamp cannot prove no trades.
5. What live finality/late-correction guarantees allow a completed bucket to be
   evaluated without incorporating information only available the following day?

Historical shadow classifications remain unchanged. Query-time rows cannot establish
their availability during the original live cycle or convert a failed day into PASS.
The two endpoints are documented separately by FMP:
[1min](https://site.financialmodelingprep.com/developer/docs/stable/intraday-1-min)
and [5min](https://site.financialmodelingprep.com/developer/docs/stable/intraday-5-min).
Their marketing descriptions alone do not establish identical aggregation semantics.
