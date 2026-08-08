# DuckDB 与 Parquet 行情基础设施

更新日期：2026-08-08

本文专门解释日线行情 writer、质量门禁和 reader。业务 SQLite 及完整数据流见
[`unified_data_storage.md`](unified_data_storage.md)。

## 1. 责任边界

```text
FMP
  -> MarketDataWriter
  -> raw immutable Parquet
  -> merge overlap window
  -> quality gates
  -> curated immutable Parquet + frozen membership
  -> DuckDB published pointer
  -> MarketDataReader
```

硬约束：

1. `scripts/run_data_pipeline.py update` 是 SP500/MAG7 的正式日线写入入口。
2. `scripts/refresh_us_active.py` 和缺数 worker 也必须复用同一个 `MarketDataWriter`。
3. `scripts/run_mvp.py`、回测、排行、group analytics 和模拟盘只读 published version。
4. 候选版本未通过质量门禁时可以留作审计，但不得推进正式指针。
5. Reader 首次读取每个文件时核对 catalog 中的 SHA-256。
6. 缺数据时报错或排队，不读取旧目录，也不直接向 FMP 补洞。

## 2. 文件布局

```text
data/catalog/
  quant.duckdb
  quant.duckdb.writer.lock

data/lake/raw/fmp/eod/ingestion_id=<run_id>/
  bars.parquet
  fetch_failures.json

data/lake/curated/equity_daily/universe=<UNIVERSE>/version=<version_id>/
  bars.parquet
  universe.parquet
  membership.parquet       # 动态 PIT 池才有
  manifest.json
```

`bars.parquet` 是长表：

| 字段 | 含义 |
|---|---|
| `date` | XNYS 交易日 |
| `ticker` | 标准化证券代码 |
| `open/high/low/close` | 未复权 OHLC |
| `adj_close` | 复权收盘价 |
| `volume` | 成交量 |

Reader 可把长表 pivot 为 `date × ticker` 宽表，供既有因子接口使用。宽表只是运行时 DataFrame，
不再写到旧 `data/processed`。

## 3. DuckDB 表

| 表 | 作用 |
|---|---|
| `ingestion_runs` | 每次摄取的股票池、目标日、开始/结束时间、状态和错误 |
| `dataset_versions` | 候选版本的路径、日期范围、行数、票数和 checksum |
| `quality_checks` | 每个版本每项门禁的 observed、threshold、passed 和消息 |
| `published_versions` | 每个 universe 当前唯一正式 version ID |

`REJECTED` 版本和失败 run 默认保留，便于解释“某天为什么没有发布”。它们不是消费者可读版本。

## 4. 增量摄取

首次发布读取配置研究起点。后续发布执行：

```text
上一正式版本
  + 最近 21 个日历日重新请求的数据
  -> 按 date,ticker 去重，新请求优先
  -> 候选版本
  -> 质量门禁
  -> 原子发布
```

重叠窗口用于吸收拆股、分红复权和供应商修订。新版本不会原地覆盖旧版本，因此旧回测仍能按
version ID 复现。相对起点 `5Y` 不会让系统每天自动删掉最早一天；新版本会继承已有历史。

动态股票池会下载研究区间内 PIT 历史成员并集，而不是只下载今天的成分股。

## 5. 质量门禁

`src/data/foundation.py::validate_daily_bars()` 当前检查：

| 检查 | 要求 |
|---|---|
| schema | 必须有 date、ticker、完整 OHLC、adj_close、volume |
| 主键 | `date,ticker` 不重复 |
| 日期 | 可解析且不晚于 target session |
| 数值 | 必需值非空且有限；价格大于 0；volume 不小于 0 |
| OHLC | high/low 必须包住 open 和 close |
| 最新覆盖 | 当前成分目标日覆盖至少 98% |
| 新鲜覆盖 | 本次供应商请求目标日覆盖至少 98% |
| PIT 日覆盖 | 每个历史 session 至少覆盖 95% 当时成分 |
| PIT 历史票 | 所有历史 active ticker 都必须有行情 |

阈值在 `configs/default.yaml -> data.foundation`。任一关键门禁失败，writer 返回非零且旧正式指针
不变，页面继续读取上一版完整数据。

## 6. PIT 冻结

SP500 发布前必须先有有效的 `data/pit_universes/SP500.parquet`。Writer 会把本次使用的 PIT
快照复制进版本目录，并记录 `membership_sha256`。因此后来修订 PIT 文件不会偷偷改变已经完成
的历史回测。

发布条件包括：最后快照等于目标 session、最后快照与当前 constituents 一致、历史事件通过
校验、历史成员行情满足覆盖门槛。详细格式见
[`point_in_time_universe.md`](point_in_time_universe.md)。

## 7. Reader 契约

消费者通常先调用：

```python
reader = MarketDataReader()
version = reader.require_latest("SP500")
bars = reader.load_bars(version=version)
membership = reader.load_membership(version=version)
```

一次业务运行应把 `version` 继续传下去，不要在流程中反复查询“最新”。这样开始与结束不会因为
刚好发生一次新发布而使用两套输入。

Reader 会拒绝：版本不存在、文件缺失、checksum 不符、universe 不符和无 PIT 的动态池。

## 8. 常用命令

```bash
# 只构建/发布主 SP500 PIT
python scripts/run_data_pipeline.py pit

# 增量发布配置中的 SP500 和 MAG7
python scripts/run_data_pipeline.py update

# 只更新一个池
python scripts/run_data_pipeline.py update --universe MAG7

# 查看正式版本
python scripts/run_data_pipeline.py status
python scripts/run_data_pipeline.py status --json
```

`--force` 会重发目标 session 并刷新 overlap window，不代表跳过质量门禁。

## 9. 当前本地状态

2026-08-08 本地检查：

| universe | target session | rows | tickers | coverage | version |
|---|---|---:|---:|---:|---|
| SP500 | 2026-07-31 | 711,247 | 591 历史并集 | 100% 当前成分 | `511ee86d...` |
| MAG7 | 2026-07-31 | 8,785 | 7 | 100% | `f4eeb46c...` |

本地旧 `data/raw/ohlcv` 和 `data/processed` 已归档移出，代码也不再依赖它们。SG 是否处于同一
commit 和同一清理状态，必须通过服务器部署验收单独确认。

## 10. 保留与清理

- 正式 version：必须保留，只能在完成引用分析和备份后归档。
- rejected candidate：可按保留策略清理，但会失去失败审计细节。
- raw ingestion：可在保留 curated 版本与审计要求允许时归档。
- `data/cache`：可重建，可直接清空。
- `data/raw/market_regime`、`data/raw/intraday`：属于独立模块，不是旧主行情残留。
