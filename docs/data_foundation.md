# DuckDB 与 Parquet 行情基础设施

更新日期：2026-08-12

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

1. `scripts/run_data_pipeline.py update` 是 SP500/NASDAQ100/MAG7 的正式日线写入入口。
2. `scripts/refresh_us_active.py` 和缺数 worker 也必须复用同一个 `MarketDataWriter`。
3. `scripts/run_mvp.py`、回测、排行、group analytics 和模拟盘只读 published version。
4. 候选版本未通过质量门禁时可以留作审计，但不得推进正式指针。
5. Reader 首次读取每个文件时核对 catalog 中的 SHA-256。
6. 缺数据时报错或排队，不读取旧目录，也不直接向 FMP 补洞。
7. 全美宽基使用专用单写者链，但继续复用同一个 DuckDB catalog、DatasetVersion 和 Reader 完整性
   合同；研究和网页仍然禁止直接调用 FMP。

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
  membership_events.parquet # 动态 PIT 池的成分变更事件账本
  manifest.json

data/lake/security_master/generation=<generation_id>/
  master.parquet
  symbols.parquet
  classifications.parquet
  identity_keys.parquet
  research_history_policy.parquet # 前瞻参与/历史排除的公开审计台账
  manifest.json

data/lake/curated/US_EQUITY_COVERAGE/version=<version_id>/
  bars_index.json
  bars/year=<YYYY>/month=<MM>/part-*.parquet
  security_universe.parquet
  manifest.json

data/lake/universes/US_LIQUID_5M/version=<universe_version_id>/
  membership.parquet
  eligibility_audit.parquet
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
| `security_master_generations` / `published_security_master` | 稳定证券身份快照及当前正式 generation |
| `derived_universe_versions` / `published_universe_versions` | coverage parent 派生出的 PIT 比较池版本 |

`REJECTED` 版本和失败 run 默认保留，便于解释“某天为什么没有发布”。它们不是消费者可读版本。

## 4. 增量摄取

正式研究池首次发布固定从 `universe.point_in_time.main_factor_start` 读取历史；当前值为
`2020-01-01`。这段历史包含因子 warm-up，正式五年研究窗口从 `2021-08-09` 开始。后续发布执行：

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
快照和规范化成分事件账本都复制进版本目录，并分别记录 SHA-256。因此后来修订 PIT 文件或原始
事件理由不会偷偷改变已经完成的历史回测。Reader 会同时验证路径不能越出版本目录和文件哈希。

发布条件包括：最后快照等于目标 session、最后快照与当前 constituents 一致、历史事件通过
校验、历史成员行情满足覆盖门槛。详细格式见
[`point_in_time_universe.md`](point_in_time_universe.md)。

## 7. Reader 契约

业务消费者通常调用 `src/data/access.py`，由统一入口同时执行版本绑定、覆盖门禁和契约生成：

```python
bundle = load_published_daily_data(
    requested_universe="SP500",
    lookback_calendar_days=400,
)
version = bundle.version
contract = bundle.contract
```

因子消费者使用 `load_published_bundle()`；动量突破通过
`load_breakout_daily_dataset()` 得到 universe、ticker frames 和同一份 `DataContract`。一次业务
运行应把 `version` 或 contract 继续传下去，不要在流程中反复查询“最新”。

Reader 会拒绝：版本不存在、文件缺失、checksum 不符、universe 不符和无 PIT 的动态池。
`load_bars()` 会把 ticker、start 和 end 条件下推给 DuckDB，避免先把完整 Parquet 载入 Pandas。

## 8. 常用命令

```bash
# 构建/发布注册表中全部 PIT 研究池
python scripts/run_data_pipeline.py pit

# 只检查 NASDAQ100 候选，不替换正式 PIT
python scripts/run_data_pipeline.py pit \
  --universe NASDAQ100 --candidate-only --json

# 增量发布配置中的 SP500 和 MAG7
python scripts/run_data_pipeline.py update

# 只更新一个池
python scripts/run_data_pipeline.py update --universe MAG7

# 查看正式版本
python scripts/run_data_pipeline.py status
python scripts/run_data_pipeline.py status --json
```

`--force` 会重发目标 session 并刷新 overlap window，不代表跳过质量门禁。

全美宽基首次和每日命令：

```bash
# 首次回填，默认命令都可先不加 --publish 做候选
python scripts/build_security_master.py --publish --json
python scripts/backfill_us_equity_coverage.py --publish --json
python scripts/build_us_liquid_pit.py --full-rebuild --publish --json
python scripts/run_broad_factor_data.py --publish --json

# 日常冻结一个 target session，串行刷新前三层
python scripts/run_broad_daily_pipeline.py --json

# 完整验证因子数据并记录影子日
python scripts/check_broad_shadow_observation.py --record-current --json
```

正式 coverage 读模型按月分区。首次下载仍可按证券批次/年份 checkpoint，发布时才压实为月份；普通
日重建 overlap 涉及月份并硬链接其余月份，避免一次修订复制或重算全部历史。

`US_LIQUID_5M` 发布还会把研究区间内每个 XNYS session 映射到最近一次完整 PIT membership，使用
DuckDB 检查当日成员是否都有 `(date, security_id)` 行情；任何一天低于 95% 都拒绝发布。网页只读
元数据时认证 manifest 与月分片 index，实际查询分片逐文件验哈希；每日 shadow 仍完整校验所有子分片。

## 9. 当前本地状态

2026-08-08 本地检查：

| universe | target session | rows | tickers | coverage | version |
|---|---|---:|---:|---:|---|
| SP500 | 2026-07-31 | 711,247 | 591 历史并集 | 100% 当前成分 | `511ee86d...` |
| MAG7 | 2026-07-31 | 8,785 | 7 | 100% | `f4eeb46c...` |

本机是否有旧动量用 `US_LIQUID_5M` DatasetVersion，不代表新的 derived PIT 宽基已经发布。新的
全美宽基正式链必须同时存在 `US_EQUITY_COVERAGE`、Security Master、derived universe 和
factor-data publication；截至 2026-08-12 尚未执行正式全量回填。系统不会用旧 180D 版本冒充。

本地旧 `data/raw/ohlcv` 和 `data/processed` 已归档移出，代码也不再依赖它们。SG 是否处于同一
commit 和同一清理状态，必须通过服务器部署验收单独确认。

## 10. 保留与清理

- 正式 version：必须保留，只能在完成引用分析和备份后归档。
- rejected candidate：可按保留策略清理，但会失去失败审计细节。
- raw ingestion：可在保留 curated 版本与审计要求允许时归档。
- `data/cache`：可重建，可直接清空。
- `data/raw/market_regime`、`data/raw/intraday`：属于独立模块，不是旧主行情残留。
