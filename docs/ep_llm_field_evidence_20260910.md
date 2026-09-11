# EP 主体、期间与单位证据定位验收

## 本轮边界

仅在 SG 独立副本 `/home/projects/quant/tmp/ep-llm-trial-20260909` 进行手动验收。
使用 `/home/projects/quant/.venv/bin/python`、Kimi 国内平台 `kimi-k2.6`，
沿用原 `data/ep/llm_trial_20260909.sqlite3` 预算台账，累计上限 10 美元、每日上限 3 美元。
没有新增定时任务、Discord 推送、评级、交易触发或生产部署；没有改动杯柄和多因子逻辑。

## 数据流

1. 已核准 SEC 公告的归档正文进入分批请求，保留文档 ID、正文修订号与段落 ID。
2. 模型每批最多提出 4 条事实，保留原有数字行证据 `evidence`。
3. 每条额外给出最多 6 个 `context_evidence`，每个引用不超过 400 字符。
4. `SUBJECT` 指向公司名，`PERIOD` 指向期间，`CURRENCY` 指向币种/单位符号，`SCALE` 指向百万/千等数量级。
5. 校验器确认引用是指定段落的精确子串、字段文字在引用中、非空字段有对应角色的引用。
6. 程序计算引用及字段在归档段落内的起止位置；重复/重叠出现保留歧义，不随意选一个位置。每个锚点最多保留 64 个位置，超出明确标记截断。
7. 原有指标与数字同行引用、数字边界、GAAP/非 GAAP 等检查继续执行。
8. 接受与拒绝均写入原审计记录；即使位置存在，仍不自动证明主体、列、期间与数值的对应关系。

例如 `Q2 FY 2027` 是一段连续原文，可以直接定位。
`Three Months Ended July 31,` 和另一行的 `2026` 必须保留为两个 PERIOD 片段，
不能编造一段不存在的 `Three Months Ended July 31, 2026` 引用。
`$` 与 `in millions` 分别定位；表头的 `except per share data` 必须留证，不能把 EPS 乘以百万。

字符位置是 Python/Unicode 码点计数，左闭右开，相对于**已归档并清洗的段落**，不是原 HTML 字节位置或 PDF 坐标。

## 代码位置

- `src/breakouts/ep/llm_evidence.py`：角色约束、精确位置与歧义记录。
- `src/breakouts/ep/llm_contract.py`：新提案结构、线上 schema 与本地校验；旧非分批契约不变。
- `src/breakouts/ep/llm_batches.py`：版本升级为 `ep-llm-batches-v3`，增加字段证据约束及合成格式例子。
- `tests/test_ep_llm_evidence.py`：伪造引用、错角色、合成期间、单位例外、重复位置和旧协议隔离测试。
- `tests/test_ep_llm_batches.py`：分批 schema、预算、去重和 CLI 回归。

旧调用不删除、不覆盖、不重新计费；新契约产生新的请求摘要。
`VALIDATED` 仅代表模型返回了可处理的响应，不等于其中每条提案被接受。
`TEXT_GROUNDED_ONLY` 也不等于财务语义已核准。

## 验收结果

**结论：证据定位功能已实现，真实多公告事实提取验收未通过。**

| 公告与批次 | 已存档段落输入覆盖 | 完整响应耗时 | 提案数 | 接受 / 拒绝 |
| --- | --- | --- | --- | --- |
| GTLB / revenue | 283 / 283 | 34.770 秒 | 4 | 0 / 4 |
| ANF / special_items | 343 / 343 | 49.996 秒 | 4 | 0 / 4 |
| PLAB / eps | 157 / 157 | 42.233 秒 | 4 | 0 / 4 |
| NYAX / revenue | 41 / 41 | 6.032 秒 | 0 | 0 / 0 |

四次均为 HTTP 200、`finish_reason=stop`，没有截断、超时或重试。
前三批均报告 `MORE_FACTS_REMAIN`；NYAX 报告 `COMPLETE_FOR_SCOPE`，这仍只是模型自报。
12 条提案全部未通过严格证据契约，不代表 12 个经济事实全部虚假；不能把没有核准的提案当成正式事实。
最终定位器做了重叠匹配与输出大小修正后，离线重放这四份原响应，接受/拒绝计数不变。

这是四份公告各取一个维度的横向验收，不是四份公告全部四个批次的完整验收。
没有在失败后反复调整提示词并重新付费，也没有把缺失证据改为默认通过。

### 为什么被拒绝

- **GTLB**：主体 `GitLab Inc.` 已定位到 p0007，两个简写季度定位到 p0019，数量级定位到 p0018。
  但模型把 p0004 的 `Total revenue` 和 p0020 的 `$ 286.3` 拼在一起；前者同句写的是 `$286.3 million`，后者行标题是 `Revenue`。
  另外两条仍把表头与年份拼成不存在的连续期间，并给没有 `$` 的行引用 `$`。这不是请求截断，是提案证据绑定失败。
- **ANF**：约 1 亿美元退税和每股 1.75 美元影响在原文中可见，但主体引用指向不含公司名的 p0005。
  模型还把 p0023 的弯引号改成直引号、把 `in Millions` 改成 `in millions`；精确引用失败。
  另两条把 `$ (38,574)` 改写成 `$38,574`，丢失了会计括号，并拼接指标描述。不能默默消除这些差异。
- **PLAB**：主体已在 p0002/p0008/p0010 定位，但模型把多行表头拼成单一期间；
  在只有 `$0.49` / `$0.50` 的正文引用下填写 `$ 0.49` / `$ 0.50`，还使用未逐字出现的指标名称。
  有的提案只引表头、未在 `evidence` 引数字行，并混用大小写不同的公司名及会计口径文本。
- **NYAX**：返回空列表，符合这个“发行人已实现营收”窄范围的对照预期。
  p0015 的超过 9000 万美元是 **IPS 的 FY2026 预计营收**，不是 NYAX 已实现营收。
  该结果不表示 NYAX 没有并购催化，也不证明模型能普遍正确区分所有主体。

### 请求、预算与留档

| 样本 | 请求 key | 输入 / 输出 tokens | 预算预留（美元） |
| --- | --- | --- | --- |
| GTLB | `d1bf56afbba87f4dc4939fc16ca0f71cf7f5e6bdf94ca9889ef1a35ffb25fa66` | 12405 / 1807 | 0.173096 |
| ANF | `c433a0e8ed1649e27d03a2aaf09326a230029ac4a93901a58d290835a8fde9e9` | 14394 / 2322 | 0.193798 |
| PLAB | `2350a55ad66451216f116e2174135e105869a828d293938f9981639bcdf7ed28` | 7028 / 1906 | 0.124827 |
| NYAX | `d0996deecfa193fce6825ed26f24b86463eacb63738e3cf63005163770b54aaf` | 4199 / 138 | 0.107028 |

本轮预留合计 **0.598749 美元**，历史累计预留 **1.996705 / 10 美元**，剩余总预留额度 **8.003295 美元**。
台账累计 13 次调用：3 次历史 FAILED、10 次 VALIDATED；本轮未增加 FAILED，但事实验收仍未通过。
按当前保守价表和返回 token 估算，本轮用量约 0.082769 美元；这不是供应商实际扣费账单，也不用于释放预留预算。

完整原响应、拒绝原因、定位结果仍在原 SQLite 台账。
额外只读审计报告与更新前文件备份在 SG：

```text
/home/projects/quant/tmp/ep-field-evidence-miQJymDB/
  field-evidence-audit.json
  audit_ep_field_evidence.py
  preimage/
  before-location-cap/
```

报告包含已引用的归档原段落、原始模型提案、原判定和最终代码的离线重放结果；没有密钥。
本地定向回归：`tests/test_ep_*.py tests/test_breakout_isolation.py` **296 passed**。
SG：`tests/test_ep_*.py` **281 passed, 6 skipped**；未声称整个仓库测试通过。

以下命令只读，不调用模型或读取 API key：

```bash
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn batch-status GTLB'
```

## 尚未解决的边界

归档解析器明确标记 `table_layout_semantics=NOT_RECONSTRUCTED`。
本轮没有恢复 HTML 表格的行列、跨列标题和单位适用范围，也没有验证数字属于哪个公司或季度。
错误主体恰好出现在另一个段落、借用另一张表的单位等情况，**不能仅凭文字存在判定正确**。
因此所有定位结果保持 `association_verified=false`，所有提案保持 `financial_semantics_verified=false`，
并明确保留 `UNIT_SCALE_AND_EXCEPTION_APPLICABILITY_REQUIRES_REVIEW` 等人工核查标志。
不可把这一层接入自动 Strong/Moderate 评级或交易触发。

## 下一步建议

本轮证明“增加上下文引用字段”并不足以解决生成式抄写错误。下一步不应继续原样重试：

1. 把这 12 条拒绝提案固化为离线回归样本，分别标注漏引、改写、跨行期间和真实财务歧义。
2. 改成让模型选择原文片段/单元格 ID 与角色，由程序回填原文，减少模型反复重写数字、公司名和长引用。
3. 对表格恢复 table/row/cell、跨列表头与单位例外关系；无法恢复的表格保持待核，不用全篇同词匹配补证。
4. 先用已存公告进行零付费离线验证，再申请下一轮有限真实样本；不是现在就开启自动运行或 Discord。

以上后续方案尚未实施；本轮没有证明主体、期间、数量级的财务对应关系已经自动解决。
