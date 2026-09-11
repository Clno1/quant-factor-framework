# EP 未核准 AI 公告解读：SG 受限上线说明

## 范围与边界

用户选择无人审核发布，但每条明确标注“未核准的 AI 解读”。这不是 Strong/Moderate 评级，不是盘前价格雷达，也不产生杯柄或 EP 开盘交易触发。现有杯柄、小时动量、板块轮动策略和配置不作替换。

当前模型输出范围是定性事件提案。中文数字、日期、金额、财务增长比较、EPS/营收等未经财务关系核验的表述会被阻止。原文引文可以包含数字；不代表程序已验证这些数字的期间、单位或财务口径。该版本尚不能完整复刻示例截图的财报拆解。

## 时间线与数据流

1. SG systemd 启动独立 worker，读取原有 FMP 环境和专用 EP 配置。
2. 从 SEC 公司索引获取 ticker/CIK 映射，缓存 24 小时；这是当前映射，不是历史 PIT 股票池。
3. 有界抓取 FMP 新闻、新闻稿与财报日历。优先为已出现在 SEC 索引中的新公司事件取得 profile；仅允许活跃 NASDAQ/NYSE/AMEX 普通股与 ADR，排除 ETF。
4. 按主体和新闻时间查找 SEC 8-K/6-K 的 HTML 附件，对齐发行人及原文。每轮最多尝试 3 篇，优先尚未尝试的主体，防止按股票代码排序造成长期饥饿。未知身份、容量延后、原文未匹配都留下原因。
5. 从已匹配原文中选择近期且可确认日期窗口的公告。失败抓取记录不占用这一步的有效原文名额；过期材料不会消耗在线模型预算。
6. 只把最多 8 个开头段落交给模型，明确记录不完整覆盖。模型提案并选择段落 ID，程序回填完整原文，不让模型重新抄写引用。
7. 审核返回结构、文档版本、引用 ID、数量表达与范围。`VALIDATED` 只代表协议处理完成，不能当作事实已验证；`content_status` 区分有提案与全部被拦截。每条仍保留 `semantic_support_verified=false`。
8. 独立 Discord outbox 检查证券身份、公告时效、内容、路由和去重。无需人工 APPROVE；人工 REJECT/REVOKE 如果存在则阻止发布。

隔夜边界：公告只有日期而供应商时间落在前收盘后的，可作为带额外时间警示的未核准解读；不能因此宣称真实发布时间已确认。旧公告、未来公告、日期冲突及无法定位日期的公告仍不发。

## 代码位置

- `src/breakouts/ep/event_ingest.py`：在线有界采集与 SEC 索引接入。
- `src/breakouts/ep/service.py`、`discovery.py`：证据收集与官方附件匹配；原有调用默认行为保留，在线优先队列显式启用。
- `src/breakouts/ep/llm_event_claims.py`：提示词、段落 ID 协议、逐条结构性检查。
- `src/breakouts/ep/llm_service.py`：复用已有预算、请求去重、请求与响应留档。
- `src/breakouts/ep/event_worker.py`：挑选原文、有界模型周期。
- `src/breakouts/ep/event_workflow.py`：报告重建、可选人工决策，决策绑定不可变原文和提案。
- `src/alerts/ep_event.py`：通知传输与独立 outbox；策略目录不导入通知层。
- `scripts/run_ep_event_worker.py`：运行、预检、状态、报告入口。
- `scripts/configure_ep_event_discord.py`：私密配置频道，不输出 webhook。

## SG 部署

- 主目录 `/home/projects/quant` 的生产源码未覆盖；采用独立发布目录及软链接 `/home/projects/quant/ep-event-current`。
- 当前发布 `/home/projects/quant/releases/ep-event-review-7c4dfbe7b1e22b2f`；源码清单逐文件 SHA-256 验证，不含环境文件、密钥或数据库。
- 运行 Python：`/home/projects/quant/.venv/bin/python`。
- 配置：`/etc/quant/ep-event-worker.json`，SEC 联系信息单独置于私密环境文件；无密钥进入仓库。
- 模型：Kimi CN `kimi-k2.6`。使用原有私密 key 文件，不复制到源码。
- EP 专用 webhook 文件：`/etc/quant/ep-event-discord.key`。来自原有动量配置，并通过 Discord 元数据核实与板块轮动不是同一频道。
- 预算与原文沿用 `/home/projects/quant/tmp/ep-llm-trial-20260909/data/ep/llm_trial_20260909.sqlite3`，不要重建此账本或删除 RESERVED 来重置额度。
- 输出 `/home/projects/quant/data/ep/event_reports/history/` 与 `latest.json`。
- 独立 outbox `/home/projects/quant/data/ep/event_outbox.sqlite3`。
- 可选决策库 `/home/projects/quant/data/ep/event_reviews.sqlite3`，不作为自动发布前置条件。
- SG unit：`quant-ep-event-review-root.service`，timer：`quant-ep-event-review.timer`；名字中的 review 是沿用的部署命名，不表示强制人工审核。
- 计划工作日美东 08:20、09:20、10:20，随机抖动最多 30 秒，自动处理夏令时，不补发机器停机期间错过的任务。未在 Mac 添加任何定时任务。
- 已在 SG 执行启用并核对 `enabled / active / waiting`。本次核对下一次时间为 2026-09-10 20:20:01 CST（北京时间）。这不是保证每轮有消息；没有合格原文/提案或额度不足时只记录状态。

## 成本与可靠性

每轮最多 2 次新模型请求、3 篇原文、20 次 profile 查询；新闻每个 feed 最多 2 页。单模型调用输出上限 4,000 tokens。日预算 $3、月预算 $10、整个试验累计 $10，同时生效。账本使用保守预留值，不等同 Kimi 实际扣款；定价和汇率快照不是账单保证。

systemd 限制内存 512 MiB、CPU 为一个核的 50%、任务数 32、总时限 12 分钟、数值库单线程。进程文件锁防重入，SQLite 事务防重复预留和并发发送。服务不自动无限重启。

每文档/文本版本只入队一次，单股票冷却一小时、消息 TTL 90 分钟。发送前重新检查原文、时效、撤销状态和绑定频道；没有 message ID 不声称成功。超时或结果不确定进入 UNKNOWN，不自动重发；明确可重试的限流等最多 3 次尝试。

## 操作与排查

在 SG 执行只读状态检查：

```bash
/home/projects/quant/.venv/bin/python /home/projects/quant/ep-event-current/scripts/run_ep_event_worker.py --config /etc/quant/ep-event-worker.json status
systemctl status quant-ep-event-review.timer
journalctl -u quant-ep-event-review-root.service -n 40 --no-pager
```

检查单次报告：查看 `event_reports/latest.json` 的 `ingestion`、`selection.skipped`、`items[].report.rejected`、`notifications`。源层逐股细节用既有 `run_ep_radar.py --db <上述账本> dossier <TICKER>` 查询。空输入不等于零 EP，PARTIAL 不等于全市场已扫描。

停用后续运行：

```bash
systemctl disable --now quant-ep-event-review.timer
```

这不会中断正在执行的 oneshot。紧急停用执行 `systemctl stop quant-ep-event-review-root.service`，之后须检查预算 RESERVED 和 outbox UNKNOWN，不自动重置/重试。

回滚时先停止 timer、确认服务结束，再将 `ep-event-current` 指回已验收版本并核对相应 unit；预算与 outbox 始终保留。旧协议无法校验的新报告应保留不可发布，而非伪造兼容。

## 验收记录与尚缺内容

最终版本本地 448 项测试通过；SG 442 项通过、6 项可选 PDF 测试跳过。覆盖原有 EP 回归、通知隔离、预算去重、并发 outbox、源队列、时效和真实输出范围错误。systemd 单次端到端运行结果为 success；内存/CPU 限额已配置，但退出后 MemoryPeak 未可用，不能把限额当作已测量峰值。

2026-09-10 小样本真实 Kimi 请求共 2 次：PLAB 的 3 条提案全部被数量/期间范围检查拦截；NYAX 4 条中保留 3 条。它们均为历史公告，没有作为当前消息发送。重复运行 0 次新请求，预算预留保持 $2.489062（含本轮前全部调用）。

随后在线采集到了 LSAK 官方原文，补充 `today released` 日期格式回归后，完成真实解读及 Discord 投递。这次投递检查发现中文“每股收益/指引”绕过范围检查，已补回归并原地更正消息 `1547523863401996331`：仅保留“Lesaka Technologies发布了财年业绩公告”的未核准提案。未再次请求模型，未重发消息；前后文本和结果保存在 SG `event_reports/lsak_message_correction.json`，原 outbox 投递快照保持不变作为历史审计。

截至这次更正，试验账本共 21 次调用、累计预留 $2.527505，距 $10 预留上限尚余 $7.472495。它不是供应商实际账单。历史 `latest.json` 是当时运行快照；更正后的内容以重新生成的 `report` 与更正审计为准。

Discord 通道部署测试成功，消息 ID `1547521301021724702`。测试文字明确不包含市场信号。

实时 FMP 和 SEC 索引访问已验证，初次源队列存在股票代码排序抢占；后续修复优先顺序、日历空时间和失败记录挤占有效原文查询等问题。实时覆盖依然是有界、部分覆盖，不能承诺扫描所有截图股票。

真实性边界：一条当天公告链路跑通，仅证明该样本的采集、提案、引用与投递可执行，不证明覆盖率、财务事实正确率或交易收益。范围检查属于防护，不是依靠不断加关键词复刻完整分析。

SG 缺少可选 pypdf，PDF 附件测试跳过；此在线路径只处理 HTML SEC 附件，PDF 显式标注独立处理需求。尚未补齐广泛 IR 原文回退、历史盘前同刻量比、报价/价差确认、财务关系可靠核准、Strong/Moderate 评级和 ORH 交易触发。它们不能因为 AI 消息能发送就被宣布完成。
