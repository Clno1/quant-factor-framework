# EP 财务事实分批提取验收

## 实现范围

本轮只修改 EP 的人工 LLM 验收路径。没有改杯柄检测、多因子策略、生产定时任务或 Discord。
执行位置是 SG `/home/projects/quant/tmp/ep-llm-trial-20260909`；解释器仍使用
`/home/projects/quant/.venv/bin/python`。不是本地 Mac 定时任务，也不是生产主目录部署。

旧的整篇提取协议保留；新批次使用独立版本、范围、schema 和请求哈希。
所有记录仍进入原 `data/ep/llm_trial_20260909.sqlite3`，包括失败记录和预算占用，不新开免费账本。

## 数据流

1. 只读取得已核准 SEC 原文，保留来源、段落 ID、文本版本和来源限制。
2. `prepare_request(..., batch=...)` 生成当前批次范围，保留允许输入的全部上下文。
3. `responses_payload()` 发送与批次一致的 JSON Schema，约束提案数量、类别和引用长度。
4. `run_llm()` 在事务中预留预算、去重；真正发送一次请求，没有自动重试或自动跑完全部批次。
5. 完整返回后，原校验器检查引用、数值、字段依据和口径，再检查批次范围及引用容量。
6. 保存原始响应、提案、逐条拒绝原因、用量、模型自报的完整性和是否达到容量上限。
7. `batch-status` 只读汇总当前版本的四个批次；旧版本的结果留在完整历史中。

| 批次 | 范围 | 明确不做的判断 |
|---|---|---|
| `revenue` | 已报告营收，优先最近一期及可比期间 | 不混入指引，不计算 beat |
| `eps` | 已报告 EPS，优先当期 GAAP / non-GAAP diluted | 不混淆基本/稀释，不自己调整利润 |
| `guidance` | 公司明确的营收、EPS、经营利润率指引 | 不直接判定上修或高于市场预期 |
| `special_items` | 明确且量化的特殊税收、收益、费用等 | 不将所有 non-GAAP 调整自动当成一次性 |

每批最多 4 条提案，每条最多 4 个引用，每个引用最多 1,200 字符。
输出预算为 8,000 tokens；读取等待 180 秒。仍然可能遇到供应商限流或输出截断，失败时拒绝接受半截 JSON。
这不是一个无限续提任务，也没有“跑到没有遗漏为止”的后台循环。

## 两种覆盖率

`input_coverage.complete` 只表示原文是否全部进入当前请求。GTLB 是 283/283 段，27,365 字符。
超过现有 60,000 字符 / 500 段边界的文档仍会显式显示输入不完整，不声称已经处理全文。

模型另返回 `scope_status`：`COMPLETE_FOR_SCOPE`、`MORE_FACTS_REMAIN` 或 `UNCERTAIN`。
这只是模型自报。如果恰好返回 4 条，还会记录 `claim_limit_reached=true`。
`exhaustiveness_verified` 和 `financial_semantics_verified` 始终为 false；任何通过引用校验的提案仍需语义审核。
ARR、cRPO、backlog、留存、GMV、回购及合同金额不属于本轮四个输出批次，不能当作已经提取。

`VALIDATED` 表示响应进入并完成校验过程，不代表所有事实都通过。
必须同时查看 `validation_status`、`accepted_count`、`rejected_count` 与 `rejection_reasons`。
例如 `VALIDATED + 0 accepted / 4 rejected` 是完整返回了四条不合格提案，不是财务提取成功。

## 为什么有 v2

`ep-llm-batches-v1` 的 GTLB 营收、EPS 请求都完整返回，分别使用 1,048 和 1,327 输出 tokens，
不再撞上原 4,000 token 截断。但各有 4 条提案被拒绝。
实际问题包括把 `ACTUAL` 枚举当作原文、把期间当主体、遗漏表头引用、改写数字中的空格，以及无依据的口径标签。

`ep-llm-batches-v2` 增加通过同一校验器验证过的合成示例，明确区分枚举字段和 `*_text`，
以及空值、收入的非每股属性、数字空格和表头引用。示例不来自 GTLB，不进入待提取原文；
测试确认把示例直接复制为实际提案会被拒绝。校验器没有放宽，也不替模型补写来源或修复数值。
这是有版本记录的协议说明改进，不是随机改 prompt 绕过失败记录。

## 代码位置

- `src/breakouts/ep/llm_batches.py`：四个范围、固定上限、规则示例和批次 schema。
- `src/breakouts/ep/llm_contract.py`：原文请求身份、输入覆盖、原校验器和新增批次约束。
- `src/breakouts/ep/llm_provider.py`：按协议生成 Kimi/OpenAI 请求；批次低于 8,000 输出额度时本地阻止。
- `src/breakouts/ep/llm_service.py`：预算、请求、失败留存和当前批次状态汇总。
- `reviews/2026-09-09-ep-llm-trial/run.py`：SG 人工计划、执行和检查入口。
- `tests/test_ep_llm_batches.py`：数量、范围、引用、示例、覆盖、版本、并发去重和预算测试。

## 操作命令

查看只读计划，不读取密钥、不调用模型：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn batch-plan GTLB'
```

查看当前四批状态、拒绝原因与原累计预算：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn batch-status GTLB'
```

以下是一次性执行示例，会在首次执行时消耗预算。已有同版本、同输入、同模型及输出配置的记录会复用，
不会因重复敲命令而重新推理。没有 `all` 参数；运行其他批次需明确指定名称。

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn batch-run GTLB revenue --execute'
```

## 验收结果

本轮使用 `kimi-cn / kimi-k2.6`，先运行 v1 的营收、EPS 两批，再运行 v2 的完整四批。
六次调用均为 HTTP 200、`finish_reason=stop`；旧的三个失败记录仍保留。
以下仅统计当前 v2，不用历史版本的重复提案凑数量。

| v2 批次 | 耗时（秒） | 输入 / 输出 tokens | 引用通过 / 拒绝 | 模型自报 |
|---|---:|---:|---:|---|
| 营收 | 17.649 | 11,996 / 741 | 0 / 4 | MORE_FACTS_REMAIN |
| EPS | 19.006 | 12,001 / 814 | 2 / 2 | MORE_FACTS_REMAIN |
| 指引 | 26.258 | 12,021 / 1,047 | 4 / 0 | MORE_FACTS_REMAIN |
| 一次性项目 | 19.248 | 12,030 / 831 | 0 / 4 | MORE_FACTS_REMAIN |

分批机制解决了本轮样本的输出截断，并显著缩短了等待时间；不构成其他公告或未来请求绝不会截断的保证。
当前 v2 合计 6 条引用通过、10 条拒绝，四批均到达 4 条上限，不能声称已经提取全部事实。

拒绝链没有放宽：营收和一次性项目主要缺少主体、期间或单位对应的引用；
EPS 剩余两条还涉及会计口径上下文和基本/稀释标签。数值在全文出现，不意味着这条完整提案已得到充分引用支持。
所以 `0 accepted` 不是“公司没有营收/没有一次性项目”，而是本批没有达到当前证据契约的提案。

通过的 EPS 两条分别引用 `p0019 + p0030`、`p0019 + p0032`，保留 GAAP 与 non-GAAP 稀释口径。
指引四条引用 `p0043–p0048` 中的公司主体、单位、表头和数值行，涵盖下一季度/全年营收与 EPS 指引。
这些仍是 `TEXT_GROUNDED_ONLY`，没有自动更新为财务语义已核准或可参与评级。
模型和校验器之间的表格列、期间、主体关联仍应作为独立验收环节。

当前四批请求键：

```text
revenue       b98ae94ac58d20d86e4da7f0d874828b3fddb019ec5b0591cbe6c0fd042cb086
eps           ea529da696a993a5a17434d6d7d81398b9a41f39adc83806c353dab947dcb3f8
guidance      abd7a845eb4685e12c9277378b85bd1b6e3bcfdf29c665248620e8b3ebfe5312
special_items 947a3cc899c69df3c44e71a89a9181f9e36037ccef7f2ee1b3e92bf57ea963c1
```

原账本现在共有 9 条记录，累计预留 **USD 1.397956 / 10**；剩余应用预留空间 USD 8.602044。
其中本轮新增预留 USD 0.988495。按原来的保守费率、忽略缓存折扣估算，本轮六次已返回 usage 的调用
合计 USD 0.123695；该估算不是供应商实际账单。没有释放失败预算，也没有使用新模型、账户或数据库规避上限。

源代码和测试已部署到 SG 独立验收目录，备份在 `/home/projects/quant/tmp/ep-llm-batches-bJsMuC0B`。
最终测试：本地 EP 与突破隔离测试 **283 passed**；SG EP 测试 **268 passed、6 skipped**。
`git diff --check` 通过。本轮没有提交或推送 Git，也未改动其他并行需求的文件。
下一步应优先解决主体、期间、单位的证据定位，并用其他已核准公告验证泛化；
不能只凭 GTLB 上的少量引用通过，就认定催化分析已能可靠复刻截图。当前仍未开启自动提取、评级或 Discord。
