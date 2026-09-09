# 模拟盘运行与运维

更新日期：2026-08-24

## 运行模型

模拟盘不是浏览网页时临时计算，也不依赖浏览器常驻。每个完整 XNYS 交易日结束后，
独立 worker 执行一次：

1. 校验 active 账户使用的命名股票池已有与最新 DuckDB version 一致的完整研究发布。
2. 强制刷新 active 模拟盘中自定义 Watchlist 的 OHLCV。
3. 校验策略目标日期等于最近完整 XNYS session；陈旧或超前都失败。
4. 从 SQLite 的 `fills` 与 `cash_events` 事实账本重建现金和持仓。
5. 只用截至本次 `as-of` 已出现的开盘价处理旧 pending 订单。
6. 按账户 `rebalance_mode` 判断当天是否调仓；观察日只记录排名，不创建订单。
7. 以可执行收盘价估值；分红通过独立现金事件入账，再保存每日决策回放、账户投影和运行诊断。

因此，网页只读取已经落盘的最新结果。当天收益能否出现，取决于 worker 是否在收盘数据和
因子产物刷新成功后运行，而不是取决于用户是否登录。

## 本机手动运行

在项目根目录执行：

```bash
python scripts/run_factor_research.py
python scripts/prepare_paper_data.py
python scripts/run_paper.py
```

只运行一个账户或历史 `as-of`：

```bash
python scripts/run_paper.py --account-id <ACCOUNT_UUID>
python scripts/run_paper.py --account-id <ACCOUNT_UUID> --asof 2026-07-29
```

`--asof` 是数据可见截止日。订单只能使用该日及之前已经出现的开盘价，绝不会读取未来 bar。

## 新加坡服务器自动运行

通用 `/opt/quant + quant` 部署使用：

- `deploy/systemd/quant-paper-trading.service`
- `deploy/systemd/quant-paper-trading.timer`
- `deploy/systemd/paper-trading.env.example`

当前 `/home/projects/quant + root` 部署把 root 模板安装成同一个正式 service 名：

```bash
install -m 0640 deploy/systemd/paper-trading.env.example \
  /etc/quant/paper-trading.env
sudoedit /etc/quant/paper-trading.env

install -m 0644 deploy/systemd/quant-paper-trading-root.service \
  /etc/systemd/system/quant-paper-trading.service
install -m 0644 deploy/systemd/quant-paper-trading.timer \
  /etc/systemd/system/quant-paper-trading.timer

systemd-analyze verify \
  /etc/systemd/system/quant-paper-trading.service \
  /etc/systemd/system/quant-paper-trading.timer
systemctl daemon-reload
systemctl enable --now quant-paper-trading.timer
```

独立因子研究 timer 在 08:45 发布 `research_publication.json`；模拟盘 timer 在新加坡时间
周二至周六 10:30 启动。Service 先执行 `scripts/prepare_paper_data.py`，校验命名股票池的
研究清单与最新 DuckDB version、bars hash、PIT hash 和全部 factor generation 一致，并仅
刷新自定义 Watchlist 的 OHLCV。只有前置检查成功才运行账户。任一步失败都会返回非零，
systemd 每 20 分钟重试，最多受 unit 启动频率限制。

第一次启用前应手动跑一次并检查：

```bash
systemctl start quant-paper-trading.service
systemctl status quant-paper-trading.service --no-pager
journalctl -u quant-paper-trading.service -n 200 --no-pager
systemctl list-timers --all quant-paper-trading.timer
```

## Fail-closed 条件

以下情况不会生成看似正常的新结果：

- 正式行情版本没有 `open` 列，或 pending 订单没有可用的下一交易日开盘价；
- `volume_share` 模型没有历史成交量，或回测订单超过 ADV 上限；
- 目标因子日期不是要求的 XNYS session；
- 动态股票池没有 PIT 成分文件，首个快照太晚，或历史成分缺行情/因子列；
- 因子 raw/clean manifest 缺失、哈希不匹配或不属于同一 generation；
- `research_publication.json` 缺失、落后于最新 DuckDB version，或某个 factor generation
  在整体发布后被替换；
- 任一策略因子在某只股票当天缺失，系统不会用其余因子拼成不完整综合分；
- 持仓股票在 `as-of` 当日及之前没有可用估值价格；
- 成交账本存在重复 `fill_id`、超卖或非法记录；
- 分红现金账存在重复 `event_id`，或已入账事件与新行情版本推导出的经济金额冲突；
- 决策快照文件集合或 SHA-256 与 manifest 不一致。

账户级线程锁和文件锁会阻止网页手动运行与 systemd worker 同时修改同一账户。账户主记录和
`positions`、`orders`、`fills`、`cash_events`、`equity_curve`、`target_weights`、`target_history`、
`position_history`、`runs` 都保存在 `outputs/quant_app.sqlite3`。每次 record/frame 写入使用
SQLite 事务并核对 checksum。

当前执行顺序有意先持久化 `fills` 事实账本，再更新 `orders` 和账户投影；后续步骤失败时，
重跑会从 fill ledger 重建状态。整次账户运行仍包含多次数据库事务，因此上线真实账户前还需做
中途故障注入、重启恢复和重复运行验收，不能只凭单元测试宣称整轮完全原子。

## 边界

当前 `cash_events` 根据可执行收盘价与总回报收盘价在除息日推导经济应计，并不包含券商实际
派息日、预扣税、股票股利或拆股处理。因此它解决了“持仓收益漏掉现金分红”，但还不能等同于
券商级 corporate-action ledger；模拟盘页面会把这些事件单独展示，避免把它们混成价格收益。

这是内部日线模拟盘，不会连接券商，也不能提交真实订单。费用和滑点是显式模型，不是券商
真实回报；交易所路由、盘口排队、盘前盘后、借券费、融资利息和 corporate actions 的券商
落账差异仍不在当前模型内。具体参数见 `docs/trading_costs.md`。

模拟成交和每日账户日结可以通过独立 Discord 频道发送。通知 worker 只对账持久化交易事实，
Discord 失败不会回滚或污染账户；详细状态机、11:00 SGT 调度和部署方式见
[`paper_trading_discord_notifications.md`](paper_trading_discord_notifications.md)。
