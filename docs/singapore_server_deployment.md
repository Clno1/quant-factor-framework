# 新加坡服务器部署

推荐在 Ubuntu/Debian 服务器上使用 `systemd timer`。告警 worker 与 FastAPI
完全分开运行，所以服务器不需要启动网页，也不需要浏览器会话。

## 1. 准备项目

以下单元文件默认使用用户 `quant` 和目录 `/opt/quant`。如需改名，请同步修改
`deploy/systemd/*.service`。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv build-essential
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

## 3. 首次准备数据

默认只刷新流动性达到 500 万美元的股票，并额外刷新 `QQQ` 作为市场过滤基准。
`QQQ` 不会进入告警候选。

```bash
sudo -u quant /bin/bash -c '\
  set -a; source /etc/quant/momentum-alerts.env; set +a; \
  exec /opt/quant/.venv/bin/python /opt/quant/scripts/refresh_us_active.py \
    --workers 6 --force-universe --stocks-only \
    --min-current-dollar-volume-m 5 --skip-precompute'
```

先做不发送的真实数据检查：

```bash
sudo -u quant /bin/bash -c '\
  set -a; source /etc/quant/momentum-alerts.env; set +a; \
  exec /opt/quant/.venv/bin/python /opt/quant/scripts/run_momentum_alerts.py \
    --no-include-etfs --max-rows 5'
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

## 5. 运维与更新

```bash
sudo journalctl -u quant-momentum-alerts.service -f
sudo journalctl -u quant-us-daily-refresh.service -n 100 --no-pager
sudo systemctl status quant-momentum-alerts.timer quant-us-daily-refresh.timer
```

更新代码后执行：

```bash
cd /opt/quant
sudo -u quant git pull --ff-only
sudo -u quant /opt/quant/.venv/bin/pip install -r requirements.txt
sudo systemctl daemon-reload
```

持久状态位于 `/opt/quant/outputs/momentum_alerts/state.sqlite3`，运行快照位于
`/opt/quant/outputs/momentum_alerts/runs/`。部署迁移时应一并备份，否则当日信号去重
状态会从空白开始。
