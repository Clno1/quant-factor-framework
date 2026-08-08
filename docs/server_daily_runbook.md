# SG 服务器日常运维速查

更新日期：2026-08-08

适用部署：`root@SG`、项目 `/home/projects/quant`、Web 环境 `.venv`、worker 环境
`.venv-worker`。完整架构和本地/服务器差异见
[`sg_operations_overview.md`](sg_operations_overview.md)。

## 1. 每日健康检查

```bash
ssh root@<SG_IP>
cd /home/projects/quant

systemctl status quant-web.service --no-pager
systemctl list-timers --all 'quant-*'

.venv-worker/bin/python scripts/run_data_pipeline.py status
.venv-worker/bin/python scripts/check_app_storage.py
```

预期：

- `quant-web.service` 是 `active (running)`；
- 六个生产 timer 有下一次触发时间；
- 行情 status 有 SP500、MAG7 和已经正式发布的 `US_LIQUID_5M`；
- SQLite 报告 `passed=true`、`sqlite_integrity=["ok"]`、`issues=[]`。

## 2. 生产时间线

目标 unit 时间均为新加坡时间：

| 时间 | Timer | 作用 |
|---|---|---|
| Tue-Sat 07:15 | `quant-us-daily-refresh.timer` | 发布 `US_LIQUID_5M` |
| Tue-Sat 08:15 | `quant-market-data.timer` | SP500 PIT + SP500/MAG7 行情 |
| Tue-Sat 08:45 | `quant-factor-research.timer` | 发布主因子研究 |
| Tue-Sat 09:15 | `quant-group-analytics-eod.timer` | 读取正式 SP500 version，发布板块研究 |
| Tue-Sat 10:30 | `quant-paper-trading.timer` | 运行 active 模拟盘账户 |
| 每 5 分钟 | `quant-data-requests.timer` | 处理 Watchlist 缺数请求 |

`quant-premarket-digest.timer` 当前应保持 disabled，除非明确重新启用外发 Discord。仓库内的盘中
monitor 模板不等于 SG 已安装。

本地 2026-08-08 已把 group timer 改为 09:15；SG 在部署这次 commit 前仍可能是上次审计的
07:45 unit。以服务器上的 `systemctl cat quant-group-analytics-eod.timer` 为准。

## 3. 查看最近结果和错误

```bash
systemctl show quant-us-daily-refresh.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-market-data.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-factor-research.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-group-analytics-eod.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-paper-trading.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-data-requests.service -p Result -p ExecMainStatus -p ActiveState
```

oneshot 成功后 `ActiveState=inactive` 正常，关键是：

```text
Result=success
ExecMainStatus=0
```

查看单项日志：

```bash
journalctl -u quant-market-data.service -n 200 --no-pager
journalctl -u quant-factor-research.service -n 200 --no-pager
journalctl -u quant-data-requests.service -n 200 --no-pager
journalctl -u quant-paper-trading.service -n 200 --no-pager
journalctl -u quant-web.service -n 200 --no-pager
```

## 4. Web 检查

```bash
systemctl enable --now quant-web.service
ss -lntp | grep ':18823'

read -r -p 'Web 用户名: ' QUANT_USER
read -r -s -p 'Web 密码: ' QUANT_PASSWORD; echo
curl -u "$QUANT_USER:$QUANT_PASSWORD" \
  -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
unset QUANT_PASSWORD
```

公网应通过 HTTPS 反向代理、VPN 或 SSH 隧道访问。Basic Auth 不提供传输加密，不应长期把原始
18823 明文端口开放给 `0.0.0.0/0`。

## 5. 手动补跑

先确认没有同名 service 正在运行，再用 systemd 启动，避免绕过环境文件和权限：

```bash
systemctl start quant-us-daily-refresh.service
systemctl start quant-market-data.service
systemctl start quant-factor-research.service
systemctl start quant-group-analytics-eod.service
systemctl start quant-paper-trading.service
```

任务有依赖顺序。上游失败时不要强行启动下游来制造陈旧结果。

## 6. 发布新代码

这次本地旧存储清理不会自动同步到 SG。推荐发布步骤：

1. 本地运行完整测试并提交明确 commit。
2. 服务器备份配置、SQLite、DuckDB catalog 和当前代码。
3. 服务器切到该 commit，安装变更后的依赖。
4. 安装或覆盖 root 版 systemd service 与 timer。
5. 执行 `systemd-analyze verify`。
6. 运行存储和数据检查。
7. 重启 Web，手动验收核心页面。
8. 再按同样的外部归档策略移动 SG 旧目录，不直接 `rm -rf`。

unit 校验示例：

```bash
systemd-analyze verify \
  /etc/systemd/system/quant-web.service \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-market-data.service \
  /etc/systemd/system/quant-factor-research.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-paper-trading.service \
  /etc/systemd/system/quant-data-requests.service
```

然后：

```bash
systemctl daemon-reload
systemctl restart quant-web.service
systemctl enable --now \
  quant-us-daily-refresh.timer \
  quant-market-data.timer \
  quant-factor-research.timer \
  quant-group-analytics-eod.timer \
  quant-paper-trading.timer \
  quant-data-requests.timer
```

## 7. 不能直接做的事

- 不要删除 `data/catalog/quant.duckdb` 或正式 version 目录。
- 不要直接编辑 `outputs/quant_app.sqlite3`。
- 不要在 writer 运行时复制 DuckDB/SQLite 单文件当作一致性备份。
- 不要让回测或模拟盘在缺数时直接调用 FMP。
- 不要在未备份、未发布新 commit、未验收页面前清理 SG 旧目录。
- 不要用 `git reset --hard` 覆盖服务器上尚未确认归属的改动。
