# EP SEC 附件、AFRM PDF 与文本权限验证

2026-09-08，北京时间。本地隔离验证，未部署 SG，未调用 LLM，未发送 Discord 或邮件。

后续迭代：[自动来源发现与统一证据报告](ep_auto_sources_iteration.md)。已新增按 CIK 自动查找 SEC 申报/附件、schema 4 留存与 dossier；同时确认 Discord 为用户本人单独使用，不再按多人群假设推进授权询问。

## 1. 为什么需要 SEC 和邮箱

我们需要的是公司向 SEC 提交的**原始财报附件**，不是 SEC 的股票买卖建议，也不是开户或交易接口。

```text
FMP 新闻摘要发现事件
  -> 公司 IR 或 SEC 官方附件取得原文
  -> 核对财务期间、实际/预期、GAAP/非 GAAP、一次性收益
  -> 将来才交给 AI 提取带引用的事实提案
```

IR 原文足够可靠时，不必强制再走 SEC。此次它的价值是：部分 IR 网站在本地程序中读取失败，但 SEC 的副本可以访问。

[SEC 的自动访问说明](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)要求声明 User-Agent，并给出含管理联系邮箱的示例。邮箱仅作为程序身份/联系信息发送给 SEC：不是注册，不是验证邮件，也不是登录凭据。本轮仅通过进程环境传入用户授权的真实邮箱，没有写入仓库、报告或永久配置，也未发信。

## 2. 实际结果

实际允许网络访问的探测发生在北京时间 00:53:31 至 00:54:03，SEC 总共 5 个 HTTP 请求，含一次 robots。此前沙箱内探测的 SOURCE_TRANSPORT_ERROR 单独保留，不把它当作远端站点拒绝。

| 来源 | 网络结果 | 正文处理 | 是否算本轮补齐 |
|---|---|---|---|
| AFRM 8-K | HTTP 200 | 保存原披露及 EX-99.1 链接关系 | 补齐原附件的来源链，不是股东信全文 |
| AFRM EX-99.1 | HTTP 200 | 24 个图片引用，几乎没有正文文本 | 否，SOURCE_IMAGE_ONLY_OCR_REQUIRED |
| PLAB EX-99.1 | HTTP 200 | 提取 13,624 字符；与已有公告标题匹配 | 是，新增可核查正文 |
| ANF EX-99.1 | HTTP 200 | 提取 32,794 字符；与已有公告标题匹配 | 是，新增可核查正文 |
| AFRM IR 股东信 PDF | robots 阶段超时，未取得响应 | 没有 PDF 文件进入本地解析器 | 否，仍是下载缺口 |

正文字符数随空格和表格分隔的处理而变化，不等于原始 HTML 字符数，也不证明正文完整或财务事实已核准。

本轮有两个真实进展：**SEC 直连可用，PLAB/ANF 的正文已补齐**。仍不能说 AFRM PDF 已补齐，更不能把 24 张图片的 HTML 外壳当作财报文字。

官方来源链：

- [AFRM 8-K](https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/afrm-20260825.htm) 的 Item 2.02/9.01 指向[股东信 EX-99.1](https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/affirmfq426shareholderle.htm)。附件为图片页面，不能假设所有 SEC 财报附件都有文本层。
- [AFRM 季度资料页](https://investors.affirm.com/financial-information/quarterly-results)提供[24 页股东信 PDF](https://investors.affirm.com/static-files/f853cf71-ea63-4fbb-a1e8-6429beba46d0)。网页检索工具可识别这份 PDF，与项目下载器尚未成功是两件事。
- [PLAB 官方附件](https://www.sec.gov/Archives/edgar/data/810136/000081013626000006/plabQ32026EarningsEx99-1PR.htm)、[ANF 官方附件](https://www.sec.gov/Archives/edgar/data/1018840/000101884026000041/q22026pressrelease.htm)为本轮正文实际来源。

## 3. 代码边界

| 文件 | 本轮作用 |
|---|---|
| `src/data/public_articles.py` | 新增显式 fetch_pdf；沿用域名、DNS、公网 IP、robots、拒绝保护、字节与请求预算。普通 fetch 仍只接受 HTML |
| `src/data/sec_attachments.py` | 限定 www.sec.gov 和明确的 Archives 附件路径，要求真实联系邮箱；不自动搜索，不访问任意域名 |
| `src/breakouts/ep/pdf_source.py` | 离线 PDF 页码/行号提取；不核准财务事实，不自动 OCR |
| `src/breakouts/ep/sec_source.py` | 独立解析 SEC EX-99 的 SGML 包装，校验类型与文件名；识别图片空壳 |
| `reviews/2026-09-08-ep-attachments/probe.py` | 单次、限额网络验证；不请求更多 FMP 数据 |
| `reviews/2026-09-08-ep-attachments/pdf_worker.py` | PDF 解析子进程，父进程 30 秒超时、CPU 20 秒限制；Linux 另限内存，Mac 没有等价地址空间硬上限 |
| `reviews/2026-09-08-ep-attachments/integrate_sec.py` | 归档重解析，写入已有 IR 审计库的新副本，原库不变 |

PDF 必须同时满足 application/pdf 和 `%PDF-` 文件头；上限 5 MB、80 页、25 万文本字符、6,000 行。不接受加密 PDF，不执行链接/脚本或解出嵌入文件。PDF 字节上限不是解压内存上限，因此实际审计走独立子进程。

PDF 解析使用测试环境已有的 pypdf，延迟导入，尚未加入生产依赖，也未接进 enrich 的日常循环。这次真实 PDF 下载失败，只有合成样本的解析、限额与格式测试通过；**不能把单元测试当作 AFRM 真 PDF 验收**。

SEC 附件解析要求支持的 EX-99 包装及匹配的文件名；不是任何 SEC 页面都能解析。表格只保留行文本，财务列头/期间/口径仍需核查，发行人身份没有因为 URL 位于 SEC 域名而被自动补写。

## 4. 已接入排查链

新审计库为 `/tmp/quant-ep-sec-integrated-20260908.sqlite3`。它继承上轮 GTLB/NYAX IR 来源，并新增：

- PLAB、ANF：DOCUMENT_MATCHED + SEC_OFFICIAL_ATTACHMENT。
- AFRM：SOURCE_IMAGE_ONLY_OCR_REQUIRED，保存 8-K 父链接与手工核对的附件关系；不冒充标题已匹配或正文已核准。

原始采集候选未改；补录有实际收到时间、原始 SHA-256、HTTP trace 与新解析器版本。按原采集时刻做 as-of 查询不会看到今天补录的正文。VEEV 仍在以前独立审计库，没有声称这个副本包含全部历史证据。

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db /tmp/quant-ep-sec-integrated-20260908.sqlite3 explain PLAB

/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db /tmp/quant-ep-sec-integrated-20260908.sqlite3 explain AFRM
```

原始附件保存在 `/tmp/quant-ep-attachments-network-20260908`。临时目录不是长期备份；仓库只保存审计脚本、元数据和哈希，不发布整篇第三方正文。

证据：[网络报告](../reviews/2026-09-08-ep-attachments/network_report.json)、[离线集成报告](../reviews/2026-09-08-ep-attachments/integration_report.json)。报告不含用户联系邮箱或 API key。

## 5. FMP 权限核查的结论

详见[文本使用权限与客服询问草稿](ep_fmp_text_permissions.md)。**已核对公开规则，未取得账户专属书面授权**；没有声称用户已经违规，也没有声称个人 API 订阅天然允许内部群展示或第三方 AI 处理。

这不是要求用户提供一封已有邮件。“授权回复”指接下来可向 FMP 客服确认的结果。本轮没有替用户发信、购买套餐或修改任何现有服务。

## 6. 验证

289 项测试、25 个 subtests 通过，包含本轮新增 22 项 PDF/SEC 测试和既有 EP、杯柄、分钟监控、频道路由等回归。3 条警告来自既有 pandas fillna 行为；不是全仓库测试。

另行执行 Python 3.12 `-S` 只读 explain，PLAB/ANF 显示正确正文与 SEC 来源，AFRM 显示图片 OCR 缺口，全部保持 DISABLED_SHADOW_ONLY。原候选一致性、历史 as-of 排除补录、原始响应哈希均已核对；git diff --check 通过。本轮新增代码、报告和文档中没有落入用户联系邮箱。

## 7. 下一步和未完成项

1. AFRM 优先取得官方 PDF 原件，再验证 24 页完整性、文本层、关键财务表和页码引用；本轮未绕过 robots 超时，也未把网页检索缓存灌入应用数据库。
2. 若使用 SEC 图片附件，需要独立的图片归档和 OCR 方案，保留原图及置信度，数值、负号、小数点和表格列必须抽样复核。不能靠“LLM 写出了合理摘要”验收。
3. 现阶段 SEC 路径只支持显式已知附件；CIK 注册、申报发现和附件自动选择尚未实现。没有新增定时器或生产轮询。
4. 向 FMP 确认具体订阅的用途，收到书面答复后再决定 FMP 文本是否能送入外部模型、保存多久及如何在群里展示。

茶杯柄与多因子逻辑未改，EP 评级/开盘触发/自动推送仍未开启。
