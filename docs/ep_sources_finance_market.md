# EP 第 2–4 项实现与验收边界

本轮增加官方来源路由、分区财务解读协议和独立行情 shadow 工具。未部署 SG，未改变定时任务，未进行付费模型调用或发送 Discord。

## 数据流与隔离

```text
持久新闻任务 -> 当前证券身份 / CIK
  -> 登记官网的 IR 链接验证 -> IR 索引 / 新闻稿 -> 同站 PDF 附件
  -> 不可用时回退 SEC -> 原始响应 / 文本版本 / 来源记录
  -> ANALYSIS 队列 -> event-context 模型选择 -> 确定性引用检查
  -> 公告财务原文摘录 | 程序比较及缺口 | 未核准 AI 解读
  -> 现有预算、请求去重、响应留档、独立 Discord outbox

行情单独运行：批量扩展时段 trade / quote + 少量 1min
  -> 独立 SQLite 原始快照，默认 RAW_UNVERIFIED
  -> 经核准的数据契约 + 标准化行情输入 -> 离线指标和开盘确认
  -> 独立 shadow 信号表，不进入 Discord outbox
```

EP 不导入 `src/breakouts/live`。仅复用公共数据适配层、公共交易日历和已有 EP 证据库。杯柄检测仍在 `src/breakouts/live/cup_handle.py`，没有更改其逻辑。新的 gap-through 检测器只接收冻结的 T-1 杯形参考对象，不自行改写杯柄算法。

## 官方来源

- `official_sources.py`：登记公司、实时身份 CIK 与官网到 IR 的链接共同确定发行人归属；同一发行人域名内寻找标题匹配的新闻稿，核对正文公司名和 ticker；保留抓取记录、原始内容 hash、文本版本和来源关系。
- `configs/ep_official_domains.json`：首批 AFRM、ANF、GTLB、NYAX、PLAB。登记不是访问成功，也不是全市场 IR 覆盖承诺。其他公司仍走 SEC，缺口进入队列重试原因。
- `pdf_worker.py`：同 IR 域名公告或匹配季度索引链接的 PDF 单独解析、存档和排队；必须同时匹配发行人和报告季度。限 5 MB、80 页、25 万字符、20 秒。Linux 子进程设 CPU / 384 MiB 地址空间上限；Mac 用 CPU 上限和父进程 RSS 轮询约束。
- 现阶段不抓跨域 CDN 附件，不执行 JS，不做 OCR，不推断 PDF 表格列关系。无法确认的 PDF 不进入模型分析。
- HTTP 401/403/429、robots 拒绝不会被绕过；拒绝跨域重定向，未知链接不能扩大网络访问范围。

2026-09-11 本地只读探针实际执行 16 次 HTTP 请求（包括 robots）：

| 公司 | 官网到 IR 链接 | IR 页面访问 |
| --- | --- | --- |
| AFRM | 已找到 | 超时 |
| ANF | 当前入口未找到 | 超时 |
| GTLB | 已找到 | 成功，但动态新闻列表的完整内容未验证 |
| NYAX | 官网 robots 返回 403 | IR 索引可访问，但不能据此跳过身份关联 |
| PLAB | 已找到 | 超时 |

原始诊断在本机 `tmp/ep-official-probe-20260911.json`。这不是 SG 网络验收，更不是这些公告已经全部入库。部分来源仍需处理正确的公司入口、公开动态索引或经确认的 CDN 路径。

## 财务解读

新协议 `event-context` 保留旧 `event-claims` 的所有版本与历史记录。`WorkerConfig.analysis_protocol` 未配置时仍使用旧协议；新的部署示例显式选择新协议但保持 enabled / delivery 关闭。

1. 公告财务原文：模型选择 `disclosure_id`，程序回填完整段落。展示营收、EPS、指引和一次性项目的真实原文，而非笼统的“公布了业绩”。这是原文摘录，不冒充已完成期间、主体和口径绑定的标准化财务事实。
2. 程序比较：`compare_observations()` 只接受上游契约已核准绑定的观察单元。校验主体、指标、期间、币种、会计口径和每股口径，统一千/百万/十亿单位。预期或旧指引必须在公告前已获得；公司预估不能冒充一致预期，管理层指引不能冒充实际业绩。基数非正时只计算绝对差，不给误导性的百分比。
3. AI 解读：允许财务词汇，逐条引用原文，明确标记管理层预期或可能影响。数字留在摘录层，不让模型重新抄写；没有程序核准的比较，不允许生成 beat、超预期或指引升降结论。引用检查通过仍不代表语义已核准。

**当前自动来源仍没有完整、可比、公告前的一致预期绑定流程，因此自动报告的“程序比较”区为空并显示 `NO_BOUND_ASOF_COMPARABLE_BASELINE`。比较引擎已经测试，但不能把这说成已经自动完成财报 beat/raise 分析。**

新协议沿用累计 10 美元实验预算、日额度、响应留档和请求去重，没有为新协议另开预算。选取原文为有上限的开头及财务相关完整段落，不声称全文覆盖。

## 行情 Shadow

- `src/data/fmp.py`：新增稳定版 batch-aftermarket-trade、batch-aftermarket-quote、单交易日 1min 原始接口。单筆成交 size 不是累计成交量。
- `market_worker.py`：每次最多 5 个 ticker、7 次请求、90 秒软截止；原始快照进入独立 `ep_market_receipts`，只留允许的市场字段。未核准时不会把 API 返回值自动转换成可靠量比。
- `market.py`：在可靠契约下计算美东 04:00–09:30 已完成分钟的累计量与成交额、相对上一交易日收盘的涨幅、过去 20 个交易日同一时刻基线、常规时段 VWAP / RVOL。缺失窗口不是零量；价格过期、重复分钟、口径不明均阻断。
- 成交额必须来自可靠的实际成交额字段或供应商核准的成交量加权价格；不使用典型价乘量冒充实际成交额。当前原始 FMP 接口是否足以提供该口径尚未放行。
- `gap_detector.py`：日线杯合格且前收不高于杯沿、今日开盘高于杯沿；等待完整 1/5/15 分钟开盘区间之后的新收盘突破 ORH，同时高于杯沿和 VWAP、常规同刻 RVOL >= 2、到已知当日低点距离不超过 8%。形成中 K 线不触发。信号按股票 / session / 杯形快照 / OR 窗口独立去重。
- 当前 shadow 候选参数：gap >= 5% 才进入观察；盘前成交额 >= 100 万美元且 RVOL >= 3 才允许市场质量升级；gap >= 10% 加独立核准 Strong 催化才给 shadow Strong。**这是待验证的初始工程参数，不是已证明盈利的阈值，也不代表截图系统的精确规则。**
- 催化真实性、直接性、时效和质量来自独立证据对象；未核准 AI 文本不能自动将这些标志改为 true。没有杯形只保留 EP_WATCH，不冒充杯柄突破。

真实行情规范化、历史扩展时段覆盖验证、催化评级对象自动构建、T-1 杯形快照桥接和调度仍是下一轮接线/验收工作。现在支持原始数据采集与标准化输入回放，不是已经上线的自动 EP 开盘提醒。

## 操作入口

以下均需在已更新代码的机器上运行；本轮没有替你部署或创建任何定时任务。

```bash
# 默认只展示计划：不请求行情、不写数据库、不发送消息
.venv/bin/python scripts/run_ep_market_shadow.py --database /home/projects/quant/data/ep/market_shadow.sqlite3 --symbols AFRM GTLB PLAB

# 显式采集原始行情。依然不调用 LLM、不发 Discord
.venv/bin/python scripts/run_ep_market_shadow.py --database /home/projects/quant/data/ep/market_shadow.sqlite3 --symbols AFRM GTLB PLAB --execute

# 只读网络能力探针，默认不执行；加 --execute 才访问公开页面
.venv/bin/python scripts/probe_ep_official_sources.py --output /home/projects/quant/data/ep/official_probe.json

# 原有逐股排查入口
.venv/bin/python scripts/run_ep_event_worker.py --config /etc/quant/ep-event-worker.json explain PLAB
```

行情命令可附 `--queue-database /绝对路径/pipeline.sqlite3`，使单股 explain 同时展示最近市场采集/回放状态。行情库与队列库必须是不同路径。PDF 依赖通过 `requirements-ep.txt` 安装。

## 验收重点

新增回归覆盖：官网关联缺失、CIK 冲突、跨域重定向、PDF 分开留档、完整段落 ID 回填、非法财务比较、公告前预期要求、负基数、旧协议兼容和共享预算去重；行情覆盖形成中分钟、同一时刻历史窗口、过期价格、重复数据、杯形缺失、主题联动和 shadow 去重。测试行情/催化契约明确是合成 fixture，不能用于生产放行。

接口依据：[FMP 批量扩展时段成交](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-trade)、[FMP 批量扩展时段报价](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-quote)。文档列出 API 不等于账户数据覆盖和字段口径已获验证。
