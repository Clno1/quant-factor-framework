# 新加坡服务器部署

> 当前腾讯云服务器采用 `/home/projects/quant`、`root` systemd 用户和统一 `.venv`，
> 日常启动、状态检查和代码更新请先读
> [server_daily_runbook.md](server_daily_runbook.md)。本文件下面的通用示例
> 仍以 `/opt/quant + quant 用户` 为默认环境，不应直接复制到当前 root 服务器。

推荐使用 Ubuntu 24.04 或 Debian 12，并通过 `systemd timer` 运行。项目使用
`enum.StrEnum`，因此 Python 必须是 3.11 或更高版本。告警 worker 与 FastAPI
完全分开运行，所以服务器不需要启动网页，也不需要浏览器会话。

## 1. 准备项目

以下单元文件默认使用用户 `quant` 和目录 `/opt/quant`。如需改名，请同步修改
`deploy/systemd/*.service`。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv build-essential tzdata
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
sudo useradd --system --home-dir /opt/quant --shell /usr/sbin/nologin quant
sudo git clone <YOUR_REPOSITORY_URL> /opt/quant
sudo chown -R quant:quant /opt/quant
sudo -u quant python3 -m venv /opt/quant/.venv
sudo -u quant /opt/quant/.venv/bin/pip install -r /opt/quant/requirements.txt
```

代码未托管在 Git 时，也可以从本机上传：

```bash
rsync -az --exclude .git --exclude .env.local --exclude outputs/ \
  /path/to/Quant/ user@server:/tmp/quant-upload/
```

上传后再由管理员移动到 `/opt/quant`，并将所有者设为 `quant:quant`。

## 2. 安装密钥

不要把 FMP key 或 Discord Webhook 放进 Git。服务器使用独立的 root 管理环境文件：

```bash
sudo install -d -m 0750 -o root -g quant /etc/quant
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/momentum-alerts.env.example \
  /etc/quant/momentum-alerts.env
sudoedit /etc/quant/momentum-alerts.env
```

至少填写 `FMP_API_KEY` 和 `DISCORD_WEBHOOK_URL`。Webhook 是群频道的写入凭证，
不要粘贴到聊天、终端历史或日志中。

`root:quant 0640` 配合每个 unit 的独立 `EnvironmentFile` 可以避免无意注入，但不是强安全
边界：这些 worker 同属 `quant` 用户/组，技术上可读取该组文件。需要强隔离时，使用独立
service user、专用 ACL/组或 systemd credentials。

## 3. 首次准备数据

### 3.1 主多因子 DuckDB 日线

主多因子数据链路应先安装单独的 FMP 环境文件并完成首次发布。完整的数据目录、质量门禁和
故障语义见 [`data_foundation.md`](data_foundation.md)。

```bash
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/market-data.env.example \
  /etc/quant/market-data.env
sudoedit /etc/quant/market-data.env
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_data_pipeline.py pit \
  --env-file /etc/quant/market-data.env
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_data_pipeline.py update \
  --universe SP500 --universe MAG7 \
  --env-file /etc/quant/market-data.env
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_data_pipeline.py status
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_factor_research.py
```

正式 SP500 数据要求 `data/pit_universes/SP500.parquet` 的最后快照等于目标交易日，并与
current constituents 一致。缺少或不一致时，数据写入和研究都会拒绝继续。

默认只刷新流动性达到 500 万美元的股票，并额外刷新 `QQQ` 作为市场过滤基准。
`QQQ` 不会进入告警候选。

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/refresh_us_active.py \
  --env-file /etc/quant/momentum-alerts.env \
  --workers 6 --force-universe --stocks-only \
  --min-current-dollar-volume-m 5 \
  --market-symbol QQQ --market-symbol SPY
```

输出必须包含 `published_universe_manifest=...us_active.premarket.json`。该 manifest 固定真实
XNYS 已完成 session、Parquet hash 与行数；provider 失败并回退旧 cache 时任务会失败，不会
给旧数据重新签发新鲜 manifest。低流动性例外统一配置在
`configs/default.yaml -> momentum_alerts.always_tickers`；旧 env 中的
`MOMENTUM_ALERT_EXTRA_TICKERS` 只保留给盘中小时任务兼容，迁移后应清空。

先做不发送的真实数据检查：

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/run_momentum_alerts.py \
  --env-file /etc/quant/momentum-alerts.env \
  --no-include-etfs --max-rows 5
```

确认输出正常后，可把末尾参数改成 `--send --no-include-etfs --max-rows 5` 做一次
服务器到 Discord 的手动投递测试。

## 4. 启用定时器

```bash
sudo install -m 0644 /opt/quant/deploy/systemd/*.service /etc/systemd/system/
sudo install -m 0644 /opt/quant/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quant-market-data.timer
sudo systemctl enable --now quant-factor-research.timer
sudo systemctl enable --now quant-us-daily-refresh.timer
sudo systemctl enable --now quant-momentum-alerts.timer
sudo systemctl list-timers 'quant-*'
```

安装后可手动触发一次正式 service 并查看日志：

```bash
sudo systemctl start quant-momentum-alerts.service
sudo journalctl -u quant-momentum-alerts.service -n 100 --no-pager
```

正式 service 命令含 `--scheduled-hourly`；休市时会显示 `skip` 而不会发消息。

两个任务分别是：

- `quant-market-data.timer`：新加坡时间周二至周六 08:15，发布
  SP500/NASDAQ100 PIT 和 SP500/NASDAQ100/MAG7 已校验版本。
- `quant-factor-research.timer`：新加坡时间周二至周六 08:45，发布与当日 DuckDB
  version 绑定的 SP500/NASDAQ100 因子研究、MAG7 参考结果和跨池结论。
- `quant-us-daily-refresh.timer`：新加坡时间周二至周六 07:15，更新刚结束的美股交易日。
- `quant-momentum-alerts.timer`：每小时 35 分唤醒；worker 再按 NASDAQ 实际开市状态和
  美东 10:00-15:59 判断是否扫描，因此自动适配夏令时、节假日和提前收盘。

### 4.1 内部模拟盘每日运行

模拟盘必须在因子产物刷新成功后运行，不能只定时执行 `run_paper.py`。安装独立的只含 FMP key
环境文件和串行 service：

```bash
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/paper-trading.env.example \
  /etc/quant/paper-trading.env
sudoedit /etc/quant/paper-trading.env
sudo install -m 0644 \
  /opt/quant/deploy/systemd/quant-paper-trading.service \
  /etc/systemd/system/
sudo install -m 0644 \
  /opt/quant/deploy/systemd/quant-paper-trading.timer \
  /etc/systemd/system/
sudo systemd-analyze verify \
  /etc/systemd/system/quant-factor-research.service \
  /etc/systemd/system/quant-factor-research.timer \
  /etc/systemd/system/quant-paper-trading.service \
  /etc/systemd/system/quant-paper-trading.timer
sudo systemctl daemon-reload
sudo systemctl start quant-paper-trading.service
sudo journalctl -u quant-paper-trading.service -n 200 --no-pager
sudo systemctl enable --now quant-paper-trading.timer
```

因子研究在 08:45 独立运行；模拟盘 Timer 在新加坡时间周二至周六 10:30 启动。Service
先执行 `prepare_paper_data.py`，校验 active 账户使用的命名股票池研究发布与最新 DuckDB
version 一致，并只刷新自定义 Watchlist，成功后再执行 `run_paper.py`。动态 `SP500/US_ACTIVE`
账户还必须准备
`data/pit_universes/<UNIVERSE>.parquet`；缺文件时账户会 fail closed。完整说明见
[`paper_trading_operations.md`](paper_trading_operations.md)。

## 5. 分钟级突破监控与自动晋级

该服务与旧的 `quant-momentum-alerts.timer` 不同：它在 09:20 ET 启动一个常驻进程，默认
600 只每 5 分钟宽筛、40 只每分钟观察，并在交易所实际收盘五分钟后退出。unit 使用 `--auto`：
前五个合格 session 只写 shadow outbox；最近五个预期 session 全部 `PASS` 后才进入 Discord live。

先安装独立环境文件并确认依赖：

```bash
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/intraday-momentum-monitor.env.example \
  /etc/quant/intraday-momentum-monitor.env
sudoedit /etc/quant/intraday-momentum-monitor.env
sudo -u quant /opt/quant/.venv/bin/python -c \
  'import exchange_calendars; print(exchange_calendars.__version__)'
```

运行第 3 节的日线刷新后，先做性能回放和只读状态检查：

```bash
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/benchmark_intraday_monitor.py \
  --days 5 --candidates 600 --active 60 --enforce

sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_intraday_momentum_monitor.py \
  --env-file /etc/quant/intraday-momentum-monitor.env --status
```

第一次真实 smoke 应在美股开市后显式使用 `--shadow`，不会发送 Discord：

```bash
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_intraday_momentum_monitor.py \
  --env-file /etc/quant/intraday-momentum-monitor.env \
  --shadow --once
```

输出必须同时满足：

- `market_open=true`；
- `source_data_date` 等于上一真实 XNYS session；
- 日线 exact coverage 至少 80%，否则进程应明确失败关闭；
- `errors=[]`；
- `active_count <= 40`；
- 没有 Discord 消息。

通用 `/opt/quant` 部署安装标准 unit；当前 `/home/projects/quant + root` 服务器把
`quant-intraday-momentum-monitor-root.service` 安装为规范 unit 名：

```bash
sudo install -m 0644 \
  /home/projects/quant/deploy/systemd/quant-intraday-momentum-monitor-root.service \
  /etc/systemd/system/quant-intraday-momentum-monitor.service
sudo install -m 0644 \
  /home/projects/quant/deploy/systemd/quant-intraday-momentum-monitor.timer \
  /etc/systemd/system/
sudo systemd-analyze verify \
  /etc/systemd/system/quant-intraday-momentum-monitor.service \
  /etc/systemd/system/quant-intraday-momentum-monitor.timer
sudo systemctl daemon-reload
sudo systemctl enable --now quant-intraday-momentum-monitor.timer
sudo systemctl status quant-intraday-momentum-monitor.timer
```

连续观察至少 5 个完整交易日：

```bash
sudo journalctl -u quant-intraday-momentum-monitor.service -n 300 --no-pager
sudo -u quant /opt/quant/.venv/bin/python \
  /opt/quant/scripts/run_intraday_momentum_monitor.py \
  --env-file /etc/quant/intraday-momentum-monitor.env --status
```

状态库是 `outputs/intraday_momentum_monitor/state.sqlite3`。独立 env 可以预先配置 Webhook；
`INTRADAY_MOMENTUM_DISCORD_ENABLED=false` 是总 kill switch。设为 true 仍不能绕过五日 SQLite
晋级闸门，旧小时提醒在分钟 live 稳定前继续保留。

## 6. 行业涨跌影子任务

`group_analytics` 的 writer 与网页入口使用两个独立开关。仓库默认均关闭；systemd
单元只通过环境变量开启 writer，并显式保持网页关闭，因此影子运行不会改变现有因子、
回测、模拟盘或 FastAPI 导航。

日行情刷新单元额外准备 `SPY`；行业任务仍会自行按 XNYS 日历核对目标 session、
SP500 成员和 benchmark 的 adjusted-close 覆盖率。上游未完成、覆盖不足或任一层级失败时，
CLI 返回非零，systemd 每 15 分钟重试，且不会切换已有的 `latest_success.json`。

先执行一次不发布的确定性 smoke test：

```bash
sudo -u quant env GROUP_ANALYTICS_ENABLED=true GROUP_ANALYTICS_WEB_ENABLED=false \
  /opt/quant/.venv/bin/python /opt/quant/scripts/run_group_analytics.py \
  --env-file /etc/quant/momentum-alerts.env \
  --mode eod --universe SP500 --taxonomy FMP --level all \
  --asof latest --limit 50
```

`--limit` 自动隐含 dry-run，不会发布正式 pointer。确认两级返回 `SUCCESS` 后再安装并
启动正式 timer：

```bash
sudo systemctl enable --now quant-group-analytics-eod.timer
sudo systemctl start quant-group-analytics-eod.service
sudo systemctl status quant-group-analytics-eod.timer quant-group-analytics-eod.service
sudo journalctl -u quant-group-analytics-eod.service -n 200 --no-pager
```

正式输出位于
`/opt/quant/outputs/universes/SP500/group_analytics/FMP/<level>/eod/`；全局 attempt
索引位于 `/opt/quant/outputs/_group_analytics_attempts/`。开放只读页面需要在 Web
进程环境中同时设置 `GROUP_ANALYTICS_ENABLED=true` 和
`GROUP_ANALYTICS_WEB_ENABLED=true` 后重启 Web；完成连续 10 个实际交易日的影子
观察与生产机性能复测前，不要开启页面。

## 7. 美股开盘前 Discord 动量日报

独立盘前 worker 在每个 XNYS session 的 09:20 America/New_York 读取上一完整交易日的
动量日线并投递到 `#momentum-alerts`。当前生产 unit 显式传入 `--channel momentum`；
sector rotation 的代码和独立 Webhook 可以保留，但配置默认关闭，且不由该 timer 发送。
盘前 worker 不修改盘中小时告警状态，也不让主框架反向依赖通知层。

先配置并测试动量 Webhook。现有配置向导同时支持 sector Webhook；若 sector 尚未发布，保留其
已有值但不要启用 sector channel：

```bash
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/premarket-digest.env.example \
  /etc/quant/premarket-digest.env
sudo /opt/quant/.venv/bin/python /opt/quant/scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env --test-send
sudo chown root:quant /etc/quant/premarket-digest.env
sudo chmod 0640 /etc/quant/premarket-digest.env
```

必须人工确认动量频道恰好收到一条无 mention 测试消息，且没有投错频道；未确认前不要启用
`quant-premarket-digest.timer`。

先按一个已完成 session 运行不发送的 preview；`--session` 参数表示即将开盘的交易日，
数据会自动取其 previous XNYS session：

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env --session 2026-07-16
```

确认 universe、动量 payload、日期、精确日期覆盖和可计算历史覆盖后即可安装动量调度。
sector Discord 若以后启用，仍需单独完成 group analytics 的观察和发布门槛，不能借用动量
频道的验收结果。当前安装命令如下：

```bash
sudo install -m 0644 /opt/quant/deploy/systemd/quant-premarket-digest.service /etc/systemd/system/
sudo install -m 0644 /opt/quant/deploy/systemd/quant-premarket-digest.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/quant-premarket-digest.service \
  /etc/systemd/system/quant-premarket-digest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now quant-premarket-digest.timer
sudo systemctl status quant-premarket-digest.timer
```

`After=` 只保证同一启动事务中的排序，不证明当天上游成功。当前 unit 固定
`--channel momentum`，启用前检查 `quant-us-daily-refresh.timer` 最近一次成功日志；数据门槛
仍会阻止陈旧消息，sector rotation 不由该 timer 发送。

详细算法、消息合同、幂等状态和故障处理见 `docs/premarket_discord.md`。如果希望动量频道
每天严格只有盘前一条，应另行停用 `quant-momentum-alerts.timer`；否则两者可并存。

## 8. 运维与更新

```bash
sudo journalctl -u quant-momentum-alerts.service -f
sudo journalctl -u quant-us-daily-refresh.service -n 100 --no-pager
sudo systemctl status quant-momentum-alerts.timer quant-us-daily-refresh.timer quant-group-analytics-eod.timer quant-premarket-digest.timer
```

更新代码后执行：

```bash
# 先记录更新前真实 active 的集合；下面的 TIMER_1 TIMER_2 只替换成该集合。
sudo systemctl list-units --type=timer --state=active 'quant-*'
sudo systemctl stop TIMER_1 TIMER_2
sudo systemctl is-active quant-momentum-alerts.service quant-us-daily-refresh.service \
  quant-group-analytics-eod.service quant-premarket-digest.service
# 上一行应全部显示 inactive；若仍 active，等待自然结束，不要中断正在投递的 worker。
cd /opt/quant
sudo -u quant git pull --ff-only
sudo -u quant /opt/quant/.venv/bin/pip install -r requirements.txt
sudo install -m 0644 /opt/quant/deploy/systemd/*.service /etc/systemd/system/
sudo install -m 0644 /opt/quant/deploy/systemd/*.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/quant-*.service \
  /etc/systemd/system/quant-*.timer
sudo systemctl daemon-reload
sudo systemctl start TIMER_1 TIMER_2  # 与更新前记录的集合完全相同
```

更新流程不执行 `enable`，也绝不能启动更新前处于 inactive/disabled 的 timer；尤其不能在
release gate 未完成时顺手启用盘前日报或旧盘中小时任务。

如果 FastAPI 与 worker 共用该 venv，也要在维护窗口按你的实际 Web service 名称先停 Web，
更新完成后再启动，避免运行中的进程同时看到两套依赖。

持久状态位于 `/opt/quant/outputs/momentum_alerts/state.sqlite3`，运行快照位于
`/opt/quant/outputs/momentum_alerts/runs/`。部署迁移时应一并备份，否则当日信号去重
状态会从空白开始。

盘前日报的独立 outbox 位于 `/opt/quant/outputs/premarket_digest/state.sqlite3`。迁移时也应
备份它，否则当日已发送频道无法由新服务器识别；Webhook URL 不在该数据库中。必须按
单活顺序迁移：旧机停 timer、确认 service inactive、执行 SQLite backup/checkpoint、复制并
恢复所有权与权限，最后才在新机启 timer。不要两机并行，也不要在 WAL 未合并时只复制主文件。

停 timer 且确认 inactive 后，可用 Python 的 SQLite backup API 生成一致副本（目标目录需
预先设为 root/quant 可写的受保护目录）：

```bash
sudo -u quant /opt/quant/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:/opt/quant/outputs/momentum_alerts/state.sqlite3?mode=ro',uri=True); d=sqlite3.connect('/secure-transfer/momentum-state.sqlite3'); s.backup(d); assert d.execute('PRAGMA integrity_check').fetchone()==('ok',); ts=('signal_state','alert_runs'); assert [s.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ts]==[d.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ts]; d.close(); s.close()"
sudo -u quant /opt/quant/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:/opt/quant/outputs/premarket_digest/state.sqlite3?mode=ro',uri=True); d=sqlite3.connect('/secure-transfer/premarket-state.sqlite3'); s.backup(d); assert d.execute('PRAGMA integrity_check').fetchone()==('ok',); assert s.execute('SELECT count(*) FROM deliveries').fetchone()[0]==d.execute('SELECT count(*) FROM deliveries').fetchone()[0]; d.close(); s.close()"
```

`/etc/quant/momentum-alerts.env` 与 `/etc/quant/premarket-digest.env` 含真实凭据，不能进入普通
rsync、Git 或非加密备份。新机上的 FMP key/旧盘中 Webhook 用
`sudoedit /etc/quant/momentum-alerts.env` 填写；`configure_momentum_discord.py` 没有服务器
`--env-file` 模式，不要在服务器用它。盘前双频道则重新运行：

```bash
sudo /opt/quant/.venv/bin/python /opt/quant/scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env --test-send
```

人工核对两条测试消息后再继续。若必须安全转移 env，先用受控加密通道，落盘后立即恢复
`root:quant`、0640，再启 timer。group immutable artifacts 也要完整迁移，或在新机先重建并
通过 98% 门槛。恢复 SQLite 后在新机再次执行 `PRAGMA integrity_check` 和同样的表行数核对。
