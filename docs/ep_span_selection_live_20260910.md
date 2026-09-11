# EP 片段 ID 选择：预算接入与真实小样本验收

后续离线关联检查已完成一轮，见 [关联审查报告](ep_association_audit_20260910.md)。
本文保留真实调用当时的结果，不会用后续离线结果覆盖历史记录。

## 结论

已将 `ep-span-selection-v1` 接入现有预算、请求去重、原始响应留档和确定性校验流程。
先完成零请求预检，再在 SG 独立验收目录执行三次真实 Kimi 调用，每份公告一次，无重试。

**工程链路通过；财务事实自动验收尚未通过。** 六条选择全部能解析、回填原文，
没有未知 ID、引用抄写错误或输出截断。四条因会计口径证据不匹配被拒绝，
两条进入文字证据提案，但其中一条经人工核对发现期间错配。
不能将 HTTP 200、台账 `VALIDATED` 或 `accepted` 理解成财务事实已经正确。

全部结果仍为 `financial_semantics_verified=false`、`eligible_for_rating=false`，不输出评级或交易信号。
没有启用自动任务、Discord 或修改 SG 生产服务。

## 本轮实现

| 文件 | 职责与修改 |
| --- | --- |
| `src/breakouts/ep/llm_service.py` | `plan_llm` / `run_llm` 增加 `protocol="span-selection"`，生成片段目录，执行前检查预检请求摘要，复用原有调用台账和预算事务 |
| `src/breakouts/ep/llm_provider.py` | 按协议选择响应 schema；Kimi 的可空 ID 对象展开为兼容的内联 schema；旧 claims 请求内容保持不变 |
| `src/breakouts/ep/store.py` | `preview_llm_call` 只读检查共享日/月/累计预算、已有请求和计费异常闸门；真实执行仍原子预留并二次检查 |
| `reviews/2026-09-09-ep-llm-trial/run.py` | 新增 `span-plan`、`span-run`，显式指定段落，真实运行必须提供匹配的 `--expected-request-key` 和 `--execute` |
| `tests/test_ep_span_integration.py` | 验证只读预检、并发去重、旧调用共享预算、原始响应留档、截断及失败不自动重试、请求变化时拒绝执行 |

本地专用的 `selection_packet` 随请求存入审计台账，包含目录、校验输入及摘要；不会作为私有校验数据发送给模型。
返回的原始选择、供应商响应、用量、回填字段、偏移位置、拒绝原因均留档，可脱离模型离线复算。
失败调用和被拒绝提案不释放历史预留，也不通过改变请求摘要绕过去重。
旧 `batch-run` 仍为 claims 协议，不会自动切换。

## 预检及执行范围

模型为已配置的普通 Kimi CN `kimi-k2.6`，不是 Coding Plan。每次最多四条选择，
输出额度 8000 tokens，读取超时 180 秒，沿用每日 3 美元、累计 10 美元上限。
预检不读取 key、不请求模型、不创建预算记录。三个请求执行前均为 `READY`。

| 公告 / 批次 | 人工限定段落 | 全文段落数 | 片段数 | 预留美元 |
| --- | --- | ---: | ---: | ---: |
| GTLB / revenue | p0007,p0018,p0019,p0020 | 283 | 80 | 0.099644 |
| ANF / special_items | p0005,p0009 | 343 | 117 | 0.112766 |
| PLAB / eps | p0002,p0008,p0010 | 157 | 169 | 0.122581 |

这是**人工限定段落、模型自行选择字段 ID**的试验。没有把人工参考答案或参考 ID 给模型，
但也没有验证全公告自动召回。三个请求均明确标记全文覆盖不完整。

## 真实模型结果

| 批次 | 输入 / 输出 tokens | 耗时秒 | 文字证据接纳 / 拒绝 | 主要结果 |
| --- | ---: | ---: | ---: | --- |
| GTLB | 5621 / 667 | 22.114 | 0 / 2 | 营收数字和期间选对，缺少 GAAP 证据却声明 GAAP |
| ANF | 7541 / 804 | 26.644 | 0 / 2 | 选出退税总额及每股影响，但把 pre-tax 当作 GAAP 依据 |
| PLAB | 8995 / 1021 | 35.121 | 2 / 0 | EPS 与口径有原文依据；非 GAAP 一条期间选错 |

三次都是 HTTP 200、`finish_reason=stop`，每次一次 HTTP 尝试。
三个模型响应均声明 `MORE_FACTS_REMAIN`，因此不能声称完整提取。

### GTLB：没有依据时仍猜测口径

回填 `$ 286.3` / `Q2 FY 2027` 和 `$ 236.0` / `Q2 FY 2026`，数量级选择 `millions`。
模型两条均选择 `basis=GAAP`，却返回 `basis_span=null`；提供的段落也没有 GAAP 文字。
校验器以 `ACCOUNTING_BASIS_CONTRADICTION` 拒绝两条，未擅自改成 UNKNOWN。
两条主体片段也为空；即使修复口径，主体关联仍不能视为核准。

### ANF：金额选到了，属性选错了

回填 `$100 million` 和 `$1.75 per diluted share`，指标为 `IEEPA tariff refund benefit`。
模型用 `pre-tax basis` 支撑 `GAAP`，混淆税前/税后与会计口径，两条均被同一口径检查拒绝。
人工复核还发现两条主体标为 `SEGMENT` 且主体片段为空；总额也被标成 `DILUTED`，
单位范围吞入完整金额，说明“ID 存在”远不足以证明字段含义正确。
这些额外问题不是本轮校验器给出的独立拒绝原因，应作为下一轮语义检查样本。

### PLAB：文字可引用，不等于期间正确

`$0.49` 与 GAAP、`$0.50` 与 Non-GAAP 均准确回填，稀释每股口径有原文支撑。
前一条选择公告开头的第三季度、财年 2026、截至 August 2, 2026。
后一条却选中比较句中的 `third quarter` / `of 2025`，把本期 `$0.50` 配到去年期间；
该句真正的去年比较值是 `$0.51`。

现有检查只证明选中的文字真实存在，尚不能验证期间属于哪个数值，因此仍将两条列入
`TEXT_GROUNDED_ONLY` 提案。期间以独立片段保留，`period_text=null`，并带
`SUBJECT_PERIOD_VALUE_ASSOCIATION_REQUIRES_REVIEW` 等标志。
**这不是两条已核准事实，更不能把 2/6 当成金融准确率。**

## 预算、去重与留档核对

- 本轮新增三次调用，共预留 **0.334991 美元**。
- 累计记录从 13 条变为 16 条，累计预留从 **1.996705** 变为 **2.331696 / 10 美元**。
- 按既有价格快照和实际 tokens 估算，本轮用量金额分别为 0.010910、0.014145、0.017207 美元，合计 **0.042262 美元**。这是估算，不是供应商账单；预算继续保守保留完整预留。
- 台账现有 3 FAILED / 13 VALIDATED；VALIDATED 表示响应完成校验流程，不代表所有提案通过。
- 结束后再次只读预检，三个请求均 `existing_request=true`、新增预留为 0。
- 三份归档原始响应离线回放，与入库 validation 逐项相等。
- 旧 GTLB claims 请求摘要不变，仍为 `d1bf56afbba87f4dc4939fc16ca0f71cf7f5e6bdf94ca9889ef1a35ffb25fa66`。

本轮三个请求 key：

```text
GTLB 78bf058016acba5108cb46dca2184f89f10c22dad292987270277fc02f42f911
ANF  d56e1a5d7ea0a75235e975e691096a1fba4af23349f4ade5c4d15056decfbc51
PLAB 728c112488a978d301e5b97b180ca6db03ca2b41f00bd4aef98a1c4e64741f26
```

唯一台账：`/home/projects/quant/tmp/ep-llm-trial-20260909/data/ep/llm_trial_20260909.sqlite3`。
SG 完整审计结果：`/home/projects/quant/tmp/ep-span-live-gh9ls11m/span-live-audit.json`。
该文件含原始选择、目录和离线回放结果，不含密钥。

## 复查命令

以下命令仅预检，不读密钥、不请求模型、不改变预算：

```bash
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn span-plan PLAB eps --paragraph-ids p0002,p0008,p0010'
```

真实执行入口为 `span-run`，额外要求预检给出的 `--expected-request-key` 和 `--execute`。
不需要重新配置 API key。本轮样本已执行，不应通过修改段落或协议版本重复付费凑通过率。

本地 EP 与隔离测试：319 passed。SG EP 测试：304 passed、6 skipped。
这些是限定测试集，不是全仓库测试；SG 隔离副本缺失条件的跳过项未宣称通过。
代码同步仅限 SG 独立验收副本；没有覆盖生产代码、提交其他并行需求或推送 main。

## 建议下一步

先使用本轮原始响应离线补齐**字段关联校验**，而不是继续付费重试：

1. 将口径声明绑定到有效的会计口径证据；税前不等于 GAAP，未知必须明确保留。
2. 将数值、期间、主体和每股/总额构成关联单元，优先处理正文中的本期/比较期句子，再处理 HTML 表格行列。
3. 单位与数量级单独约束，不能把完整金额当作单位，也不能让总额继承附近的每股标签。
4. 固定本轮错误为离线回归样本，明确区分“文字存在”“属性对应”“语义核准”三个状态。
5. 离线规则及新合同通过后，再冻结版本开展下一批少量真实测试；不要覆盖本轮失败证据。

全文自动召回、完整 EP 催化分类、量能评级及开盘触发仍不在本轮验收范围。
