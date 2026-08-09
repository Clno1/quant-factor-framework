# Momentum Alerts

> 这份文件只说明旧的盘中小时告警 worker。涵盖 Web、盘前 T-1 日报、完整算法、状态机、
> root 服务器命令和当前限制的统一说明见
> [`momentum_breakout_summary.md`](momentum_breakout_summary.md)。

Discord 动量提醒采用独立 worker，不依赖 FastAPI 进程。现有强势筛选、日线 Setup
评分和盘中触发器保持不变，提醒层只负责定时运行、状态去重和消息投递。

## 日线数据契约

旧 worker 现在也通过 `load_breakout_daily_dataset()` 一次加载正式日线：

- `US_ACTIVE` 实际解析为一个明确的 `US_LIQUID_5M DatasetVersion`；
- bars、证券属性和 membership 来自同一版本；
- DuckDB 在读取时下推 ticker 和 400 日窗口，随后在内存中批量计算；
- 输出快照包含 `data_universe / dataset_version_id / DataContract`；
- FMP 只提供当日 market hours、batch quote 和可选分钟线，不再临时补日线或证券 profile。

`always_tickers` 只有在正式版本股票池中且资产类型合规时才有效。缺失股票会进入
`excluded_forced_tickers`，应由收盘后的统一数据任务补入并发布，不能在提醒进程里旁路补洞。

## 资产范围

Discord 告警默认只扫描股票，不包含 ETF：

```yaml
momentum_alerts:
  asset_types:
    include_etfs: false
```

这个设置只影响告警 worker。网页的 `US_ACTIVE` 仍然保留股票和 ETF，因此可以继续
搜索和研究 `SOXL`。临时打开或关闭 ETF 可使用：

```bash
python scripts/run_momentum_alerts.py --include-etfs
python scripts/run_momentum_alerts.py --no-include-etfs
```

若服务器平时用 `--stocks-only` 只更新股票日线，首次打开 ETF 告警前应先运行一次
不带 `--stocks-only` 的全量刷新。

## 安全配置

运行配置向导：

```bash
python scripts/configure_momentum_discord.py
```

向导会隐藏读取 Webhook，先发送测试消息，再写入 Git 已忽略的 `.env.local`：

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_ALERT_ROLE_ID=123456789012345678
```

Webhook 相当于密码，不要写入 `configs/default.yaml`、源码、日志或提交记录。

## 影子运行

不发 Discord，只生成快照并保留待发送状态：

```bash
python scripts/run_momentum_alerts.py --extra-ticker PENG,AEVA,OKTA
```

快照保存到 `outputs/momentum_alerts/runs/`，去重状态保存在
`outputs/momentum_alerts/state.sqlite3`。

## 手动发送

首次手动发送建议继续关闭分钟线，只验证群消息格式：

```bash
python scripts/run_momentum_alerts.py --send --extra-ticker PENG,AEVA,OKTA
```

稳定后再打开分钟线扩展：

```bash
python scripts/run_momentum_alerts.py --send --intraday
```

## 调度语义

自动任务调用：

```bash
python scripts/run_momentum_alerts.py --send --scheduled-hourly
```

`--scheduled-hourly` 会同时要求 FMP 返回 NASDAQ 正在开市，并且美东时间处于
10:00 至 15:59。建议调度器每小时调用一次；休市、节假日、提前收盘和窗口之外
都会正常退出，不发送消息。

第一阶段不自动启用调度。至少观察一个完整交易日的小时摘要后，再开启
`--intraday` 的高优先级突破提醒。

新加坡 Linux 服务器的完整部署步骤见 `docs/singapore_server_deployment.md`。

当前 root 服务器应把 `deploy/systemd/quant-momentum-alerts-root.service` 安装为
`/etc/systemd/system/quant-momentum-alerts.service`，并配套安装仓库中的 timer。它使用
`/home/projects/quant/.venv`。分钟 worker 在连续五个交易日晋级并验证 live 投递前，小时提醒
继续运行；之后是否停用小时 timer 应按消息重复度单独决定。
