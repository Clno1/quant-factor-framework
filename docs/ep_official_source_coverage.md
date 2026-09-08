# EP 原文覆盖：公司 IR、官方披露与 FMP 全文验证

日期：2026-09-08，北京时间。范围：本地、已知案例、单次限额验证；不是全市场盲扫或 SG 上线。

后续更新：[SEC 附件、AFRM PDF 与权限验证](ep_sec_pdf_followup.md)。用户提供真实联系信息后，SEC 已直连成功并新增 PLAB/ANF 正文；AFRM 仍需解决 PDF 下载或图片 OCR。下文保留上一批当时的结果。

## 1. 结论

**公司 IR 路径确实补上了两份原文；现有 FMP 账号也能读取四份电话会长文本。但不能据此宣布原文已全覆盖，或直接进入 Strong/Moderate 评级。**

| 路径 | 本轮实测 | 可以确认 | 仍不能确认 |
|---|---|---|---|
| 公司 IR | 8 个已知公告 URL，2 个抓取并解析成功 | GTLB、NYAX 对应正文可以归档并关联原候选 | 其他公司可用率、自动发现能力、全文完整性、历史盘前可得性 |
| FMP 电话会 | 4 次成功，各约 5 万字符 | GTLB、SNOW、AFRM、DELL 的股票与财政年度/季度匹配 | 当时盘前是否已提供；是否含完整 Q&A；不能替代财报公告 |
| FMP 新闻稿 | GTLB、AFRM 两次请求，3 条记录 | 当前返回正文仍各为 500 字符 | 未找到能替代公司完整公告的新闻稿全文接口 |
| SEC | 官方接口与访问规则已核对，本轮没有项目直连抓取 | 可以规划申报记录到原附件的路径 | 原附件访问尚未重新验证；需要真实联系信息 |

之前已取得的 VEEV 财报正文仍保留在上一批审计库。本轮**新增 2 只公司的公告正文**，不是新增 6 只：电话会与公告分开统计，也没有把多个临时库冒充一份完整覆盖库。

没有改茶杯柄条件、分钟监控、多因子策略或 SG 配置，没有本机定时任务、Discord 发送、LLM 调用或数据采购。

## 2. 公司 IR 逐项结果

实测时间：北京时间 00:25:52 至 00:27:37。IR 共 9 个实际 HTTP 请求，FMP 共 6 个请求。DNS/连接前失败不等于已完成 HTTP 请求；请求数包括 robots 检查。

| 股票 | 程序直连结果 | 解析后的正文 | 下一处缺口 |
|---|---|---|---|
| GTLB | robots、公告均 HTTP 200 | 27,399 字符，255 段，DOCUMENT_MATCHED | 原案例证券身份缺失，不能自动核准发行人；财务表格口径待审核 |
| NYAX | robots、公告均 HTTP 200 | 10,717 字符，28 段，DOCUMENT_MATCHED | 并购买方/卖方/标的角色尚未结构化核准 |
| SNOW | SOURCE_HTTP_429 | 未取得 | 本批停止该主机访问，没有绕过限流 |
| AFRM | SOURCE_TIMEOUT | 未取得 | 详细股东信是 PDF，现有 HTML 客户端尚不支持 |
| DELL | SOURCE_TIMEOUT | 未取得 | 后续验证官方附件或账户允许的替代来源 |
| SAIC | SOURCE_TRANSPORT_ERROR | 未取得 | 需定位连接阶段并验证官方附件 |
| PLAB | SOURCE_TIMEOUT | 未取得 | 已找到官方 IR 与 SEC 附件位置，项目直连仍待验证 |
| ANF | SOURCE_TIMEOUT | 未取得 | 同上 |

这里的超时**不表示公司没有公告**。搜索工具能读到网页也不等于项目抓取器能稳定读取；未将网页检索结果冒充应用网络成功记录。

经过核对的正文入口：[GitLab IR](https://ir.gitlab.com/news/news-details/2026/GitLab-Reports-Second-Quarter-Fiscal-Year-2027-Financial-Results/default.aspx)、[Nayax IR](https://ir.nayax.com/news/news-details/2026/Nayax-Enters-into-Definitive-Agreement-to-Acquire-IPS-Group-a-Leading-Smart-Parking-Technology-Provider/default.aspx)。完整 8 个 URL 与失败记录见原始审计报告，不在生产配置里硬编码这些股票。

AFRM 的[季度资料页](https://investors.affirm.com/financial-information/quarterly-results)链接到[股东信 PDF](https://investors.affirm.com/static-files/f853cf71-ea63-4fbb-a1e8-6429beba46d0)。这说明“成功读取财报公告通知”仍可能只取得入口，没有取得真正财务材料。本轮只确认材料位置，没有绕过该主机失败状态去批量抓取 PDF，也没有宣称已实现 PDF 财务抽取。

## 3. 修复了什么代码

### 3.1 IR 正文解析

`src/breakouts/ep/source_verifier.py` 的版本升为 `ep-article-body-v2`：

- 新增严格匹配的 `evergreen-news-body` 正文容器。
- GTLB、NYAX 页面用 ASP.NET form 包裹整页。旧代码移除整个 form，正文随之丢失；现在仅移除输入框、按钮等控件，继续排除脚本、导航、隐藏内容。
- 从该正文容器的同级 `evergreen-news-headline` 读取标题，不再把页面总标题 News / News Details 当作公告标题。
- 支持正文内 `Source:` 署名，但两份实际原文仍没有通过发行人归属核准，不能因此虚构身份。

仍不采用“整页所有 div 都当正文”的回退。字符数只是提取规模，不是完整性证明；表格没有恢复多层表头、列对应和 GAAP 口径。

### 3.2 显式替代原文 URL

`src/breakouts/ep/enrichment.py` 和 `scripts/run_ep_radar.py` 新增 `--source-overrides`：

```text
原 candidate / document_id / revision_id 保持不变
  -> 人工确认同一公告的公司 IR URL
  -> document_id 到替代 URL 的显式 JSON 映射
  -> 精确 allow-host / HTTPS / 公网地址 / robots 检查
  -> requested_url + original_url + source_route 一并留存
  -> 正文和标题重新核对
  -> source / explain 显示独立证据，不改原采集结论
```

映射中出现当前运行不存在的 document_id、非 HTTPS、未允许主机，会在抓取前拒绝。原站失败缓存不会挡住显式允许的另一个官方来源；同一 URL、同一解析器版本的有效缓存可以复用，替代 URL 变化则重新获取。替代网址本身不等于验证通过，标题不符仍为 DOCUMENT_UNVERIFIED。

**这不是自动搜索 IR、自动追踪 PDF 或自动 SEC 回退。** URL 目前经人工核对后传入；自动化来源注册与发现还需要下一批开发。

### 3.3 时间与数据隔离

本轮用 SQLite backup 创建旧案例库的副本，在新库导入已实际抓到并保存哈希的两份 IR HTML。离线重新解析不重发网络请求，标记 `ARCHIVED_IR_REPARSE`，保留原始收到时间和 HTTP trace。

验证结果：原 candidate 报告逐项未变；source 可以读取正文；explain 可以追溯；以原案例采集完成时刻执行 as-of 查询看不到这次补录。因此不能用这次历史补录证明“当时能够盘前提醒”。

茶杯柄仍是 `src/breakouts/live/cup_handle.py`，本次没有改动。新内容只影响 EP 来源层及其独立审计工具，没有进入价格计算循环。

## 4. FMP 全文能力与边界

使用项目唯一 FMP 适配层的 `_request`，没有新建带密钥的外部网站客户端，也没有把密钥传给公司 IR。

| 股票 | 请求财政期 | API 返回日期 | 正文字符数 | 身份/财政期 |
|---|---|---|---|---|
| GTLB | FY2027 Q2 | 2026-09-01 | 51,593 | 匹配 |
| SNOW | FY2027 Q2 | 2026-09-02 | 51,547 | 匹配 |
| AFRM | FY2026 Q4 | 2026-08-27 | 49,925 | 匹配 |
| DELL | FY2027 Q2 | 2026-09-01 | 50,634 | 匹配 |

接口为 `/earning-call-transcript`，按 symbol/year/quarter 请求；[FMP 电话会数据说明](https://intelligence.financialmodelingprep.com/datasets/earnings-call-transcripts)给出该类历史文本接口。

审计中发现返回字段为 `period: Q2/Q4`，而非 `quarter`。最初 report.json 因只读 quarter 错记了 expected_period=false；已用同一份归档响应重新核对并修正审计代码，最新 coverage_verified.json 为 true。旧报告保留，不覆盖当时记录。归一化同时检查两字段冲突，不能直接把请求季度当返回季度。

电话会文本可用于补充经营指标和管理层解释，但必须单列为 EARNINGS_CALL_TRANSCRIPT，而非原财报新闻稿。返回的 date 不是精确上线时刻：未验证盘前可用延迟、历史修订与当时版本。四份文本只在审计 raw 库中，尚未作为评级输入或批量生产采集接口。

`/news/press-releases` 本轮仍返回 500 字符字段，不能把“订阅 API 可以访问”理解成“已获得整篇财报”。

## 5. SEC 与使用授权

[SEC 官方 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)提供 submissions 元数据及 XBRL，不需要 API key；但拿到申报目录不是拿到财报附件，标准 XBRL 也不能替代自定义 ARR、cRPO 等全部指标的原文。

下一条验证链应是：确认证券 CIK -> 8-K/6-K 申报 -> 对应 EX-99 财报/新闻附件 -> 下载归档 -> 正文、身份、期间及可得时间核查。对失败主机停止，不伪造浏览器、联系方式或绕过限制。[SEC 开发者规则](https://www.sec.gov/about/developer-resources)要求识别自动访问，并限制整体请求速率。

**本轮没有真实联系邮箱配置，因此没有重新做 SEC 自动全文抓取。** 所需是用于 SEC User-Agent 的真实联系信息，不是 AI API key。可以之后通过本机环境变量配置，不必写入报告、仓库或日志。

此外，HTTP 200 只验证技术访问，不验证全部使用权。[FMP 条款](https://intelligence.financialmodelingprep.com/terms-of-service)区分个人使用与多用户展示/再分发；内部 Discord 群并不自动等于个人使用。应按你的实际订单与数据协议向 FMP 确认，不能只从通用条款断定你当前账户是否已获授权。

建议提交给 FMP 的具体问题（本轮未替你发送）：

1. 现有订阅能否取得公司新闻稿完整正文，而不是 text 的 500 字符？对应端点与限制是什么？
2. 电话会全文通常在何时上线？是否提供精确首次可用时间、更新记录和历史版本？
3. 是否允许本地持久化、提交第三方模型进行事实抽取，以及向内部 Discord 多用户提供简短衍生摘要/指标？
4. 盘前/分钟成交量是否覆盖扩展时段、是否为累计量、历史同钟数据如何获得？这一项仍是市场侧独立验收。

这些问题不等于建议立刻购买额外数据或提供 LLM 密钥。

## 6. 证据和复现

- [最初网络结果](../reviews/2026-09-08-ep-official-sources/report.json)：保留 v1 解析失败及当时字段判断。
- [最终覆盖与集成验证](../reviews/2026-09-08-ep-official-sources/coverage_verified.json)：v2 重解析、电话会期间修正、导入与 as-of 校验。
- [实际公告映射](../reviews/2026-09-08-ep-official-sources/source_overrides_verified.json)：只绑定原案例对应 document_id，不适用于任意新数据库。
- [单次网络审计](../reviews/2026-09-08-ep-official-sources/probe.py) / [离线重解析](../reviews/2026-09-08-ep-official-sources/reparse.py)：均要求新输出路径，避免覆盖审计历史。

原始 HTML 和 FMP 响应在 `/tmp/quant-ep-official-sources-20260908.sqlite3`；最后的 EP 集成副本在 `/tmp/quant-ep-ir-verified-20260908.sqlite3`。**临时目录不是长期备份。** 仓库报告仅保留元数据、哈希与验证结果，不发布整篇第三方全文。正式部署前还需确定保留期限、使用授权和备份位置。

只读检查，不联网：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db /tmp/quant-ep-ir-verified-20260908.sqlite3 explain GTLB

/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db /tmp/quant-ep-ir-verified-20260908.sqlite3 explain NYAX
```

后续在原案例副本上显式补抓的命令格式如下。本轮最终集成采用归档重解析，**没有额外执行这条网络命令**：

```bash
python scripts/run_ep_radar.py --db <原案例副本.sqlite3> enrich \
  --run-id 9277d9ca-2dd3-4380-838e-b841ca553b49 --ticker GTLB \
  --source-overrides reviews/2026-09-08-ep-official-sources/source_overrides_verified.json \
  --allow-host ir.gitlab.com --allow-host ir.nayax.com \
  --max-documents 1 --max-http-requests 6 --deadline-seconds 60
```

映射中两家主机都需要允许；ticker 只限制本轮抓取对象。正常缓存期、预算和拒绝保护依旧生效。

## 7. 验证结果

267 项测试、25 个 subtests 通过；包含 EP 采集/来源/核查、IR 表单解析、替代 URL 策略与缓存、电话会季度归一化、杯柄、分钟监控、FMP 覆盖、动量频道路由和盘前摘要回归。新增 19 项测试，不是全仓库测试。3 条警告来自既有 pandas fillna 行为。

GTLB、NYAX 的 Python 3.12 `-S` 只读 explain 命令另行通过：各显示 DOCUMENT_MATCHED、EXPLICIT_OVERRIDE、两条原始 HTTP trace；delivery 保持 DISABLED_SHADOW_ONLY。git diff --check 与本轮修改文件的行末空白检查通过。

## 8. 接下来

1. 在真实联系信息配置后，优先验证 SEC 附件路径；同步确认 FMP 实际订阅的文本用途与时间字段。
2. 做受控公司来源注册表，记录 ticker/CIK、官方域名、公告索引、格式与验证证据；不从新闻正文任意访问 URL。
3. 对仍缺失的 AFRM 等材料增加受限 PDF 路径，单独验收段落、表格和页码引用，不与 HTML 解析混为一谈。
4. 给已取得正文的案例完成身份、事件角色、财务口径和发布时间核查，再接 LLM 做有引用的事实提案。

本轮尚未验证全市场召回率、自动催化归类、Strong/Moderate 一致性或交易表现。现在最值得补的是来源可靠性，而不是先让 AI 写得像截图。
