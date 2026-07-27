# 服务器登录后日常速查

> 适用于当前腾讯云服务器：`root`、项目 `/home/projects/quant`、网站环境 `.venv`、
> 定时任务环境 `.venv-worker`。日常只看第 1 节；手机打不开看第 2 节；定时任务看第 3 节；
> 更新代码看第 4 节；网站尚未标准化时才做第 5 节。安装原理和复杂故障请查
> [`root_discord_operations_guide.md`](root_discord_operations_guide.md)。

## 先看结论

- 仓库现在提供 root 网站单元模板 `deploy/systemd/quant-web-root.service`；服务器第一次更新
  到本版本后按第 5 节安装为 `quant-web.service`，以后登录、重启服务器和更新代码都用这个
  固定服务名。
- 根据你提供的 **2026-07-17** 执行日志，服务器当时已经安装三条正式 timer，但只有
  `quant-us-daily-refresh.timer` 和 `quant-group-analytics-eod.timer` 在运行；
  `quant-premarket-digest.timer` 被明确设成了 `disabled/inactive`。如果后来没有重新启用，
  Discord 现在不会自动发送。
- 上述只是历史日志，不是服务器实时状态。当前状态以第 3.1 节两条 `systemctl` 命令为准。
- 普通 Python 代码更新后不用“重装 timer”；oneshot 任务下次会自动使用新代码。长期运行的
  网站需要 `systemctl restart quant-web.service`。

> **第一次开放网页前先处理安全组。** `quant-web.service` 会监听 `0.0.0.0:18823`，而当前
> 页面没有登录认证并包含写接口。先在腾讯云控制台确认 TCP 18823 的来源只允许你自己的
> 可信公网 IP，不能是 `0.0.0.0/0`；或者先配置带认证的 HTTPS 反向代理/私有网络。没有完成
> 其中一种保护时，不要执行下面的 `systemctl enable --now quant-web.service`。

## 1. 登录后最常用的一组命令

完成第 5 节的一次性 Web 标准化后，日常登录只需要下面这些命令：

```bash
cd /home/projects/quant

# 确保网站现在运行，并在服务器重启后自动运行
systemctl enable --now quant-web.service

# 确保收盘刷新、板块计算、盘前双频道日报已启用
systemctl enable --now \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
wei
# 如果只要每天盘前一条，关闭旧的盘中小时提醒
systemctl disable --now quant-momentum-alerts.timer 2>/dev/null || true

# 检查网站和全部定时器
curl -sS -o /dev/null -w 'web HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
systemctl list-timers --all 'quant-*'
```

正常结果：

- `web HTTP 200`；
- 三个正式 timer 出现在表格中，并且有 `NEXT` 时间；
- `quant-momentum-alerts.timer` 为 inactive/disabled，或者服务器根本没有安装它；
- timer 使用 `Persistent=true`，首次启动时可能补跑错过的任务；盘前 sender 仍会检查
  美股交易日、09:20–09:29 美东窗口和防重复状态，不会在窗口外直接发送。

完成 `enable --now` 后，网站和 timer 都由 systemd 托管。退出 SSH 不会关闭它们，服务器重启后
也会自动恢复，所以不需要每天登录执行一遍。

## 2. 怎么让电脑和手机查看多因子页面

### 2.1 启动并检查网站

```bash
systemctl enable --now quant-web.service
systemctl status quant-web.service --no-pager
ss -lntp | grep ':18823'
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
```

应看到：

```text
Active: active (running)
0.0.0.0:18823
HTTP 200
```

浏览器访问：

```text
http://服务器公网IP:18823/
```

如果已经配置 Nginx/Caddy、HTTPS 和域名，应继续使用已有域名，不要额外暴露原始端口。

启动 Web 只会展示磁盘上已有的多因子产物，不会自动重新计算因子。这里的三条 Discord 链路也
不会更新多因子主页；需要刷新主页数据时按长手册第 20.7 节单独运行 SP500 pipeline。

### 2.2 本机正常但其他设备打不开

按以下顺序排查：

1. `curl http://127.0.0.1:18823/` 是否为 200；
2. `ss` 是否显示 `0.0.0.0:18823`；
3. 腾讯云安全组是否允许入站 TCP 18823；
4. TencentOS 的 firewalld/nftables 是否拦截；
5. 浏览器使用的是否为公网 IP，而不是 `10.x/172.16-31.x/192.168.x` 私网地址。

安全提醒：当前 FastAPI 页面 **没有登录认证**，而且包含策略、回测、Watchlist 和模拟盘写接口。
不要把 TCP 18823 对 `0.0.0.0/0` 长期开放。最低要求是让腾讯云安全组只允许你自己的可信公网
IP；需要手机移动网络长期访问时，应使用带认证的 HTTPS 反向代理或私有网络访问方案。

### 2.3 查看网站日志

```bash
journalctl -u quant-web.service -n 200 --no-pager
```

实时跟随：

```bash
journalctl -fu quant-web.service
```

按 `Ctrl+C` 只退出日志查看，不会关闭网站。

## 3. Discord 定时任务有没有、怎么启动

### 3.1 仓库定义的四个 timer

| Timer | 时间 | 作用 | 发 Discord |
|---|---|---|---|
| `quant-us-daily-refresh.timer` | 新加坡 Tue–Sat 07:15 | 更新 US_ACTIVE 和日线 | 否 |
| `quant-group-analytics-eod.timer` | 新加坡 Tue–Sat 07:45 | 生成板块/细分行业产物 | 否 |
| `quant-premarket-digest.timer` | 美东 Mon–Fri 09:20 | 两个频道各一条盘前日报 | 是 |
| `quant-momentum-alerts.timer` | 每小时 `:35` | 旧盘中小时提醒 | 是，可能每天多条 |

仓库有这些模板，不代表服务器此刻一定已经安装或启用。登录服务器后运行：

```bash
systemctl list-unit-files 'quant-*.timer'
systemctl list-timers --all 'quant-*'
```

- 第一条回答“安装了哪些、是否 enabled”；
- 第二条回答“当前是否 active、上次和下次什么时候运行”。

如果三条正式 timer 中任何一条显示 `not-found` 或根本没有列出，不要继续运行
`enable --now`；按长手册第 4～5 节先安装当前 root 服务器版 unit。你提供的 2026-07-17 日志
显示这三条当时已经安装，这个分支主要用于以后重装服务器。

### 3.2 启用每天盘前两条消息所需任务

先完成第 3.4 节不发送预览并确认两个 Webhook 已配置，再执行：

```bash
systemctl enable --now \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer

systemctl disable --now quant-momentum-alerts.timer 2>/dev/null || true
```

再确认盘前发送开关，不显示 Webhook：

```bash
grep '^PREMARKET_DIGEST_ENABLED=' /etc/quant/premarket-digest.env
```

应为：

```text
PREMARKET_DIGEST_ENABLED=true
```

不要执行 `cat /etc/quant/premarket-digest.env`，Webhook URL 等同频道写入密码。

### 3.3 检查三个任务最近一次是否成功

```bash
systemctl show quant-us-daily-refresh.service \
  -p Result -p ExecMainStatus -p ActiveState \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp
systemctl show quant-group-analytics-eod.service \
  -p Result -p ExecMainStatus -p ActiveState \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp
systemctl show quant-premarket-digest.service \
  -p Result -p ExecMainStatus -p ActiveState \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp
```

oneshot 任务成功结束后 `ActiveState=inactive` 是正常的；成功关键是：

```text
Result=success
ExecMainStatus=0
```

同时确认 `ExecMainStartTimestamp` 和 `ExecMainExitTimestamp` 是最近一次预期执行时间；空时间或
很早以前的 `success/0` 不能证明今天真的跑过。

查看盘前发送日志：

```bash
journalctl -u quant-premarket-digest.service -n 100 --no-pager
```

数据刷新还要在日志里确认 `failures=0`，不能只看 systemd 退出码。

### 3.4 第一次启用前先做不发送预览

先在提示中输入即将开盘的真实美股交易日，不是行情数据日期。例如要预览 2026-07-27
开盘前的消息就输入 `2026-07-27`：

```bash
cd /home/projects/quant
read -r -p "请输入即将开盘的美股交易日 (YYYY-MM-DD): " SESSION
.venv-worker/bin/python scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --session "$SESSION"
```

不带 `--send` 就不会联系 Discord。成功应看到两个频道均为 `DRY_RUN`，并且动量两项覆盖率不低于
80%、板块产物覆盖率不低于 98%。

第一次配置 Webhook 或更换频道后，还应运行一次通道测试：

```bash
cd /home/projects/quant
.venv-worker/bin/python scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env \
  --test-send
```

这个命令会向两个频道各发送一条明确标注的测试消息；Webhook 输入不会回显。若使用角色提醒，
提示时也要重新填写角色 ID，留空会清空原角色设置。日常登录不需要重复执行。

## 4. 更新代码后到底要不要“更新 timer”

最重要的结论：

> timer 只保存“什么时候启动哪个 service”。Python 代码更新后，下一次 oneshot service 会直接
> 读取 `/home/projects/quant` 中的新代码。因此普通代码更新 **不需要重新安装 timer，也不需要
> `daemon-reload`**。

不同变化的处理方式：

| 更新内容 | 要 `daemon-reload` | 还需要做什么 |
|---|---:|---|
| Python 业务代码 | 否 | worker 下次自动使用；长期 Web 要重启 |
| `requirements.txt` | 否 | 更新 `.venv` 和 `.venv-worker` |
| `configs/default.yaml` | 否 | worker 下次读取；Web 重启后读取 |
| `/etc/quant/*.env` | 否 | worker 下次读取；长期 Web 需要重启 |
| `.service` 文件 | 是 | 人工合并 root 本机版，不能直接覆盖 |
| `.timer` 文件/时间 | 是 | 合并后 restart 对应 timer，重新检查 `NEXT` |

### 4.1 普通代码更新的安全流程

先确认 Git 工作区干净：

```bash
cd /home/projects/quant
git status --short
```

如果有输出，先停下来确认这些是不是服务器本地修改；不要使用 `git reset --hard` 覆盖。

先确保旧小时提醒关闭，再把当前 active 的正式 timer 名称保存到临时文件，并只暂停这些任务：

```bash
systemctl disable --now quant-momentum-alerts.timer 2>/dev/null || true
systemctl list-units \
  --type=timer --state=active --no-legend --plain 'quant-*' | \
  awk '{print $1}' > /tmp/quant-active-timers.before-update
cat /tmp/quant-active-timers.before-update
xargs -r systemctl stop < /tmp/quant-active-timers.before-update

systemctl is-active \
  quant-us-daily-refresh.service \
  quant-group-analytics-eod.service \
  quant-premarket-digest.service
```

如果某个 service 显示 `activating`，等待它自然结束，不要在 Discord 请求中途强杀。全部 inactive
后停止网站并更新：

```bash
systemctl stop quant-web.service
cd /home/projects/quant
BEFORE=$(git rev-parse HEAD)
git pull --ff-only

git diff --name-only "$BEFORE" HEAD -- requirements.txt
git diff --name-only "$BEFORE" HEAD -- deploy/systemd
```

如果第一条 `git diff` 列出 `requirements.txt`，才在维护窗口更新两个环境；没有输出就整段
跳过，避免每次更新都让 `>=` 依赖发生漂移：

```bash
/home/projects/quant/.venv/bin/python -m pip install -r requirements.txt
/home/projects/quant/.venv/bin/python -m pip check
/home/projects/quant/.venv-worker/bin/python -m pip install -r requirements.txt
/home/projects/quant/.venv-worker/bin/python -m pip check
```

四条命令必须全部成功才能继续。如果 `git pull` 或安装依赖失败，停止后续迁移并先尝试恢复：

```bash
systemctl start quant-web.service
xargs -r systemctl start < /tmp/quant-active-timers.before-update
```

如果 Web 恢复失败，保留 `BEFORE` 值，不要执行 `git reset --hard`；查看
`journalctl -u quant-web.service -n 200 --no-pager`，修复依赖后再启动。这里不自动回滚，是为了
避免覆盖服务器上无法恢复的本地文件。

如果上面针对 `deploy/systemd` 的 `git diff` 没有输出，说明 unit 没变化，直接恢复：

```bash
systemctl start quant-web.service
xargs -r systemctl start < /tmp/quant-active-timers.before-update

curl -sS -o /dev/null -w 'web HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
systemctl list-timers --all 'quant-*'
```

临时文件保证只恢复更新前确实处于 active 的 timer，不会因为更新代码顺手启用之前关闭的
发送任务；下次更新时它会被重新生成并覆盖。

### 4.2 如果 `deploy/systemd/` 有变化

`quant-web-root.service` 可以按本服务器固定路径重新安装：

```bash
install -m 644 \
  /home/projects/quant/deploy/systemd/quant-web-root.service \
  /etc/systemd/system/quant-web.service
```

其他 worker 模板仍采用 `/opt/quant + quant` 的通用环境，而服务器是
`/home/projects/quant + root + .venv-worker`。不能把模板直接覆盖到 `/etc/systemd/system`；应先
`diff -u`，再把真正新增的参数人工合并到 root 本机版。详细步骤见长手册第 15 节。

完成 unit 合并后：

```bash
systemd-analyze verify \
  /etc/systemd/system/quant-web.service \
  /etc/systemd/system/quant-*.service \
  /etc/systemd/system/quant-*.timer
systemctl daemon-reload
systemctl restart quant-web.service
xargs -r systemctl restart < /tmp/quant-active-timers.before-update
systemctl list-timers --all 'quant-*'
```

重点查看 `NEXT` 是否符合第 3.1 节的时间。

## 5. 第一次把网站统一成 `quant-web.service`

这一节只做一次。以后开站、关站、重启都只使用 `systemctl`。

先检查 18823 是否已经由旧进程占用：

```bash
ss -lntp | grep ':18823'
systemctl status quant-web.service --no-pager
```

如果端口已有进程，但 `quant-web.service` 不存在，不要直接启动第二份。先用长手册第 20.3 节识别
旧进程属于哪个 service、Supervisor、screen/tmux 或手工 Shell，再安排迁移。

端口没有进程时，安装网站环境文件和 unit。下面的判断会保留已经存在的
`/etc/quant/web.env`，不会覆盖真实配置：

```bash
cd /home/projects/quant
install -d -m 700 data/cache/matplotlib outputs logs
install -d -m 700 /etc/quant
if [ ! -e /etc/quant/web.env ]; then
  install -m 600 deploy/systemd/web.env.example /etc/quant/web.env
fi
vi /etc/quant/web.env
install -m 644 deploy/systemd/quant-web-root.service \
  /etc/systemd/system/quant-web.service

systemd-analyze verify /etc/systemd/system/quant-web.service
systemctl daemon-reload
systemctl enable --now quant-web.service
```

在 `vi /etc/quant/web.env` 中至少填写真实 `FMP_API_KEY`。不要放 Discord Webhook。需要显示
`/group-analytics` 时，再把两个 `GROUP_ANALYTICS_*` 开关都设为 true，并确保网站 `.venv` 已安装
`requirements.txt`。

安装后检查：

```bash
systemctl status quant-web.service --no-pager
journalctl -u quant-web.service -n 100 --no-pager
curl -sS -o /dev/null -w 'home HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
curl -sS -o /dev/null -w 'factors HTTP %{http_code}\n' \
  http://127.0.0.1:18823/factors
```

## 6. 最常见的四个误区

1. 不需要 `conda activate`。systemd 使用 `.venv` 和 `.venv-worker` 的绝对 Python 路径。
2. `systemctl enable --now TIMER` 会启用并启动 timer；由于 `Persistent=true`，它可能补跑
   错过的触发，但不等于绕过交易日、发送窗口和防重复保护的手工强制发送。
3. 普通 Python 更新不用 `daemon-reload`；只有 unit 文件变化才需要。
4. 不要删除 `outputs/premarket_digest/state.sqlite3` 来强制重发，否则会丢失防重复记录。
