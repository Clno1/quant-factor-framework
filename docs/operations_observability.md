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

1. `07:15 SGT` 全美活跃股票行情缓存。
2. `08:15 SGT` SP500、NASDAQ100、MAG7 核心行情。
3. `08:45 SGT` 多因子研究与跨池发布。
4. `09:15 SGT` 板块和子行业研究。
5. `10:30 SGT` 模拟盘日终运行。
6. `11:30 SGT` 全美宽基生产链，首次回填完成前预期关闭。
7. 每 5 分钟缺数队列。
8. `09:20 ET` 盘前动量和板块轮动双频道投递。
9. 美股交易时段每小时动量扫描。
10. 美股交易时段盘中动量持续监控和心跳。
11. 每分钟运维 watchdog 自身。

采集器只读以下既有事实：

- `data/catalog/quant.duckdb` 的 ingestion、dataset 和 publication 表；
- `outputs/universes/*/research_publication.json`；
- group analytics 的 `latest_success.json` 和 `last_attempt.json`；
- `outputs/quant_app.sqlite3` 的 data requests、paper accounts 和运行 frame；
- premarket、hourly momentum、intraday monitor 三个专属 SQLite；
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

针对当前 2 GB SG，Web 的 `MemoryHigh/MemoryMax` 为 `140M/180M`，watchdog 为
`180M/260M`。watchdog 是短时 oneshot；Web 只读 SQLite 快照，不持有 DuckDB 连接。若触发内存
上限，systemd 会留下明确失败证据，不会继续挤占盘中动量进程。

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
