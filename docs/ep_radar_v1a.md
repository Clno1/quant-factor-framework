# EP v1A 第一批实现：数据、留存与排查

日期：2026-09-07。本文描述已经写入仓库的第一批代码，不是完整 EP 系统上线声明。

2026-09-08 更新：原文补抓、段落证据、归属核查和未来抽取接口已作为独立第二批加入，见[原文证据实现说明](ep_source_enrichment.md)。下文“本批”仍指第一批历史交付；最新能力与限制请结合第二批文档阅读。

原方案：[范围与验收标准](ep_v1_scope_acceptance_20260906.md)。数据限制仍以[能力验证报告](ep_data_capability_audit_20260906.md)为准。

## 1. 本批完成与未完成

已实现：独立命令入口、全局新闻/公告分页入口、按日财报日历、证券身份查询、证据规范化与版本留存、候选逐项原因、只读 report/explain、请求预算和单库采集串行锁。ETF 默认排除，权证等特殊证券不会因开启 ETF 就被放行。

**尚未实现：公告原文抓取、发行人角色的可靠核准、跨文章事件聚类、按交易 session 自动过期、LLM、同钟盘前量、Strong/Moderate 评级、开盘价格触发、Discord outbox、Web 页面或 SG 部署。** 本批没有修改原杯柄算法、参数、分钟服务或多因子逻辑。

因此目前每个候选的 `grade` 为 null，`catalyst_quality` 为 UNASSESSED；市场数据缺失是 null/UNKNOWN，而不是零量或薄量。`EP_WATCH` 在这里表示待核验观察，不能读作“已经证明它是 EP”。

## 2. 代码在哪里

| 文件 | 作用 |
|---|---|
| [models.py](../src/breakouts/ep/models.py) | CatalystSnapshot、PremarketSnapshot、EpSettings；日期、数字、URL、版本 ID 的契约 |
| [provider.py](../src/breakouts/ep/provider.py) | 可注入的数据接口及 FMP 组合层；不直接发 HTTP |
| [fmp.py](../src/data/fmp.py) | 唯一供应商适配层；新增 EP 新闻、按日财报、严格证券身份方法 |
| [store.py](../src/breakouts/ep/store.py) | 独立 SQLite；保留文档版本、收到时间、每页结果、运行状态与评估 |
| [classifier.py](../src/breakouts/ep/classifier.py) | 财报、合同、并购等标题线索分类；不是经核准的催化判断 |
| [ranker.py](../src/breakouts/ep/ranker.py) | 证券排除、待核验状态、缺失原因和财报原值；暂不评分 |
| [service.py](../src/breakouts/ep/service.py) | 有限范围、一次性采集与评估；无永久循环或发送器 |
| [run_ep_radar.py](../scripts/run_ep_radar.py) | collect / report / explain 命令 |
| [test_ep_radar.py](../tests/test_ep_radar.py) | 数据边界、时间版本、隔离、去重、预算、接口和 CLI 测试 |

杯柄继续位于 [`src/breakouts/live/cup_handle.py`](../src/breakouts/live/cup_handle.py)，两者没有相互导入策略或运行服务。仅使用已有通用文件锁与 FMP 请求设施。

上层 [`src/breakouts/__init__.py`](../src/breakouts/__init__.py) 改为延迟加载原有导出名，保留原导入方式，避免只读 EP 查询隐式加载旧扫描器和 pandas。

## 3. 顺着一次运行理解

1. CLI 校验参数，打开专属 SQLite，取得该数据库的采集锁；同库采集串行，读取不需要这个锁。
2. 新建 run，保存开始时间、请求范围、参数和算法版本 `ep-observation-v1a.1`。
3. 获取 FMP 全局最新股票新闻和公告，各自分页，不以 Return20、ADR 或杯形为入口条件。
4. 对请求日期逐日获取财报日历，不把多个日期塞进一个可能被 4,000 条上限截断的大请求。
5. 每次请求结束保存收到时间、行数、窗口外行数、拒绝原因、分页及失败状态。规范化的证据快照与这页记录在一个事务中保存；个别缺代码/时间的记录保留有限原始字段用于排查，不阻断后续有效页。
6. 按实际收到时间读取当前可见的证据版本，再按 ticker 汇总。上一轮已收到且仍在本次日期窗口内的证据可以继续使用。
7. 在预算内查询证券身份；有效身份缓存 24 小时。支持的事件标题优先，其次财报日历，再次普通新闻；同一优先级先处理未缓存的候选。没轮到的股票依旧出现在报告中，明确标记身份待处理。
8. 标题分类给出线索；证券类型、交易所和活跃状态决定是否排除。未核准全文、EPS 口径和盘前行情不会被自动放行。
9. 一次事务保存候选评估和最终运行摘要；进程退出。不推送、不交易、不创建定时任务。

全局新闻使用 latest 页接口，本批没有实现任意历史日期的全市场新闻回溯。向前翻页达到上限仍未覆盖请求窗口，会报告 `PAGINATION_LIMIT_REACHED`，不假称已经查全。[FMP Stock News 官方接口](https://intelligence.financialmodelingprep.com/developer/docs/stable/stock-news)。

## 4. 数据存储与时间语义

默认数据库是 `data/ep/observations.sqlite3`，与现有分钟状态库分开。误把已有非 EP 数据库传给 CLI 会被拒绝。

| 表 | 保存什么 |
|---|---|
| `ep_runs` | 请求窗口、配置、运行状态与最终摘要 |
| `ep_pages` | 每个来源/日期/页的收到时间、接收与拒绝数量及原因 |
| `ep_documents` | 文档 ID、内容版本 ID、原始发布时间、版本 first_seen、规范化供应商摘要/财报原值 |
| `ep_observations` | 每次运行实际观察到哪个版本，以及收到时间 |
| `ep_profiles` | 证券身份查询结果、失败状态及查询时间 |
| `ep_evaluations` | 每次运行中每只股票的评估与证据引用 |

URL 去重移除 fragment、尾部斜杠和常见 tracking 参数，但保留有业务意义的 query。相同 URL 的内容变化保留为新 revision；原文先改动后恢复旧版，也能按实际观察顺序读取。不同 URL 的同一事件尚未做语义合并，不能把文档数量解释为独立事件数量。

FMP 新闻的无偏移时间暂按 America/New_York 解释，并在证据中标记 `timezone_assumed`。财报日历没有准确公告时刻，不把 event date 的午夜造作发布时间。`--as-of` 检查的是本系统实际收到/完成的时间，不允许今天抓到的数据出现在昨天的报告里。

本批尚不读取日线、分钟或 DuckDB 行情数据，不创建另一套行情版本。事件摘要和状态暂按自己的版本契约存于独立 SQLite；原文证据归档以及与现有 Raw/Curated 目录的正式对接留待下一批设计，不把新闻塞入 OHLCV 发布表。

## 5. 怎么运行和排查

以下命令在仓库根目录执行。`collect` 需要项目依赖及已有 FMP 密钥；密钥只从既有环境/配置加载，不通过 CLI 参数传入。

```bash
python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 collect \
  --start 2026-09-04 --end 2026-09-07 \
  --max-pages 3 --max-profiles 20 --max-requests 40 --deadline-seconds 120

python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 report

python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 explain SNOW

python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 explain SNOW \
  --as-of 2026-09-03T08:15:00-04:00
```

日期需根据要查询的窗口调整，默认最多 7 个自然日，不能请求未来日期。财报发现可以在周末/假日手工运行，但不是交易触发。`--include-etfs` 是唯一放开 ETF 的显式开关。

`report` 和 `explain` 不联网、不创建缺失数据库，也不加载 FMP/网页模块；已用 Python 3.12 的 `-S` 模式验证，不需要第三方包。但仍应使用项目的 Python/SQLite 运行时：本机 Apple Python 3.9/SQLite 3.43 首次只读打开新 WAL 库实测失败，不能据此判定 EP 数据丢失，也不自动改成可写模式绕过。SG 的实际运行时兼容性留到部署前验证。

`--run-id` 可指定旧运行；不指定时查看最近运行。`--as-of` 没有可用历史时返回 `NO_RUN_AVAILABLE_AS_OF`，不会拿最新数据补给过去。

排查重点：

| 输出 | 如何理解 |
|---|---|
| `NOT_FOUND_IN_FETCHED_SCOPE` | 这个运行的候选中没有它；应继续看覆盖页和拒绝记录，不等于没有催化 |
| `PAGINATION_LIMIT_REACHED` / `REPEATED_PAGE` | 翻页尚未证实覆盖完整或供应商重复返回同页 |
| `CALENDAR_LIMIT_REACHED` / `CALENDAR_DATE_MISMATCH` | 日历仍达到上限或返回日期不符合请求 |
| `PROFILE_BUDGET_EXCEEDED` / `PROFILE_NOT_FOUND` | 候选保留，但证券身份尚未处理或无法确认 |
| `SECURITY_TYPE_EXCLUDED` / `EXCHANGE_EXCLUDED` | 明确的证券或交易所政策排除 |
| `FUTURE_PUBLICATION` / `PUBLICATION_TIME_MISSING` | 记录时间不符合证据契约，见每页 rejected 明细 |
| `NO_SUPPORTED_CATALYST_HINT` | 当前标题未识别到支持的事件线索，不代表已证明“无催化” |
| `FULLTEXT_NOT_VERIFIED` / `PREMARKET_DATA_UNVERIFIED` | 本批尚缺能力，不是该股票的量不足或形态失败 |
| `DISABLED_SHADOW_ONLY` | 本批根本没有消息发送路径，不是 Discord 发送失败 |

运行级 `COMPLETE_OBSERVATION` 只表示这次有限请求和身份处理没有记录缺口，**不表示全市场覆盖完整，更不表示任何候选可以买入**。`PARTIAL` 表示有预算、覆盖或身份缺口；CLI collect 对此返回退出码 2 并仍输出报告。未来 systemd 必须理解此状态，不能把它当程序崩溃反复重启。

请求预算在每个请求前检查，单请求使用剩余预算与配置 timeout 的较小值。底层 requests timeout 是网络等待超时，不是硬实时中断；等待采集锁的时间也不计入预算。v1A 不声称严格在 N 秒内结束。429/401/403 会停止本轮继续请求，不自行绕过；旧 FMP 调用的默认重试行为保持不变。

如果进程崩溃，已提交的页和证据仍存在；下次取得采集锁后把未结束运行标为 `INTERRUPTED`。本批没有分页游标断点续跑，下一次重新读取所配置窗口并以内容 ID 去重。

## 6. 本地验证材料

本批相关回归：177 项测试通过，另 25 个 subtests 通过；3 条警告来自既有 FMP DataFrame 的 pandas fillna 行为。范围覆盖 EP、杯柄、分钟监控、旧突破扫描、板块/动量频道路由和盘前摘要，不是全仓库测试。测试依赖安装在 `/tmp/quant-ep-v1a-test`，没有修改依赖文件或 SG 环境。

- [EP 测试](../tests/test_ep_radar.py)：使用合成输入和 mock，不依赖实时网络。
- [17 案例离线验证脚本](../reviews/2026-09-07-ep-v1a/replay_audit.py)：使用 9/6 留存的数据，不查询网络。
- [17 案例结果](../reviews/2026-09-07-ep-v1a/replay_report.json)：17 个 ticker 均进入证据观察结果；未伪造证券身份、量比或评级。
- [真实接口限额冒烟结果](../reviews/2026-09-07-ep-v1a/live_smoke_report.json)：本地手工执行，不是 SG 部署或交易时段性能验收。

真实冒烟执行 8 个请求，形成 261 个待核验观察对象；只有 2 个查了身份，另 259 个明确待处理，且有分页上限和 15 个无效代码记录。它不是 261 个 EP。该次初始冒烟促使后续补上“坏记录留存但继续分页”的处理，后续行为由新增测试验证，未额外宣称已完成全市场实跑。

离线案例报告为 PARTIAL 是预期行为：证券身份没有在回放中编造，两个未留档的周末财报日期也没有冒充零事件。它验证的是已知输入兼容性，不是回测收益、盲扫召回率或截图 Strong 等级的一致性。

本地已生成的案例数据库可直接排查：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py --db /tmp/quant-ep-v1a-audit-20260907.sqlite3 explain GTLB
```

临时数据库不是长期归档；机器可读 JSON 结果保存在上述 reviews 目录。重新运行案例验证需提供新的数据库路径，避免混淆采集时间。

## 7. 下一批工作

先补稳定原文获取和发行人证据核准，再加入有来源的结构化事实抽取及 LLM 接口。与此同时解决盘前累计量、分钟量口径和历史同钟基线。正式评级、开盘触发、独立 outbox 和 SG shadow 部署仍需各自验收，不随本批代码自动开启。
