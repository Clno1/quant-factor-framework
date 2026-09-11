# EP 候选覆盖与持久任务队列

## 本轮范围

这是四阶段开发中的第 1 阶段：候选覆盖、身份复用、增量采集、持久队列及排查入口。
本轮没有改动生产定时器、执行付费模型请求、向 Discord 发消息或修改杯柄算法。
**不代表已补齐官方 IR/PDF 自动来源、财务分析或盘前行情确认。**

| 阶段 | 状态 | 放行标准 |
| --- | --- | --- |
| 候选覆盖与队列 | 本地实现与离线回归完成 | 新版部署后核对真实积压、处理耗时和候选来源 |
| 官方原文来源 | 待下一阶段实现 | 发行人身份、域名/链接依据、正文版本、PDF 资源限制与覆盖实测 |
| 财务与事件分析 | 现有版本仍是受限 AI 解读 | 披露事实、程序比较、未核准 AI 解读分栏；期间/主体/口径一致 |
| 行情与触发 | 待实现 | 同口径盘前成交额/量比、开盘确认；独立于杯柄 |

## 数据流

```text
已有 Security Master / 批量 FMP 身份快照
                  |
增量新闻 -> IDENTITY -> SOURCE -> ANALYSIS -> DELIVERY -> Discord outbox
             身份        原文       模型提案     持久交接      实际投递
```

- `event_ingest.py`：持采集锁，加载身份快照，增量采集新闻，建任务，运行身份及原文队列。
- `identity.py`：只读使用已发布 Security Master；不可用时尝试有 manifest/hash 的 FMP 批量输入。
- `incremental.py`：每轮读最新页，并从上轮尾页继续抓取；保存每个新闻 feed 的游标、水位与覆盖限制。
- `queue.py`：独立 SQLite，保存任务、租约、重试时间、过期时间、状态变更和采集检查点。
- `pipeline.py`：串接身份与原文任务，保留容量溢出、上游失败和身份冲突。
- `event_worker.py`：优先处理最早就绪的分析任务；继续使用原有预算、请求去重和响应账本。
- `src/alerts/ep_event.py`：从持久交接任务写入既有 outbox；真正的发送状态仍由 outbox 记录。
- `scripts/run_ep_event_worker.py`：增加只读 `pipeline`、`explain TICKER` 命令。

杯柄继续位于 `src/breakouts/live/cup_handle.py`，本轮没有修改其筛选、柄形成或触发条件。
EP 队列不调用 Web、多因子策略或杯柄检测器，只读取共享数据层的证券身份。

## 身份复用的边界

优先使用 `SecurityMasterStore.load_published()`，沿用已有文件哈希与版本校验。
它失败时，才尝试 `provider_sources/manifest.json` 指向的批量 profiles。
后者明确标成 `FMP_BULK_INPUT`，不是绕过质量检查重新发布 Security Master。

批量身份必须满足：接收时间不晚于当前、至多 96 小时、目标日期不早于上一个 XNYS 收盘日，且不在未来。
96 小时用于周末/假日边界，不表示可以忽略交易日检查。
逐只 profile 缓存仍要求不足 24 小时。新观测时间不会刷新底层批量数据的年龄。
同一 ticker 存在冲突身份时会撤销旧缓存的可用状态，保留 `AMBIGUOUS_BULK_IDENTITY`，不选择其中一个猜测。
ETF、权证、非目标交易所及非活跃证券仍不能进入正式分析队列。

SG 在本轮只读核对中已有目标日期为 2026-09-10、共 10,758 条的已发布快照。
哈希验证和所需字段检查通过，未发现重复 current_ticker；AEVA、OKTA、PENG、INTC 为活跃 NASDAQ 普通股。
这是证券身份核对，不是 EP 新闻召回率或催化判断验收。本地现有批量副本过期，未被放行为实时身份。

## 增量与队列语义

新闻每轮读取最新一页；其余页继续上轮未完成的尾部，默认有一页重叠。
只有扫到已观察范围边界才推进完成水位，并保留 24 小时重叠窗口。
财报日历成功结果缓存一小时；失败结果不作为成功缓存。
原始响应先入证据库，随后更新检查点；重复读取按文档及修订去重。

**FMP latest 接口采用移动 offset 分页，不是固定快照。**
新插入、迟到、删除或重新排序仍可能产生缺口，因此始终显示 `market_complete=false`，不声称全市场完整。

任务状态：`PENDING`、`RUNNING`、`RETRY`、`BLOCKED`、`COMPLETE`、`EXCLUDED`、`EXPIRED`。
任务 ID 基于阶段、股票、文档及修订；重发现不重置处理历史。
身份/原文任务按新闻文档区分；同一股票、同一官方 URL 和正文修订只生成一个分析任务，避免重复转载消耗分析名额。

领取采用 SQLite 事务和独立租约 token。进程退出后，默认 10 分钟租约过期可重新领取；旧 worker 不能覆盖新 worker 的结果。
容量不足、429、临时缺数等进入有退避时间的 `RETRY`，不是删除任务。
默认任务最多保留到新闻发布时间后 72 小时；消费时另检当前事件窗口，旧事件进入 `EXPIRED` 并留下原因。
这不是无限期积压：陈旧新闻不能在数日后伪装成新事件发出。

分析的预算不足、验证拒绝、未完成预留和不确定请求进入 `BLOCKED`，不会用重跑队列绕过原模型账本。
`VALIDATED` 只表示提案通过现有结构检查，仍是未核准 AI 解读。
分析完成后先建 `DELIVERY` 任务；即使随后退出，下次也能把报告恢复写入 outbox。
`PERSISTED_TO_DISCORD_OUTBOX_NOT_YET_SENT` 不等于 `SENT`；需继续查看 outbox 状态。

## 配置与排查

配置样例：`deploy/systemd/ep-event-worker.example.json`。
独立 release 部署必须显式填写生产身份路径，不能依赖 release 下相对的 `data/`、`outputs/`。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `queue_database` | `database + .pipeline.sqlite3` | 独立队列；不能与账本、审核库、outbox、密钥路径重合 |
| `identity_catalog_path` | 共享 CONFIG 目录库 | 已发布证券身份目录；release 部署填生产绝对路径 |
| `identity_snapshot_root` | 共享 CONFIG 身份快照目录 | 已发布身份快照根目录 |
| `identity_source_root` | 批量身份构建审计目录 | 读取 `asof=*/run=*/provider_sources/manifest.json` |
| `identity_fallback_requests` | 20 | 每轮无法批量解决的身份请求上限，不是候选总数上限；可设 0 |
| `news_pages_per_feed` | 4 | 每个新闻 feed 每轮页数，跨轮继续 |
| `source_jobs_per_cycle` | 8 | 每轮原文任务上限 |
| `max_sources` / `max_calls` | 3 / 2 | 每轮分析选择及模型调用上限，累计预算仍为原来的 10 美元 |

本轮不改变既有 timer 频率，也不提高模型预算。增大候选覆盖不等于允许无限调用模型。

部署新版后，在配置所在的环境运行以下命令；它们只读，不读取 API key、不请求模型、不发送 Discord：

```bash
python scripts/run_ep_event_worker.py --config /path/to/worker.json pipeline
python scripts/run_ep_event_worker.py --config /path/to/worker.json explain AEVA
python scripts/run_ep_event_worker.py --config /path/to/worker.json plan
```

`explain` 展示该股票的阶段、原因、尝试次数、重试/过期时间、最近状态变更和实际 outbox 状态。
没发现任务时返回 `NOT_DISCOVERED_IN_OBSERVED_FEED_SCOPE`，并附采集边界，不等于“没有 EP”。
每股默认只显示最近 100 个任务，返回总数及截断标记；数据库中的历史不会删除。
采集结束还写出 `output_directory/pipeline.json` 汇总，历史证据及模型日志仍保留在原库。

## 验证与后续

新增测试覆盖 65 个批量身份零逐只查询、冲突与过期身份、manifest/hash/path 校验、移动分页断点、
容量退避、并发租约、SEC 故障保留任务、模型账本去重、投递交接恢复和只读 CLI。
本轮 EP 全套、杯柄、分钟监控、Discord 路由、小时告警及盘前摘要回归结果为 **604 passed、43 subtests passed**；
`git diff --check` 通过。测试使用合成响应，没有执行真实付费模型请求或 Discord 投递。
SG 只执行了身份快照的只读核对，没有部署或启动新版 worker；新版常驻内存、真实队列积压和日内召回仍需部署验收。
不能将离线通过当作实时召回率提高。

```bash
python -m pytest tests/test_ep_*.py tests/test_breakout_isolation.py tests/test_cup_handle.py tests/test_intraday_monitor.py tests/test_momentum_discord_routing.py tests/test_discord_transport.py tests/test_momentum_alerts.py tests/test_premarket_digest.py -q
```

下一阶段从 `SOURCE` 消费器增加经过发行人核实的 IR、官方稿和 PDF 路由，继续留存来源依据、原文和失败原因。
目前 SEC 仍是自动分析的主要原文入口，现有 LLM 的财务词限制仍未解除。
之后再增加三类输出：带来源的披露观察单元、满足比较契约的程序计算、明确未核准的 AI 解读。
没有同口径、公告前可得预期时不声称 beat；管理层预期不记为已实现。
最后接入经过口径验证的盘前和开盘数据；在那之前不产生 Strong/Moderate 或开盘突破信号。
