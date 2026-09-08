# EP v1A 第三批：原文核查底稿与财务口径

日期：2026-09-08。代码仍在 `src/breakouts/ep/`，本批没有 SG 部署、定时任务、LLM 调用、评级或消息发送。

## 1. 本批的结论

已经把“看到原文后如何核查”做成可执行入口，支持生成证据索引、人工填写结构化字段、验证引用、保存审核版本和按时间查询。**尚未自动识别并核准所有催化、发行人角色或财报数字。** 这批是未来 AI 抽取结果的核查基础，不是靠关键词直接输出 Strong 的分类器。

扩大到九只已知股票后，当前公共抓取路径仍只有 VEEV 得到了可用正文（财报缓存，以及新取得的 Biogen CRM 公告）。其他来源的前置 robots 请求超时，或受到同域名失败短路保护，没有取得对应正文。现在最需要解决的是可靠原文来源，不能仅靠增加 AI API 解决这个问题。

## 2. 顺着数据看代码

```text
原 collect 观察结果
  -> enrich / ep_source_contents 原文版本
  -> review-template(source_id)
       保留原文时间、供应商发布时间、身份缺口
       按财务口径/经营指标/指引/一次性项目/事件角色/期间定位段落
       生成空的 review_input，不自动填财务结论
  -> 人工核查并填写 review_input
  -> review-import --reviewer 审核人
       绑定 source_id + document_id + text_revision
       核对引用、字段取值、部分明显口径冲突
       全部通过才一次事务追加审核记录
  -> reviews / source / explain 查询历史
```

新增 [fact_review.py](../src/breakouts/ep/fact_review.py) 负责底稿和输入契约；[store.py](../src/breakouts/ep/store.py) 负责新表和时间约束；[run_ep_radar.py](../scripts/run_ep_radar.py) 提供命令。杯柄仍在 `src/breakouts/live/cup_handle.py`，没有导入或修改它。

### 段落索引

六类关键词只用于快速定位原文，不作最终分类。例如，命中 acquisition 不能证明当前 ticker 是买方；命中 non-GAAP 也不能证明附近所有数字都采用相同口径。

默认最多列出 40 段，可设置 1–200 段。不同主题轮流取段，避免大量 EPS 表格挤掉一次性税务收益、合同等内容。结果明确列出匹配段数、遗漏段数及全文总段数；没有命中不等于不存在事实。

### 发行人和事件角色

可记录 REPORTING_COMPANY、ACQUIRER、ACQUISITION_TARGET、CONTRACT_AWARDEE、CUSTOMER、PARTNER、MENTION_ONLY 或 UNKNOWN。发行人名称必须出现在指定原文引文中。

这是人工对角色的断言，并保留原始证券身份核查状态。**名字在原文中不证明角色正确，也不证明它与股票代码对应。** 本批不自动处理别名/母子公司、不判断直接催化或主题联动、不把人工角色标签提升为 ISSUER_ATTRIBUTION_MATCH。缺身份时可以留核查笔记，但不允许因此放行评级。

### 财务事实表

每行包括以下独立维度，不能只记录一个 EPS 数字：

| 字段 | 含义 |
|---|---|
| metric / metric_text | 标准指标枚举及原文名称，如 EPS、REVENUE、ARR、CRPO、BACKLOG |
| value_text | 原文数值字符串，不擅自转币种、倍率或会计单位 |
| measure | LEVEL / GROWTH_RATE / MARGIN / RATIO / UNKNOWN，区分金额、增长率和比例 |
| basis / basis_text | GAAP / NON_GAAP / NOT_APPLICABLE / UNKNOWN 与对应原词 |
| share_basis / share_basis_text | BASIC / DILUTED / NOT_APPLICABLE / UNKNOWN 与对应原词 |
| value_kind / value_kind_text | ACTUAL / COMPANY_GUIDANCE / CONSENSUS / UNKNOWN 与原文线索 |
| period_text / unit_text | 原文期间、单位；未知保留 null |
| evidence | paragraph_id 与逐字 quote |

原文字段必须包含在引文中。比如原文 $2.35 不能填成 $2.34；引用 diluted 不能同时标记 BASIC；只有 non-GAAP 的引文不能标记 GAAP；只有 adjusted、但没有明确 non-GAAP 字样时不能自动等同非 GAAP。

缺期间、单位、基本/稀释口径等会列入 missing_context。市场预期始终额外标记 CONSENSUS_ASOF_NOT_PROVEN：公司今天的公告或 FMP 今天更新的预期，不能直接当作公告前的市场预期。

本批不计算 beat 百分比，不比较两种不同口径的 EPS，不判定财报质量。多列财务表仍未重建表头；同一引文中多个数字与多个指标的对应关系仍需审核。所有记录保留 `financial_semantics_verified=false`、`comparison_status=DISABLED_REVIEW_ONLY`、`eligible_for_rating=false`。

### 发布时间

底稿区分实际收到原文的时间与原新闻供应商发布时间。人工可引用日期并标注 ANNOUNCEMENT_DATE、FISCAL_PERIOD_DATE、FUTURE_EVENT_DATE 或 UNKNOWN；季度结束日期不会因此变成公告发布时间。

此处暂不从日期文字推断精确美东发布时间，不实现交易 session 过期、新闻去旧或历史可得性核准。审核记录时间由进程实际运行时写入，CLI 不提供回填时间参数。

## 3. 怎么使用

先用 source 查看已取得的正文，再生成底稿。以下是真实本地 VEEV 财报来源，可只读运行：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db /tmp/quant-ep-fact-review-final-20260908.sqlite3 \
  review-template 1d4a7237-de59-418c-a4d0-bb2d4b66fd89 --max-paragraphs 12
```

底稿中 `review_input` 是人工填写的输入形状，不要把整份底稿当审核结果导入。以下为**合成示例**，不是 VEEV 或 SNOW 的真实财务判断；三个 ID 必须替换成对应真实底稿的 ID，引用必须真实存在：

```json
{
  "schema_version": "ep-fact-review-v1",
  "source_id": "SOURCE_ID",
  "document_id": "DOCUMENT_ID",
  "text_revision": "TEXT_REVISION",
  "role": null,
  "publication": null,
  "facts": [{
    "metric": "EPS",
    "metric_text": "EPS",
    "value_text": "$2.35",
    "measure": "LEVEL",
    "basis": "NON_GAAP",
    "basis_text": "Non-GAAP",
    "share_basis": "DILUTED",
    "share_basis_text": "diluted",
    "value_kind": "ACTUAL",
    "value_kind_text": "actual",
    "period_text": "Q2 fiscal 2027",
    "unit_text": "$",
    "evidence": {
      "paragraph_id": "p0001",
      "quote": "Example announced Q2 fiscal 2027 actual Non-GAAP diluted EPS was $2.35."
    }
  }],
  "note": "人工核查说明"
}
```

只有人工完成的文件才应执行导入：

```bash
python scripts/run_ep_radar.py --db /path/to/ep.sqlite3 \
  review-import /path/to/review.json --reviewer reviewer-name
python scripts/run_ep_radar.py --db /path/to/ep.sqlite3 reviews SOURCE_ID
python scripts/run_ep_radar.py --db /path/to/ep.sqlite3 explain VEEV
python scripts/run_ep_radar.py --db /path/to/ep.sqlite3 reviews SOURCE_ID \
  --as-of 2026-09-08T00:30:00+08:00
```

review-template、reviews、source、explain 均离线只读，支持 --as-of，并通过 Python 3.12 `-S` 验证。review-import 是显式写入，无网络调用；最多读取 1 MB JSON，最多 100 行事实，不接受空审核。

reviewer 是本地审核署名，不是认证签名或登录权限。仓库尚无多用户审核权限控制，不应把共享目录里的任意 JSON 当作可信审核输入。本批没有替用户提交真实样本的人工审核。

## 4. 存储与版本

独立 EP SQLite 升级为 schema 3，新增 `ep_fact_reviews`，外键指向具体 source_id。旧 schema 1/2 可只读查询，不自动写迁移；可写打开时事务升级。生产迁移前仍需备份，当前只在测试库副本验证。

审核采用追加记录：相同来源、相同审核人和相同内容重复导入会返回原记录，保持初次时间；更正内容产生新 review_id，不覆盖旧记录。任何一行校验失败，整份不写入。

按 as-of 查询同时检查原文批次完成、原文收到和审核记录时间。旧 collect 的评估完全不变；source 显示该 source_id 的审核，explain 汇总指定 collect 运行中该 ticker 的审核历史。重新抓取后的新 source_id 不自动继承旧审核的批准，也没有“最新审核自动生效”路径。

## 5. 九只样本验证

最终验证使用新副本 `/tmp/quant-ep-fact-review-final-20260908.sqlite3`，没有改原案例库：

| 股票 | 最终结果 |
|---|---|
| VEEV | 财报缓存原文 25,841 字符/258 段；新抓 Biogen CRM 公告 2,549 字符/13 段；两份仍缺证券身份核准 |
| GTLB | BusinessWire robots 前置读取超时 |
| SNOW / AFRM / DELL | 同源超时后不重复请求，未取得原文 |
| NYAX | GlobeNewswire robots 前置读取超时 |
| SAIC / PLAB / ANF | 同源超时后不重复请求，未取得对应原文；PLAB 律所类公告另行排除 |

最终轮合计 4 个实际 HTTP 请求：PRNewswire 两个请求、两个其他域名各一次前置请求。其余同域名请求直接短路，不把请求数误报为“每只股票都连接过”。九只已知样本不是九只验证通过，也不是全市场召回率测试。

VEEV 财报底稿识别到 79 个相关段落；max_paragraphs=12 时明确报告还有 67 段未列出。六类主题都有原文位置可供查看，但尚未自动确认数字和事件质量。CRM 公告命中 4 段，可以用于下一步验证报告公司与客户角色的区分。

材料：[最终机器结果](../reviews/2026-09-08-ep-sources/expanded_smoke_final_report.json)、[可重复脚本](../reviews/2026-09-08-ep-sources/expanded_smoke.py)。第一次七只股票验证的[旧结果](../reviews/2026-09-08-ep-sources/expanded_smoke_report.json)也保留了，它有 8 个请求；其中每批 summary.http_requests 是当时版本的共享客户端累计数，不能逐批求和。随后已修复为本批新增请求数，并加入测试。

本批共新增 33 项测试（31 项审核测试、2 项抓取/预算回归）。合并 EP、杯柄、分钟监控、旧扫描、频道路由和盘前摘要后，248 项测试、25 个 subtests 通过；3 条警告仍来自既有 pandas fillna。不是全仓库测试。

## 6. 真正还欠缺什么

1. 稳定且合法的原文来源：核查公司 IR/官方披露源或供应商授权全文接口，分别验证可用性；不能绕过当前网站拒绝或 robots 策略。
2. 证券与发行人映射、原始公告时间、跨报道事件归并，以及可核准的公告前市场预期版本。
3. 原文表格结构和跨段落口径解析，积累人工审核样本作为抽取评测集。
4. 然后接入 LLM 做事实提案和带引用摘要；用评测验证字段而非仅比较文风。
5. 独立解决盘前同钟量、报价时效和分钟量契约，才进入 EP 评级与开盘触发。

这不是要求以后每天全靠人工做日报。人工底稿是为了建立可信测试样本和失败排查链；后续自动抽取可以复用这些证据边界，但模型提案与人工审核记录必须保持不同来源标识。本批没有自动模型结果导入路径，目前仍无需提供 AI 密钥。
