# EP 原文片段 ID 回填：离线验收

后续进展：预算、去重与响应留档已接入，三次真实模型验收见
[真实调用报告](ep_span_selection_live_20260910.md)。本文保留上一轮离线验收时的状态和统计，
文末“尚未实施”描述的是当时状态，不代表最新进展。

## 结论与边界

已经实现“模型只选择片段 ID，程序回填原文”的独立协议与本地校验器。
本轮只用**人工选定的参考 ID**验证回填机制，没有调用模型、读取 API key 或写入预算台账。
不能把参考案例通过率当成模型准确率，更不能当成 EP 评级或交易胜率。

来自上轮三个正样本的 12 条失败提案，使用明确标注的人工参考选择后，12 条均通过现有文字证据校验。
PLAB 中两对表格/正文案例对应相同 EPS，去重后是 **10 条不同提案**，不是 12 个独立事实。
NYAX 另有一条人工空选择对照，检查空列表处理，不宣称程序已自动识别其并购催化。

`model_selection_accuracy=null`、`financial_semantics_verified=false`、`eligible_for_rating=false`。
缺失币种、跨行期间和单位适用关系仍需核查，并未为了通过验收补默认值。

## 从旧流程到新流程

旧流程要求模型重复输出公司名、期间、数字和长引用。模型可能把 `$0.49` 抄成 `$ 0.49`，
把 `$ (38,574)` 抄成 `$38,574`，或把多行表头拼成原文没有的一句话。

新流程：

1. 读取已验证公告的归档段落；新来源仍须经过既有 SEC 来源与发行人关联检查。
2. 程序切出数字原子和文字边界，为每个片段计算稳定 ID。
3. 给模型的是段落、片段目录和选择 schema。每个字段只允许返回 `start_id` / `end_id`，不允许提供原文、引文或字符偏移。
4. 程序校验 ID 属于本次目录，同一字段的首尾片段来自同一段落且顺序正确。
5. 程序根据 ID 的已存位置截取归档原文，生成 `value_text`、`subject_text`、引用等字段。
6. 回填结果继续交给原来的确定性校验器，检查指标/数字同行、口径、原文引用等。
7. 同时保留模型选择、精确字符范围、回填结果、拒绝原因和未核准标志。

这里的“片段 ID”不是模型编写的字符位置。范围由两个**预先存在的 ID**定义，期间的多个片段单独保存。
ID 绑定文档、正文修订、段落正文摘要、位置和片段类型；另一份公告或另一版原文的 ID 不通用。

## 具体防护

- **不让模型抄文字**：选择响应 schema 禁止 `value_text`、`quote`、`start` 等额外字段。
- **保留数字原貌**：币种、符号、会计括号和小数构成不可拆的数字原子；仅取其中的美元符号作为数字会被拒绝。
- **不跨段拼接**：同一字段的起止 ID 必须来自同一段落，指标和数字也必须来自同一段落。
- **不合成期间**：两行表头对应两个 `period_spans`；回填 `period_text=null` 并保留 `PERIOD_FRAGMENTS_NOT_ASSEMBLED`。
- **不偷偷裁引用**：回填完整归档段落；超过现有引用长度上限则拒绝，不能删掉否定词或每股例外以求通过。
- **不混用多个实际值**：一个实际值范围不能同时吞下本期与上期数字。指引允许有明确连接符的双值范围。
- **不默默扩大输入**：目录最多 1200 个片段、序列化请求最多 100000 字节；超出的段落列为省略，覆盖率标为不完整。
- **不改变旧审计记录**：没有把上轮 Kimi 的 12 条拒绝记录改成成功；这是新的、明确标注为人工参考的离线结果。

这些防护保证回填文字可追溯，不保证模型会选对公司、季度或单元格。错误的选择仍可能指向真实但无关的文字。

## 实测结果

| 来源 | 人工参考案例 | 文字证据通过 | 重要保留项 |
| --- | --- | --- | --- |
| GTLB / revenue | 4 | 4 | 两条多行期间不拼接；两条未选币种保留缺失 |
| ANF / special_items | 4 | 4 | 会计括号保留；2025 年比较项不冒充当日新催化 |
| PLAB / eps | 4 | 4 | 正文 `$0.49` / `$0.50` 原样回填；包含两条重复案例 |
| NYAX / revenue | 人工空选择 | 空列表正常处理 | 不代表自动完成事件分类 |

输入 fixture 包含四份归档公告的 **24 个明确选定段落**，不是全部 824 个归档段落。
每个案例进一步只选择相关段落，所有结果均标记覆盖不完整。
本轮请求目录约 5182–13889 字节，单条选择响应约 1080–1146 字节。
这只是参考案例的序列化体积，不是 tokenizer 测量、线上 API 用量或付费报价。

本地：`tests/test_ep_*.py tests/test_breakout_isolation.py` **312 passed**。
SG：`tests/test_ep_*.py` **297 passed, 6 skipped**。未声称整个仓库所有测试均通过。
SG 离线复跑同样为 12/12 参考案例通过、10 条不同提案。

最终只读核对台账仍为 13 次历史调用，累计预留 **1.996705 / 10 美元**，与本轮开始相同。
本轮新增模型请求数为 **0**。fixture 内预算是历史快照，不能替代运行时预算查询。

SG 完整离线结果保存在：

```text
/home/projects/quant/tmp/ep-span-selection-KPHK84fm/offline_report.json
```

代码仅增加到 SG 独立验收副本，与生产 `/home/projects/quant` 的运行代码、任务和配置无关。

## 代码阅读

- `src/breakouts/ep/llm_span_selection.py`：纯函数模块；包含片段目录、选择 schema、ID 解析、原文回填和审核。
- `reviews/2026-09-10-ep-span-selection/reference_cases.py`：人工参考选择，包含案例专用 ticker 与原文片段。它不是生产发现逻辑，也不会自动纠正模型响应。
- `reviews/2026-09-10-ep-span-selection/run_offline.py`：离线回放命令；只读输入，独占创建结果文件，避免覆盖旧结果。
- `reviews/2026-09-10-ep-span-selection/build_fixture.py`：从只读导出生成带来源标识的段落子集。
- `tests/fixtures/ep_span_selection_20260910.json`：固定输入、来源请求 key、旧拒绝原因和历史预算快照，无密钥。
- `tests/test_ep_llm_span_selection.py`：ID 越界、版本串用、自由文字注入、数字截断、输入限额、负数原貌及真实案例回归。

新协议是 `ep-span-selection-v1`。原来的 `llm_service.py` / `llm_provider.py` / 分批付费 CLI 没有接入它。
**现在运行旧 `batch-run` 命令仍使用旧协议，不会自动变成 ID 选择模式。**

## 如何复现

本地执行，输出文件需尚不存在；重复运行请使用新的结果文件名：

```bash
cd /Users/huozhihong/Documents/Quant
/tmp/quant-ep-v1a-test/bin/python reviews/2026-09-10-ep-span-selection/run_offline.py --archive tests/fixtures/ep_span_selection_20260910.json --output /tmp/ep-span-review-new.json
```

SG 独立副本执行，同样不会请求模型或推送消息：

```bash
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-10-ep-span-selection/run_offline.py --archive /home/projects/quant/tmp/ep-llm-trial-20260909/tests/fixtures/ep_span_selection_20260910.json --output /home/projects/quant/tmp/ep-span-review-new.json'
```

不要传 `--execute`，这套离线命令根本没有付费执行选项。

## 后续尚未实施

1. 自动从完整公告召回正确的相关段落。当前参考段落由人工指定，不能声称覆盖率已解决。
2. 原始 HTML table/row/cell 与跨列表头恢复，验证具体值的期间、主体和数量级归属。
3. 将选择协议接入现有预算、去重和原始响应留档流程，先只做 dry-run 预算检查。
4. 单独批准并开展少量真实模型 ID 选择测试，测选错、漏选和未知 ID；继续区分模型表现与人工参考表现。

不会因为离线参考通过就开启评级、Discord、定时运行或生产部署。
