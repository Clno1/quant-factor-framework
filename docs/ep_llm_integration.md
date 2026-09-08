# EP LLM 事实提案接入

日期：2026-09-08。

2026-09-09 更新：用户已确认 GPT-5.4 mini、首轮累计 $10、SG 手动验收，并明确授权上传。独立验收目录已部署，SG 63 项针对性测试通过，实际模型调用尚未开始，等待用户在服务器配置密钥。已新增跨日/月累计预算限制和固定试验入口。见 [SG 试验记录](ep_llm_trial_20260909.md)。下文 9/8 的未选型描述保留为当时状态。

## 1. 当前状态

**接口、请求约束、确定性校验、持久化预算与离线测试已实现；真实模型联调尚未完成。** 用户尚未选择模型、提供密钥或确认付费试验预算。本轮没有模型 HTTP 请求、费用、SG 部署、定时任务或 Discord 发送。

第一版只实现 OpenAI Responses 适配器，配置允许 `gpt-5.4-mini` 或 `gpt-5.4`，默认不选择模型、不开启调用。Claude/Gemini 尚未实现，不能仅凭通用接口就声称支持。

这不是完整 EP 评级器，也不能证明能复刻截图。新增的是更广泛的**有原文约束的模型事实提案**能力；通过校验不等于财务语义已经核准。

## 2. 数据流与文件

```text
已归档、发行人身份匹配的 SEC 正文
  -> llm_contract.prepare_request：来源准入、版本、段落和覆盖率
  -> llm_provider.responses_payload：严格 JSON Schema，无工具调用
  -> llm_service.run_llm：SQLite 事务预留预算、领取唯一请求
  -> src/data/llm_transport.py：一次 HTTPS 请求，无自动重试
  -> llm_provider.extract_response：检查完成状态、拒答、模型和用量
  -> llm_contract.validate_response：逐条核对引用与字段
  -> store.ep_llm_calls：保留响应、通过/拒绝提案和预算记录
  -> llm-history：只读查看，后续人工或语义审核
```

| 文件 | 职责 |
|---|---|
| `src/breakouts/ep/llm_contract.py` | 提案 Schema、输入来源约束、引用和基础口径校验 |
| `src/breakouts/ep/llm_provider.py` | 模型配置、Responses 请求与响应解析 |
| `src/data/llm_transport.py` | HTTP 供应商传输；密钥不进入保存的请求或报告 |
| `src/breakouts/ep/llm_service.py` | 只读预检、预算估计、请求去重、运行编排 |
| `src/breakouts/ep/store.py` | 独立 `ep_llm_calls` 表、事务预算和历史查询 |
| `scripts/run_ep_radar.py` | `llm-plan`、`llm-extract`、`llm-history` 命令 |
| `configs/ep_llm.env.example` | 默认关闭的环境变量示例，不会自动加载 |
| `tests/test_ep_llm.py` | 模拟模型、恶意引用、并发、预算和失败测试 |

没有把 HTTP 客户端塞进策略目录，也没有修改 `src/breakouts/live/cup_handle.py`。新提案不会覆盖旧分析、人工审核或候选快照，不会进入分钟触发、Strong/Moderate 评级或消息发送。

## 3. 模型能提议什么

支持营收、EPS、ARR、cRPO、backlog、留存率、GMV、利润率、特殊项目、回购及合同金额等字段。模型必须提供原文数值、主体、期间、单位、会计口径及段落引用；缺失上下文可明确留空，不能补写成已核实事实。

校验包括文档版本一致、段落存在、引用逐字属于输入、字段出现在引用中、指标与数字共同出现，以及部分显式 GAAP/non-GAAP、basic/diluted 冲突。会检查完整原段落，避免从 `non-GAAP` 截取 `GAAP` 冒充另一口径。拒绝重复 JSON 键、非有限数值、额外字段、伪造来源、截断输出及工具调用。

**校验器不是第二个财报分析师。** 表格行列归属、主体和期间语义、否定句、特殊项目的税务处理仍可能理解错误。因此通过记录标为 `TEXT_GROUNDED_ONLY`，附带审核事项。模型报告了原文中的数字，不代表该数字就属于模型所说的期间或公司。

本轮没有接入共识预期的历史版本比对、自动计算 beat/指引上调、一次性收益调整或催化重要性评级。旧 `catalyst.py` 的类型与时效逻辑保持原状；新提案尚未自动成为它的已核准输入。

## 4. 来源与输入边界

目前只放行已提取、文档匹配且有注册 CIK/当前 SEC ticker 身份关联的 SEC Archives HTML 正文。不是任意 URL，不向模型发送 FMP 新闻全文或电话会文字。官方 IR 或 PDF 即使已经归档，也不因此自动获得本轮 LLM 准入。

输入按原段落顺序取前缀，上限 60,000 字符、500 段，序列化请求另有 120,000 字节上限。不是继续靠关键词挑句子。超过范围明确记录覆盖不完整，尚未实现长文分块合并；这些上限也不保证输出能覆盖全部事实。

默认输出上限 4,000 tokens，截断则失败，不把半份 JSON 当成功。公告里的指令作为不可信数据，模型没有工具权限。`store:false` 只是 API 存储选项，不等同于零数据保留承诺。

## 5. 预算与失败控制

示例默认值为每日 $3、每月 $50，按 UTC 自然日/月统计，金额以整数 micro-USD 保存。它们是**关闭状态下的建议配置，不是用户已经批准的支出**。

9/9 新增 `EP_LLM_TOTAL_MICROUSD`（默认 10,000,000），对同一个数据库中全部调用的累计预留求和，不随日/月切换重置。专用试验入口固定每日 $3、每月 $10、累计 $10，忽略环境中更宽松的预算和模型配置。累计预留仍不是供应商账单的绝对保证，也不能通过换数据库来继续同一次试验。

发送前在同一个 SQLite 数据库中事务预留预算；多个 worker 对同一请求只会领取一次。请求键包含模型、正文版本、提示和 Schema 等内容。重复成功、失败或进程崩溃留下的请求均不会自动重新付费。改模型、提示或正文版本会生成新请求，必须重新占用预算。

预留采用保守字节估计并计入最大输出，不是真实 tokenizer 测量。失败也不退回预留，因此可用额度可能比实际账单少。响应模型不符或按返回用量计算的金额超过预留，会挂起后续新调用，要求人工核查；不能靠无限重试绕过。

这是**本 EP 数据库的调度预算，不是供应商账户的账单硬上限**。其他数据库、其他项目、费率变化、网络不确定性不受它完全控制。启用前应复核费率、配置供应商侧可用的用量控制与告警，并从小额试验开始。没有自动清除失败或计费异常记录的命令。

## 6. 本轮验证

完整回归：**374 passed，25 subtests passed**。有 3 条现有 FMP/pandas FutureWarning。覆盖 EP、杯柄、分钟监控、小时提醒、频道路由、盘前摘要和代码隔离等测试。

对本地研究库 `data/ep/research_shadow_20260908.sqlite3` 的归档正文运行只读预检，结果如下；不是历史市场回放，也不是模型提取质量评测。

| 正文 | 输入段落覆盖 | 字符数 | mini 单次保守预留 | GPT-5.4 单次保守预留 |
|---|---:|---:|---:|---:|
| GTLB | 283/283 | 27,365 | $0.084585 | $0.281925 |
| ANF | 343/343 | 32,794 | $0.096189 | $0.320605 |
| NYAX | 41/41 | 10,972 | $0.046469 | $0.154870 |
| PLAB | 157/157 | 13,624 | $0.056753 | $0.189150 |
| AFRM | 无合格正文 | 未送入 | 不调用 | 不调用 |

数字是预算预留，不是账单或实际 token 消耗。研究库仍为 schema 5，没有被这次预检修改。测试中的可写库已验证 schema 5 -> 6 迁移；首次实际可写操作会增加独立 LLM 表，旧库的只读查询不会偷偷迁移。

## 7. 如何使用

在项目根目录、已安装项目依赖的 Python 环境中执行；下面仅预检，不读取模型密钥、不产生费用：

```sh
.venv/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 \
  llm-plan a7ff3ffe-9687-49bc-ad0c-0470c08058c7 --model gpt-5.4-mini
```

`llm-history SOURCE_ID` 可查看该来源的已留存调用。`source_id` 对应具体来源记录，不是 ticker。

付费执行必须同时具备：用户选定模型与预算、服务器环境变量 `EP_LLM_ENABLED=true` / `EP_LLM_MODEL` / `EP_LLM_API_KEY`、以及显式 `llm-extract SOURCE_ID --execute`。不要把密钥贴在聊天、命令参数或提交进仓库。本轮没有配置这些运行条件，也没有安装 SG unit。

## 8. 下一步验收

1. 选一个模型，确认试验预算，再在目标服务器环境变量中配置密钥。
2. 先用少量已核实公告实测，人工逐条标注数字、期间、主体、会计口径、漏提取及引用正确性；比较实际 tokens、费用、延迟和失败率。
3. 专门测试 GTLB basic/diluted EPS、ANF 一次性退款、NYAX 收购对象收入等陷阱；不能只看输出文风是否像截图。
4. 通过后再建设审核后的事实合并、催化质量判断和带证据的摘要；盘前量口径与开盘触发仍需独立验收。

建议先限定总计 $10 的试验额度，但需要用户确认。没有真实模型样本前，不报告准确率，也不声称能完整复刻对方 EP 分析。

## 9. API 依据

请求格式依据 [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 和 [Responses create](https://developers.openai.com/api/reference/python/resources/responses/methods/create)。预算配置参考本轮核对的 [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) 与 [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) 标准费率；正式启用前仍需复核。

## 10. 代码与研究数据的发布边界

GitHub 保留源码、配置模板、文档、离线测试及验收脚本。`data/ep` 数据库、EP 原始行情/新闻/正文证据与生成的 JSON 报告不随源码提交；文档中这些报告链接指向研究机的本地归档，不保证在 GitHub 或全新克隆中可打开。需要这些归档才能运行的固定快照审计会明确跳过，常规 `tests/test_ep_*.py` 使用模拟数据，不依赖私有归档或 API 密钥。

SG 主项目代码同步不等于启用 EP 服务。此前的独立试验目录和累计预算数据库仍是本次 $10 试验的唯一入口，不复制新库来重置预算；LLM 和 Discord 均需继续遵循原有显式启用门禁。
