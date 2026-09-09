# EP v1A 第二批：原文证据与抽取接口

日期：2026-09-08（北京时间）。本批是本地隔离开发，不是 SG 上线。

后续更新：[第三批核查底稿与财务口径](ep_fact_review.md)已新增审核入口、schema 3 和九只样本验证。下文保留第二批当时的实现记录；最新来源可用性及限制见第三批说明。

第四批更新：[公司 IR 与授权全文覆盖验证](ep_official_source_coverage.md)：新增 GTLB/NYAX IR 正文、v2 解析修复和显式替代 URL；四份 FMP 电话会长文本单独审计，不冒充财报公告。

## 1. 这次交付什么

第一批做到“发现新闻、保存摘要、逐股解释缺口”，本批接着做到：

```text
已完成的 EP collect 运行
  -> enrich 选择带 URL 的公告（可指定 ticker）
  -> 域名、HTTPS、DNS 公网地址和 robots 访问检查
  -> 限额抓取原始 HTML，记录收到时间和 SHA-256
  -> 提取正文段落、标题及发行人归属线索
  -> 核对标题与已有公告、归属名称与证券身份
  -> 独立 SQLite 原文版本和核查记录
  -> sources / source / explain 离线排查
```

**没有接入真实 LLM，没有生成 Strong/Moderate，没有盘前量评级、开盘突破信号或 Discord 发送。杯柄、多因子、SG 服务和本机定时任务均未改动。** 当前无需提供 AI 密钥。

## 2. 文件与边界

| 文件 | 职责 |
|---|---|
| `src/data/public_articles.py` | 不携带凭据的公开 HTML 读取；与 FMP 请求/密钥隔离 |
| `src/breakouts/ep/enrichment.py` | 独立补抓批次、锁、文章预算、缓存、解析核查编排 |
| `src/breakouts/ep/source_verifier.py` | HTML/JSON-LD 正文提取、段落 ID、标题及发行人名称对齐 |
| `src/breakouts/ep/extractor.py` | 将来可注入的 EvidenceExtractor 接口、请求约束、逐字引用校验；当前不调用模型 |
| `src/breakouts/ep/store.py` | 独立原文表、内容版本、核查批次与按收到时间读取 |
| `scripts/run_ep_radar.py` | 新增 enrich / sources / source；原 report / explain 展示独立核查结果 |
| `tests/test_ep_sources.py` | 网络策略、正文、证据、缓存、迁移、历史时间与只读 CLI 测试 |

茶杯柄仍在 `src/breakouts/live/cup_handle.py`。EP 不导入该文件，不改变原分钟循环；HTML 网络请求仅存在于数据适配层。lxml 是仓库已有依赖，没有添加新依赖或 LLM SDK。

## 3. 怎样核查，什么不能据此下结论

正文优先选择 release-body、articleBody 或 article 区域；再尝试唯一 JSON-LD articleBody。不把整页导航、脚本或任意 div 当作正文。最少 300 字符、2 个段落只是“正文已建立”的技术门槛，不代表财报完整。超 250,000 字符或 4,000 段落不会放行。

`DOCUMENT_MATCHED` 要求正文已建立，而且已有公告标题中至少 85% 的不同英文/数字词出现在页面标题。它是文档对齐检查，不是原文真实性、完整性或数字真伪的证明。页面作者元数据仅在其 headline 与所选标题一致时使用；也读取所选正文内的 SOURCE 署名。

发行人名称去除有限公司后缀后，必须与采集时保存的证券名称一致才标记 `ISSUER_ATTRIBUTION_MATCH`。证券身份缺失为 `IDENTITY_UNAVAILABLE`；署名是律所、不同公司或名称无法匹配时为 `ISSUER_ATTRIBUTION_UNVERIFIED`，不能靠标题出现 ticker 放行。

这仍未解决母子公司别名、并购买方/卖方/被收购方、主题联动、新事件与旧消息、事件重要性。表格只按行保留文本，未重建多层列头和财务口径。`financial_facts_verified` 和 `historical_availability_verified` 始终为 false。

只有标题/正文和发行人归属都通过，才能构造未来模型的抽取请求。模型输出必须绑定文档 ID、正文版本、段落和逐字引用；数值、指标名、期间、口径必须出现在所引原句。无引用、错版本、捏造数字会被拒绝，额外交易命令不会进入结果。

**逐字引用通过也只叫“有文本支持的事实提案”。** 同一句可能含多个数值，模型仍可能关联错列、把基本 EPS 当稀释 EPS、混淆 GAAP/非 GAAP 或实际/预期。当前校验器不承担这层语义核准，输出 `eligible_for_rating=false`。没有来源的 whisper、挤空解释或“利好足够支撑涨幅”不会自动生成。

## 4. 留存与时间线

数据库仍是专属 EP SQLite，新增三张表：

| 表 | 内容 |
|---|---|
| `ep_source_runs` | 补抓批次、原 collect run_id、开始/结束时间、预算配置与摘要 |
| `ep_source_contents` | 原始 HTML BLOB、SHA-256、解析后的段落与正文版本；相同内容去重 |
| `ep_source_attempts` | 每篇公告每批次的收到时间、核查状态、原文引用或延期/失败原因 |

首次可写打开 schema 1 库时事务升级到 schema 2，保留旧表和旧评估；只读打开 schema 1 不做迁移，原文结果显示 NOT_ENRICHED。运行前请备份有价值的数据库；本次实测使用副本，没有升级原案例库或生产库。

采集报告 `ep_evaluations` 保持不变。因此旧 candidate 里的 `FULLTEXT_NOT_VERIFIED` 表示“那次采集时未核准”；之后的核查单独显示在 `source_enrichment`，不会覆盖旧结论。

`--as-of` 同时限制批次完成和实际收到时间。今天抓到一个上周发布的网页，只能作为今天收到的证据，不能回填成上周盘前已经知道。原始 HTML 存在本地库，不随普通报告输出；正文需要 source 命令显式查看。

`sources` 展示指定 collect 运行中每份公告最近一次处理结果，保留其他 ticker 在早前补抓批次的结果；顶层 summary/status 只描述最近批次，明确标记 `summary_scope=LATEST_BATCH_ONLY`。每项自带 source_batch_id 和完成时间。旧尝试仍保存在表内并可通过原 source_id 查阅；缓存不会改写初次 retrieved_at。

本批尚未把文章归档接入 Raw/Curated 行情发布目录，也没有自动清理、备份或容量维护任务。每篇原始响应默认最多 2 MB，正式部署前需确定保留期限和备份方式。原文仅作内部证据使用，后续推送应限制为简短摘录和来源链接，并遵守来源使用条款。

## 5. 网络、预算与缓存

默认只允许 `www.prnewswire.com`、`www.globenewswire.com`、`www.businesswire.com` 的 HTTPS。不是“所有新闻网站都能抓”；新增主机必须显式 --allow-host，不支持通配符。网页中的其他链接不会自动追踪。

所有跳转重新检查域名、协议和 robots；拒绝私网/回环/链路本地地址。DNS 返回中只要有非公网地址就拒绝；连接固定到已校验 IP，同时保留原主机的 TLS/SNI 证书验证。无 Cookie、代理、FMP 密钥或 Authorization，不复用浏览器登录。

robots 404 按未提供策略处理；明确 Disallow 不抓。robots 不可用、挑战页或非成功响应不视为许可。尊重 crawl-delay / request-rate；默认同主机至少间隔 1 秒。401/403/429 后停止该主机的本批请求，不读取拒绝页正文、不重试、不绕过访问控制。

默认每批最多尝试 5 篇文章、16 个 HTTP 请求（含 robots/跳转），90 秒软预算，单次网络等待最多 10 秒。DNS、阻塞 IO、锁等待等不提供硬实时中断保证。仅支持静态未压缩 HTML，不支持 PDF、登录或浏览器 JS 渲染。候选保留明确的延期原因，不因预算删除。

抓取成功且文档对齐、访问拒绝或 robots 禁止的同一文档版本，默认缓存 24 小时；普通失败 15 分钟。URL/DNS 策略拒绝、HTTP 请求或时间预算不足不享受成功缓存。缓存命中仍按本次候选身份重新核查；24 小时内原站修改可能尚未发现，这是明确的时效限制，不适合直接作为盘前实时催化承诺。

独立 `.sources.lock` 令同库补抓串行，避免同时请求同一文章。下次持锁启动会把未完成的原文批次标为 INTERRUPTED；已提交的证据仍保留。当前没有定时器、永久循环或自动续跑。

## 6. 怎么运行

使用项目已配置依赖的 Python。先 collect，后 enrich；不会自动补建缺失库。

```bash
python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 enrich \
  --ticker VEEV --max-documents 1 --max-http-requests 6 --deadline-seconds 60

python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 sources
python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 explain VEEV
python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 source <source_id>
python scripts/run_ep_radar.py --db /tmp/ep-shadow.sqlite3 sources \
  --as-of 2026-09-03T08:15:00-04:00
```

report/explain/sources 可加 --run-id；source_id 自带批次归属。四个查询命令均离线只读，并已通过 Python 3.12 `-S` 验证，不需要 lxml/FMP 初始化。原 Apple Python 3.9 的 WAL 只读兼容问题仍未绕过，请使用项目运行时。

| 状态 | 排查结论 |
|---|---|
| NOT_ENRICHED | 当前运行或截至指定时间尚无可用补抓批次 |
| SOURCE_DOCUMENT_BUDGET_EXCEEDED | 文章存在，但本轮没有轮到；不是无催化 |
| SOURCE_REQUEST_BUDGET_EXCEEDED / SOURCE_TIME_BUDGET_EXCEEDED | 网络预算不足 |
| URL_POLICY_REJECTED / NON_PUBLIC_ADDRESS_REJECTED | 来源不在策略内或地址不安全 |
| ROBOTS_DISALLOWED / SOURCE_HTTP_403 / SOURCE_HTTP_429 | 网站不允许或限流，不自动绕过 |
| SOURCE_TIMEOUT / SOURCE_TRANSPORT_ERROR | 未取得可靠响应；不存异常全文 |
| DOCUMENT_UNVERIFIED / SOURCE_PARSE_ERROR | 抓到内容但未建立正文/标题对齐，或解析失败 |
| DOCUMENT_MATCHED + IDENTITY_UNAVAILABLE | 找到对应正文，证券身份仍待处理 |
| DOCUMENT_MATCHED + ISSUER_ATTRIBUTION_MATCH | 文档与归属线索通过；尚不代表催化/评级通过 |
| NO_SOURCE_TARGETS | 本批没有可抓 URL，或指定 ticker 不在当前结果；继续查看 collect 范围 |

enrich 对 PARTIAL_SOURCES / NO_SOURCE_TARGETS 输出 JSON 并返回退出码 2。SOURCE_PASS_COMPLETED 只表示该批处理没有记录缺口（不检查发行人核准是否通过），不是全市场覆盖或 EP 策略 PASS。若全被政策排除，也可能是已完成处理；必须查看逐项结果。未来 systemd 不能把退出码 2 当作崩溃反复重启。

## 7. 本地实测

2026-09-08 00:04 北京时间，使用原 17 案例数据库的独立副本，只尝试 VEEV 财报：

- 2 个请求：robots 与 PRNewswire 公告，均返回 200。
- 正文 25,841 字符、258 段；提取到发行人署名 Veeva Systems。
- 标题对齐通过；正文包含 928、2.35、2027，但没有把这些字符串当作已核准财务字段。
- 原离线档案没有证券身份，结果仍是 IDENTITY_UNAVAILABLE，未编造身份、评级或信号。
- 另两篇 VEEV 公告因 max_documents=1 标为预算延期，批次 PARTIAL_SOURCES 是预期结果。
- 原采集评估逐项未改；0 次 LLM 调用、0 条 Discord、无 SG 操作。

材料：[冒烟脚本](../reviews/2026-09-08-ep-sources/source_smoke.py)、[机器可读结果](../reviews/2026-09-08-ep-sources/source_smoke_report.json)。原始 HTML/段落位于临时库 `/tmp/quant-ep-sources-20260908.sqlite3`，不应将临时目录视为长期备份。

这是一个已知公告的读取验证，不是 17 个案例都已核准、历史盲扫召回率或全来源可用率验收。

本批新增 38 项原文相关测试。合并 EP 第一批、杯柄、分钟监控、旧扫描、FMP 覆盖审计、动量频道路由和盘前摘要回归后，215 项测试、25 个 subtests 通过；3 条警告来自既有 pandas fillna 行为。不是全仓库测试。没有变更项目依赖，测试运行时仍是 `/tmp/quant-ep-v1a-test/bin/python`。

## 8. 下一步

先扩大原文样本验证，并把发行人/事件角色、发布时间及 GAAP/非 GAAP/实际/预期口径构造成可人工审核的事实表。可靠证据链建立后再接一家可配置的 LLM，测试它是否能逐项给出有效引用，而不只看中文摘要是否像截图。

市场侧仍须独立验证盘前累计成交量、历史同钟基线、报价时效和开盘分钟量。两条线通过各自验收之后，才能进入 EP 评级与开盘确认。不会通过给现有杯柄放宽条件或只加 AI key 就跳过这些检查。
