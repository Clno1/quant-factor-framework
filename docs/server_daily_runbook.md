# SG 服务器日常运维速查

更新日期：2026-08-13

适用部署：`root@SG`、项目 `/home/projects/quant`、所有 Web 与 worker 统一使用
`.venv`。完整架构和本地/服务器差异见
[`sg_operations_overview.md`](sg_operations_overview.md)。

## 1. 每日健康检查

```bash
ssh root@<SG_IP>
cd /home/projects/quant

systemctl status quant-web.service --no-pager
systemctl list-timers --all 'quant-*'

.venv/bin/python scripts/run_data_pipeline.py status
.venv/bin/python scripts/check_app_storage.py
```

预期：

- `quant-web.service` 是 `active (running)`；
- 9 个业务 timer 和 1 个运维 watchdog timer 均有下一次触发时间；全美宽基完成首次回填并正式
  启用后应为 11 个；
- 当前 SG 基线的行情 status 有 SP500、MAG7 和 `US_LIQUID_5M`；研究池改造部署完成后还必须有
  NASDAQ100；
- SQLite 报告 `passed=true`、`sqlite_integrity=["ok"]`、`issues=[]`。

## 2. 生产时间线

日线类 unit 使用新加坡时间；美股盘前和盘中 unit 直接使用 `America/New_York`：

| 时间 | Timer | 作用 |
|---|---|---|
| Tue-Sat 07:15 | `quant-us-daily-refresh.timer` | 发布 `US_LIQUID_5M` |
| Tue-Sat 08:15 | `quant-market-data.timer` | SP500/NASDAQ100 PIT + SP500/NASDAQ100/MAG7 行情 |
| Tue-Sat 08:45 | `quant-factor-research.timer` | 发布 SP500/NASDAQ100 因子研究、MAG7 参考结果和跨池结论 |
| Tue-Sat 09:15 | `quant-group-analytics-eod.timer` | 读取正式 SP500 version，发布板块研究 |
| Tue-Sat 10:30 | `quant-paper-trading.timer` | 运行 active 模拟盘账户 |
| Tue-Sat 11:30 | `quant-us-equity-coverage.timer` | **必须启用**：Security Master -> 全美 coverage -> PIT 宽基 -> 八因子 -> readiness -> 影子核验 |
| 每 5 分钟 | `quant-data-requests.timer` | 处理 Watchlist 缺数请求 |
| Mon-Fri 09:20 ET | `quant-premarket-digest.timer` | 分别发送 momentum 与 sector rotation 盘前摘要 |
| 每小时 :35 SGT | `quant-momentum-alerts.timer` | worker 内部只保留 10:00–15:59 ET |
| Mon-Fri 09:20 ET | `quant-intraday-momentum-monitor.timer` | 分钟 shadow；五日验收通过后可人工启用推送 |
| 每分钟 | `quant-operations-watchdog.timer` | 汇总任务、版本、投递、心跳与 systemd 证据，不发送 Discord 运维告警 |

分钟 monitor 使用独立 SQLite outbox。环境开关、五个连续交易日和 `--auto` 三重条件必须同时
满足才会发送 Discord；否则始终是 shadow。生产环境默认关闭发送开关，五日通过只取得晋级
资格，不会绕过人工启用步骤。

本地 2026-08-08 已把 group timer 改为 09:15；SG 在部署这次 commit 前仍可能是上次审计的
07:45 unit。以服务器上的 `systemctl cat quant-group-analytics-eod.timer` 为准。

NASDAQ100 改造已于 2026-08-11 部署。NASDAQ100 PIT 任一门禁失败时，08:15 service 可以继续
发布彼此独立的 SP500/MAG7，但 unit 最终应为失败，NASDAQ100 不得前移；08:45 仍需发布可审计的
`INSUFFICIENT` 跨池结论，不能沿用昨天的绿色状态。全美宽基首次链和首日人工验收已完成，
日常 timer 从 2026-08-24 起必须保持 `enabled/active`；连续 5 日门槛只约束网页默认开关，
不允许再用它关闭产生每日观察数据的 timer。

## 3. 查看最近结果和错误

```bash
systemctl show quant-us-daily-refresh.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-market-data.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-factor-research.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-group-analytics-eod.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-paper-trading.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-data-requests.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-premarket-digest.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-momentum-alerts.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-intraday-momentum-monitor.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-us-equity-coverage.service -p Result -p ExecMainStatus -p ActiveState -p MemoryPeak
systemctl show quant-broad-factor-data.service -p Result -p ExecMainStatus -p ActiveState -p MemoryPeak
systemctl show quant-broad-research-readiness.service -p Result -p ExecMainStatus -p ActiveState
systemctl show quant-broad-shadow-observation.service -p Result -p ExecMainStatus -p ActiveState
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
journalctl -u quant-premarket-digest.service -n 200 --no-pager
journalctl -u quant-momentum-alerts.service -n 200 --no-pager
journalctl -u quant-intraday-momentum-monitor.service -n 300 --no-pager
journalctl -u quant-us-equity-coverage.service -n 200 --no-pager
journalctl -u quant-broad-factor-data.service -n 200 --no-pager
journalctl -u quant-broad-research-readiness.service -n 100 --no-pager
journalctl -u quant-broad-shadow-observation.service -n 100 --no-pager
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

独立运维站在 SG 使用 `0.0.0.0:18825` 和另一套认证。它不会出现在主业务页面：

```bash
set -a
. /etc/quant/operations-web.env
set +a
systemctl status quant-operations-watchdog.timer quant-operations-web.service --no-pager
.venv/bin/python scripts/migrate_operations_storage.py verify --json
ss -lntp | grep ':18825'
curl -u "$QUANT_OPS_AUTH_USER:$QUANT_OPS_AUTH_PASSWORD" \
  -sS http://127.0.0.1:18825/healthz
unset QUANT_OPS_AUTH_PASSWORD
```

公网入口为 `http://43.156.89.232:18825/`。无认证访问应返回 `401`。腾讯云安全组需允许 TCP
`18825`；应优先将来源限制为固定办公公网 IP。直接 IP 当前是 HTTP，Basic Auth 不提供传输
加密，长期应迁移到独立域名或独立上游的 HTTPS 反向代理。

统一页面、状态定义和 SG 安装方式见
[`operations_observability.md`](operations_observability.md)。运维 watchdog 不发送 Discord 告警；
既有 Discord 仍只承载盘前、板块轮动和动量等业务消息。

## 5. 手动补跑

先确认没有同名 service 正在运行，再用 systemd 启动，避免绕过环境文件和权限：

```bash
systemctl start quant-us-daily-refresh.service
systemctl start quant-market-data.service
systemctl start quant-factor-research.service
systemctl start quant-group-analytics-eod.service
systemctl start quant-paper-trading.service
systemctl start quant-us-equity-coverage.service
```

任务有依赖顺序。上游失败时不要强行启动下游来制造陈旧结果。

全美宽基只需手工启动 `quant-us-equity-coverage.service`；其余三个 unit 由 `OnSuccess=` 触发。
首次回填期间不要直接启动该日常 service，也不要恢复绑定旧 Security Master 的
`run=20260812T152208Z_57bca7cb`。先按实施文档重建并验证 Security Master，再以新 generation
开始正式 coverage 回填。
日常观察命令为：

```bash
.venv/bin/python scripts/check_broad_shadow_observation.py --json
```

返回 `OBSERVING` 表示当前日验证通过但尚未达到连续 5 日。任何 FAIL 都不得计入；首次回填和 unit
安装步骤见 [`us_broad_factor_research_implementation.md`](us_broad_factor_research_implementation.md)。

NASDAQ100 首次正式发布前先运行候选检查：

```bash
.venv/bin/python scripts/run_data_pipeline.py pit \
  --universe NASDAQ100 --candidate-only --json
```

只有当前成分、10 组官方历史事件和全部质量门禁通过后，才能运行正式 `pit`、行情和研究任务。

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

研究池改造的首次重发顺序和完成门槛见
[`research_universe_redesign_implementation.md`](research_universe_redesign_implementation.md)。

因子数据浏览器部署后还要检查：

```bash
curl -u "$QUANT_USER:$QUANT_PASSWORD" -sS \
  http://127.0.0.1:18823/api/research/factor-data/meta
curl -u "$QUANT_USER:$QUANT_PASSWORD" -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  'http://127.0.0.1:18823/research/factor-data?universe=SP500&factor=MOM_12M&date=latest'
```

随后在浏览器抽查 SP500 正向因子、SP500 负向因子、NASDAQ100 和一个历史退出证券。若正式数据
尚未重发，页面必须显示可理解的无效/未发布状态，不能出现 500 或临时 FMP 回退。完整清单见
[`factor_data_explorer_implementation.md`](factor_data_explorer_implementation.md)。

unit 校验示例：

```bash
systemd-analyze verify \
  /etc/systemd/system/quant-web.service \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-market-data.service \
  /etc/systemd/system/quant-factor-research.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-paper-trading.service \
  /etc/systemd/system/quant-data-requests.service \
  /etc/systemd/system/quant-premarket-digest.service \
  /etc/systemd/system/quant-momentum-alerts.service \
  /etc/systemd/system/quant-intraday-momentum-monitor.service \
  /etc/systemd/system/quant-us-equity-coverage.service \
  /etc/systemd/system/quant-broad-factor-data.service \
  /etc/systemd/system/quant-broad-research-readiness.service \
  /etc/systemd/system/quant-broad-shadow-observation.service \
  /etc/systemd/system/quant-us-equity-coverage.timer
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
  quant-data-requests.timer \
  quant-premarket-digest.timer \
  quant-momentum-alerts.timer \
  quant-intraday-momentum-monitor.timer
```

上面的既有 timer 列表不自动包含全美宽基。首次正式回填、手工完整链和
`systemd-analyze verify` 已成功，生产必须另行保持：

```bash
systemctl enable --now quant-us-equity-coverage.timer
```

运维 registry 将该任务设置为 `enabled_expected=true`。如果 timer 变为 disabled 或 inactive，
watchdog 必须产生告警；`Persistent=true` 不能补偿一个从未启用过、没有历史时间戳的 timer。

## 7. 不能直接做的事

- 不要删除 `data/catalog/quant.duckdb` 或正式 version 目录。
- 不要直接编辑 `outputs/quant_app.sqlite3`。
- 不要在 writer 运行时复制 DuckDB/SQLite 单文件当作一致性备份。
- 不要让回测或模拟盘在缺数时直接调用 FMP。
- 不要在未备份、未发布新 commit、未验收页面前清理 SG 旧目录。
- 不要用 `git reset --hard` 覆盖服务器上尚未确认归属的改动。
