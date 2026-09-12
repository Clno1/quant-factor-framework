# EP 候选分流与原文覆盖验收

日期：2026-09-12。范围：修复真实队列的原文抓取分流，使用 SG 留档原文回归。不优化推送文案，不扩大模型请求。

## 1. 实际发现的问题

读取 SG 生产队列，在 `2026-09-11T15:50:00+00:00` 这一队列评估时间检查了 1,101 项 SOURCE 待处理任务。它们均在事件窗口内，并非主要被过期新闻堵塞。旧提示中 1,075 项为 UNKNOWN。

重要限制：这是当前队列快照上的排序回归，不是历史队列状态重建，不能据此声称当日 08:15 已发现或已完成分析。历史状态仍应使用 `explain --as-of` 检查。

- “Hsbc Acquires 22,500 Shares of Fluor”等证券持仓报道误归公司并购，占用了优先抓取名额。
- “Oracle Q1 Earnings”等已发布财报标题未被有效分流。
- 同一股票多个事件可以挤占一轮容量；同一证券的负面身份查询结果也可能被重复请求。
- 新分流实际选出了 ORCL，SEC 申报和附件均可访问，但期间匹配失败。公告标题没有财年，正文 p0014 明确写出 `Oracle Corporation (NYSE: ORCL) today announced Q1 FY27 results`。
- Oracle 官网能建立到官方 IR 的链接证明，但 SG 请求 IR 站返回 403。不能把网页搜索可见等同于生产可抓取。

## 2. 本轮代码

- `classifier.py`：财报、财报预告、持仓更新分流；旧队列可重新计算路由提示，不覆盖原始证据。提示始终不是催化事实或评级。
- `pipeline.py`：保留 WATCH、重试及泛资讯容量；优先不同发行人，有余量才给同一发行人第二个任务，每轮每家最多两个。失败的单证券身份查询缓存 15 分钟。
- `pipeline.py` / `run_ep_event_worker.py`：新增只读 `source-plan`，并保存每轮选取快照 `source:selection`，展示股票、任务、等待时间及新旧提示。最多扫描 5,000 项，达到上限明确标记可能截断。
- `event_identity.py`：识别明确的 `Q1 2027 Earnings` 期间；财报预告不与已发布财报合并。
- `source_verifier.py`：在已核验 SEC CIK、ticker、申报链之后，从同一发行人的过去式发布声明提取期间，记录段落 ID。标题、正文或新闻期间冲突仍拒绝；不从当前日期猜财年，不从财务表格或未来计划拼接期间。
- `event_ingest.py`：标明 `unmapped_symbols` 仅指 SEC 公司索引未匹配，不等于证券身份未覆盖，保留兼容字段。
- `ep_official_domains.json`：加入 ORCL 的官方域名链。IR 被拒绝时继续走 SEC，不绕过拒绝。

现有杯柄、量价确认、模型预算及 Discord 发送代码未改动。

## 3. 真实队列到原文

不是手工指定八只股票：验收脚本从真实待处理队列选取了 ADBE、ZUMZ、DAVE、MNY、HOFT、ORCL、SHIP、FOXA。隔离运行共发起 28 次公开来源 HTTP 请求，不修改生产队列。

初次抓取八项均未匹配；修复期间关联后，对同一批留档响应离线回放：

| 样本 | 原文链回归 | 边界 |
|---|---|---|
| ORCL | DOCUMENT_MATCHED | 完整 SEC 链、正文期间段落、待分析队列及模型输入结构通过；未调用模型 |
| MNY | DOCUMENT_MATCHED | 明确 Q2 2026 财报标题与官方正文关联通过，进入待分析队列 |
| ADBE、ZUMZ、HOFT | 未匹配 | 未补齐必要的期间/发布声明证据，不猜测、不放行 |
| DAVE | 窗口内无支持申报 | 新闻正文实际提到 PLAY，且为财报预告；新分流已识别预告，不将其当作已发布业绩 |
| SHIP、FOXA | 窗口内无支持申报 | 行业泛资讯不是公司新催化的证据 |

`2/8` 只描述这批来源回归，既不是全市场召回率，也不是 EP 胜率。ORCL、MNY 的成功只到原文及待分析输入，不等于通知端到端成功。

## 4. 验收与隔离

- SG 验收代码：`/home/projects/quant/tmp/ep-discovery-20260912-code`。
- SG 原始回执：`/home/projects/quant/tmp/ep-discovery-20260912/queue-live-1`。
- 本地原始队列报告：`sg-queue-sample.json`；公开原文压缩样本：`tests/fixtures/ep_discovery_sources_20260912.json`。
- 测试涵盖错季度、错财年、未来计划、旧发布声明、其他发行人、缺失年份，以及原文成功进入 ANALYSIS 队列。SG 完整 EP 测试 571 项通过，结果保存在 `ep-release-tests.xml`。
- 所有模型请求、Discord 消息均为零。没有修改生产服务、生产配置或定时器；没有在 Mac 创建定时任务。本轮代码尚未切换到 SG 生产 release，也未提交 main。
- 验收只证明功能回归；不将 systemd 输出的异常偏小内存统计当作生产资源验收。

只读诊断命令（当前在 SG 独立验收代码运行）：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-discovery-20260912-code/scripts/run_ep_event_worker.py --config /etc/quant/ep-event-worker.json source-plan'
```

该命令仅显示执行时当前队列，不模拟历史。

## 5. 尚未解决与下一步

1. 全市场盘前行情 WATCH 生产入口仍未接通。新闻队列改善不等于能够发现所有没有新闻的异动股票。
2. 缺年份或正文访问失败的事件，需要补充同一事件的可靠证据；不能继续单纯放宽标题规则。公司 IR/PDF 的覆盖仍受域名清单、访问限制及解析能力约束。
3. 需要在真实盘前窗口记录首次行情发现、新闻首次到达、原文到达与分析完成时间，再测发现延迟和漏报原因。
4. 量比口径、可比预期输入、Strong/Moderate 综合评级、开盘触发和 ORCL/CPRT/RH 自动通知全链路，仍未完成生产验收。

因此当前不能声称已经完整复刻对方系统。下一阶段应接价格优先的候选入口，缺量能或催化的股票保持 WATCH，再逐步补证，杯柄继续独立。
