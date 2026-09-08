# EP 正文事实提取与催化判断

日期：2026-09-08。

后续进度：已新增默认关闭的 LLM 提案接口、引用校验与预算日志，见 [EP LLM 接入说明](ep_llm_integration.md)。本文保留上一轮确定性提取的范围与验收结果；真实模型质量仍待联调。

## 1. 本轮交付

新增一个离线分析步骤，将已匹配的官方正文转成带引用的财务提案，并判断事件类型、公司角色及相对于观察时点的新旧。

```text
collect 候选和新闻
  -> discover / enrich 获得官方正文
  -> analyze 读取当时已可用的正文版本
  -> facts：金额、期间、实际/指引、EPS 口径、特殊项目
  -> catalyst：事件类型、主体角色、发布日期和时效
  -> 带段落引用的逐股报告
  -> --persist 可选追加到独立 EP 分析表
```

**这是第一版确定性规则提取，不是 LLM 阅读器，也不是已经完成的 Strong/Moderate EP 评级。** 没有修改茶杯柄、分钟触发器、股票池准入、SG 服务或 Discord 推送。整个验收没有网络请求和模型调用。

## 2. 真实样本结果

使用上一轮留存的五家公司官方正文，不重新下载，也不将这次回填当作历史实时扫描。

| 股票 | 财务提案数 | 实际提取结果举例 | 催化判断 |
|---|---:|---|---|
| GTLB | 16 | Q2 FY2027 营收 $286.3m；non-GAAP basic EPS $0.25、diluted EPS $0.24，分别保留；Q3 营收指引 $281m–$283m、diluted EPS $0.19–$0.20 | 财报，公司为报告主体 |
| ANF | 10 | 营收 $1.3b；报告 EPS $4.17；关税退款税前影响约 $100m、每稀释股影响 $1.75，单独记录；全年 EPS 指引 $13.10–$13.60 | 财报，公司为报告主体 |
| NYAX | 1 | IPS FY2026 预计收入超过 $90m；明确属于交易中的另一家公司，不记成 NYAX 已实现收入 | 并购，NYAX 为收购方提案 |
| PLAB | 8 | 本季收入 $216.0m；GAAP diluted EPS $0.49、non-GAAP diluted EPS $0.50；下季收入 $207m–$227m、non-GAAP diluted EPS $0.40–$0.56 | 财报，公司为报告主体 |
| AFRM | 0 | 股东信仍是图片正文，未生成财务数字 | 保留来源缺口，不猜测催化质量 |

35 条是**提案记录数**，包含不同季度、会计口径及重复披露位置，不是 35 个独立已核实事实。表中 `$` 保留来源写法；程序未单凭美元符号确认币种，另标记 `CURRENCY_NOT_EXPLICITLY_VERIFIED`。ANF 的简写全年/第三季度指引未擅自补年份，保留 `FISCAL_YEAR_UNRESOLVED`。

GTLB、ANF、NYAX、PLAB 的公告日期分别为 9/1、8/26、8/25、8/26；相对于本次 9/8 北京时间的观察，都属于旧催化。重新抓取或重新分析不会刷新公告年龄。

验收文件：[落库报告](../reviews/2026-09-08-ep-analysis/report.json)、[最终只读复验](../reviews/2026-09-08-ep-analysis/verified_report.json)。报告移除了完整段落文本，但保留来源 URL、source_id、text_revision 和 paragraph_id；完整引用只在本地 EP 库中。

## 3. 财务数字如何提取

### 收入与 EPS

目前支持明确的叙述句，例如 `Revenue was $216 million`、`EPS of $0.50`、`or $0.50 per diluted share`。叙述中的比较值暂不自动全部拆解，避免把前一年或上一季度数字套到当前期间。

表格仅支持有明确 `Qn FY YYYY` 列头的两列摘要结构，分别处理实际值与指引区间。必须满足列数、数值数、同比变化列及金额单位的校验。没有重建通用 HTML 表格，也不会猜测任意财务报表的列含义。

每条提案包含：

- `metric / measure`：收入、EPS、关税退款影响；金额或公司报告的同比增速。
- `value_text / values / normalized_values`：原文数值、十进制字符串、按 million/billion 换算后的数值；不使用浮点数做金额换算。
- `period_text`：来自正文或明确列头；缺年份则标记缺失，不自行补齐。
- `basis / share_basis`：GAAP、non-GAAP、未明确口径，以及 basic/diluted；adjusted 不自动等同 non-GAAP。
- `value_kind`：实际值、公司指引、公司估计或特殊项目影响，互不混用。
- `subject_scope`：报告公司、分部/主体未核实、交易中的另一家公司。
- `evidence / missing_context`：准确段落引用及缺失口径；完整来源版本在父报告中。

畸形数字、倒置区间、正数写法但语义为 net loss 等情况会被拒绝或要求核查，不能静默变成正常化数值。美元符号也不自动证明币种。

### 指引和超预期

当前能提取新指引，但**没有比较基准就不计算“上调”或“beat”**：

- 计算 earnings surprise 需要公告前已可用的共识预期，以及一致期间、GAAP/adjusted 和 basic/diluted 口径。
- 计算指引变动需要此前公司指引，且期间、指标和口径可比。
- “实际 EPS 高于此前 outlook”仍是实际值，不因为句中出现 outlook 就标成新指引。
- 同比增长是公司报告的增长数据，不是超预期比例。

### 一次性与特殊项目

先区分回购和影响利润的特殊项目。回购属于资本回报，不能仅凭 repurchase 关键词断定是一次性收益。

本版支持明确的关税退款影响句式，例如 ANF 的税前约 $100m、每稀释股 $1.75。其他税务优惠、退款和 non-recurring 段落先定位，并保留金额提及及“尚未分配到具体项目/期间”的状态。

**没有自动用 $4.17 - $1.75 生成“经调整 EPS $2.42”。** 即使算术简单，也仍需审核税前税后、每股分母、公司调整口径和期间。附录中的复杂调节表、一笔特殊项目的跨期分配，尚未自动完成。

## 4. 催化与时效如何判断

### 类型与角色

分类使用已匹配的正式公告，而非只看 FMP 新闻标题。需同时看到注册公司在公告标题中作为主体、SEC 公司身份关联或正文主体归属依据、以及带 ticker 的公告日期行，才能给出直接公司披露的角色提案。

支持的第一版类型是财报、指引、并购、终止交易及明确获授合同。收购方和被收购方分开；缺主体依据则 `UNRESOLVED`，不输出确定的直接关联。

这仍是基于规则的披露分类：不能证明新闻造成了上涨，也未证明事件对企业价值的重大性。没有找到直接新闻时，不会自动断言“只是主题联动”；行业传导、合同收入重要性、经营质量等仍需后续能力。

### 新旧窗口

使用项目已有 `exchange_calendars` 的 XNYS 交易日历，窗口为**上一交易日正式收盘至观察时刻**。盘后则进入下一交易日事件窗口；周末、假日、夏令时和提前收盘均由交易日历决定，不用固定 24/36 小时粗算。

原公告日期仅从带发行人 ticker 和 `today reported/announced` 的日期行提取，且日期必须位于主体标识前；不把财务季度结束日或电话会日期当作发布日期。

- `STALE_FOR_CURRENT_EVENT_WINDOW`：整个公告日期都早于当前窗口。
- `CURRENT_WINDOW_DATE_ONLY`：公告日期落在窗口内，但没有精确发布时间证明。
- `BOUNDARY_RELEASE_TIME_UNVERIFIED`：公告是上一交易日，尚不能确定在收盘前还是收盘后发布。
- `PROVIDER_DATELINE_DATE_CONFLICT`：供应商新闻日期与原文日期冲突，可能是转载或时间口径问题，需核查。
- 日期、交易日历缺失或日期在未来，分别记录原因。

FMP 新闻时间仅作为供应商时刻展示，不能冒充原文发布时间证明。旧文章被重新转载不会因此成为新催化。

## 5. 留存与查询

研究库仍为 `data/ep/research_shadow_20260908.sqlite3`。写入前已建立 `data/ep/research_shadow_20260908.before_analysis.sqlite3` 备份；原始候选、正文和人工审核均未覆盖。

schema 5 新增 `ep_source_analyses`，保存分析 ID、所属采集 run、ticker、实际记录时刻和完整报告。算法版本、来源版本和观察时刻都在报告中。相同输入/观察时刻的重复持久化不重复插入；变化后的分析新增记录。

旧 schema 1–4 的只读打开不迁移，只有可写打开会升级。`--as-of` 同时限制当时可用的候选和正文；后来补录的资料不能穿越时间门槛。历史回放分析也不能冒充当时已运行的实时系统。

只读分析：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 analyze GTLB
```

显式追加一份当前分析快照：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 analyze GTLB --persist
```

查看已保存的报告：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 analyses GTLB
```

所有命令都不需要 FMP/SEC/LLM 密钥；时效计算需要项目运行环境中的交易日历包。没有依赖包时显示缺口，不回退为猜测交易日。

## 6. 代码路线

| 文件 | 作用 |
|---|---|
| `scripts/run_ep_radar.py` | analyze / --persist / analyses CLI 入口 |
| `src/breakouts/ep/analysis.py` | 读取符合观察时刻的逐股证据，编排提取和分类 |
| `src/breakouts/ep/facts.py` | 数值、单位、期间、EPS 口径、特殊项目和缺口 |
| `src/breakouts/ep/catalyst.py` | 公告日期、公司角色、事件类型和交易日历窗口 |
| `src/breakouts/ep/store.py` | schema 5 分析快照、去重及历史查询 |
| `tests/test_ep_analysis.py` | 本轮提取和分类的可执行用例 |
| `reviews/2026-09-08-ep-analysis/run.py` | 归档样本离线验收，可选备份后落库 |

旧 `classifier.py` 继续提供新闻级提示；没有将新正文分析覆盖到旧候选判定中。旧 `fact_review.py` 仍是人工审核入口，规则提案不会伪装成人工审核提交。茶杯柄路径仍是 `src/breakouts/live/cup_handle.py`。

## 7. 验证与剩余范围

回归覆盖 EP、杯柄、隔离、分钟监控、小时告警、Discord 路由和盘前摘要：340 个测试、25 个子测试通过，其中本轮新增 29 个测试用例；另有 3 条既有 FMP pandas FutureWarning。`git diff --check` 与新文件尾部空白检查通过。

真实样本逐条检查了来源版本和引用包含关系；原候选及人工审核不变，四家 35 条提案、AFRM 保留缺口。首次加载交易日历加 GTLB 分析约 0.34 秒，后续逐股约 0.002–0.016 秒；这是 Mac 小样本耗时，不是 SG 压测结论。

现在没有自动 Strong/Moderate、盘前量能评级或开盘触发。尚未完成的主要是：通用复杂表格和 AFRM OCR、行业经营指标的广泛抽取、可比历史指引/公告前预期、经营质量与事件重要性判断，以及真实盘前行情确认。

下一步适合以本轮已验证的结构和引用约束接入 LLM 做更广泛的事实提案，再以确定性校验器审核；届时再确认模型 API 与预算。不能只靠继续增加关键词规则来声称能完整复刻截图中的分析。
