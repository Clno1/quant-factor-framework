# 独立运维监控站

更新日期：2026-08-16

## 1. 解决什么问题

以前检查“今天有没有发盘前板块轮动、行情是否补到最新交易日、盘中动量是否持续计算”，需要在
多个 systemd journal、DuckDB、SQLite 和 JSON 文件之间人工拼接。现在统一为两个独立进程：

```text
既有任务产物 / systemd 状态
        ↓ 只读采集
quant-operations-watchdog.service（每分钟 oneshot）
        ↓ 单写者事务
outputs/operations/operations.sqlite3
        ↓ SQLite Backup API + 原子替换
outputs/operations/operations_snapshot.sqlite3
        ↓ 只读 immutable 连接
quant-operations-web.service（SG: 0.0.0.0:18825）
```

主业务 Web 继续使用 `18823`，本地开发通常使用 `18824`。运维 Web 使用独立 `18825`，不注册到
`src.webapp.app`，也不复用主业务侧栏、Cookie 或认证变量。SG 生产 unit 监听
`0.0.0.0:18825`，但在绑定非回环地址前会强制校验独立运维账号和至少 16 位密码；认证缺失时
服务会拒绝启动。

运维站不提供启停、重跑、改配置或确认异常按钮。页面刷新只读取原子快照，不会触发 FMP、研究、
交易或消息发送。

## 2. 页面与口径

| 页面 | 回答的问题 | 主要证据 |
|---|---|---|
| 运维总览 | 今天哪些任务正常、运行中、失败或漏跑 | 全部结构化证据 + systemd |
| 任务中心 | 任务何时该跑、当前阶段、真实耗时、历史运行 | `job_runs`、`job_stages` |
| 数据新鲜度 | 正式行情/研究应到哪天，实际到哪天 | DuckDB publication、研究 pointer |
| 消息投递 | 盘前、每小时和盘中业务消息是否真实发送 | 三个业务 SQLite 的 delivery/outbox/run 记录 |
| 上线专项 | 全美宽基一次性建设和 5 日门槛到了哪里 | broad report、publication、shadow ledger |
| 异常中心 | 哪些问题仍开放，何时首次和最近出现 | 指纹去重的 incident ledger |

状态不是从日志关键字猜测：

- `SUCCESS`：目标交易日的正式版本或投递证据已经存在。
- `RUNNING`：结构化运行状态或心跳仍在推进。
- `SCHEDULED`：计划时间还没到。
- `SKIPPED`：没有账户、休市或无信号等合法跳过。
- `DEGRADED`：部分股票池、频道或账户完成，部分未完成。
- `BLOCKED`：前置数据或上线门槛明确阻断。
- `MISSED`：过了截止时间仍没有当日运行证据。
- `STALE`：有旧证据，但版本或心跳已经落后。
- `FAILED`：已有明确失败结果。
- `DISABLED`：配置声明当前应关闭，例如首次回填前的宽基 timer。

“没有候选”与“没有运行”不同。小时动量只要 `alert_runs` 正常完成，即使候选数为 0 也算成功；
没有当日 `alert_runs` 且超过应运行时间才是漏跑。页面中的耗时取持久化
`started_at/completed_at`，不会因浏览器刷新归零。

“消息投递”会主动列出当日预期项，而不是只显示已经成功的记录。盘前动量、盘前板块轮动、
每小时动量摘要和盘中突破会分别显示“等待运行、无信号、影子模式、已发送、未投递或失败”。
`NO_SIGNAL` 表示任务已运行但没有可发内容，`MISSED` 才表示应该运行却没有运行证据。

## 3. 当前纳管任务

配置唯一来源是 [`../configs/operations.yaml`](../configs/operations.yaml)。当前包括：

1. 旧 `07:15 SGT` 短周期行情缓存已归档并预期关闭，历史证据保留。
2. `08:15 SGT` SP500、NASDAQ100、MAG7 核心行情。
3. `08:45 SGT` 多因子研究与跨池发布。
4. `13:15 SGT` 在宽基生产后运行板块和子行业研究。
5. `10:30 SGT` 模拟盘日终运行。
6. 每两分钟对账模拟盘新成交；`11:00 SGT` 发送每日账户日结。
7. `11:30 SGT` 全美宽基生产链，首日验收后必须保持启用。
8. 每 5 分钟缺数队列。
9. `06:30 ET` 盘中候选预计算。
10. `07:00 ET` 盘前双频道 payload 预计算。
11. `09:20 ET` 盘前动量和板块轮动双频道投递。
12. 美股交易时段每小时动量扫描。
13. 美股交易时段盘中动量持续监控和心跳。
14. 每分钟运维 watchdog 自身。

采集器只读以下既有事实：

- `data/catalog/quant.duckdb` 的 ingestion、dataset 和 publication 表；
- `outputs/universes/*/research_publication.json`；
- group analytics 的 `latest_success.json` 和 `last_attempt.json`；
- `outputs/quant_app.sqlite3` 的 data requests、paper accounts 和运行 frame；
- premarket、hourly momentum、intraday monitor 三个专属 SQLite；
- intraday monitor 内的茶杯柄使用独立算法版本、评估/周期/交易日三张表和独立 `5/5`，运维任务详情展示命中、拒绝原因、P95 延迟与有界序列长度；
- 全美宽基 pipeline report、factor publication、readiness 和 shadow ledger；
- Linux systemd 的 service/timer 状态；
- 可用磁盘和内存。

采集器之间故障隔离。一个 SQLite 损坏只会产生该域的 `EVIDENCE_COLLECTOR_FAILED`，其余任务仍
写入同一份一致快照。

## 4. 本地启动

先生成第一份快照，再启动运维站：

```bash
cd /Users/huozhihong/Documents/Quant
python scripts/run_operations_watchdog.py --no-systemd
python scripts/run_operations_web.py --host 127.0.0.1 --port 18825
```

浏览器打开 `http://127.0.0.1:18825/`。macOS 没有 systemd，因此任务证据仍可显示，systemd 栏会
显示“本地不可用”。主业务站可同时运行在 `18824`，两者没有路由重叠。

存储核验：

```bash
python scripts/migrate_operations_storage.py verify --json
```

## 5. SG 安装

部署前照常备份代码、`data/outputs`、`/etc/quant` 和 systemd。然后安装独立环境文件：

```bash
install -m 0600 -o root -g root \
  deploy/systemd/operations-web.env.example \
  /etc/quant/operations-web.env
vi /etc/quant/operations-web.env
```

运维账号使用 `QUANT_OPS_AUTH_USER/PASSWORD`，不读取 `QUANT_WEB_AUTH_*`。密码默认至少 16 位。
示例占位密码会被代码拒绝，必须先替换后服务才能启动。不要把 FMP key 或 Discord webhook 放进
这个文件；生产 unit 也会在环境文件缺失时直接失败。

安装 unit：

```bash
install -m 0644 deploy/systemd/quant-operations-watchdog-root.service \
  /etc/systemd/system/quant-operations-watchdog.service
install -m 0644 deploy/systemd/quant-operations-watchdog.timer \
  /etc/systemd/system/quant-operations-watchdog.timer
install -m 0644 deploy/systemd/quant-operations-web-root.service \
  /etc/systemd/system/quant-operations-web.service

systemd-analyze verify \
  /etc/systemd/system/quant-operations-watchdog.service \
  /etc/systemd/system/quant-operations-watchdog.timer \
  /etc/systemd/system/quant-operations-web.service

systemctl daemon-reload
.venv/bin/python scripts/migrate_operations_storage.py init
systemctl start quant-operations-watchdog.service
.venv/bin/python scripts/migrate_operations_storage.py verify --json
systemctl enable --now quant-operations-watchdog.timer quant-operations-web.service
```

验收：

```bash
systemctl status quant-operations-watchdog.timer quant-operations-web.service --no-pager
journalctl -u quant-operations-watchdog.service -n 100 --no-pager
journalctl -u quant-operations-web.service -n 100 --no-pager
ss -lntp | grep ':18825'
curl -u 'quant-ops:<password>' http://127.0.0.1:18825/healthz
curl -I http://43.156.89.232:18825/
```

生产 unit 绑定 `0.0.0.0:18825`。腾讯云安全组需要允许入站 TCP `18825`；更稳妥的规则是只允许
固定办公公网 IP，而不是长期允许 `0.0.0.0/0`。直接访问地址为：

```text
http://43.156.89.232:18825/
```

浏览器会弹出独立 Basic Auth 登录框。账号和密码保存在 SG 的
`/etc/quant/operations-web.env`，不会写入仓库或网页。未认证请求必须返回 `401`，认证后
`/healthz` 和六个只读页面必须返回 `200`。

当前直接 IP 使用 HTTP，Basic Auth 只负责身份校验，不加密传输中的账号、密码或页面内容；因此
这是便于当前验收的访问方式，不应视为最终安全边界。长期方案是在 `18825` 前增加 HTTPS 反向
代理并关闭公网直连。需要临时避免明文公网访问时，仍可使用 SSH 隧道：

```bash
ssh -L 18825:127.0.0.1:18825 root@43.156.89.232
```

然后打开 `http://127.0.0.1:18825/`。反向代理应给主业务站和运维站不同域名或至少不同上游，
两套认证仍须保持隔离。

针对当前 2 GB SG，独立运维 Web 的 `MemoryHigh/MemoryMax` 为 `140M/180M`，watchdog 为
`180M/260M`；主业务 Web 为 `420M/600M`。watchdog 是短时 oneshot；运维 Web 只读 SQLite
快照，不持有 DuckDB 连接。若任一 Web 触发内存上限，systemd 会留下明确失败证据并重启对应
服务，不会继续挤占盘中动量进程或整台主机。

## 6. 告警边界

本阶段按产品决定不新增 Discord 运维告警：

- watchdog 的 `external_notifications` 被配置与代码双重固定为 `false`；
- 运维环境文件不接收 webhook；
- 现有盘前、动量信号 Discord 是业务消息，不是运维告警，继续由原 worker 管理；
- 异常通过独立网页查看，恢复后 watchdog 自动把 incident 标为 `RESOLVED`。

将来若接入 Sentry、邮件或短信，应从 incident ledger 消费，不应让每个任务各自再造告警规则。

## 7. 数据保留与安全

- 主台账保留 180 天运行和投递历史；快照只保留当前完整视图。
- watchdog 是 SQLite 唯一写入者；Web 用 `mode=ro&immutable=1` 打开快照。
- 快照使用 SQLite Backup API 创建并原子替换，Web 不会读到半个事务。
- 页面不会展示密钥；采集错误会脱敏项目绝对路径和常见 secret/token/password 字段。
- 运维 SQLite 是派生台账，可从正式证据重新生成，但仍应纳入 SG 日常备份。

## 8. 2026-08-13 SG 生产验收

独立运维站已部署到 `/home/projects/quant`，部署前备份位于：

```text
/home/projects/quant-backups/operations-site-20260813T033148+0800
/home/projects/quant-backups/operations-public-20260813T093923+0800
```

本次生产验收结果：

- `systemd-analyze verify` 通过；唯一输出是腾讯云 `tat_agent.service` 的旧 `/var/run` 提示，
  与 Quant unit 无关；
- `quant-operations-watchdog.timer` 已启用，每分钟触发；连续三个自动周期均为 `SUCCESS`，
11/11 个任务均有状态，单次约 2 秒，峰值内存约 80 MB；
- `quant-operations-web.service` 已启用并保持 `active (running)`，实际内存约 40 MB；
- 运维站初次部署仅监听 `127.0.0.1:18825`；随后按产品决定切换为独立公网端口
  `0.0.0.0:18825`。无认证返回 401，六个页面和 `/healthz` 带独立认证均返回 200；主业务站
  继续监听 `0.0.0.0:18823`，未改路由；
- 从项目开发机直连 `http://43.156.89.232:18825/` 返回 401，证明 SG 公网链路和腾讯云安全组
  已放行且认证仍生效；服务器内认证访问 `/healthz` 与六个页面全部返回 200；
- 运维 SQLite `integrity_check=ok`，原子快照可读，当前纳管 11 个任务、13 个新鲜度对象和
  4 条当日业务投递证据；
- 当日盘前动量、盘前板块轮动均为 `SENT`；小时动量完成 6/6 个不同计划小时，最近投递为
  `discord_http_200`；盘中监控处于 `RUNNING`，FEIM 信号仍为 `SHADOW`，没有被误报为已发送；
- 全美宽基日常 timer 按上线计划保持 `disabled`，缺失的 coverage/PIT/八因子显示“等待运行”；
  当前唯一开放提醒是缺数队列中 1 条历史失败请求，不代表当日行情或研究主链失败。

SG 的业务 SQLite 使用 WAL。watchdog 在严格只读 systemd 沙箱中首先尝试标准只读连接；如果仅因
不能创建 `-shm` 而失败，只在 WAL/回滚日志为空且主库文件在查询前后完全未变化时使用
`immutable=1`。遇到正在写入的事务会 fail closed，并保留上一份完整快照到下一分钟重试，不会
扩大 watchdog 对业务 `outputs/` 的 systemd 写权限。

## 9. 2026-08-14 宽基首次链进度证据

上线专项不再只读取正式 pointer。watchdog 还读取：

- 最新 Security Master 候选审计：显示 target、质量状态、身份覆盖率、失败项和报告路径；
- `broad_initial_rollout` 报告：显示当前阶段、阶段结果、总耗时和峰值内存；
- coverage checkpoint：显示成功批次/总批次、失败 ticker 区间数和是否恢复；
- 因子 checkpoint：显示已完成分片/总分片及精确输入绑定；
- 日常 pipeline 与首次报告按文件更新时间选择最近运行，不会永久停留在旧首次报告。

2026-08-14 的 Security Master 修复后，运维站已能显示最新候选从 `FAIL/99.980365%` 转为正式
generation `231b5b53d46a47d9a3a463cab6b06766`。首次长任务由
`quant-broad-initial-rollout-scheduled.timer` 在 2026-08-15 11:35 SGT 启动；日常宽基 timer 在
首日数据和页面验收前仍保持关闭。运维站不提供启动、重试或启用开关，继续保持只读边界。

生产认证保存在 `/etc/quant/operations-web.env`，权限为 `0600 root:root`。密码没有写入仓库或
本文档。需要查看时，在自己的终端执行：

```bash
ssh root@43.156.89.232 'cat /etc/quant/operations-web.env'
```

## 10. 中断任务与旧 checkpoint 的状态规则

2026-08-16 处置宽基首次回填停滞时发现：首次报告和 checkpoint 都可能因进程被外部停止而继续保留
`RUNNING` 字段。它们代表“当时可恢复的执行证据”，不能单独证明 systemd 进程现在仍然存活。

运维采集现采用以下优先级：

1. 同一目标交易日的最新质量审计失败，优先级高于更早的正式 publication；
2. 上游 Security Master 被阻断时，未发布的 coverage、PIT 和因子全部显示 `BLOCKED`；
3. 旧 checkpoint 仍展示完成批次和路径，但文案明确说明“不代表任务仍在运行”；
4. 只有质量门禁通过且当前执行证据仍有效时，项目才显示 `RUNNING`。

SG 验收时，运维 API 已从错误的“首次回填 47/78 批，运行中”改为：项目 `BLOCKED`、证券主表
`BLOCKED`、coverage `BLOCKED`，并显示最新失败项 `overlapping ticker intervals`。这次修复只改变
只读状态推导，没有重写 checkpoint、删除 staging、恢复回填或打开任何生产开关。

同次验收中，`systemctl --failed` 暴露了 2026-08-14 盘前动量和盘中监控的数据过期失败。下一日
`US_LIQUID_5M` 已成功发布后，无发送的数据读取预检通过，历史 systemd failed 标志被清理；事件和
投递失败仍保留在 SQLite 台账。运维上必须区分“当前服务已经恢复”和“上一交易日确实失败过”，
不能通过清除 systemd 标志抹掉历史证据。

2026-08-16 起，运维站的证券主表步骤还读取 manifest 中的第五份 `history_policy` 产物。正式台账
最终包含 30 条 `PROSPECTIVE_ONLY` 和 36 条历史排除；coverage 新 run 必须绑定包含该产物哈希的新
Security Master generation。旧 `47/78` 即使文件仍在，也只能显示为保留审计，不能恢复成运行中。

最终 66 条政策发布后，运维站应显示 Security Master generation
`fb434632cd434b9289b71453e774c68e`，coverage checkpoint 应为
`run=20260815T221208Z_b1d33eaf`、证券数 7,952、总批次 80。2026-08-16 的现场验收中，项目状态为
`RUNNING`，证券主表为 `SUCCESS`，coverage 从 8/80 继续推进且 alias failure 为 0；PIT 和八因子
必须显示为等待上游，而不是失败或伪运行。watchdog 每分钟刷新成功，运维网页继续独立运行在 18825。

## 11. Coverage/PIT 长任务状态口径

2026-08-16 的正式 coverage 已发布为版本 `ad5de5cfd10d47e2ae21364f1808248d`。运维站在读取到该正式
pointer 后，应将“全美行情覆盖”显示为成功，并展示 10,369,223 条有效记录、1,445 条供应商坏条
隔离和 target 2026-08-14；不得继续把旧 80 批 checkpoint 显示为当前仍在抓取。

PIT 构建当前由临时受控 unit `quant-broad-pit-continuation-v5.service` 执行。此类没有分段 JSON
进度的任务只在 systemd 进程确实存活、CPU/内存证据新鲜时显示“运行中”；unit 结束后若没有正式
publication，必须改为失败或等待人工核验，不能沿用旧 RUNNING。v5 的最终成功还必须同时具备：
membership 与 eligibility 文件哈希通过、parent coverage 版本精确相等、Security Master generation
精确相等，以及 `historical_pit_daily_bar_coverage` 通过。仅有退出码 0 或候选文件都不够。

## 12. 2026-08-20 供应商失败与恢复状态

运维站判断宽基日更时还必须区分三种证据：正式 publication、最新 pipeline report 和 provider
cache。2026-08-20 当前正式 Security Master 已更新为 `559f310170984b67bcee18d0f12c44dc`，但
coverage v10 在第一份 2026-08-17 FMP EOD bulk 上因 read timeout/502 失败。因此页面应显示
`BLOCKED/供应商异常`，不能因为 identity history cache 已完成就显示 coverage 成功，也不能继续显示
旧 PIT continuation 正在运行。

精确 provider cache binding
`b4a378e25ac74347964f11cccc777d164673295e72261caa41c216a1c171c6fd` 已保存 29,829 行身份历史，
作用只是让下次从三个未发布交易日恢复。cache 本身不是 publication，不计 shadow，不推进网页默认
开关。watchdog 应以最新失败 report
`run=20260820T063540Z_c122abe8.json` 为当前生产结论，同时保留旧正式 coverage
`ad5de5cfd10d47e2ae21364f1808248d` 供既有只读消费者按其各自新鲜度合同判断。

同日旁路检查发现盘中动量的 systemd failed 是上一交易日核心 `US_LIQUID_5M` 过期门禁，不是本次
宽基失败。运维站应同时展示这两个独立 incident：一个是 FMP bulk 供应商阻断宽基上线，另一个是
核心动量输入未满足新鲜度；不能把二者合并成一个“宽基任务失败”。

运维适配器已在 SG 验收：专项状态为 `FAILED`，coverage 阶段显示最新 FMP 超时、旧正式版本和
`run=20260820T063540Z_c122abe8.json`，项目 blocker 为
`FMP_EOD_BULK_PROVIDER_UNAVAILABLE`。一次性恢复 timer 将于 2026-08-20 15:53 CST/SGT 运行；
只有新 pipeline report 成功后，watchdog 才能清除这个当前 blocker，历史 incident 仍保留。

14:57 CST 复核时，核心 `US_LIQUID_5M` 已发布 target 2026-08-19、版本
`839aa104e09249a988c40afcb6949254`。盘中候选生产入口的只读结果为 2,940/2,940、覆盖率 100%，
盘前动量入口也成功绑定同一版本；Discord 路由三项均通过。两个业务服务仅清除了 systemd 的当前
failed 标志，历史 journal/SQLite incident 未删除，下一次仍按 21:20 timer 运行。运维站应把这两项
显示为“当前已恢复、上一交易日失败”，同时继续把宽基显示为独立的 FMP provider blocker。

15:54 CST 后 FMP provider 已恢复，正式 coverage target 2026-08-19 发布为
`74ab17464aff4156becdc0416580c018`，活动恢复 service 随后进入 PIT 构建。适配器现遵循两条补充
规则：当前目标日的不可变 publication 优先于同目标日更早的失败 attempt；活动中的受控 transient
service 是当前 `RUNNING` 证据。旧失败 attempt 继续保留在 runs/incidents，不再覆盖当前 stage。
生产 API 验收结果为项目 `RUNNING`、coverage `SUCCESS`、PIT `RUNNING`，当前 blocker 列表为空。

## 13. 2026-08-21 PIT 阶段证据与因子恢复进度

PIT 任务新增标准错误阶段事件：`loading authenticated coverage and Security Master`、`building
incremental/full PIT candidate`、`candidate complete`、`running/full-history coverage gate complete`
和 `publishing immutable PIT universe`。`building` 事件同时记录 `reason`；其中
`SECURITY_MASTER_CHANGED` 表示身份权威已变化，系统按合同执行全量重建，而不是卡在增量任务。

运维判断必须结合 systemd 活跃状态、最新阶段事件和正式 publication：达到
`TimeoutStartSec` 后收到 `SIGTERM` 的任务显示失败，不能沿用旧 `RUNNING`；只有新 manifest、
membership/eligibility 哈希和全部 quality checks 验证通过才显示 PIT 成功。2026-08-21 修复验收中，
同一主机的全量 PIT 从两小时超时恢复为 76 至 85 秒完成，证明旧异常来自跨代次增量分支，而非资源
容量不足。

八因子进度读取 `.staging_<generation>/checkpoint.json` 的
`completed_partition_count/expected_partition_count`。当前正式 generation 为
`bab021a29e7547f0a95e2963d96bd067`、总分片 640；unit 使用 `--auto-resume`，重启后只接受唯一的
精确输入匹配。运维站不得把 staging checkpoint 当作正式 factor publication，必须等
`factor_data_publication.json` 原子发布后再将“八因子数据”标记成功。

## 14. 2026-08-21 因子失败证据、日历合同与 1/5 状态

因子 checkpoint 的 `640/640` 只表示计算分片齐全，不表示发布成功。旧 generation
`bab021a29e7547f0a95e2963d96bd067` 在最终质量门槛发现 TURNOVER 最新覆盖率为 0，以及若干因子
存在 `unexplained_clean_disappearance`，因此状态必须显示失败并保留技术详情。根因审计定位到
FMP 历史行情中的 424 条非 XNYS 记录；这类数据以后统一显示为 coverage quarantine，而不能作为
因子程序异常或静默清洗掉。

运维证据新增以下合同：

- coverage manifest 的 `statistics.off_xnys_session_rows` 必须为 0；
- `quality_checks.xnys_session_calendar` 必须通过；
- quarantine reason 统计单独展示 `NON_XNYS_SESSION`；
- 因子 checkpoint 必须绑定 `BROAD_FACTOR_INPUT_V2_XNYS_ONLY`；
- 旧 V1 checkpoint 即使完成 640/640，也不得作为当前可恢复代次；
- 只有 `factor_data_publication.json`、640 个子哈希和真实排名查询同时通过，八因子阶段才为成功。

修复后运维站的上线专项应显示：证券主表成功、全美行情覆盖成功、PIT 宽基股票池成功、八因子
数据成功、五交易日影子验收运行中、宽基正式置信研究被 PIT 行业历史门槛阻断。当前正式绑定为
coverage `5ed0bc1f4b104e4f8b85256f15efba45`、PIT `8b37e3ec99eb46d8b2d52a1a54808690`、
factor generation `844e6a7a8bd642a0a0466bfb137529cf`；首日 2026-08-20 已通过，shadow 1/5。

运维站和主业务站本机验收均返回 HTTP 200，MDB 搜索与历史 API 可读取最新因子数据。持久宽基
timer 仍为 disabled，网页默认开关仍为 false；运维站应明确显示这是上线观察门槛，而不能把它
误报为数据缺失或任务卡死。

## 15. 2026-08-24 2/5 证据与服务器无响应事件

2026-08-21 新观察已通过，运维快照的正确业务状态应为：Security Master、coverage、
PIT 和八因子成功，五交易日影子为“观察中 2/5”，readiness 只显示
`PIT_CLASSIFICATION_POLICY`/`PIT_INDUSTRY_COVERAGE`，网页默认开关保持关闭。

因子服务落盘后，MDB 单股历史验收请求未返回，随后 SSH、主站和运维站均出现建立 TCP 后
长时间无应用响应。上一启动周期 kernel journal 已给出确定证据：2026-08-24 12:09:51 CST
发生全局 OOM，OOM killer 杀死 `quant-web.service` 内约 1.66 GB anonymous RSS 的 Python；
主机只有约 1.96 GB RAM 且无 swap。该时刻 `dirty=0`、`writeback=0`，没有 hung task 或块设备
错误，因此磁盘写回不是根因。

旧单股历史查询对一个因子的全部历史分片执行窗口排名，再在最外层按 `security_id` 筛选，生产
数据规模约 928 万行。修复后逐月执行同一排名公式，并加入 DuckDB 192 MB 查询上限和主 Web
`420M/600M` cgroup 软硬边界。硬边界的目的不是让大查询成功，而是发生未知异常时优先重启
Web，保住 SSH、运维站和生产 timer。

影子观察 1/5 的可观测性缺口也已确认：`broad_us_pipeline.enabled_expected=false` 导致 watchdog
按设计跳过 timer inactive/disabled 检查；首次 rollout 又从不自动启用日常 timer。现已将运维
期望改为 true。正确状态机是“首日完整链和人工验收通过 -> 启用每日 timer -> 连续累计 5 日 ->
打开网页默认开关”，不能把最后一步的门槛反向用于阻止第二步。

部署后 watchdog 已重新采集：宽基快照为 `RUNNING/观察中`，systemd 证据中的 timer 为
`enabled/active`，没有宽基 timer 告警。真实 MDB、AEVA、重复 MDB 与主页面请求后，主 Web
峰值稳定在约 420.5 MiB、`NRestarts=0`；SG 完整回归 `527 passed`。当前唯一开放 incident 是
`data_requests` 的 `JOB_DEGRADED`，与本次 OOM 和宽基影子停滞无关，应作为独立缺数队列问题处理。

## 16. 2026-08-25 身份漂移门禁告警

宽基日常 timer 已按期触发，但 target `2026-08-24` 连续两次在 Security Master 阶段返回同一
`policy selector drifted`。这类错误是确定性数据合同冲突，不应归类为供应商网络瞬断，也不应在
页面显示为“继续运行”。运维证据应展示失败 security_id、政策期望、候选观测、冻结 source 路径
和重试次数；下游四个阶段应显示“未运行”，shadow 保持 2/5。

本次冲突由 FMP 同一 CIK 的 GRML/KLTO profile 使用不同 CUSIP、而 SEC 官方文件声明改名换码时
CUSIP 保持不变触发。修复前保持 fail-closed 是正确行为。自动重试无法解决 selector drift，达到
StartLimit 后应保持 failed，等待带 SEC 来源的 correction 与双重冻结源幂等验证。

## 17. 2026-08-26 恢复证据和数据消费者迁移

GRML/KLTO 的当前 incident 只能在三类证据同时成立后关闭：source-backed correction 精确匹配、
同一冻结源双构建精确幂等、正式 Security Master/coverage/PIT/factor 链通过。仅修改配置或
`systemctl reset-failed` 不算恢复。现网正式恢复链已经产生 2026-08-24 的完整 publication，shadow
记为第三日 PASS；原两次 selector drift attempt 和冻结源继续保留在运行历史。

运维站对旧 `us_daily_refresh` 的口径改为“已归档、预期关闭”，不再把旧短历史 publication 的
过期状态当作当前动量故障。当前证据来自 `broad_us_pipeline`，而动量读合同还应显示父 coverage、
PIT universe、membership/eligibility/manifest 哈希和 Security Master 代次。旧 timer 关闭不产生
告警；宽基 timer 关闭、过期或失败仍必须告警。

核心行情的自动恢复报告落在 `outputs/data_audits/core_market_data/target=<date>/`。每次运行包含普通
增量结果、识别出的 semantic-drift 股票池、受控 full-rebuild 结果和有限日志尾部。日志在子进程
运行时实时进入 journal，不能再因包装器缓冲而长时间显示无进度。只有所有失败池都属于明确的
non-uniform revision/no-overlap/zero-volume 语义迁移且错误要求 full rebuild，才允许自动恢复；网络、
PIT、版本哈希及混合错误保持红色 fail-closed。

板块研究改为在宽基生产后执行，其 benchmark 读取全美 coverage；盘前和盘中动量使用同一父行情
加精确 PIT 合同。运维验收必须分别检查：宽基 target、核心 SP500 target、板块 publication target、
盘前 dry-run source session、盘中 candidate contract。任何一个交易日不一致都不能显示为整体正常。

## 18. 2026-08-26 预计算、严格恢复和模拟盘证据

盘前任务现分为两个可独立观测的阶段：`premarket_digest_prepare` 在 07:00 ET 计算并以
`PENDING + payload_hash` 冻结消息，不接触 Discord；`premarket_digest` 在 09:20 ET 只能领取已冻结
payload 并发送，缺失时 fail closed，禁止临时冷算。今日首次准备 13 分钟、峰值 547.3 MB；定时重复
准备返回 `PREPARED_ALREADY_EXISTS`，发送后两条状态均为 `SENT/attempts=1`。运维页应分别显示
“准备成功”和“两频道已发送”，不能把准备成功等同于已投递。

盘中候选也有独立 `intraday_candidate_prepare` 证据，记录 coverage/PIT 版本、source session、候选数
和耗时。今日为 363 个候选、热运行 0.949 秒；持续监控心跳正常、137 个循环、0 错误。小时扫描已在
10:35/11:35 ET 成功完成并得到 Discord HTTP 200。

缺数队列新增严格语义恢复日志 `Confirmed semantic drift; rebuilding requested universe`。该事件只有在
writer 已完成供应商抓取和重叠认证、错误明确要求 full rebuild 时出现。网络超时、FMP 5xx、身份/PIT
冲突和质量门禁不允许记录为可恢复。今日 Watchlist 请求由历史 failed 恢复为 success，队列当前
pending/running/unresolved failed 均为 0；旧失败仍作为历史证据保留。

模拟盘适配器当前显示 1/1 启用账户成功，目标日 2026-08-25。证据不仅包括 service exit 0，还包括
数据版本 `93eb4878bc4b4e0b9829fbf690bc39f4`、逐票成本字段、现金/持仓/权益台账和同日幂等重跑。
FMP `adj_close` 美分量化造成的伪分红使用区间边界审计：零在区间内才归零，整个区间为负仍产生红色
数据质量失败。运维页面不得把该修复描述成“忽略负分红”。

宽基专项为连续 4/5，日期 2026-08-20、21、24、25，剩余 1 日；第五日只能来自下一次不同的正式
target。资源、主站和运维站健康，当前无失败 `quant-*` unit。旧短周期 `US_LIQUID_5M` publication
仍可在 freshness 历史区显示过期，但其任务已归档，不能覆盖当前 broad coverage + PIT 消费链的成功状态。

部署后最终回归由受限 transient systemd unit 执行，结果为 `608 passed, 1 warning in 108.42s`，
峰值内存 281.1 MB、未使用 swap；唯一警告是 FastAPI TestClient 弃用提示。运维站的正式 unit 名是
`quant-operations-web.service`，端口为 18825；排障脚本不得使用不存在的 `quant-ops-web.service`
来判断站点状态。

## 19. 2026-08-27 五日完成与 catalog 锁观测

上线专项已进入 `SUCCESS`：五个连续交易日 2026-08-20、21、24、25、26 全部 PASS，网页默认
开关已启用。运维快照必须同时展示 target 2026-08-26、coverage `e4963942c52a`、PIT
`ded547cbef6b`、factor `1a60b302fa47`、Security Master `6706c172a3f0`，不能只展示 5/5 数字。

当日新增一类明确 incident：长任务同时读取同一 DuckDB catalog 时，只读进程持有共享锁，而另一个
所谓“读取”入口先调用 schema initialize，会尝试升级为写锁并失败。修复后的观测合同要求：

1. `published_generation()` 只以 read-only 连接查询，读取过程中不得执行 DDL；
2. 八因子必须先于板块研究，核心行情、宽基链和板块研究使用同一生产 `flock`；
3. 运维事件必须记录锁持有 PID、unit、失败阶段和自动重试次数，不能归类成 FMP 网络错误；
4. 停止被错误调度的板块任务后，八因子只能从输入哈希完全匹配的 checkpoint 恢复。

真实恢复结果为 640/640、无 swap，readiness 仅保留预期 PIT 行业历史 blocker，shadow 自动落为
5/5。完整测试为 `609 passed, 1 warning`；MDB/AEVA 搜索与历史 API 均返回 200。正式宽基置信研究
仍显示 BLOCKED 是正确状态，不应覆盖因子数据浏览已经正式启用的 SUCCESS 状态。核心 SP500
落后一个交易日是独立 incident，应继续显示在 freshness/jobs 区，不能把上线专项重新降为失败。

## 20. 2026-08-28 上线完成后日更状态语义

网页默认开关与 5/5 台账只证明“上线门槛已经完成”，不能证明“今天的数据已经发布”。此前专项总
状态只检查这两个条件，所以在四层 publication 都落后一天时仍显示 `SUCCESS/正常`；阶段列表却
显示四个 `STALE/已过期`，两者互相矛盾。

修复后的证据模型把状态拆开：

1. freshness 始终按实际 publication target 判断，旧版本仍如实记录 stale；
2. 若核心行情、核心研究或模拟盘等 systemd 上游正在执行，专项显示 `RUNNING`，尚未轮到的四层
   显示 `SCHEDULED/等待运行`，并记录具体上游 unit；
3. 只有四层 publication 当前、5/5 已通过且网页开关开启时，专项总状态才是 `SUCCESS`；
4. 网页已开启但四层滞后且没有活跃恢复任务时，总状态为 `DEGRADED`，不得继续显示正常；
5. 5/5 是不可变的上线验收历史，不因每日更新过程重置；PIT 行业历史 blocker 继续单独显示。

现网最新证据已显示 `RUNNING`，原因为“等待核心行情日更完成
（quant-market-data.service）”；前四层为等待运行，shadow 保持 5/5。全量重建的性能证据还应记录
raw 完成时间、CPU、MemoryCurrent/Peak、`memory.events.high` 和 Python/C 调用栈，避免把本地内存
退化误判成 FMP 网络卡顿。

## 21. 2026-08-28 恢复后的最终观测状态

target `2026-08-27` 已完成正式恢复。watchdog 在 2026-08-28 重新采集后，上线专项为
`SUCCESS`，前四层分别绑定 Security Master `be02e2fff93d`、coverage `378d1f3fae89`、PIT
`8f19d47b45b6`、八因子 `11247203be72`；影子台账显示连续 `6/5`，网页默认开关为 true。
MDB、AEVA 的真实 MOM_12M 查询均返回 6 个交易日并通过版本绑定。运维站刷新后若仍显示旧
`STALE`，应先比较快照 `observed_at`，不得仅凭浏览器缓存判断生产状态。

本次恢复应产生三类不同 incident，不得合并为“FMP 失败”：

- `CORE_REBUILD_DTYPE_PRESSURE`：FMP 已抓取成功，但数值列被转换成 object，出现大量 cgroup high
  event；修复后 SP500 full rebuild 约 2 分钟完成。
- `BROAD_PUBLICATION_CONTRACT_MISMATCH`：日更脚本传入价格语义合同，而存储接口未同步接收；
  修复后 manifest v5 强制校验父版本和 lineage。
- `BROAD_PRODUCTION_LOCK_CONTENTION`：板块研究超时重试持有生产锁，宽基人工重跑等待锁后退出；
  当前主链已恢复，板块任务仍需独立性能处置。

宽基正式置信研究继续显示 BLOCKED，并只列出 `PIT_CLASSIFICATION_POLICY`、
`PIT_INDUSTRY_COVERAGE`，这是正确的 fail-closed 证据。专项数据 freshness、历史 6/5 上线验收、
正式置信研究三者必须在页面和告警中保持独立。

## 22. 2026-08-28 SLA 截止时间与消费者性能证据

运维状态不能直接等同于业务表中的原始状态。盘前 outbox 的 `PENDING` 表示消息从未成功领取，
在投递窗口内可解释为运行中；超过 09:29 ET 后，同一原始状态必须解释为 `MISSED`。适配器现在同时
保留：

- `source_status=PENDING`：SQLite 原始证据；
- `past_deadline=true`：按任务 schedule 计算的时间事实；
- `status=MISSED`：面向值班人员的运维结论。

盘前准备同理。两个 payload 都存在只能证明工作最终完成；若最大 `created_at` 晚于 08:30 ET，
状态为 `DEGRADED`，`last_success_at` 仍记录实际完成时间。运维站不得把“晚完成”显示成“提前冻结
成功”，也不得把准备成功等同于投递成功。

本次事故链为：消费者重复全量哈希 92 个历史分片并加载无界行情 -> 板块任务 3 小时超时、盘前准备
超过 4 小时 -> 09:20 发送器找不到预生成 payload -> 当日两个频道漏发。修复后板块研究 CPU
9.609 秒；盘前准备约 11 分钟完成，CPU 8 分 42.79 秒、峰值 537.1 MB、无 swap，但因已过窗口
仅保留审计，没有迟到补发。

完整 publication 的安全门禁没有被移除。消费者只跳过与查询无关的 child hash，实际读取分片仍
验哈希；shadow、发布验收和人工完整核验仍检查全部 child。watchdog 新测试固定验证两条合同：
截止后的 PENDING 必须为 MISSED，晚于截止时间的完整准备必须为 DEGRADED。当前 SG 快照已按该
合同重新生成，今天的盘前漏发保留为开放 incident。

数值指标还必须区分“零”和“缺失”。`remaining_sessions=0` 表示五日门槛已完成，不能通过
`value or default` 被替换成默认值 5。适配器现在只在值为 `None` 时回退；现网最终指标为
`shadow_passed=6`、`shadow_required=5`、`shadow_remaining=0`。

## 23. 2026-08-30 主站宽基扫描阻塞事故

主站出现“进程 active、所有页面持续转圈”的假健康。根因不是界面改名、FMP、网络或整机内存
耗尽，而是旧 `quant-us-daily-refresh` 归档后不再预热 `data/cache/momentum_scans/`；Web 仍保留
“缓存缺失就在 HTTP 请求内构建扫描”的旧合同。一次 `/breakouts` 请求因此读取约 2,780 只 PIT
成员、每票约 400 个日历日，并在 DuckDB 内执行全量排序。

现网证据包括：主站 RSS 约 565 MB、cgroup `MemoryHigh=420M` 被越过、多个请求线程分别等待
DuckDB 实例锁和 SQLite；GDB 原生栈明确停在 DuckDB `PhysicalOrder`、`SortedRunMerger` 和 catalog
checkpoint 读取。后续点击继续进入线程池并等待数据库锁，所以静态资源已加载、页面主体却一直没有
响应。服务状态 `active` 在此场景不能代表 HTTP 健康。

永久修复提交为 `46aa2f0`。后台脚本继续允许在独立资源边界内生成扫描缓存；Web 页面和 JSON API
统一传入 `allow_build=false`。缓存不存在时，页面立即显示等待后台发布，API 返回 503，禁止在 Web
进程内退化为全美现场扫描。新增测试固定验证 cache miss 不会调用 `build_breakout_scan()`。

SG 定向回归为 `19 passed`。部署后六个入口的实测响应为：研究 0.994 秒、策略 0.416 秒、回测
0.405 秒、模拟盘 0.405 秒、股票池 0.423 秒、茶杯柄 1.325 秒，均为 HTTP 200。整组请求后 Web
cgroup 峰值约 383 MB，未再次越过 420 MB 高水位。部署备份：

```text
/home/projects/quant-backups/web-broad-scan-guard-20260830T1544CST
```

## 24. 2026-08-30 模拟盘周末日历边界事故

模拟盘任务在周六进入 `failed`，根因是 XNYS 动态日历的右边界停在前一交易日，周六日期无法执行
`direction=previous`。观测上应把这种错误标记为 `CALENDAR_SESSION_BOUNDARY`，因为服务在行情读取、
因子计算和订单执行之前失败；不得归类为 FMP、成本模型或策略失败。systemd 的三次自动重试使用
相同输入，因此只会重复同一确定性错误，随后触发 start limit。

修复后生产验收证据：service exit 0，decision/expected/mark 均为 2026-08-28，`last_error=null`，
dataset version 为 `c18ef8024a494896860fb5ade7783ecb`。首次恢复产生 2 个 next-open fill；紧接着
同 session 重跑产生 0 fill、0 order，持久账本计数保持 orders=7、fills=7、equity rows=5，仅新增
一条运行审计记录。SQLite integrity、ID 唯一性、成交日期、滑点方向和费用模型全部通过。

运维适配器以后判断模拟盘健康时至少应同时展示：最新 service 退出状态、账户 `last_error`、
expected/decision/mark session、输入 dataset version、当次 fill/order/pending 数，以及最近一次成功
时间。旧账户的 research/Watchlist 创建时快照缺失属于 provenance warning，应与 runtime failure
分开展示。
