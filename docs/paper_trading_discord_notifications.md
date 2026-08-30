# 模拟盘 Discord 成交与日结通知

更新日期：2026-08-30

## 1. 业务口径

模拟盘通知使用一个独立 Discord 文字频道，建议命名为 `模拟交易`。它只发送两类消息：

1. **模拟成交**：持久化 `fills` 账本出现新 `fill_id` 后，最多约两分钟内发送。挂单、目标权重变化、
   无成交轮次和浏览网页都不会触发消息。
2. **每日账户日结**：每个 XNYS 交易日之后，于周二至周六 `11:00 Asia/Singapore` 发送一次。
   这是 10:30 模拟盘任务和 11:30 全美宽基重任务之间的低负载窗口。

成交消息展示账户、买卖方向、股票、数量、决策日、实际 `next_open` 成交日、原始开盘价、模拟
成交价、费用、滑点和行情版本。日结展示每个 active 账户的净值、当日/累计盈亏、现金、持仓、
成交额、费用、滑点、pending 订单和错误状态。所有消息明确标注“内部模拟交易”，不代表真实券商
订单。

## 2. 数据与可靠性

交易事实仍只来自 `outputs/quant_app.sqlite3`：

- `fills` 是成交事实源；通知以 `fill_id` 确定性去重。
- `runs` 提供目标交易日和 `dataset_version_id`。
- `orders`、`positions`、`equity_curve` 和账户主记录组成日结快照。

投递状态单独保存在：

```text
outputs/paper_notifications/state.sqlite3
```

表 `paper_notification_outbox` 使用不可变 payload hash。状态含义：

| 状态 | 含义 |
|---|---|
| `BASELINED` | 上线前历史成交，只登记、不补发 |
| `PENDING` | 已冻结消息，等待发送 |
| `SENDING` | 已领取，正在请求 Discord |
| `SENT` | Discord 返回 2xx 且具有 message ID |
| `FAILED` | 明确失败；只有可安全重试的错误才有限重试 |
| `UNKNOWN` | 请求可能已成功但响应不确定，禁止自动重试以避免重复消息 |

每次 worker 都会从不可变成交账本重建缺失 outbox，因此通知 SQLite 丢失不能改写交易账本；恢复时
必须先人工 baseline 历史成交，不能直接打开 timer。

## 3. 创建 Discord 频道

需要由 Discord 服务器管理员在 Discord 界面完成：

1. 新建文字频道 `模拟交易`。
2. 打开“编辑频道 -> 整合 -> Webhook -> 新建 Webhook”。
3. 确认 Webhook 目标就是 `模拟交易`，复制 Webhook URL。
4. 不要把 URL 发到聊天、Git、网页或日志；它等同于该频道的发送密码。

模拟交易必须使用独立 Webhook。配置脚本会拒绝复用动量、盘前动量或板块轮动频道。

## 4. SG 安全配置

代码部署并安装 unit 后，在 SG 项目目录交互运行：

```bash
cd /home/projects/quant

.venv/bin/python scripts/configure_paper_discord.py \
  --env-file /etc/quant/paper-notifications.env \
  --dashboard-base-url http://43.156.89.232:18823 \
  --test-send
```

脚本使用不回显输入，自动执行三件事：验证这是独立有效 Webhook、把现有成交设为 `BASELINED`、
发送一条不带角色提醒的测试消息。密钥文件权限为 `0600`，真实 URL 不进入主站进程。

## 5. systemd 安装

当前 `/home/projects/quant + root` 部署使用 root 模板覆盖正式 unit 名：

```bash
install -m 0644 deploy/systemd/quant-paper-discord-events-root.service \
  /etc/systemd/system/quant-paper-discord-events.service
install -m 0644 deploy/systemd/quant-paper-discord-events.timer \
  /etc/systemd/system/quant-paper-discord-events.timer
install -m 0644 deploy/systemd/quant-paper-discord-daily-root.service \
  /etc/systemd/system/quant-paper-discord-daily.service
install -m 0644 deploy/systemd/quant-paper-discord-daily.timer \
  /etc/systemd/system/quant-paper-discord-daily.timer

systemd-analyze verify \
  /etc/systemd/system/quant-paper-discord-events.service \
  /etc/systemd/system/quant-paper-discord-events.timer \
  /etc/systemd/system/quant-paper-discord-daily.service \
  /etc/systemd/system/quant-paper-discord-daily.timer

systemctl daemon-reload
systemctl enable --now \
  quant-paper-discord-events.timer \
  quant-paper-discord-daily.timer
```

首次启用前必须确认配置脚本已经成功 baseline。事件 worker 每两分钟只读少量 SQLite 记录，内存
上限 220 MB；日结只在 11:00 运行一次，不调用 FMP、不计算因子、不扫描 DuckDB 全历史。

## 6. 验收和排错

```bash
.venv/bin/python scripts/run_paper_notifications.py \
  --env-file /etc/quant/paper-notifications.env --status

systemctl start quant-paper-discord-events.service
systemctl start quant-paper-discord-daily.service
systemctl status \
  quant-paper-discord-events.service \
  quant-paper-discord-daily.service --no-pager
journalctl -u 'quant-paper-discord-*' -n 200 --no-pager
```

运维站分别显示“模拟盘成交通知”和“模拟盘每日账户日结”，包括 pending、sent、failed、unknown 和
baseline 数量。历史 `FAILED` 可以保留审计，但当前最新 `SENT` 才代表本次目标消息成功。

若出现 `UNKNOWN`，先在 Discord 频道人工确认是否已有相同成交或日结消息；不得直接删除 SQLite
记录或无限重试。Webhook 泄露时应立即在 Discord 删除旧 Webhook、创建新 Webhook并重新运行安全
配置脚本。

## 7. 2026-08-30 SG 预部署记录

提交 `74defad` 已部署到 `/home/projects/quant`，生产备份位于：

```text
/home/projects/quant-backups/paper-discord-notifications-20260830T2350CST
```

四个 systemd unit 已安装并通过 `systemd-analyze verify`；唯一输出是腾讯云 `tat_agent.service`
使用旧 `/var/run` 路径的既有警告，与本功能无关。SG 定向回归为 `11 passed`。

生产通知 SQLite 为 `outputs/paper_notifications/state.sqlite3`，`integrity_check=ok`。上线前已有的
7 笔 fill 已按不可变 `fill_id` 登记为 `FILL:BASELINED`，不会在正式启用后补发。当前环境文件权限
为 `0600`，但 Webhook 为空且 `PAPER_DISCORD_DELIVERY_ENABLED=false`；事件与日结 timer 均为
`disabled`。这是有意的安全状态，不是任务故障。

剩余人工门槛只有：Discord 管理员创建独立“模拟交易”文字频道和 Incoming Webhook，并在 SG 终端
运行第 4 节的不回显配置命令。测试消息成功后才能启用两个 timer，并把 `configs/operations.yaml`
中的两个通知任务 `enabled_expected` 改为 `true`。真实 Webhook 不得写入本文档或 Git。

## 8. 2026-08-31 正式激活

独立 Webhook 测试消息已成功送达。生产配置显示 `delivery_enabled=true` 和
`discord_configured=true`，但不会暴露 Webhook URL。`quant-paper-discord-events.timer` 与
`quant-paper-discord-daily.timer` 已正式 enabled + active，运维期望同步切换为 true。

首次事件 worker 运行成功，没有补发 7 笔历史成交。随后手工执行最近交易日 `2026-08-28` 日结，
一次尝试即取得 Discord message ID 并提交为 `DAILY_SUMMARY:SENT`；outbox
`integrity_check=ok`，没有 `PENDING/FAILED/UNKNOWN`。此后新 fill 最长约两分钟通知，日结按
Tue-Sat 11:00 SGT 自动发送。
