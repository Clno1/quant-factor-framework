# SP500 PIT 与每日研究发布

更新日期：2026-08-02

## 1. 两条 PIT 研究边界

项目不再让两个时间跨度不同的研究共用一份“看似通过”的结论：

| 研究链 | 时间范围 | 状态 | 正式用途 |
|---|---|---|---|
| 主多因子 | 固定从 2020-01-01 开始 | 严格 PASS | 因子、回测、策略、模拟盘 |
| 大盘顶底研究 | 1990 年至今 | 仍有 22 组旧代码身份问题 | 只保留诊断，不能发布完整 PIT 宽度研究 |

旧的 24 组异常中，主多因子窗口实际只涉及两组：

1. FMP 在 2025-10-31 的 Solstice 入选事件中使用了 when-issued 代码 `SOLSV`，
   S&P DJI 正式公告使用 `SOLS`。构建器只修正这一条精确日期、精确公司名的事件。
2. EchoStar 于 2026-06-24 将代码从 `SATS` 改为 `ECHO`。这是证券代码变化，不是
   S&P 500 删除；构建器添加一条有日期和来源的 symbol transition。

规则保存在 `configs/sp500_pit_corrections.yaml`。每条规则必须有唯一 ID、精确日期、
修正前后代码和 primary-source URL。供应商行匹配不到、匹配多行或出现第三个未知代码时，
构建立即失败，不能使用宽泛 alias 静默修复。

来源：

- S&P DJI Solstice 公告：
  https://www.spglobal.com/spdji/en/documents/indexnews/announcements/20251027-1480741/1480741_hon-dd-5-spin.pdf
- EchoStar 代码变更公告：
  https://ir.echostar.com/news-releases/news-release-details/echostar-changing-stocker-ticker-sats-echo-marking-companys-next

1990 年研究剩余异常主要是 `T`、`C`、`S`、`AGN`、`TT` 等代码在不同年代被不同证券复用，
以及公司合并、拆分后代码被供应商回填。只靠 ticker 无法证明证券身份，因此不能通过
关闭 strict 或手写全历史 alias 来“解决”。该研究需要永久 security ID 和公司行动主数据。

## 2. 主因子 PIT 发布

正式命令：

```bash
python scripts/run_data_pipeline.py pit
```

只生成候选和诊断：

```bash
python scripts/run_data_pipeline.py pit --candidate-only --json
```

每次运行保留不可变审计目录：

```text
data/raw/pit/SP500/asof=<SESSION>/run=<RUN_ID>/
  current_constituents.parquet
  provider_changes.parquet
  normalized_events.parquet
  candidate_membership.parquet
  corrections_audit.json
  diagnostics.json
```

只有 `quality_status=PASS` 且 strict 重建再次通过时，才原子替换：

```text
data/pit_universes/SP500.parquet
data/pit_universes/SP500.metadata.json
```

metadata 记录 FMP 两个 endpoint、原始 payload hash、修正规则 hash、每条修正动作和原始
审计目录。固定起点 2020-01-01 是为了让每日相对研究窗口前移时仍保留旧 DuckDB 版本所需的
基线快照。

2026-08-02 本地真实 FMP 验证结果：

- as-of：2026-07-31；
- 完整快照：85；
- 历史成员并集：618；
- 每个快照成员数：503 至 506；
- inconsistency：0；
- 正式状态：PUBLISHED。

## 3. 行情与研究版本绑定

每日链路分成三个独立发布阶段：

```text
08:15 market data
  -> SP500 PIT PASS
  -> OHLCV / open / volume / PIT coverage PASS
  -> DuckDB published_versions

08:45 factor research
  -> 要求 DuckDB target_session 等于应发布交易日
  -> 8 个因子逐个生成 raw/clean 同代 manifest
  -> 每个 manifest 写入 DuckDB version_id 和行情/PIT hash
  -> 全部完成后原子写 research_publication.json

10:30 paper trading
  -> 要求 research_publication 与最新 DuckDB version 完全一致
  -> 处理 pending 订单、生成新目标和每日估值
```

研究入口：

```bash
python scripts/run_factor_research.py
python scripts/run_factor_research.py --target-session 2026-07-31 --json
```

相同数据版本已经完整发布时返回 `NOOP`。完成清单位于：

```text
outputs/universes/<UNIVERSE>/research_publication.json
```

它绑定：

- DuckDB `version_id`、target session、bars SHA-256、membership SHA-256；
- 配置中全部启用因子的 generation ID；
- 每个 factor manifest 的 SHA-256。

只要行情指针前进、某个因子缺失、generation 被后来覆盖或 hash 不一致，模拟盘前置检查就
失败，不能把昨天的因子配到今天的行情。

## 4. 新加坡服务器环境

2026-08-02 通过 SSH 实机确认：

| 用途 | Python |
|---|---|
| Web | `/home/projects/quant/.venv/bin/python` |
| 行情、研究、模拟盘等 worker | `/home/projects/quant/.venv-worker/bin/python` |

root 模板已经按这个分工固定：

- `quant-market-data-root.service`
- `quant-factor-research-root.service`
- `quant-paper-trading-root.service`
- `quant-web-root.service`

不要再把 worker 模板手工改回 `.venv`。三个每日 timer 分别是：

- `quant-market-data.timer`：Tue-Sat 08:15 Asia/Singapore；
- `quant-factor-research.timer`：Tue-Sat 08:45 Asia/Singapore；
- `quant-paper-trading.timer`：Tue-Sat 10:30 Asia/Singapore。

每个 oneshot 失败后由 systemd 间隔重试；旧的行情、研究和模拟盘发布结果保持可读。

## 5. 每日检查

```bash
python scripts/run_data_pipeline.py status
python scripts/run_factor_research.py --json

systemctl list-timers --all \
  quant-market-data.timer \
  quant-factor-research.timer \
  quant-paper-trading.timer

journalctl -u quant-market-data.service -n 200 --no-pager
journalctl -u quant-factor-research.service -n 200 --no-pager
journalctl -u quant-paper-trading.service -n 200 --no-pager
```

`run_factor_research.py --json` 在当日已经完整发布时应显示 `NOOP`，这也是最短的端到端
版本一致性检查。
