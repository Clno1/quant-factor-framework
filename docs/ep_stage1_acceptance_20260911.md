# EP 第一阶段部署与验收

记录时间：2026-09-11 20:13，中国时间。后续队列、预算和发送数会继续变化。

## 结论与边界

已在 SG 上线**受限的自动公告事件摘要版**，不是完整 EP 突破雷达。
真实新闻发现、持久队列、官方正文、带引用解读、独立 Discord outbox 已跑通。
来源覆盖、队列及时性和模型语义仍有明确缺口，不能宣称第一阶段所有上线指标已通过。

按用户本轮选择，个人 Discord 使用 `commentary_style=personal`，无人审核自动发送，
不显示“未核准 AI 解读”通用标签；实际缺失的比较基准、部分公告覆盖、推测措辞和来源链接仍保留。
内部 `semantic_support_verified=false`，没有伪造人工批准。
消息不是 Strong/Moderate 评级、买入指令或已确认的量能/开盘突破信号。

## 生产部署

- SG：`43.156.89.232`，Python：`/home/projects/quant/.venv/bin/python`。
- 活跃链接：`/home/projects/quant/ep-event-current`。
- 当前不可变版本：`/home/projects/quant/releases/ep-stage1-9b04a5312a414b43`。
- 发布包 SHA-256：`d601e03f5a795d1d14bdf6d3a0fad6f8babe6abd1910f06cca1dbfd7e52ea0ca`。
- 575 个源码、测试及配置示例文件由逐文件哈希清单校验；不含真实环境文件、密钥、研究库。
- systemd：`quant-ep-event-review-root.service` + `quant-ep-event-review.timer`。
- 美东周一至周五 08:00–11:50，每 10 分钟处理一次，另有最多 30 秒随机延迟。
  按 `America/New_York` 自动处理夏令时；这是处理频次，不是必发消息频次，也不是全天监控。
  当前 timer 只判断工作日，不判断交易所假日；本版仅生成公告摘要，不生成交易触发。
- 同一 oneshot 服务不会并发启动；超时 12 分钟，MemoryMax 512 MiB、CPUQuota 50%、TasksMax 32。
  长周期可能错过某次 timer tick，不能把十分钟配置解释为严格十分钟完成 SLA。
- 配置：`/etc/quant/ep-event-worker.json`，保留既有私密 key/webhook 文件和期望频道绑定。
- 只切换 EP 独立版本，没有覆盖 SG 主仓库、其他并行开发、杯柄或板块轮动的服务。
- 在指定 `.venv` 中只新增 `pypdf==6.10.0`，没有升级其他依赖。
- 本轮没有创建本地 Mac 定时任务，也没有自动 git commit/push。

## 真实发现验收

不是手工输入截图中的股票。使用实际 FMP 新闻/公告与财报日历，复用已发布证券主表，
先做证券身份与当前事件窗口检查，再进入来源队列。
证券主表版本：`496db448b54e4ae49701512b3acfd64c`，目标交易日 2026-09-10。

截至记录时，48 个来源任务实际尝试过，6 个找到匹配官方正文：

| 股票 | 官方正文路径 | 来源队列建立至正文匹配 | 本轮模型/发送情况 |
| --- | --- | ---: | --- |
| RUM | SEC 8-K 附件 | 216 秒 | 新协议验收完成；原公告已发送，没有重复发 |
| MNY | SEC 6-K/披露附件链 | 217 秒 | 保留 4 条财务摘录；一条错误预测归因被拦；已发送 |
| REF | SEC 8-K 附件 | 949 秒 | 原文摘录和带引用解读；已发送 |
| ZUMZ | SEC 披露附件链 | 1,222 秒 | 原文摘录和带引用解读；已发送 |
| LPTH | SEC 披露附件链 | 1,220 秒 | 已获取正文，分析任务仍等待容量/重试时间 |
| TMDX | SEC 披露附件链 | 1,329 秒 | 人事及重申指引公告解读；已发送 |

这些等待时间包含人工验收及版本切换间隔，**不是公告首次发布到推送的延迟，也不是稳定运行 SLA**。
没有跳空/量能确认，以上不能称为六只已确认 EP；负面财报、人事公告也可能作为事件摘要出现。

766 个身份合格任务对应 509 个不同股票；一个股票可能有多篇新闻。
来源队列仍有 718 个未尝试任务，另有 42 个待重试；最老未尝试任务约 28.6 分钟。
这说明容量和优先级仍需继续验证，**没有达到全市场完整召回或低延迟覆盖**。
采集报告中的约两千个候选还混合了历史窗口、全球证券和日历，不应称为约两千只 EP。

## 本轮修复

1. `incremental.py`：单条无效新闻不再让抓取永远停在第 0 页。失败条目留档，后续页继续，
   但该轮明确保持 `PARTIAL_INVALID_RECORDS`，不推进“完整扫描”水位。
   SG 已验证尾页推进至第 8 页以后；offset feed 仍可能移动，不能声称无漏抓。
2. `pipeline.py`：来源任务按事件线索分配容量，默认 8 个位置中优先 6 个财报/指引/合同等线索，
   保留 2 个一般线索名额。不删除低优先任务；关键词只用于抓取排序，不核准催化。
3. `public_articles.py`：DNS 放入可超时终止的子进程，保留公共 IP 校验和 TLS 原域名，
   返回明确的 DNS 超时/不可用原因。未使用代理、绕过 robots、忽略 403 或关闭 TLS 检查。
4. `run_ep_event_worker.py`：新增 `collect --execute`，只采集，不请求模型或发送通知；
   增加采集、模型、发送和总耗时记录。
5. `queue.py`：已完成分析但发送交接缺失时，恢复 DELIVERY 任务，不重复付费调用。
6. `ep_event.py`：个人消息格式不改变同公告/文本版本去重、冷却、频道校验、过期和拒绝规则。

## 官方来源与 PDF

官方域名登记从 7 家扩至 11 家，新增 KR、MNY、CPRT、DSGX 的公司根域/IR 路径及 CIK。
但“登记了域名”不等于“SG 已读到正文”。本轮独立 IR-only 实测：

- MNY：公司根页与 IR 关联可建立，IR 读取超时。
- DSGX、KR：根页访问/解析出现网络错误；新增 DNS 诊断用于后续周期定位。
- CPRT：SG 返回的根页未建立有效 IR 链接，不能因为 HTTP 200 就放行。

**本轮新增成功正文仍来自 SEC，没有新增成功的 SG 直接 IR 全链路。**
CPRT 存在同一窗口多个可匹配附件；继续保留歧义，不随意选一个。
KR/RH 等“已发现新闻但未匹配正文”的状态不等于没有事件。
上轮 PLAB PDF 文字提取能力继续保留；AFRM 图片型附件作为独立缺口，不阻断其他公司的队列。
未以 OCR 或模型猜测冒充已读取的 AFRM 文字正文。

## 解读协议与真实错误

生产切换为 `analysis_protocol=event-context`：模型选择财务摘录 ID，程序回填完整原文；
另给出带段落引用的中文解释。数字不在自由解读中重抄；没有核准的公告前比较基准，
不声称 beat/双超，也不把管理层预期当作已实现财务事实。

MNY 的真实响应把既有 IFRS 现金奖励处理解释成“公司预计”未来影响。
新增前瞻来源检查后，重新校验已归档响应即可拒绝该条，未为此再次付费请求。
MNY 最终保留原文摘录，不为了让内容显得丰富而保留错误预测归因。

这仍是结构/引用/数量表达审核，不是完整语义证明。尤其“原文未披露某项”的广义断言，
不能仅由有限输入段落证明；REF 抽查仍提示这类范围表达需要进一步收紧。
业务推断保留“可能”，不把结构检查通过或少数成功样本当作事实全部核准。
部分数字观点可能被拒绝，报告保留拒绝理由和原文；尚未完整复刻截图中的深度分析。

## 资源、费用和消息

SG 实际 `/proc/meminfo` 总内存约 1,963 MiB、无 swap，不是此前假设的 4 GB。
两轮受 systemd 限额约束的生产执行：

| 执行 | 采集 | 模型 | 发送 | 总时长 | systemd 内存峰值 | CPU 时间 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 手动首轮 | 53.123s | 29.805s | 1.632s | 84.559s | 197.7 MiB | 8.225s |
| timer 自动第二轮 | 50.065s | 15.254s | 1.418s | 66.737s | 188.3 MiB | 7.520s |

两轮退出码均为 0，timer enabled，等待下一轮。没有在生产故意杀死付费中的请求。
故障恢复通过 SG 测试验证租约过期、断点抓取、去重、重试、费用保留及交接恢复；
真实环境仅观察了两个生产周期，不能宣称连续五日稳定或高峰负载验收完成。
SG 同时存在其他任务的 failed 状态，本轮未诊断/修改，不代表整个项目所有服务均健康。

本轮合计新增 5 次真实 Kimi 请求；沿用唯一原有预算账本。
截至记录时累计 27 次调用、保守费用预留 $2.853858，剩余额度 $7.146142，累计上限仍为 $10。
这不是供应商最终结算金额。日限额和累计限额仍生效，到限不能静默换账本或自动追加预算。

部署后新增 4 条成功回执：REF、ZUMZ、MNY、TMDX。
MNY 的待发任务在下一轮发送；RUM 复用已有去重记录，未重复发。
此时 outbox 累计 SENT=6（含本轮之前 2 条），没有待发项。
发送结果不确定时保留 UNKNOWN，不盲目重发来追求表面成功率。

## 复现、排查和回滚

本地 666 tests + 43 subtests 通过；SG 最终包 666 tests 通过。
打包验收曾发现漏带测试依赖脚本和配置示例，均修复后重新打包并全量通过。
`systemd-analyze verify` 通过；唯一提示来自既有 tat_agent 的旧 PIDFile 路径，与 EP 无关。

所有生产命令都在 SG 上运行：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/ep-event-current/scripts/run_ep_event_worker.py --config /etc/quant/ep-event-worker.json status'
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/ep-event-current/scripts/run_ep_event_worker.py --config /etc/quant/ep-event-worker.json explain KR'
ssh root@43.156.89.232 'systemctl status quant-ep-event-review-root.service quant-ep-event-review.timer --no-pager'
```

排查依次看 IDENTITY、SOURCE、ANALYSIS、DELIVERY 和独立 outbox；DELIVERY COMPLETE
只表示已交给 outbox，实际是否送达看 `delivery[].state` 与 message_id，不把交接完成当作发送完成。
完整队列汇总使用同一 CLI 的 `pipeline`；这些只读命令不产生模型费用或消息。

SG 审计文件：

- `/home/projects/quant/data/ep/stage1-20260911/acceptance-summary.json`：本记录时点的真实队列统计。
- 同目录 `sg-tests.xml`、`inspection.json`、`ir-audit.json`：测试及来源审计。
- `/home/projects/quant/data/ep/event_reports/history/`：持续生产周期报告。
- `/home/projects/quant/data/ep/stage1-20260911/pipeline.sqlite3`：当前生产持久队列，不要当临时文件删除。
- `/home/projects/quant/data/ep/event_outbox.sqlite3`：持续消息状态。
- `rollback/` 保存旧配置、两个 unit、旧版本路径以及只作审计的账本/outbox 备份。

回滚必须等当前 EP 周期结束；脚本不会强行终止付费中的请求：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/releases/ep-stage1-9b04a5312a414b43/reviews/2026-09-11-ep-stage1/release.py rollback'
```

回滚只恢复配置、unit、版本链接，**不恢复旧费用账本或 outbox**，避免重复调用和发送。

## 下一项验收

优先观察积压增长与真实财报召回，补 KR/CPRT/DSGX/RH 等已发现但无正文的具体路径，
再改善有限段落解读的范围声明。保留至少五个交易日的生产日志和高峰资源样本；
现有两个周期不构成该验收。盘前成交量口径、历史同刻 RVOL、评级和 ORH 触发继续不放行。
