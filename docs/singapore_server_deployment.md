# 新加坡服务器部署

> 当前腾讯云服务器若采用 `/home/projects/quant`、`root` systemd 用户和独立
> `.venv-worker`，日常启动、状态检查和代码更新请先读
> [server_daily_runbook.md](server_daily_runbook.md)；只有首次安装和复杂故障再读
> [root_discord_operations_guide.md](root_discord_operations_guide.md)。本文件下面的通用示例
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

默认只刷新流动性达到 500 万美元的股票，并额外刷新 `QQQ` 作为市场过滤基准。
`QQQ` 不会进入告警候选。

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/refresh_us_active.py \
  --env-file /etc/quant/momentum-alerts.env \
  --workers 6 --force-universe --stocks-only \
  --min-current-dollar-volume-m 5 \
  --market-symbol QQQ --market-symbol SPY --skip-precompute
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

- `quant-us-daily-refresh.timer`：新加坡时间周二至周六 07:15，更新刚结束的美股交易日。
- `quant-momentum-alerts.timer`：每小时 35 分唤醒；worker 再按 NASDAQ 实际开市状态和
  美东 10:00-15:59 判断是否扫描，因此自动适配夏令时、节假日和提前收盘。

## 5. 行业涨跌影子任务

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

## 6. 美股开盘前 Discord 双频道日报

独立盘前 worker 在每个 XNYS session 的 09:20 America/New_York 读取上一完整交易日的
动量日线，以及 `sector` / `sub_industry` immutable group artifacts，分别投递到
`#momentum-alerts` 和 `#sector-rotation`。它不修改盘中小时告警状态，也不让主框架或
group analytics 反向依赖通知层。

先配置两个独立 Webhook：

```bash
sudo install -m 0640 -o root -g quant \
  /opt/quant/deploy/systemd/premarket-digest.env.example \
  /etc/quant/premarket-digest.env
sudo /opt/quant/.venv/bin/python /opt/quant/scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env --test-send
sudo chown root:quant /etc/quant/premarket-digest.env
sudo chmod 0640 /etc/quant/premarket-digest.env
```

必须人工确认两个频道各恰好收到一条无 mention 测试消息，且没有投错频道；未确认前不要
启用 `quant-premarket-digest.timer`。

先按一个已完成 session 运行不发送的 preview；`--session` 参数表示即将开盘的交易日，
数据会自动取其 previous XNYS session：

```bash
sudo -u quant /opt/quant/.venv/bin/python /opt/quant/scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env --session 2026-07-16
```

确认 universe manifest、两个 payload、日期、动量精确/可计算覆盖和 group 98% 门槛后，
继续完成 group analytics 连续 10 个真实 XNYS
session 的影子观察，并在新加坡生产机执行 benchmark。sector Discord 是正式外发面，不仅是
Web UI，因此这两项 release gate 完成前不要启用盘前 timer。全部通过后安装调度：

```bash
sudo install -m 0644 /opt/quant/deploy/systemd/quant-premarket-digest.service /etc/systemd/system/
sudo install -m 0644 /opt/quant/deploy/systemd/quant-premarket-digest.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/quant-premarket-digest.service \
  /etc/systemd/system/quant-premarket-digest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now quant-premarket-digest.timer
sudo systemctl status quant-premarket-digest.timer
```

`After=` 只保证同一启动事务中的排序，不证明当天上游成功。启用前还要检查
`quant-us-daily-refresh.timer`、`quant-group-analytics-eod.timer` 均已启用，并核对两项
service 最近一次成功日志；最终数据门槛仍会阻止陈旧消息。

详细算法、消息合同、幂等状态和故障处理见 `docs/premarket_discord.md`。如果希望动量频道
每天严格只有盘前一条，应另行停用 `quant-momentum-alerts.timer`；否则两者可并存。

## 7. 运维与更新

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
