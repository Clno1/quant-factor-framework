# EP 自动来源发现与证据报告

日期：2026-09-08。用户已澄清 Discord 只有本人使用，是个人通知空间；本轮不再以“多人展示授权待确认”阻塞开发。外部 LLM 尚未接入，也未向第三方模型传输供应商文本。

后续进展：[正文事实提取与催化判断](ep_financial_analysis_iteration.md) 已实现离线规则提案和 schema 5 分析快照。本文保留上一轮来源发现与 schema 4 的历史验收记录。

## 1. 这一轮完成了什么

从“人工找到单篇公告 URL 再补抓”，推进到第一版自动流程：

```text
已有 collect 候选
  -> 查公司来源注册表（ticker / CIK / 官方身份依据）
  -> SEC submissions：确认 CIK 和当前 ticker
  -> 根据新闻日期筛选近期 8-K / 6-K
  -> 读取主申报，解析 EX-99 附件链接
  -> 限定同一申报目录，抓取支持的附件
  -> 提取正文并匹配原候选标题
  -> 保存整个查找过程、原始响应与正文版本
  -> dossier 输出逐股证据报告，fetches 展示请求历史
```

**本轮自动化的是已登记公司的 SEC 来源路径，不是全市场来源发现，也不是最终催化分析。** 没有把公司 IR 索引自动抓取、OCR 或 LLM 抽取说成已经完成。

## 2. 真实样本结果

注册表目前包含 AFRM、ANF、GTLB、NYAX、PLAB 五家公司。它只是来源登记表，不改变美股股票池，也不将其他股票判定为没有催化。

| 股票 | 自动来源结果 | 正文规模 | 当前限制 |
|---|---|---|---|
| ANF | 自动找到 8-K 和财报附件，DOCUMENT_MATCHED | 32,794 字符 | 财务口径及身份角色仍待核查 |
| GTLB | 自动找到 8-K 和财报附件，DOCUMENT_MATCHED | 27,365 字符 | 同上 |
| NYAX | 自动找到 6-K 和并购公告，DOCUMENT_MATCHED | 10,972 字符 | 买方/卖方/被收购方及交易重要性仍待结构化 |
| PLAB | 自动找到 8-K 和财报附件，DOCUMENT_MATCHED | 13,624 字符 | 同一申报的另一份 PDF 演示资料未处理，search_complete=false |
| AFRM | 自动追到 8-K 股东信附件 | 24 张图片，未建立文字正文 | SOURCE_DISCOVERY_INCOMPLETE，原因 IMAGE_ONLY_OCR_REQUIRED |

初次自动发现使用 17 个 HTTP 请求。真实数据暴露了两处解析缺口：GTLB/NYAX 的正文首行是 Exhibit 99.1，PLAB 的链接带页内锚点。修复后复验仅新增 2 个 HTTP 请求，其余使用同一库内的归档响应。

修复没有放松标题相似度检查：只跳过严格匹配的附件编号，并移除不影响资源地址的 URL fragment。域名、申报目录、HTTPS 和正文要求未放宽。

第二次运行的 10 个已登记、未被规则排除的候选公告中，4 篇对应正文匹配；1 篇在抓取范围内没有匹配附件，5 篇存在图片/PDF 等不完整原因。另记录 27 篇 ISSUER_NOT_REGISTERED、14 篇 EXCLUDED_FROM_SOURCE_QUEUE。**这些是公告处理记录数，不是股票数，更不是召回率或策略胜率。**

结果：[首次报告](../reviews/2026-09-08-ep-discovery/report.json)、[复验报告及五份逐股 dossier](../reviews/2026-09-08-ep-discovery/verified_report.json)。注册表中的 evidence_url 仅用于身份依据留档，自动发现没有把这些已知附件 URL 当抓取目标。

## 3. 统一留存

本轮数据库位于：`data/ep/research_shadow_20260908.sqlite3`。它从上一批 SEC/IR 审计库通过 SQLite backup 建立副本，保留旧正文和采集记录，新发现的原始响应与匹配结果继续追加到同一个库，不再分散写入新的 /tmp 文件。

该目录已加入 gitignore，数据库与第三方全文不进入 Git。默认 CLI 库仍为 `data/ep/observations.sqlite3`，没有悄悄替换；本轮研究库通过 --db 显式指定。未修改 SG 的数据库或部署路径。

schema 4 新增 `ep_source_fetches`：

- 保存 submissions JSON、主申报 HTML、附件 HTML 及失败/缓存记录。
- 每项有 fetch_id、所属批次、来源 URL、收到时间、记录时间、响应哈希。
- 正文来源记录引用 discovery_steps 中的 fetch_id，可以回查它是怎样找到的。
- 缓存命中通过 body_fetch_id 引用已有原始响应，不重复保存整个 BLOB。
- 缓存读取校验哈希；正文和财务审核仍使用原 ep_source_contents / ep_fact_reviews。

复验后共 31 条请求/缓存记录、16 份实际响应 BLOB，原始字节约 1.76 MB。元数据查询不加载全部原始 BLOB。这个规模只证明本轮小样本开销有限，不代表已完成 SG 长期容量压测。

schema 1/2/3 的只读打开不会迁移；可写打开按顺序升级到 4，旧候选不重写。原采集时刻的 as-of 查询看不到后来补录的资料，不能把回填数据用于声称历史实时能力。

VEEV 和旧 FMP 电话会的其他独立审计库没有被自动合并，本轮统一的是这条 SEC/IR 研究链和后续请求留存。长期备份、保留期限与生产库迁移尚未部署。

## 4. 自动查找规则

1. 没有注册 CIK 的公司显示 ISSUER_NOT_REGISTERED，不猜 CIK、不删除候选。
2. 使用 FMP 新闻的发布时间转为美东日期，查找当天至之后三个自然日、且在观察时刻之前已被 SEC 接受的申报。这是来源关联搜索窗口，不是催化新鲜度规则。
3. 只查 SEC submissions 的 recent 数组和支持的 8-K/6-K 及修订类型。未追历史分页；不把超出范围解释为没有公告。
4. 默认每篇新闻最多检查 3 个申报、每个申报 3 个附件，每批最多 10 篇新闻、30 次 HTTP 请求、180 秒软预算。首次样本脚本采用 35 请求上限。
5. 从主申报中带 EX-99 编号的表格行提取链接。只允许同一 accession 目录，不跟随外部广告、任意链接或目录跳转。
6. 多个附件都匹配时显示 AMBIGUOUS_MATCH_REVIEW_REQUIRED，不任意选一个。预算、解析、图片和 PDF 缺口单独保留。
7. submissions 成功响应缓存 15 分钟，HTML 成功响应缓存 24 小时；拒绝/超时等选定失败缓存 15 分钟。缓存不会改变原始收到时间，也不保证缓存期内发现网站修订。

这是受限单次任务，不是分钟价格循环的一部分。复用既有 robots、公网 DNS/IP 校验、TLS、字节上限和域名拒绝保护；没有本地定时器、永久循环或新 Discord 发送。

## 5. 报告回答什么

`dossier` 把以下内容连在一份逐股 JSON 报告里：

- 原采集时的身份、新闻标题、发布时间、首次观察时间。
- 事件类型提示和关联状态，明确标注尚未核准。
- 最近一次来源查找的结果、路径和失败原因。
- 仍可供核查的最近已匹配原文，即使新一次查找失败也不会隐藏旧证据。
- 正文版本、来源 URL、营收/EPS/指引/一次性项目等主题的段落定位。
- 已有人工审核事实及其引用；没有审核时 facts 是空数组，不由关键词自动填数字。
- 缺失核查项，包括事件角色/时效、财务口径、盘前量能和开盘确认。

**目前交付的是证据报告，不是最终 Strong/Moderate 分析报告。** 四家有正文也仍需核查；grade=null、eligible_for_rating=false。已有人工审核条目也不被当作程序已自动证明的财务事实。

## 6. 代码阅读路线

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | `configs/ep_sources.json` | 公司身份及来源注册表，首批五家 |
| 2 | `src/data/sec_attachments.py` | 受限 SEC submissions/附件访问，邮箱只用于请求头 |
| 3 | `src/breakouts/ep/discovery.py` | 申报选择、附件查找、预算、缓存与整体编排 |
| 4 | `src/breakouts/ep/sec_source.py` | EX-99 正文提取，严格跳过附件编号标题 |
| 5 | `src/breakouts/ep/store.py` | schema 4 请求归档、正文历史、时间过滤 |
| 6 | `src/breakouts/ep/dossier.py` | 将候选、原文、最新失败与审核记录整理为报告 |
| 7 | `scripts/run_ep_radar.py` | discover / dossier / fetches 命令 |

茶杯柄仍在 `src/breakouts/live/cup_handle.py`，本轮没有改动。没有把网页或模型请求塞进盘中价格监控。

## 7. 怎么查看

以下命令只读、离线，不需要邮箱、FMP key 或 LLM：

```bash
/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 dossier GTLB

/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 dossier AFRM

/tmp/quant-ep-v1a-test/bin/python scripts/run_ep_radar.py \
  --db data/ep/research_shadow_20260908.sqlite3 fetches
```

需要再次联网发现时，先在运行环境配置 SEC_CONTACT_EMAIL，再运行 discover。不要把邮箱写入共享脚本或提交 Git。示例：

```bash
python scripts/run_ep_radar.py --db data/ep/research_shadow_20260908.sqlite3 \
  discover --ticker GTLB --max-documents 2 --max-http-requests 10
```

PARTIAL_SOURCES 退出码为 2，表示有明确缺口，不应被 systemd 当作崩溃无限重启。本轮没有安装任何 systemd/本机任务。

## 8. 下一步

接下来推进带证据的结构化事实提案和催化分类；优先以已取得正文的四家公司验收，再扩展注册表。外部 LLM 的接入、模型预算和供应商文本处理范围届时单独确认，不再以不存在的“多人 Discord 群”作为前提。

IR 索引自动查找、AFRM PDF/OCR、全市场来源覆盖、真实盘前量比和开盘触发仍未完成。来源层推进不能替代这些独立验收。

## 9. 验证结果

本轮回归覆盖 EP 来源发现、正文和审核、杯柄、隔离、分钟监控、小时告警、Discord 路由与盘前摘要：311 个测试和 25 个子测试通过。另有 3 条既有 FMP pandas FutureWarning，无测试失败。

真实研究库的 dossier / fetches 已通过 `python -S` 离线只读检查；`git diff --check` 通过。联系邮箱未写入本轮源码、报告或文档，研究数据库及原文所在目录已确认被 Git 忽略。
