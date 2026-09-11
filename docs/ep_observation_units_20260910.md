# EP 财务观察单元与实际 Prompt

## 最新新闻为什么仍会配错期间

新闻的发布时间和正文里财务数字所属的期间不是同一个概念。
一篇刚发布的财报通常同时包含本期、去年同期、上一季度，以及下季度或全年指引。
只取最新文章可以缩小消息范围，但不能消除文章内部的这些关系。

上轮 PLAB 实验实际送入的是已归档、核验来源的 SEC 公司公告段落，并非仅将一条 FMP 新闻标题发送给模型。
现有入口 `src/breakouts/ep/llm_contract.py:prepare_request` 要求 SEC 原文和发行人关联核验。
FMP 新闻发现和财务原文提案是两个环节，不能混为“FMP 已经替我们核准了所有事实”。

归档公告中的非 GAAP 段落依次写了：

| 期间 | 净利润总额 | 稀释每股收益 |
| --- | --- | --- |
| 本次报告的第三季度，财年 2026 | $29.4 million | $0.50 |
| 去年第三季度，2025 | $29.4 million | $0.51 |
| 上一季度，2026 第二季度 | $24.9 million | $0.42 |

本期期间由公告开头定义；后两项的期间写在本段比较句中。
模型上轮选对了 `$0.50`，但期间 ID 指向后面的 `third quarter` / `of 2025`。
这不是 FMP 漏掉了发布日期，也不是原文没有正确答案，而是模型将不同观察值的字段拼在一起。

## 是否把事情做复杂了

需要区分两个产品目标：

1. **新闻催化摘要**：告诉用户公司刚发布财报、签了合同或上调指引，并附时间和来源。这可以先做得较轻，不一定需要全面提取每个 EPS 和期间。
2. **带精确财务依据的 EP 分析**：例如“营收超预期 5%、非 GAAP EPS 超预期 39%、剔除一次性税收收益后如何”。这要求口径、期间、主体一致；超预期还需要当时可得的一致预期数据，不能仅凭一篇公司新闻推断。

我们最近一直在验收第二层，目的是验证截图里那些具体数字，而不是证明最简单的新闻摘要必须这么复杂。
当前“很多片段 ID + 多个字段独立选择”确实增加了模型负担。更贵的模型或更长 prompt 不能从根本上禁止字段错配。
建议产品上保留轻量摘要和精确事实审核两层，未核准数字不写成肯定结论；本轮没有擅自改动现有生产扫描或推送逻辑。

## Prompt 在哪里

上轮实际请求由三部分构成：

- `src/breakouts/ep/llm_span_selection.py` 的 `RULES`：真正的 system 消息，要求返回片段 ID。
- `src/breakouts/ep/llm_batches.py` 的 `SCOPES["eps"]["focus"]`：user JSON 中的 EPS 任务重点。
- `src/breakouts/ep/llm_provider.py:responses_payload`：组合 system、user 中的段落/ID 目录，以及结构化响应 schema。

真实 system 中关于关系的原话为：

```text
Select company, period and unit context only where applicable, not merely where words match.
Use null for unresolved fields.
```

EPS 批次重点为：

```text
Reported issuer EPS. Prefer latest-period GAAP and non-GAAP diluted EPS.
Keep accounting bases separate. Exclude guidance.
```

这里应明确承认一个设计不足：旧 claims 协议有更详细的字段规则，
但上轮 ID 协议使用自己独立的 `RULES`，并未把旧 `batch_rules()` 全文拼进 system。
所以不能拿旧 claims 的长 prompt 来声称上轮已向模型强调了所有细节。
这些指令也只是要求模型注意关系，没有在输出结构上禁止错配。

已生成可直接阅读的导出：

- `reviews/2026-09-10-ep-span-selection/observation-review/prompt_readable.md`：实际 system 全文、批次重点、三个原文段落，以及新原型 prompt。
- 同目录 `plab_actual_request.json`：完整旧请求，包括 user 的片段 ID 目录和响应 schema，不含密钥。
- 同目录 `plab_observation_prompt_preview.json`：新原型的离线预览，尚未发送给模型。

旧请求由归档数据和原有构造器重建，并核对完整请求摘要等于
`728c112488a978d301e5b97b180ca6db03ca2b41f00bd4aef98a1c4e64741f26`。
归档 JSON 的字段顺序曾被排序，导出时恢复批次构造器原有顺序后核对完整摘要，不改任何字段值。

## 本轮实现的观察单元

新增 `src/breakouts/ep/llm_observations.py`，采用独立离线协议 `ep-observation-selection-v1`。
不是让模型继续分别挑日期、数字和公司，而是程序先从受支持的明确比较结构组成：

```text
单元 A：Photronics / EPS / $0.50 / NON_GAAP / DILUTED / 本期第三季度 FY2026
单元 B：Photronics / EPS / $0.51 / NON_GAAP / DILUTED / 比较期第三季度 2025
单元 C：Photronics / EPS / $0.42 / NON_GAAP / DILUTED / 比较期第二季度 2026
```

每个单元绑定原始数值、主体、期间、口径、每股类型、原文偏移和段落摘要，生成稳定 `observation_id`。
模型新输出结构只允许 `catalog_hash`、`scope_status` 和 `observation_ids`。
返回单元 A 后程序复制整个 A；模型不能同时提供“换成 2025 年”。额外字段会被拒绝。
当前任务只允许本期单元，历史单元即使被选中也会以 `COMPARATIVE_NOT_CURRENT` 拒绝。
修改目录后重新算摘要也不能通过：校验时重新从可信输入生成目录进行比对。

对于本例，取出本期两个 EPS 已可由程序确定完成，**并不需要为这个机械选择再调用 LLM**。
LLM 更适合在这些绑定数据和引用基础上做筛选与摘要；但是否需要调用，应该由实际分析目标决定。

## 范围和结果

本轮实现是**限定 EPS 正文语法的离线原型**，不是通用财报解析器：

- 支持“GAAP/Non-GAAP 净利润归属某公司股东为金额，或每股金额，比较上期金额及期间”的完整并列比较句。
- 本期缺少段内期间时，要求同一输入里有唯一的发行人名称、交易所 ticker 和本次报告期间的公告开头；有歧义则整段保留未绑定。
- 比较项必须有自身明确期间；不按发布日期猜数字所属期间。
- 口径与主体仅在受支持的完整并列语法内从段首继承；每个 EPS 绑定自己的每股类型，不继承净利润的 million 数量级。
- 未知句式、缺少比较期间、指引混入、百分数伪装成 EPS 等情况，不做部分猜测式提取。
- 不支持的营收表格、特殊项目和其他正文仍列为 `unbound`。GTLB、ANF 本轮没有强行生成观察单元。

PLAB 离线生成 **6 个绑定单元，其中 2 个本期、4 个比较期**。
旧模型的 `$0.50 + 2025` 仍被标为与绑定期间不符，没有修改旧响应。
程序选取两个本期单元的合同测试通过，但标记为程序参考选择，**不是新模型的成功结果**。
单元仍为 `financial_semantics_verified=false`：有限语法绑定不能替代来源真实性、完整覆盖和财务语义最终核准。

本轮新增模型请求 **0**、预算写入 **0**。没有更换模型、改 prompt 后付费重试、同步 SG 或开启 Discord。
原有 `span-run`、台账及线上协议保持不变。

本地 EP 与隔离测试共 **351 passed**，本轮观察单元新增 17 项；未运行全仓库或 SG 测试。
包括真实六项绑定、旧错误回放、正确历史项与本期隔离、非法字段混入、目录篡改、版本变化、
主体/期间歧义、非 PLAB 公司与数值替换，以及真实旧请求摘要复核。

## 复现与下一步

完整报告位于 `reviews/2026-09-10-ep-span-selection/observation-review/report.json`。
重新执行需要指定尚不存在的输出目录：

```bash
cd /Users/huozhihong/Documents/Quant
/tmp/quant-ep-v1a-test/bin/python reviews/2026-09-10-ep-span-selection/run_observations.py --fixture tests/fixtures/ep_live_selection_20260910.json --output-dir /tmp/ep-observation-review-new
```

下一步优先确认产品主线是否先用“新闻催化摘要 + 仅引用核准数字”，不要让全面财务解析拖住摘要产品。
技术上仍需补齐通用观察单元构造所需的表格行列上下文，并将新协议接入已有预算/留档后，
才开展有明确验收目标的小批模型测试。当前离线预览不是可以直接拿去 SG 付费执行的命令。
