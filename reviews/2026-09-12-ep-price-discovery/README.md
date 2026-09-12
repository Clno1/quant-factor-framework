# EP 价格优先发现与真实延迟验收

本轮完成代码和 SG 隔离实测，未切换生产 release、未修改定时器、未提交 main。所有模型请求和 Discord 消息为零。杯柄逻辑不变。

## 数据现在如何流转

```text
已发布证券身份快照（股票/ADR，排除 ETF 等）
  -> 每批 100 只请求价格，保存批次与逐股结果
  -> 对比明确 T-1 的 execution close，默认上涨至少 4%
  -> PRICE_WATCH：催化、成交量、跨日复权确认仍为 UNKNOWN/false
  -> 持久 NEWS 任务：按股票补查新闻与新闻稿
  -> 既有 IDENTITY -> 事件 SOURCE -> ANALYSIS 队列
  -> 沿用原有预算、引用核验和消息机制，不由价格发现模块直接发送
```

4% 是本轮**可配置宽筛初值**，不是原始 EP 战法定论，也不是交易触发阈值。盘中采用当前价格相对前收盘的涨幅，不把它写成盘前跳空。

价格缺失、报价过期/未来时间、身份不合格、日线基准缺失均独立记录。报价必须在本交易时段、距接收时间不超过 120 秒。盘前时间戳按毫秒解析，正常时段按秒解析；这只是明确、可拒绝的输入假设，尚未完成盘前生产口径验收。

日线只读访问统一 DataContract，不回退到旧 CSV，也不使用 total-return `adj_close` 计算缺口。基础数据允许部分覆盖，每只股票必须有自己的 T-1 close 才能观察。整份契约仅计算和保存一次摘要，逐股保存契约 ID。

## 目录和入口

| 文件 | 本轮职责 |
|---|---|
| `src/data/fmp.py` | 唯一 FMP 请求层：批量价格、指定股票新闻；单次请求，不在策略内另写 HTTP |
| `src/breakouts/ep/price_discovery.py` | 扫描、价格约束、T-1 基准、持久游标、独立价格 SQLite、只读逐股状态 |
| `src/breakouts/ep/price_news.py` | 无新闻也能排队补证；保留最早到期任务，其他名额优先新鲜大涨幅；持续重试，明确容量延后 |
| `src/breakouts/ep/latency.py` | 首次发现、补查开始、原文匹配与分析状态的真实时钟；支持 as-of |
| `src/breakouts/ep/queue.py` | 新增 NEWS 阶段和延迟事件表；旧队列可迁移，旧库只读查询不写表 |
| `src/breakouts/ep/pipeline.py` | 原文及附件匹配时关联准确的 ANALYSIS job ID，避免同股不同事件串接完成时间 |
| `src/breakouts/ep/event_ingest.py` | 可选接入价格和补证；该部分失败不阻断原有新闻入口 |
| `scripts/probe_ep_price_discovery.py` | 只写隔离数据库的一次性验收，不执行模型或消息发送 |
| `scripts/run_ep_event_worker.py` | 新增 `price-scan`、`price-status`、`latency` 诊断命令 |

`price_discovery_enabled` 默认 false；开启时必须配置独立 `price_database`。默认每轮最多 60 批、60 秒扫描预算、4 只补证任务。截止预算在批次间检查，网络/DNS/解析及基准准备仍可能使总运行稍超预算，不承诺硬实时；SG 验收另设 240 秒进程硬上限。

证券身份、新闻、SOURCE、分析的原有数据库和价格数据库不混用。价格记录保留原始 price/timestamp、观察时间、阶段、缺失原因与基准版本，不保存 API key 或异常中的认证 URL。

## SG 真实结果

实际时间为 **2026-09-11 美东正常交易时段**，不是历史 08:15 盘前回放。

- 身份池提供 5,739 只当前股票/ADR；这不是“全市场无遗漏”的证明。
- 前 500 只小样本扫描耗时 14.12 秒，5 次请求，40 个价格观察；进程峰值约 268 MiB。
- 大范围第一轮完成 4,900 只、50 次请求，其中最后一次失败；游标停在 4,900，没有假报完成。
- 重新运行从游标恢复，9 次请求完成剩余 839 只，耗时 18.41 秒，未从第一只重扫。
- 两轮不同时间的结果合计：336 个 PRICE_WATCH、4,090 个未过宽筛、1,313 个因报价不新鲜/时间不合格被拦截。5739 = 336 + 4090 + 1313。
- 因两次手动运行间隔，本次扫描窗口从 18:19:56 到 18:24:20 UTC，约 264.5 秒。不能将两段处理时长相加后宣称实现连续自动扫描 SLA。
- 第二轮进程峰值约 271 MiB（Linux `ru_maxrss`）；systemd 显示的异常偏小 Memory peak 不作为验收依据。验收限制为 CPU 50%、MemoryMax 640M。
- 自动发现包括 ACVA、PENG、SBET、RXT、SEI 等；ORCL、CPRT、RH 在本次盘中取样时未过宽筛。这不能反推它们盘前没有异动。

基准契约来自 `US_EQUITY_COVERAGE` 的 2026-09-10 已发布版本。契约中的约 71.3% 是其自身股票池分母上的覆盖率，不是本次身份池的行情成功率，不能混用。

### ACVA 的真实时间线

不是手工塞入股票名单。价格扫描发现后，补证优先级自动选中了 ACVA。

| 阶段 | UTC 时间或耗时 |
|---|---|
| 报价时间 | 18:20:03.000 |
| 首次发现价格观察 | 18:20:05.363，参考涨幅 +44.53% |
| 开始按股票补查新闻 | 18:21:04.205，发现后 58.84 秒 |
| 收到相关报道/新闻稿 | 18:21:05.220，发现后 59.86 秒 |
| 匹配官方原文 | 18:24:25.360，发现后 260.00 秒，收到新闻后 200.14 秒 |
| 分析排队 | 18:24:25.355 |
| 分析开始/完成 | 未发生，不填写零秒或估算值 |

原文匹配对应标题 `Copart to Acquire ACV, Expanding Position Across the Vehicle Remarketing Ecosystem`。首轮部分报道匹配失败，续跑处理同股另一条新闻稿后匹配，利用已经抓取的来源记录，没有新增 SEC 请求。

2.36 秒只表示这条报价时间到程序观察时间，**不表示市场首次异动后 2.36 秒发现**。约 260 秒含队列等待和手动验收间隔；这是观察到的时间，不是自动调度性能承诺。公告实际发表于前一晚，本次下午才开始验收，也不能算作盘前成功发现。

ACVA 仍只是价格观察和待分析事件；不能据此声称它是可交易 EP，更不能忽略其可能属于并购被收购方的性质。

## 延迟的含义

- `quote_to_detection_seconds`：报价时间到首次观察，不能代替真正的异动起点。
- `price_to_news_search_seconds`：发现后等了多久才开始补查。
- `publication_to_news_receipt_seconds`：文章标注发布时间到系统首次接收；发布时间口径仍取决于来源。
- `news_to_source_seconds`：收到新闻到匹配发行人原文，包含任务排队/失败重试。
- `analysis_queue_wait_seconds` / `analysis_elapsed_seconds`：准确 ANALYSIS job 的等待和处理时间，包含其重试；不是纯模型响应时延。
- 尚未进入/完成的阶段返回 null；`news_search_transitions` 和 `pipeline` 给出当时状态和原因。

所有首次记录幂等，不会用较晚重试覆盖首次时间。原文与价格只声明“同股票、同 session 的关联”，不伪造因果；as-of 查询不读取未来完成状态。源已经先于价格匹配时，不制造负延迟。

## 验收与排查

SG 完整 EP 离线测试 **593 项通过（49.22 秒）**，包括 22 项新增测试及真实回执回放。本地聚焦回归通过；本地完整套件在原生调用中无输出，已终止，完整验收采用 SG Linux 结果。

留档：`sg-first-pass.json`、`sg-resumed-pass.json`、`sg-latency.json`、`ep-tests.xml`。真实价格离线样本在 `tests/fixtures/ep_price_discovery_20260912.json`；上述报告 JSON 是工作区验收产物，受仓库既有忽略规则影响，不等于已经提交 Git。

SG 代码：`/home/projects/quant/tmp/ep-price-discovery-20260912-code`。
SG 验收数据：`/home/projects/quant/tmp/ep-price-discovery-20260912-live-d`。

只读查询 ACVA 延迟（配置禁用采集、模型和推送）：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-price-discovery-20260912-code/scripts/run_ep_event_worker.py --config /home/projects/quant/tmp/ep-price-discovery-20260912-live-d/diagnostics.json latency ACVA --session 2026-09-11 --as-of 2026-09-11T18:24:26+00:00'
```

将最后的子命令替换为 `price-status PENG` 可看最后价格记录；替换为 `explain ACVA --as-of 2026-09-11T18:24:26+00:00` 可看当时队列状态。不会发外部请求。

## 尚未放行

1. 盘前接口的真实可用性、时间戳/量能口径、跨日拆股/分红调整仍需盘前实测。正常时段报价成功不能替代这一验收。
2. 价格候选可达数百只，本轮仅执行 4 只股票的补查，最终仍有 332 只 NEWS 未开始。现有每轮 4 只/每 10 分钟调度若原样使用会明显滞后。不能直接据本轮成功就打开生产开关，下一步需要独立扫描与证据消费者容量验收。
3. 尚未完成所有候选的原文覆盖、可比预期输入、量比、Strong/Moderate 综合评级、开盘确认和自动分析全链路时效。律师调查报道仍可能占用 UNKNOWN 来源容量，需进一步实测分流。
4. 缺量、缺催化的价格观察不直接发买入信号，也不自动冒充确认 EP。杯柄检测保持独立。

官方接口依据：[批量正常时段报价](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-quote)、[指定股票新闻搜索](https://intelligence.financialmodelingprep.com/developer/docs/stable/search-stock-news)、[扩展时段交易接口](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-trade)。接口存在不代表本项目已核准盘前成交量或同时间量比。
