# 服务器多因子网站与 Discord 日报维护手册（root 运行版）

> 适用环境：腾讯云 TencentOS，新加坡时区，项目目录为
> `/home/projects/quant`，systemd 服务以 `root` 用户运行，任务专用 Python 环境为
> `/home/projects/quant/.venv-worker`。
>
> 这份文档以“能自己维护”为目标。每组命令都说明用途、是否需要重复执行、正常现象，
> 以及是否会真实发送 Discord 消息。

> **日常不需要先读这份长手册。** 登录服务器后如何启动网站、检查手机访问、启用 Discord
> 定时任务，以及代码更新后是否需要重载 timer，请直接看日常速查版
> [`server_daily_runbook.md`](server_daily_runbook.md)。本文保留首次安装、深度解释和故障恢复细节。

如果只想日常维护，不必每次从头读到尾：

- 只想登录后立即启动网站和定时任务：读
  [`server_daily_runbook.md`](server_daily_runbook.md)；
- 想理解整体结构：读第 1～3 节；
- 想知道以前安装命令做了什么：读第 4～5 节；
- 数据或板块任务失败：读第 6～7、13～14 节；
- 配置 Discord：读第 8 节；
- 预览或手动发送：读第 9～10 节；
- 查看、暂停或恢复自动任务：读第 11～12 节；
- 更新代码：读第 15 节；
- 平时只想复制最常用命令：直接看第 19 节。
- 想维护现有多因子项目页面，或开放“行业/主题”页面：读第 20 节。

## 0. 根据 2026-07-17 执行日志判断的状态

你提交的服务器日志已经证明：

- `.venv-worker` 的关键依赖可以正常导入；
- US_ACTIVE 刷新完成，共处理 2895 个标的，`failures=0`；
- `us_active.premarket.json` 已发布，数据 session 为 `2026-07-16`；
- sector 和 sub-industry 两层正式产物都发布成功；
- `2026-07-17` 日报 dry-run 成功，动量覆盖率和板块覆盖率都通过发送门槛；
- Discord 配置脚本最后成功写入配置。因为使用了 `--test-send`，这也表示两个 Webhook
  都返回了成功确认；
- 当时 `quant-us-daily-refresh.timer` 与 `quant-group-analytics-eod.timer` 已启用；
- 当时 `quant-premarket-digest.timer` 仍未启用，所以只会自动更新数据，不会自动发日报。

这是对当时日志的判断，不代表服务器此刻一定仍是相同状态。随时用第 11.2 和 11.3 节的
命令重新确认，不依赖记忆。

## 1. 先理解系统在做什么

整套任务不是一个一直运行的 Python 程序，而是三个按顺序执行的短任务：

```text
美股收盘后
    |
    |  新加坡时间 Tue..Sat 07:15
    v
刷新 US_ACTIVE 股票池和日线数据
quant-us-daily-refresh.service
    |
    |  新加坡时间 Tue..Sat 07:45
    v
计算 SP500 板块和细分行业涨跌
quant-group-analytics-eod.service
    |
    |  美东时间 Mon..Fri 09:20
    v
生成两份日报并发送到两个 Discord 频道
quant-premarket-digest.service
```

此外还有一条与定时任务并列、长期运行的网站链路：

```text
FastAPI 多因子网站（默认端口 18823）
    |
    |-- 读取 outputs/universes/*/factors/     因子、IC、五分位回测
    |-- 读写 outputs/strategies/              策略定义
    |-- 读写 outputs/backtests/               回测任务
    |-- 读写 outputs/papertrading/            模拟盘
    |-- 读写 outputs/watchlists/              股票池
    |-- 读取 outputs/.../group_analytics/     行业/主题页面（开关开启后）
    `-- 读取 data/raw/ohlcv/                  动量诊断页面
```

网站不是这三个 timer 中的一个，也不应该由 Discord worker 启动。网站负责展示和用户操作；
定时 worker 负责准备数据和发消息。两边共享 `data/`、`outputs/`，但核心算法和运行进程相互
独立。

对应关系如下：

| systemd timer | 被唤醒的 service | 作用 | 会发 Discord 吗 |
|---|---|---|---|
| `quant-us-daily-refresh.timer` | `quant-us-daily-refresh.service` | 刷新股票池与日线 | 不会 |
| `quant-group-analytics-eod.timer` | `quant-group-analytics-eod.service` | 计算板块和细分行业 | 不会 |
| `quant-premarket-digest.timer` | `quant-premarket-digest.service` | 生成并发送两份盘前日报 | **会** |

仓库现在提供当前 root 部署专用的 `deploy/systemd/quant-web-root.service`，安装到服务器时
统一命名为 `quant-web.service`。旧服务器可能仍由其他 systemd 名称、Supervisor、
screen/tmux 或手工 Shell 启动；首次迁移前用第 20.3 节的方法确认，避免同一端口启动两份。

这里的概念是：

- `timer` 是闹钟，只负责决定什么时候唤醒任务；
- `service` 是 systemd 保存的一条正式运行命令；
- `scripts/*.py` 是真正执行工作的 Python 程序；
- `journalctl` 用于查看这些程序的运行日志；
- `/etc/quant/*.env` 保存密钥和开关；
- `outputs/premarket_digest/state.sqlite3` 记录某天某频道是否已经发送，防止重复消息。

三个 service 都是 `Type=oneshot`。因此任务成功结束后显示 `inactive` 是正常的，
不代表失败。判断成功与否应查看 `Result=success` 和 `ExecMainStatus=0`。

## 2. 本服务器的固定路径

后面的命令都基于以下路径，不要混用旧文档中的 `/opt/quant`：

| 内容 | 当前服务器路径 |
|---|---|
| 项目目录 | `/home/projects/quant` |
| 网站原有 Python 环境 | `/home/projects/quant/.venv` |
| Discord/分析任务 Python 环境 | `/home/projects/quant/.venv-worker` |
| 网站启动入口 | `/home/projects/quant/scripts/run_mvp.py --serve-only` |
| 网站 systemd 单元 | `/etc/systemd/system/quant-web.service` |
| 网站环境文件 | `/etc/quant/web.env` |
| 网站默认监听端口 | `18823` |
| FMP 配置 | `/etc/quant/momentum-alerts.env` |
| Discord 双频道配置 | `/etc/quant/premarket-digest.env` |
| systemd 单元 | `/etc/systemd/system/quant-*.service`、`quant-*.timer` |
| 股票池清单 | `/home/projects/quant/data/raw/universe/us_active.premarket.json` |
| 板块产物 | `/home/projects/quant/outputs/universes/SP500/group_analytics/` |
| Discord 去重数据库 | `/home/projects/quant/outputs/premarket_digest/state.sqlite3` |

命令行前面即使显示 `(.venv)`，systemd 也不会使用当前激活的环境。正式任务都写了
`.venv-worker/bin/python` 的绝对路径，所以不需要 `conda activate`，也不需要先
`source .venv-worker/bin/activate`。

## 3. 命令风险标记

本文使用以下标记：

- **只读**：只查看状态，不改变数据；
- **生成数据**：会更新行情缓存或分析产物，但不会联系 Discord；
- **测试发送**：会向 Discord 发送明确标注的测试消息；
- **正式发送**：会向正式频道发送真实日报；
- **一次性配置**：安装时运行一次，平时不需要重复运行。

## 4. 首次安装时执行过的命令

这一节主要用于理解和灾难恢复。服务器已经配置好后，不要每天重复执行。

### 4.1 进入项目目录

```bash
cd /home/projects/quant
```

- 类型：只改变当前终端所在目录；
- `cd` 是 change directory；
- 后续的 `scripts/...`、`outputs/...` 等相对路径都从这里开始计算；
- 不会修改文件，不会发送消息。

检查自己当前在哪个目录：

```bash
pwd
```

正常输出：

```text
/home/projects/quant
```

### 4.2 创建独立 worker Python 环境

```bash
/usr/bin/python3.11 -m venv /home/projects/quant/.venv-worker
```

- 类型：一次性配置；
- 使用系统 Python 3.11 创建一个隔离环境；
- `.venv-worker` 专门给定时任务使用；
- 独立环境可以避免安装任务依赖时影响正在运行的网站 `.venv`；
- 已存在且可用时不需要重复创建。

安装依赖：

```bash
/home/projects/quant/.venv-worker/bin/python -m pip install --upgrade pip
/home/projects/quant/.venv-worker/bin/python -m pip install -r /home/projects/quant/requirements.txt
```

第一条升级 worker 环境中的 `pip`；第二条读取项目的 `requirements.txt`，安装项目声明的
Python 库。它们只修改 `.venv-worker`，不会修改系统 Python，也不会发送 Discord。

检查依赖内部是否冲突：

```bash
/home/projects/quant/.venv-worker/bin/python -m pip check
```

正常输出：

```text
No broken requirements found.
```

注意：`pip check` 只检查“已经安装的包之间有没有冲突”，不能证明所有项目依赖都已安装。
因此还要执行关键导入检查：

```bash
/home/projects/quant/.venv-worker/bin/python -c "import exchange_calendars, pandas, pyarrow, requests; print('worker environment OK')"
```

它依次导入交易所日历、数据处理、Parquet 和 HTTP 库。正常输出：

```text
worker environment OK
```

### 4.3 创建运行目录

```bash
mkdir -p \
  /home/projects/quant/data/cache/matplotlib \
  /home/projects/quant/outputs/premarket_digest \
  /home/projects/quant/logs
```

- 类型：一次性配置；
- `mkdir` 表示创建目录；
- `-p` 表示父目录不存在时一起创建，目录已经存在时不报错；
- 反斜杠 `\` 表示下一行仍属于同一条命令；
- 不要在反斜杠续行中间插入注释，否则 Shell 可能把后面的内容错误解析成另一条命令。

设置敏感输出目录只允许 root 访问：

```bash
chmod 700 /home/projects/quant/data/cache/matplotlib
chmod 700 /home/projects/quant/outputs/premarket_digest
```

`700` 的含义是：所有者 root 可读、可写、可进入，其他用户没有权限。

### 4.4 创建配置文件

```bash
mkdir -p /etc/quant
chmod 700 /etc/quant
```

创建 `/etc/quant` 并限制为 root 访问。这个目录存放 FMP API Key 和 Discord Webhook，
不应位于 Git 仓库中。

从仓库的示例文件复制出服务器配置：

```bash
install -m 600 \
  /home/projects/quant/deploy/systemd/momentum-alerts.env.example \
  /etc/quant/momentum-alerts.env

install -m 600 \
  /home/projects/quant/deploy/systemd/premarket-digest.env.example \
  /etc/quant/premarket-digest.env
```

`install` 在这里不是安装软件，而是“复制文件并同时设置权限”。`-m 600` 表示只有 root
可以读写。第一份文件保存 FMP 配置，第二份保存两个 Discord Webhook。

编辑 FMP 配置：

```bash
vi /etc/quant/momentum-alerts.env
```

最基本的 `vi` 操作：按 `i` 进入编辑，修改完成后按 `Esc`，输入 `:wq` 并回车保存退出；
如果想放弃本次修改，按 `Esc` 后输入 `:q!` 并回车。

至少需要正确填写：

```dotenv
FMP_API_KEY=你的真实FMP密钥
```

编辑 Discord 配置通常不直接使用 `vi`，而使用第 8 节的安全交互脚本，避免 Webhook
出现在屏幕回显和 Shell 历史中。

## 5. systemd 安装命令为什么这么长

仓库自带的模板默认是 `/opt/quant + quant 用户`，而当前服务器实际使用
`/home/projects/quant + root 用户 + .venv-worker`。所以首次安装时需要复制模板，再修改
服务器上的副本。

### 5.1 复制三个 service 和三个 timer

```bash
install -m 644 /home/projects/quant/deploy/systemd/quant-us-daily-refresh.service \
  /etc/systemd/system/
install -m 644 /home/projects/quant/deploy/systemd/quant-us-daily-refresh.timer \
  /etc/systemd/system/
install -m 644 /home/projects/quant/deploy/systemd/quant-group-analytics-eod.service \
  /etc/systemd/system/
install -m 644 /home/projects/quant/deploy/systemd/quant-group-analytics-eod.timer \
  /etc/systemd/system/
install -m 644 /home/projects/quant/deploy/systemd/quant-premarket-digest.service \
  /etc/systemd/system/
install -m 644 /home/projects/quant/deploy/systemd/quant-premarket-digest.timer \
  /etc/systemd/system/
```

- 类型：一次性配置；
- `644` 表示 root 可修改，所有用户可读取；
- systemd 必须从 `/etc/systemd/system` 读取本机服务；
- 复制文件本身不会启动任务，也不会发送消息。

不要使用通配符把所有 `quant-*` 单元都安装并启用，因为仓库中还有旧的
`quant-momentum-alerts.timer`，它是每小时盘中提醒，不是每天一条的盘前日报。

### 5.2 把模板改成当前服务器路径和 root 用户

```bash
sed -i \
  -e 's/^User=quant$/User=root/' \
  -e 's/^Group=quant$/Group=root/' \
  -e 's#/opt/quant/.venv/bin/python#/home/projects/quant/.venv-worker/bin/python#g' \
  -e 's#/opt/quant#/home/projects/quant#g' \
  -e 's/^ProtectHome=true$/ProtectHome=read-only/' \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-premarket-digest.service
```

这条命令使用 `sed` 原地修改三个 service：

- `sed -i`：直接修改文件；
- `User=quant -> User=root`：任务使用 root 身份；
- `Group=quant -> Group=root`：任务使用 root 组；
- `.venv -> .venv-worker`：使用独立 worker Python；
- `/opt/quant -> /home/projects/quant`：改成真实项目路径；
- `ProtectHome=true -> ProtectHome=read-only`：允许任务读取 `/home`。

最后一点很重要。`ProtectHome=true` 会让 systemd 任务完全看不到 `/home/projects/quant`，
即使 `User=root` 也一样。

允许任务写入指定目录：

```bash
sed -i '/^ProtectHome=read-only$/a ReadWritePaths=/home/projects/quant/data /home/projects/quant/outputs /home/projects/quant/logs /home/projects/quant/runlog' \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-premarket-digest.service
```

这条命令在 `ProtectHome=read-only` 后增加 `ReadWritePaths`。含义是：项目代码保持只读，
但允许服务写行情、分析产物、日志和现有 `runlog` 路径。

禁止 Python 尝试在只读源码目录生成缓存：

```bash
sed -i '/^Environment=PYTHONUNBUFFERED=1$/a Environment=PYTHONDONTWRITEBYTECODE=1' \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-premarket-digest.service
```

`PYTHONDONTWRITEBYTECODE=1` 表示不在源码旁生成 `__pycache__/*.pyc`。

### 5.3 检查并让 systemd 重新读取配置

```bash
systemd-analyze verify \
  /etc/systemd/system/quant-us-daily-refresh.service \
  /etc/systemd/system/quant-group-analytics-eod.service \
  /etc/systemd/system/quant-premarket-digest.service
```

- 类型：只读检查；
- 检查 unit 文件的语法和引用关系；
- 没有输出通常表示通过；
- 如果只看到其他系统服务，例如 `tat_agent.service` 的旧 `/var/run` 警告，而没有
  `quant-*` 错误，不影响本项目。

```bash
systemctl daemon-reload
```

告诉正在运行的 systemd 重新读取 `/etc/systemd/system`。修改任何 service/timer 文件后
都必须运行一次，但它不会自动启动服务。

查看 systemd 最终读取到的内容：

```bash
systemctl cat --no-pager quant-us-daily-refresh.service
systemctl cat --no-pager quant-group-analytics-eod.service
systemctl cat --no-pager quant-premarket-digest.service
```

`--no-pager` 表示直接输出，不进入翻页器，避免误按 `Ctrl+Z` 后出现 `Stopped` 的后台作业。
应确认看到：

```ini
User=root
Group=root
WorkingDirectory=/home/projects/quant
ProtectHome=read-only
```

以及 Python 路径：

```text
/home/projects/quant/.venv-worker/bin/python
```

## 6. 数据刷新命令

### 6.1 手动启动正式刷新 service

```bash
systemctl start --no-block quant-us-daily-refresh.service
```

- 类型：生成数据；不会发送 Discord；
- `start`：立即启动，不等待 timer；
- `--no-block`：命令立即返回终端，任务留在后台运行；
- 不要与 `scripts/run_mvp.py --update` 同时执行，因为两者可能同时写行情缓存。

实时查看日志：

```bash
journalctl -fu quant-us-daily-refresh.service
```

- `-f`：持续跟随新日志，类似实时监控；
- `-u`：只看指定 unit；
- 按 `Ctrl+C` 只会退出日志界面，不会终止后台刷新任务。

一次性查看最近 100 行：

```bash
journalctl -u quant-us-daily-refresh.service -n 100 --no-pager
```

- `-n 100`：最近 100 行；
- `--no-pager`：直接输出后返回终端。

检查 service 最终结果：

```bash
systemctl show quant-us-daily-refresh.service \
  -p ActiveState \
  -p Result \
  -p ExecMainStatus
```

成功结束的 oneshot 正常表现是：

```text
Result=success
ExecMainStatus=0
ActiveState=inactive
```

检查关键股票池清单：

```bash
ls -lh /home/projects/quant/data/raw/universe/us_active.premarket.json
```

`ls -lh` 显示文件权限、大小和更新时间。文件必须存在，更新时间应对应最近一次刷新。

从日志筛选关键结果：

```bash
journalctl -u quant-us-daily-refresh.service --no-pager | \
  grep -E 'published_universe_manifest|US_ACTIVE refresh|done sources|failures|missing='
```

- `|` 把左边的日志交给右边的 `grep`；
- `grep -E` 只保留匹配这些关键词的行；
- `published_universe_manifest` 证明清单已发布；
- `target=YYYY-MM-DD` 应是最近完成的美股交易日；
- `failures=0` 是最理想结果。

### 6.2 Python 刷新命令中的参数

service 内部实际执行的核心命令是：

```bash
/home/projects/quant/.venv-worker/bin/python \
  /home/projects/quant/scripts/refresh_us_active.py \
  --env-file /etc/quant/momentum-alerts.env \
  --workers 6 \
  --force-universe \
  --stocks-only \
  --min-current-dollar-volume-m 5 \
  --market-symbol QQQ \
  --market-symbol SPY \
  --skip-precompute
```

参数含义：

- `--env-file`：安全读取 FMP Key；
- `--workers 6`：最多并发处理 6 只股票；
- `--force-universe`：重新获取股票池，不只使用旧股票池；
- `--stocks-only`：排除 ETF 等非股票资产；
- `--min-current-dollar-volume-m 5`：只保留当前日成交额至少 500 万美元的股票；
- `--market-symbol QQQ`：额外准备 QQQ，供动量市场过滤使用；
- `--market-symbol SPY`：额外准备 SPY，供板块相对收益计算使用；
- `--skip-precompute`：本任务只准备原始数据，不在此处重复做其他预计算。

日常更推荐使用 `systemctl start ...service`，因为这样会使用与定时任务完全相同的命令和
日志系统。

## 7. 板块和细分行业计算命令

### 7.1 小规模 smoke test

```bash
cd /home/projects/quant

env \
  GROUP_ANALYTICS_ENABLED=true \
  GROUP_ANALYTICS_WEB_ENABLED=false \
  .venv-worker/bin/python \
  scripts/run_group_analytics.py \
  --env-file /etc/quant/momentum-alerts.env \
  --mode eod \
  --universe SP500 \
  --taxonomy FMP \
  --level all \
  --asof latest \
  --limit 50
```

- 类型：生成临时计算结果，但不会发布正式 `latest_success.json`，不会发送 Discord；
- `env KEY=value`：只为这一条命令临时设置环境变量；
- `GROUP_ANALYTICS_ENABLED=true`：允许板块分析 writer 工作；
- `GROUP_ANALYTICS_WEB_ENABLED=false`：不开放或改变网站入口；
- `--mode eod`：使用收盘后的最终日线；
- `--universe SP500`：使用标普 500 成分股；
- `--taxonomy FMP`：使用 FMP 的板块/行业分类；
- `--level all`：同时计算 sector 和 sub-industry；
- `--asof latest`：自动选择最近已完成交易日；
- `--limit 50`：只取前 50 只股票做快速测试，并隐含 dry-run。

成功时两个层级都应显示 `status: SUCCESS`，总结中应是 `failed: 0`。

### 7.2 正式生成板块产物

```bash
systemctl start --no-block quant-group-analytics-eod.service
journalctl -fu quant-group-analytics-eod.service
```

第一条在后台启动正式分析，第二条跟随日志。它不会发送 Discord。成功日志中应看到两级
`published: true`：

```text
sector        SUCCESS
sub_industry  SUCCESS
```

检查两个正式指针：

```bash
find /home/projects/quant/outputs/universes/SP500/group_analytics/FMP \
  -path '*/eod/latest_success.json' \
  -print
```

`find` 在指定目录下查找；`-path` 限定路径模式；`-print` 输出匹配结果。正常应同时出现：

```text
.../sector/eod/latest_success.json
.../sub_industry/eod/latest_success.json
```

## 8. Discord 配置命令

```bash
cd /home/projects/quant

.venv-worker/bin/python \
  scripts/configure_premarket_discord.py \
  --env-file /etc/quant/premarket-digest.env \
  --test-send
```

- 类型：**测试发送**；会在两个 Discord 频道各发送一条测试消息；
- 脚本依次要求输入两个 Webhook，输入不会回显；
- 两个 URL 必须是不同 Webhook；
- 角色 ID 可以留空，初次部署建议留空；
- `--test-send` 会真实验证两个频道；
- 只有两个 Webhook 都验证成功，才会写入配置并设置
  `PREMARKET_DIGEST_ENABLED=true`；
- 成功时显示“配置已写入……权限为 600”。

如果重新运行时直接回车，表示保留环境文件中原来的 Webhook。若文件中还没有旧值，留空
就会得到“两个频道都必须配置独立 Webhook；未写入任何更改”。

安全检查开关，不显示 Webhook 内容：

```bash
grep '^PREMARKET_DIGEST_ENABLED=' /etc/quant/premarket-digest.env
```

正常应为：

```text
PREMARKET_DIGEST_ENABLED=true
```

`grep` 只输出这一行，不会输出两个 Webhook。不要运行 `cat /etc/quant/premarket-digest.env`
后把屏幕内容复制到聊天或工单中。

## 9. 预览日报：绝对不会发送

```bash
cd /home/projects/quant

.venv-worker/bin/python \
  scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --session 2026-07-20
```

- 类型：只生成预览；**不会联系 Discord**；
- 不带 `--send` 就永远是 dry-run；
- `--session` 表示“即将开盘的美股交易日”，不是数据日期；
- 对 `2026-07-20` 的预览会自动使用上一实际交易日 `2026-07-17` 的收盘数据；
- 周末、节假日和周一回看日期由 XNYS 交易日历计算；
- 预览文件写入 `outputs/premarket_digest/dry_runs/`。

成功结果应包含：

```text
status = COMPLETED
momentum status = DRY_RUN
sector-rotation status = DRY_RUN
```

还应确认：

- 动量 `exact_asof_coverage >= 0.80`；
- 动量 `evaluable_history_coverage >= 0.80`；
- 板块 `available_levels` 同时包含 `sector` 和 `sub_industry`；
- 板块 `partial=false`；
- 板块数据覆盖率至少 98%；
- 两份日报的 source session 相同。

## 10. 手动真实发送

### 10.1 发送当前美东日期的两个频道

```bash
cd /home/projects/quant

.venv-worker/bin/python \
  scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --send \
  --allow-outside-window
```

- 类型：**正式发送**；
- `--send` 明确允许联系 Discord；
- `--allow-outside-window` 明确允许在美东 09:20–09:29 之外手动发送；
- 没有 `--session` 时，目标日期取当前美东日期；
- 如果当前美东日期是周末或交易所休市日，会返回 `SKIPPED_NON_SESSION`，不会发送；
- 默认 `--channel all`，所以会发送动量和板块两条消息。

### 10.2 明确指定某个开盘日

```bash
cd /home/projects/quant

.venv-worker/bin/python \
  scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --send \
  --allow-outside-window \
  --session 2026-07-20 \
  --allow-historical-send
```

- 类型：**正式发送**；
- `--session 2026-07-20`：明确这条日报对应 7 月 20 日开盘；
- `--allow-historical-send`：对任何显式日期做第二次人工确认。这个参数名虽然写
  `historical`，但脚本要求所有“手工指定日期的真实发送”都必须带它；
- 发送前应先用第 9 节不带 `--send` 的命令预览同一 session。

成功时两个结果都应是：

```json
"status": "SENT"
```

并包含 Discord 返回的 `message_id`。总的 `exit_code` 应为 `0`。

### 10.3 只发送动量频道

在第 10.2 节命令末尾增加：

```bash
--channel momentum
```

完整示例：

```bash
.venv-worker/bin/python scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --send --allow-outside-window \
  --session 2026-07-20 --allow-historical-send \
  --channel momentum
```

### 10.4 只发送板块轮动频道

```bash
.venv-worker/bin/python scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --send --allow-outside-window \
  --session 2026-07-20 --allow-historical-send \
  --channel sector-rotation
```

### 10.5 为什么重复执行不会重复发

第一次成功发送后，程序会在下面的数据库记录状态：

```text
/home/projects/quant/outputs/premarket_digest/state.sqlite3
```

唯一键包含“目标开盘日 + 频道”。再次执行相同 session 和频道时，正常结果是：

```text
SKIPPED_ALREADY_SENT
```

这不是错误，而是防重复保护。不要为了重发而删除 SQLite 文件；删除它还会丢失其他日期
和频道的发送记录。

如果状态是 `UNKNOWN`，表示 HTTP 请求可能已经到达 Discord，但本机没有拿到可靠确认。
这时先人工查看频道，不能直接重试。恢复参数 `--retry-unknown` 只应在确认频道没有消息后，
针对单一 `--session` 和单一 `--channel` 使用。

## 11. 自动发送定时器

### 11.1 启用三个 timer

```bash
systemctl enable --now quant-us-daily-refresh.timer
systemctl enable --now quant-group-analytics-eod.timer
systemctl enable --now quant-premarket-digest.timer
```

- 类型：改变长期运行配置；第三条会使未来到点时真实发送；
- `enable`：创建开机自动启用关系；
- `--now`：无需重启服务器，立即让 timer 进入等待状态；
- 三个 timer 都设置了 `Persistent=true`，如果停用期间错过过触发，启动时可能立刻补跑；
- 盘前 sender 即使被补跑，也会再次检查美股交易日、09:20–09:29 美东窗口和防重复状态，
  窗口外不会直接发送；
- 应先完成 dry-run 和两个 Webhook 测试，再启用第三条。

### 11.2 查看是否已启用

```bash
systemctl is-enabled quant-us-daily-refresh.timer
systemctl is-enabled quant-group-analytics-eod.timer
systemctl is-enabled quant-premarket-digest.timer
```

`enabled` 表示服务器重启后仍会自动启用。

```bash
systemctl is-active quant-us-daily-refresh.timer
systemctl is-active quant-group-analytics-eod.timer
systemctl is-active quant-premarket-digest.timer
```

`active` 表示 timer 当前正在等待下一次触发。timer 的 `active` 与 oneshot service 结束后的
`inactive` 是两件不同的事。

### 11.3 查看下一次时间

```bash
systemctl list-timers --all \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

- `NEXT`：下一次计划时间；
- `LEFT`：距离执行还有多久；
- `LAST`：上一次执行时间；
- `PASSED`：距离上次执行过去多久；
- `ACTIVATES`：到点后启动哪个 service；
- `--all` 会同时显示未激活 timer，便于发现 `disabled/inactive`。

发送 timer 固定使用 `09:20 America/New_York`，因此自动适配美国夏令时：

- 夏令时约为新加坡 21:20；
- 冬令时约为新加坡 22:20。

### 11.4 暂停与恢复自动发送

只暂停 Discord，不影响数据刷新和板块计算：

```bash
systemctl disable --now quant-premarket-digest.timer
```

- `disable`：取消开机自动启用；
- `--now`：同时停止当前 timer 的等待；
- 不会删除配置、历史数据或 outbox；
- 不会中断一个已经开始发送的 service。

恢复：

```bash
systemctl enable --now quant-premarket-digest.timer
```

暂停全部三个自动任务：

```bash
systemctl disable --now \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

恢复全部三个：

```bash
systemctl enable --now \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

## 12. 为什么不要随时 `systemctl start quant-premarket-digest.service`

正式 service 保存的命令包含：

```text
--send --scheduled
```

`--scheduled` 强制要求：

1. 当前美东日期是 XNYS 交易日；
2. 当前时间在美东 09:20–09:29；
3. 数据日期和覆盖率门槛全部通过。

因此在其他时间运行：

```bash
systemctl start quant-premarket-digest.service
```

通常只会得到 `SKIPPED_OUTSIDE_WINDOW`。这是防止服务器重启后在开盘以后补发“盘前消息”的
安全设计。需要随时手动发，使用第 10 节带 `--allow-outside-window` 的 Python 命令。

## 13. 日常查看状态和日志

### 13.1 一眼查看三个 timer

```bash
systemctl list-timers --all 'quant-*'
```

这会显示所有以 `quant-` 开头的 timer。需要确认旧的
`quant-momentum-alerts.timer` 没有被误启用。如果目标是每天盘前一条，旧 timer 应保持
`disabled/inactive`。

### 13.2 查看最近一次 service 结果

```bash
systemctl show quant-us-daily-refresh.service \
  -p Result -p ExecMainStatus -p ActiveState

systemctl show quant-group-analytics-eod.service \
  -p Result -p ExecMainStatus -p ActiveState

systemctl show quant-premarket-digest.service \
  -p Result -p ExecMainStatus -p ActiveState
```

成功的 oneshot 通常是：

```text
Result=success
ExecMainStatus=0
ActiveState=inactive
```

### 13.3 查看指定日期之后的日志

```bash
journalctl -u quant-premarket-digest.service \
  --since '2026-07-20 00:00:00' \
  --no-pager
```

`--since` 只查看给定时间之后的日志。日期按服务器本地时区解释。

查看最近 200 行：

```bash
journalctl -u quant-premarket-digest.service -n 200 --no-pager
```

实时跟随下一次自动发送：

```bash
journalctl -fu quant-premarket-digest.service
```

### 13.4 常见结果含义

| 状态 | 含义 | 应对方式 |
|---|---|---|
| `SENT` | Discord 已返回明确消息 ID | 正常 |
| `DRY_RUN` | 只生成预览，没有联系 Discord | 正常 |
| `SKIPPED_ALREADY_SENT` | 当天该频道已发送 | 正常，不要删除数据库 |
| `SKIPPED_NON_SESSION` | 周末或交易所休市 | 正常 |
| `SKIPPED_OUTSIDE_WINDOW` | scheduled 命令不在 09:20–09:29 ET | 手动发送应使用第 10 节 |
| `SKIPPED_DISABLED` | 某频道功能开关关闭 | 检查配置 |
| `FAILED_RETRYABLE` | 数据暂缺、覆盖不足或临时错误 | 先看日志和上游任务 |
| `FAILED_PERMANENT` | Webhook/角色等配置错误 | 修正配置，不要反复重试 |
| `UNKNOWN` | 发送结果无法确认，可能已发出 | 先人工检查频道 |

## 14. 最常用的维护流程

### 14.1 正常情况下

平时不需要手动执行 Python 命令，只需要偶尔查看：

```bash
systemctl list-timers --all \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

以及最近发送日志：

```bash
journalctl -u quant-premarket-digest.service -n 100 --no-pager
```

### 14.2 某天没有收到 Discord 消息

按以下顺序排查，不要一开始就重发：

第一步，确认当天是否为美股交易日：

```bash
journalctl -u quant-premarket-digest.service -n 100 --no-pager
```

第二步，确认三个 timer 都在：

```bash
systemctl list-timers --all \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

第三步，检查数据刷新：

```bash
systemctl show quant-us-daily-refresh.service \
  -p Result -p ExecMainStatus -p ActiveState
journalctl -u quant-us-daily-refresh.service -n 100 --no-pager
```

第四步，检查板块产物：

```bash
systemctl show quant-group-analytics-eod.service \
  -p Result -p ExecMainStatus -p ActiveState
journalctl -u quant-group-analytics-eod.service -n 100 --no-pager
```

第五步，对相同开盘日做 dry-run。dry-run 通过后，才能考虑手工正式发送。

### 14.3 修复失败后重新运行

清除 systemd 的失败限速状态：

```bash
systemctl reset-failed quant-us-daily-refresh.service
systemctl reset-failed quant-group-analytics-eod.service
systemctl reset-failed quant-premarket-digest.service
```

`reset-failed` 不会运行任务，只清除 `failed` 标记和启动限速计数。然后根据失败层级重新启动：

```bash
systemctl start --no-block quant-us-daily-refresh.service
systemctl start --no-block quant-group-analytics-eod.service
```

最后先 dry-run，再决定是否人工发送。不要在预定窗口以外通过 systemd service 强行测试发送。

### 14.4 `KeyError: 'adj_close'`：共享行情缓存被旧版动量刷新降级

如果执行多因子更新时看到：

```text
Pipeline failed: 'adj_close'
KeyError: 'adj_close'
```

这不是命令换行、Python 环境或 FMP Key 错误。旧版动量刷新器和多因子系统共享
`data/raw/ohlcv/*.parquet`，但旧版刷新器写回时只保留五列：

```text
open / high / low / close / volume
```

因而删除了多因子和板块算法要求的第六列 `adj_close`。旧版 `--update` 又没有把 force
传到底层，所以日志会出现“500 cached, 3 to download”，继续读取坏缓存并再次失败。

修复版代码做了四件事：

1. 动量刷新写回时保留已有 `adj_close`；
2. 多因子 loader 在缓存命中前校验六列、`adj_close` 有效性和完整历史范围；
3. `run_mvp.py --update` 真正强制刷新底层 OHLCV；
4. pipeline 失败时返回非零退出码，不再只打印错误后返回 0。

旧文件不会因为只部署代码就自动恢复。按下面顺序进行一次修复。

第一步，记录更新前 active 的 timer，然后在维护期间暂停它们：

```bash
systemctl list-units --type=timer --state=active 'quant-*'

systemctl stop \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

`stop` 不会取消开机 enabled 状态。恢复时只启动第一条命令中原本 active 的 timer；如果
某个 timer 原来没有启用，不要借修复过程把它启用。

确认三个 worker 当前没有正在写文件：

```bash
systemctl is-active \
  quant-us-daily-refresh.service \
  quant-group-analytics-eod.service \
  quant-premarket-digest.service
```

如有 `activating`，等待其自然结束。不要在 Parquet 写入过程中强制杀进程。

第二步，按实际 Git/发布流程把修复版代码部署到服务器。至少应包含：

```text
src/breakouts/scanner.py
src/data/loader.py
src/data/cleaner.py
scripts/run_mvp.py
scripts/audit_ohlcv_cache.py
```

第三步，只读审计 SP500 缓存：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python \
  scripts/audit_ohlcv_cache.py \
  --universe SP500
```

修复前预期返回 `status: FAIL`，并显示 `invalid_count`；这一步不修改文件。退出码 1 在这里
表示审计发现坏缓存，不是审计脚本自身崩溃。

第四步，强制重拉 SP500 并重算多因子产物：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python \
  scripts/run_mvp.py \
  --update \
  --only-universe SP500
```

修复后的第一次运行很可能显示：

```text
OHLCV [fmp]: 0 cached, 503 to download
```

这是预期的恢复动作，不是新的错误。它会用 FMP 标准六列数据替换损坏的 SP500 文件，
需要等待全部下载和因子计算结束。不要同时运行日常 US_ACTIVE refresh。

第五步，确认 pipeline 和 schema：

```bash
echo $?

/home/projects/quant/.venv/bin/python \
  scripts/audit_ohlcv_cache.py \
  --universe SP500
```

第一条应输出 `0`；第二条应输出：

```text
"status": "PASS"
"invalid_count": 0
"missing_count": 0
"unreadable_count": 0
```

注意：`echo $?` 必须紧跟在 `run_mvp.py` 后执行；如果中间执行了别的命令，它显示的是后一条
命令的退出码。

第六步，使用修复后的动量刷新器重新发布最新 manifest，再重建板块产物：

```bash
systemctl start --no-block quant-us-daily-refresh.service
journalctl -fu quant-us-daily-refresh.service
```

确认 `failures=0` 和 service success 后：

```bash
systemctl start --no-block quant-group-analytics-eod.service
journalctl -fu quant-group-analytics-eod.service
```

确认 sector 与 sub-industry 都 `SUCCESS`。然后按第 9 节对下一个真实 XNYS 开盘日做
Discord dry-run。全部通过后，只恢复维护前原本 active 的 timer。

不要通过删除全部 `data/raw/ohlcv` 来处理这个问题；强制更新会安全覆盖 SP500，其他股票
以后被多因子 loader 使用时也会因为 schema/历史范围校验而自动重拉。

## 15. 更新项目代码时怎么做

代码更新是最容易造成维护问题的地方。仓库里的 service 模板仍默认 `quant + /opt/quant`，
所以不要在每次 `git pull` 后无脑把模板覆盖到 `/etc/systemd/system`。

### 15.1 更新前记录并暂停 timer

```bash
systemctl list-units --type=timer --state=active 'quant-*'
```

这条命令只读，用于记录更新前实际启用的 timer。

下面示例假设三个 timer 都是更新前实际处于 active 的任务。若更新前只启用了其中两个，
就只停止并恢复那两个，不要借维护之机启用原来未启用的发送 timer：

```bash
systemctl stop \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

`stop timer` 只停止以后触发，不会自动中断已经在运行的 service。确认没有任务正在执行：

```bash
systemctl is-active \
  quant-us-daily-refresh.service \
  quant-group-analytics-eod.service \
  quant-premarket-digest.service
```

应全部显示 `inactive`。如果仍是 `activating`，等待任务自然结束，不要在 Discord HTTP
发送中途强杀进程。

### 15.2 更新代码和 worker 依赖

```bash
cd /home/projects/quant
BEFORE=$(git rev-parse HEAD)
git pull --ff-only
git diff --name-only "$BEFORE" HEAD -- requirements.txt
```

- `git pull --ff-only` 只允许快进更新，避免服务器上自动产生合并提交；
- 只有最后一条列出 `requirements.txt` 时，才运行下面的依赖安装，避免普通代码更新触发
  `>=` 依赖无意义升级；
- 如果服务器代码不是通过 Git 管理，应使用实际发布流程替换 `git pull`。

依赖确有变化时执行：

```bash
/home/projects/quant/.venv-worker/bin/python -m pip install -r requirements.txt
/home/projects/quant/.venv-worker/bin/python -m pip check
```

如果此次版本同时修改 Web 代码或 Web 依赖，完整的停站、更新 `.venv` 和恢复步骤以
[`server_daily_runbook.md`](server_daily_runbook.md) 第 4 节为准。

### 15.3 unit 文件没有变化时

如果此次版本没有修改 `deploy/systemd/`，不要重新复制 unit。下面示例仍假设更新前记录的
三个 timer 都处于 active；实际执行时只启动更新前记录的集合：

```bash
systemctl start \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

`start` 恢复本次维护前已经 enabled 的 timer，不改变开机启用关系。

### 15.4 unit 文件有变化时

先比较仓库模板与服务器版本：

```bash
diff -u \
  /home/projects/quant/deploy/systemd/quant-premarket-digest.service \
  /etc/systemd/system/quant-premarket-digest.service
```

`diff -u` 是只读比较。看到 `User=root`、`/home/projects/quant`、`.venv-worker`、
`ProtectHome=read-only` 和 `ReadWritePaths` 的差异是当前服务器的预期本地修改。

如果上游模板确有新的必要改动，应在维护窗口人工合并，不要直接覆盖。合并后执行：

```bash
systemd-analyze verify /etc/systemd/system/quant-*.service \
  /etc/systemd/system/quant-*.timer
systemctl daemon-reload
```

然后只恢复更新前已经启用的 timer。

## 16. 不要执行的操作

### 16.1 不要删除 Discord outbox

```text
outputs/premarket_digest/state.sqlite3
```

删除后系统会忘记当天是否已经发送，可能产生重复消息。

### 16.2 不要把密钥输出到终端记录

不要把以下命令的输出复制到聊天：

```bash
cat /etc/quant/premarket-digest.env
cat /etc/quant/momentum-alerts.env
```

Discord Webhook URL 本身就是频道写入凭证。

### 16.3 不要启用旧的每小时 timer

如果目标是每天开盘前每个频道各一条，应保持：

```bash
systemctl disable --now quant-momentum-alerts.timer
```

这条命令只针对旧的盘中小时提醒，不会关闭新的盘前双频道日报。

### 16.4 不要同时运行两个行情写入任务

不要同时执行：

```text
scripts/run_mvp.py --update
scripts/refresh_us_active.py
```

两者可能同时写 `data/raw/ohlcv/*.parquet`。应等待一个结束后再运行另一个。

### 16.5 不要用服务器本地时区猜美股开盘日

周末、节假日、夏令时和提前收盘都由 XNYS 日历处理。预览与发送命令中的 `--session`
始终表示“即将开盘的美国交易日”。

## 17. 备份重点

至少备份：

```text
/home/projects/quant/outputs/premarket_digest/state.sqlite3
/home/projects/quant/outputs/universes/SP500/group_analytics/
/home/projects/quant/outputs/universes/*/factors/
/home/projects/quant/outputs/strategies/
/home/projects/quant/outputs/backtests/
/home/projects/quant/outputs/papertrading/
/home/projects/quant/outputs/watchlists/
```

前两项保护 Discord 去重与行业产物；后五项保护多因子页面、用户创建的策略、回测、模拟盘
和自定义股票池。若某个目录尚未创建，表示服务器还没有产生该类数据，不是备份命令故障。

以下文件包含密钥，应通过加密的受控方式单独备份，不能进入 Git：

```text
/etc/quant/momentum-alerts.env
/etc/quant/premarket-digest.env
/etc/quant/web.env
```

迁移服务器时必须保持单机运行：先停旧服务器 timer，确认所有 service 都 inactive，再复制
outbox，最后才能在新服务器启用 timer。两台服务器同时启用会绕过单机 SQLite 去重。

## 18. Discord 自动任务的部署完成标准

满足以下所有条件，才可以认为“以后等待自动发送即可”：

```bash
systemctl is-enabled quant-us-daily-refresh.timer
systemctl is-enabled quant-group-analytics-eod.timer
systemctl is-enabled quant-premarket-digest.timer
```

三条都输出 `enabled`；并且：

```bash
systemctl is-active quant-us-daily-refresh.timer
systemctl is-active quant-group-analytics-eod.timer
systemctl is-active quant-premarket-digest.timer
```

三条都输出 `active`。最后运行：

```bash
systemctl list-timers --all \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

列表中必须有三行，并且 `NEXT` 都有下一次时间。只看到前两行时，表示数据会自动更新，
但 Discord 不会自动发送。

多因子网站是否正常是另一项独立检查，不能由这三个 timer 推断。网站检查见第 20.3～20.4
节；至少应确认端口有进程监听，并且 `/` 与 `/factors` 返回 HTTP 200。

## 19. 最短命令速查

查看三个定时器：

```bash
systemctl list-timers --all \
  quant-us-daily-refresh.timer \
  quant-group-analytics-eod.timer \
  quant-premarket-digest.timer
```

查看最近发送日志：

```bash
journalctl -u quant-premarket-digest.service -n 100 --no-pager
```

安全预览，不发送：

```bash
cd /home/projects/quant
.venv-worker/bin/python scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --session YYYY-MM-DD
```

手动发送指定开盘日的两个频道：

```bash
cd /home/projects/quant
.venv-worker/bin/python scripts/run_premarket_digest.py \
  --env-file /etc/quant/premarket-digest.env \
  --send --allow-outside-window \
  --session YYYY-MM-DD --allow-historical-send
```

暂停自动 Discord：

```bash
systemctl disable --now quant-premarket-digest.timer
```

恢复自动 Discord：

```bash
systemctl enable --now quant-premarket-digest.timer
```

## 20. 多因子项目页面的运行与维护

### 20.1 网站里有哪些页面

当前 FastAPI 网站是整个量化项目的统一页面外壳，默认端口为 `18823`。主要入口如下：

| URL | 页面作用 | 主要数据来源 |
|---|---|---|
| `/` | 多因子研究总览、IC 与五分位结果 | `outputs/universes/*/factors/` |
| `/factors` | 因子库 | 因子注册表和 `configs/factor_library.yaml` |
| `/strategies` | 多因子策略库 | `outputs/strategies/` |
| `/backtests` | 策略回测任务 | `outputs/backtests/` |
| `/paper` | 模拟盘账户与运行结果 | `outputs/papertrading/` |
| `/watchlists` | 自定义股票池 | `outputs/watchlists/` |
| `/stock/{ticker}` | 个股多因子诊断 | 因子矩阵与个股行情 |
| `/breakouts` | 动量突破扫描 | `data/raw/ohlcv/` 与突破扫描逻辑 |
| `/breakouts/{ticker}` | 单只股票动量突破诊断 | 个股日线与盘中数据 |
| `/group-analytics` | 板块和细分行业强弱 | group analytics immutable artifacts |

最后一个 `/group-analytics` 是可选页面。只有 Web 进程启动时同时开启 writer 和 Web 两个
开关，它的路由与导航入口才会注册。

### 20.2 网站、主多因子 pipeline 和三个新 timer 的边界

这是维护时最重要的区别：

| 运行入口 | 做什么 | 不做什么 |
|---|---|---|
| `run_mvp.py --serve-only` | 启动 FastAPI，读取已有产物 | 不下载行情，不重新计算因子 |
| `run_mvp.py --update --only-universe SP500` | 更新数据并重算 SP500 多因子产物 | 不启动网站，不发送 Discord |
| `quant-us-daily-refresh.service` | 更新 US_ACTIVE 日线和盘前 manifest | 不重算主页的因子、IC、五分位回测 |
| `quant-group-analytics-eod.service` | 计算板块和细分行业产物 | 不修改多因子分数、策略或模拟盘 |
| `quant-premarket-digest.service` | 读取已有数据并发送两条消息 | 不更新主页，不重算因子 |

因此：

- Discord 能按时发送，不代表多因子主页已经更新；
- 多因子网站正在运行，不代表因子数据每天自动重算；
- `group-analytics` writer 成功，只代表页面所需文件存在，不代表 Web 导航开关已经打开；
- 打开行业页面不会让 `group_analytics` 反向进入多因子算法、回测或模拟盘。

### 20.3 确认网站现在是怎么启动的

标准化完成后的固定服务名是 `quant-web.service`，先运行：

```bash
systemctl status quant-web.service --no-pager
```

如果显示 `Unit quant-web.service could not be found`，或者端口已有进程但这个 service 并未
运行，说明服务器仍是旧部署。此时再运行以下只读命令发现真实状态，不要直接启动第二份。

查看可能的 Web 进程：

```bash
ps -ef | grep -E '[u]vicorn|[r]un_mvp.py.*serve'
```

- `ps -ef` 列出全部进程；
- `grep -E` 筛选 uvicorn 或 `run_mvp.py --serve...`；
- `[u]vicorn` 这种写法避免把 grep 自己误显示为匹配结果；
- 输出会包含 PID、启动用户和完整启动命令；
- 这条命令不会改变网站。

查看谁在监听默认端口：

```bash
ss -lntp | grep ':18823'
```

- `ss` 查看网络 socket；
- `-l` 只看监听端口；
- `-n` 不做名称解析，直接显示数字；
- `-t` 只看 TCP；
- `-p` 显示进程；
- 如果有输出，通常能看到监听 `18823` 的 PID 和程序名。

查找可能的 systemd Web service：

```bash
systemctl list-units --type=service --all | \
  grep -Ei 'quant|uvicorn|fastapi|gunicorn'
```

这条命令只筛选可能的旧服务名。找到真实名称后，在带有
`YOUR_WEB_SERVICE.service` 的旧部署示例中替换占位符；不要原样执行占位符。完成迁移后统一
使用 `quant-web.service`。

如果只知道进程 PID，也可以让 systemd 尝试定位它属于哪个 service：

```bash
systemctl status 123456 --no-pager
```

把 `123456` 换成 `ps` 或 `ss` 看到的真实 PID。如果它由 systemd 管理，输出顶部会显示
所属 unit；如果是手工在 Shell、`screen`、`tmux` 或其他进程管理器启动，则不一定能得到
项目 service 名。

### 20.4 检查网站是否健康

在服务器本机请求首页：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:18823/
```

- `curl` 发起 HTTP 请求；
- `-sS` 隐藏进度条，但仍显示连接错误；
- `-o /dev/null` 丢弃完整 HTML，避免终端刷屏；
- `-w` 只输出 HTTP 状态码；
- `HTTP 200` 表示 FastAPI 首页正常响应。

检查因子库页面：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:18823/factors
```

检查行业/主题页面：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:18823/group-analytics
```

- 返回 `200`：行业页面已经注册；
- 返回 `404`：通常是 Web 进程没有开启两个 group analytics 环境变量；
- 连接失败：网站进程没有监听该端口，或实际端口不是 `18823`；
- 本机返回 200、外网打不开：继续检查腾讯云安全组、系统防火墙、Nginx/Caddy 和域名，
  这不属于 Python 页面本身故障。

标准部署查看状态和日志：

```bash
systemctl status quant-web.service --no-pager
journalctl -u quant-web.service -n 200 --no-pager
```

尚未迁移的旧部署，应把服务名换成第 20.3 节实际发现的名称。

### 20.5 网站使用 `.venv`，worker 使用 `.venv-worker`

当前建议保持两个 Python 环境：

```text
/home/projects/quant/.venv         -> 已经运行的多因子网站
/home/projects/quant/.venv-worker  -> 数据刷新、行业计算、Discord
```

这样更新 worker 依赖时，不会立即改变正在运行的网站进程。分别检查：

```bash
/home/projects/quant/.venv/bin/python -V
/home/projects/quant/.venv-worker/bin/python -V
```

分别检查依赖一致性：

```bash
/home/projects/quant/.venv/bin/python -m pip check
/home/projects/quant/.venv-worker/bin/python -m pip check
```

如果新代码为 Web 页面增加了依赖，仅更新 `.venv-worker` 不够。应在维护窗口更新网站环境：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python -m pip install -r requirements.txt
```

更新后需要重启 Web 进程才能加载新 Python 代码。不要在网站 `.venv` 更新到一半时同时
重启网站。

### 20.6 只启动网站，不重算多因子

下面命令适合临时诊断，前提是 `18823` 没有被现有网站占用：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python \
  scripts/run_mvp.py \
  --serve-only \
  --host 0.0.0.0 \
  --port 18823
```

参数含义：

- `--serve-only`：只启动 FastAPI，不下载行情、不计算因子；
- `--host 0.0.0.0`：监听服务器所有 IPv4 网络接口；
- `--port 18823`：使用项目默认端口；
- 这是前台进程，终端断开或按 `Ctrl+C` 就会停止；
- 它不会发送 Discord；
- 生产环境使用本仓库提供的 `quant-web.service`；旧部署在迁移完成前继续使用其原有进程管理器。

如果端口已被占用，会出现 `Address already in use`。这通常说明现有网站本来就在运行，
不要启动第二份。

### 20.7 更新多因子主页的数据

只更新 SP500 多因子 pipeline、不启动第二个网站：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python \
  scripts/run_mvp.py \
  --update \
  --only-universe SP500
```

- 类型：生成数据；不会发送 Discord；
- `--update` 等价于强制刷新数据并设置 `--no-web`；
- `--only-universe SP500` 只重算 SP500，避免把所有启用股票池全部跑一遍；
- 它会更新宽表、启用因子、预处理结果、IC、置信评估和五分位回测产物；
- 主页每次请求都会读取磁盘产物，因此新产物完成后通常无需重启网站；
- 运行期间不要同时启动 `quant-us-daily-refresh.service`，两者可能同时写
  `data/raw/ohlcv/*.parquet`；
- 大规模重算可能占用较多 CPU、内存和磁盘 IO，建议在低访问时段执行。

如果日志出现 `KeyError: 'adj_close'`，不要反复重试这条命令。那表示服务器仍在使用旧版
共享缓存写入逻辑，或旧坏文件尚未恢复；按第 14.4 节部署修复并完成一次 schema 恢复。

更新后可以随时进行只读审计：

```bash
/home/projects/quant/.venv/bin/python \
  scripts/audit_ohlcv_cache.py \
  --universe SP500
```

查看 pipeline 日志：

```bash
tail -n 200 /home/projects/quant/logs/quant.log
```

`tail -n 200` 显示文件最后 200 行。如果任务是在前台运行，终端本身也会同步显示日志。

当前仓库没有为主多因子 `run_mvp.py --update` 提供本文前三个 timer 那样的独立生产
systemd timer。因此，除非你原来的部署已经另行配置，它不会因为 Discord 定时任务启用而
自动执行。是否要增加“多因子每日重算 timer”应单独设计执行时间、资源限制和与
`refresh_us_active` 的互斥，不能直接复制现有 timer。

### 20.8 开放“行业/主题”项目页面

行业 writer 与行业 Web 页面是两个独立开关：

```text
GROUP_ANALYTICS_ENABLED=true       允许领域功能/产物读取
GROUP_ANALYTICS_WEB_ENABLED=true   注册 Web 路由和侧边栏入口
```

现有 `quant-group-analytics-eod.service` 只为 writer 自己设置了第一个开关，并显式关闭 Web；
它不会修改已经运行的网站环境。

你最早提交的环境检查显示，网站使用的 `.venv` 当时缺少 `exchange_calendars`，只有后来
创建的 `.venv-worker` 已安装。因此在开放行业页面前，先检查网站环境：

```bash
/home/projects/quant/.venv/bin/python -c \
  "import exchange_calendars; print('web group dependency OK')"
```

如果仍出现 `ModuleNotFoundError`，先找出真实 Web service，选择维护窗口，然后执行：

```bash
cd /home/projects/quant
/home/projects/quant/.venv/bin/python -m pip install -r requirements.txt
/home/projects/quant/.venv/bin/python -m pip check
```

不要只因为 `.venv-worker` 导入成功，就推断网站 `.venv` 也具备同样依赖。这两个环境是
彼此隔离的。

标准 `quant-web.service` 从 `/etc/quant/web.env` 读取这两个开关。编辑：

```bash
vi /etc/quant/web.env
```

设置：

```dotenv
GROUP_ANALYTICS_ENABLED=true
GROUP_ANALYTICS_WEB_ENABLED=true
```

保存后执行：

```bash
systemctl restart quant-web.service
systemctl status quant-web.service --no-pager
```

- 环境文件变化不需要 `daemon-reload`；
- `restart` 会造成一次短暂页面中断，并让 FastAPI 重新注册可选路由；
- 这些命令不会发送 Discord；
- 旧部署若不读取 `/etc/quant/web.env`，应在其真实进程管理器中设置同样变量后重启。

验证页面和 API：

```bash
curl -sS -o /dev/null -w 'page HTTP %{http_code}\n' \
  http://127.0.0.1:18823/group-analytics

curl -sS -o /dev/null -w 'api HTTP %{http_code}\n' \
  http://127.0.0.1:18823/api/group-analytics/metadata
```

两条都应为 `HTTP 200`。页面只读取第 7 节已经生成的 immutable artifacts，不会在浏览器
请求中重新下载 FMP 或重算全市场。

如果网站不是标准 systemd 服务，应在它实际使用的 Supervisor、Docker Compose、面板或
启动脚本中加入同样两个环境变量，然后按对应方式重启。

### 20.9 让 Discord 动量消息链接回项目页面

在 Discord 配置中设置网站的外部基础地址后，每个动量候选会出现“打开诊断”链接，指向
网站的 `/breakouts/{ticker}` 页面。

编辑：

```bash
vi /etc/quant/premarket-digest.env
```

设置：

```dotenv
MOMENTUM_DASHBOARD_BASE_URL=https://你的量化网站域名
```

不要在末尾填写 `/breakouts`，程序会自行拼接完整路径。修改 env 文件后不需要重启长期
worker，因为盘前 service 每次启动都会重新读取它；下一份尚未发送的日报开始生效。

当前 sector-rotation Embed 没有自动加入行业页面链接；该配置只影响动量候选的个股诊断
链接。

### 20.10 更新代码时如何保护网站

第 15 节的 timer 更新流程还需要加上网站检查：

1. 标准部署使用 `quant-web.service`；旧部署先用第 20.3 节找出真实进程管理方式；
2. 只更新 `.venv-worker` 时，现有网站环境不会被修改；
3. 更新 `.venv` 或 Web 代码时，选择维护窗口；
4. 更新依赖完成后运行 `systemctl restart quant-web.service`；旧部署重启其真实 Web service；
5. 用第 20.4 节依次检查 `/`、`/factors`；
6. 如果已开放行业页面，再检查 `/group-analytics` 与 metadata API；
7. 最后恢复更新前实际处于 active 的 timer。

尚未标准化的旧部署，不要为了更新 Discord worker 而猜测并停止一个未知名字的网站服务；
先发现真实启动方式。同样，不要把 `.venv-worker` 直接替换成网站 `.venv`，两个环境隔离
正是为了降低维护风险。
