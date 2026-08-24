# 研究完整性升级与强制重建手册

更新时间：2026-08-24

这次升级改变的是金融语义和统计口径，不只是文件格式。旧发布版本即使包含
`open`、`close`、`adj_close`，也不能仅凭列名证明可执行价与总回报价来自正确数据源，
因此禁止原地补标签。

## 1. 为什么必须重建

新版本同时强制以下合同：

- 可执行 `open/high/low/close` 与总回报 `adj_close` 分离；
- 增量版本只能继承已经通过价格语义认证的父版本；
- SP500、NASDAQ100、MAG7 的注册 ETF 基准必须绑定到同目标日不可变版本；
- 因子研究发布必须使用 HAC 置信方法和可审计未来收益；
- 行业中性化在没有 PIT 行业历史时关闭；
- 模拟盘账户使用 schema 3 的可执行价估值与分红现金账。

本机只读验收发现当前发布指针仍是旧合同：

| 股票池 | 当前版本 | 目标日 | manifest | 结论 |
|---|---|---|---|---|
| SP500 | `6d080d2a1822440f8b64ea536a246d50` | 2026-08-07 | schema 2，无 `price_semantics` | 必须全量重建 |
| MAG7 | `4da3efdf67624a1db7c559d764917387` | 2026-08-07 | schema 2，无 `price_semantics` | 必须全量重建 |
| NASDAQ100 | 无当前发布指针 | - | - | 先发布 PIT，再全量构建 |

新 Reader 对前两者会明确抛出 `DataFoundationError`，不会退回旧 Parquet、FMP 或
`close` 替代 `adj_close`。

## 2. 备份边界

部署前先停止 writer/research worker，并备份以下内容：

- `configs/default.yaml`；
- `data/catalog/quant.duckdb`；
- `data/lake/`；
- `outputs/quant_app.sqlite3` 与 `outputs/universes/`；
- SG 上对应 systemd unit 和环境文件。

旧 DuckDB、Parquet、SQLite 和研究输出只归档，不删除。回滚时必须同时回滚代码、配置、
catalog、lake 与 outputs，不能只切回一个数据库文件。

## 3. 正式重建顺序

以下命令中的 `YYYY-MM-DD` 必须是已经完成且 FMP 日线稳定的同一个 XNYS session。

### 3.1 重建 PIT 股票池

```bash
python scripts/run_data_pipeline.py pit \
  --universe SP500 \
  --universe NASDAQ100 \
  --target-session YYYY-MM-DD
```

两套 PIT 校验都必须为 `PUBLISHED`；任何未审阅历史事件都会阻止后续发布。

### 3.2 全量重建命名股票池行情

```bash
python scripts/run_data_pipeline.py update \
  --universe SP500 \
  --universe NASDAQ100 \
  --universe MAG7 \
  --target-session YYYY-MM-DD \
  --full-rebuild
```

这一步从规范 FMP 双价格源重新下载完整配置历史。它不读取旧版本作为父版本，并把 SPY、
QQQ 等注册基准作为 support ticker 写入同一行情版本，但 support ticker 不进入股票池成员矩阵。

### 3.3 全量重建全美覆盖链

```bash
python scripts/backfill_us_equity_coverage.py \
  --target-session YYYY-MM-DD \
  --history-start 2019-01-01 \
  --auto-resume \
  --publish

python scripts/build_us_liquid_pit.py --full-rebuild --publish

python scripts/run_broad_factor_data.py \
  --full-rebuild \
  --auto-resume \
  --publish
```

`update_us_equity_coverage.py` 只能在这次完整 broad backfill 成功后再次使用；旧 broad
publication 不能作为新语义增量父版本。

### 3.4 重建正式因子研究

```bash
python scripts/run_factor_research.py \
  --universe SP500 \
  --universe NASDAQ100 \
  --universe MAG7 \
  --target-session YYYY-MM-DD \
  --force
```

这会重建 raw/clean 因子矩阵、IC、HAC t/p/q、未来收益审计、分组净值、逐票成本和
`research_publication.json`。旧 publication schema 或旧 confidence methodology 不再可读。

## 4. 模拟盘迁移

- 新建账户直接使用 schema 3。
- 没有 `fills`、`cash_events`、`equity_curve` 的旧空账户可在首次运行时自动升级。
- 已有耐久账本的旧账户不会被静默重标；系统要求显式归档并重建账户，或先开发经过审计的
  corporate-action 迁移工具。
- 不允许删除旧账本后冒充原账户连续运行。

当前分红账是除息日经济应计。真实派息日、预扣税、拆股、股票股利和其他 corporate action
仍需要独立版本化事件源，不能把本次升级描述成券商级结算仿真。

## 5. 上线验收门槛

必须同时满足：

1. `python scripts/run_data_pipeline.py status --json` 中三个命名股票池目标日一致。
2. 每个新 manifest 为 schema 3，存在有效 `price_semantics`，且增量版本具有已认证父版本。
3. SP500 含 SPY、NASDAQ100/MAG7 含 QQQ 行情，但这些 ETF 不被误标为指数成员。
4. `research_publication.json` 为 schema 3，方法为
   `factor_research_v3_price_hac_censor_aware`。
5. confidence 方法为 `factor_confidence_v2_hac_censor_aware`，5 日 IC 的 `HAC_lags=4`。
6. 因子页能看到普通结果、事件结算、右边界、未解析缺失和作废截面计数。
7. 回测 config 绑定 benchmark version；策略收益、基准收益覆盖日期完全一致。
8. 模拟盘页能看到逐笔费用和分红现金事件；同一日期重跑不重复入账。
9. 完整测试、存储 verify、Web 页面和 systemd unit 验收全部通过。

任一门槛失败时，不更新 SG 正式指针，不启动每日增量任务，也不删除旧版本。

## 6. 本次代码工作的部署状态

本手册记录的是代码与本机只读数据验收结果。本次任务没有连接 SG、没有上传代码、没有执行
FMP 全量下载，也没有切换任何生产定时任务。SG 部署必须在独立备份和维护窗口内按上述顺序执行。
