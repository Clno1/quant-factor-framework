# EP 原文与扩展时段量能复核

## 本轮边界

本轮只处理来源可用性、PDF 留档和行情口径，不调整模型或 prompt，不产生付费 LLM 调用，
不发 Discord，不修改生产 timer。改动已同步到 SG 的独立验收副本，不是生产发布。
SG 继续使用 `/home/projects/quant/.venv/bin/python`；PDF 依赖仍在验收目录的隔离 dependencies 中。

## 原文覆盖

以下为两轮累计的七个明确指定历史公告样本，不是全市场扫描召回率。

| 股票 | 官方文字正文 | 本轮结果 / 保留缺口 |
| --- | --- | --- |
| ANF | SEC，32,794 字符 / 343 段 | 上轮已获取；公司动态新闻接口 403，未绕过 |
| GTLB | SEC，27,365 字符 / 283 段 | 上轮已获取；IR 访问拒绝，未绕过 |
| NYAX | SEC，10,972 字符 / 41 段 | 上轮已获取；官网访问拒绝，未绕过 |
| PLAB | SEC，13,624 字符 / 157 段 | 新增自动获取 EX-99.2 PDF：20 页 / 424 行 / 15,047 字符 |
| SAIC | SEC，31,420 字符 / 333 段 | 本轮新增；官网到 IR 域名关联通过，但 SG 读取 IR robots 超时 |
| SNOW | SEC，48,195 字符 / 337 段 | 本轮新增；官网到 IR 域名关联通过，但 SG IR 返回 403 |
| AFRM | 尚未获得可用于自动解读的文字正文 | 同一官方链接延长到 20 秒仍超时；SEC 附件为图片型，不冒充已读全文 |

SAIC 的 [官方 SEC 公告](https://www.sec.gov/Archives/edgar/data/1571123/000157112326000131/saic08312026ex991earningsr.htm)
和 SNOW 的 [官方 SEC 公告](https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm)
均经过当前证券主表 CIK、SEC submissions ticker、申报编号及附件链接路径校验。
不因为发行人相同就把财报“发布预告”当成正式财报。

目前 **没有新增一条 SG 直接 IR 正文成功链**。新增的实际可用正文来自独立公开的 SEC 披露。
公共浏览器能看到 IR 页面，不能代表 SG 自动获取链能读到同样的原文。
未修改 User-Agent、使用代理、忽略 robots 或重复请求已返回 403 的路径。

## 代码修复

- `src/breakouts/ep/discovery.py`：SEC EX-99 PDF 不再一律跳过。仅接受同一 CIK / accession
  目录中申报文件明确列出的附件，保存原始哈希、申报接收时间、附件标签和父来源 ID。
- `src/breakouts/ep/event_ingest.py`：SEC 下载上限与既有 PDF 限额对齐为 5 MB。
  首次自动实测发现原来的 2 MB 限额拦住了 4,211,599 字节的 PLAB PDF；修复后实测成功。
- PDF 仍在独立、限时限内存的解码进程中处理，不进入分钟报价循环。图片型文档不自动视为文字正文。
  每个事件最多获取两个 PDF，防止附件较多的公告占满整个采集周期。
- 附件状态为 `OFFICIAL_ATTACHMENT_TEXT_AVAILABLE`，不自动变成 `DOCUMENT_MATCHED`。
  页码和文字可供后续核验，但没有声明表格中的金额、期间、主体已绑定，也没有自动进入财务解读队列。
- `src/breakouts/ep/store.py`：主文和附件分别去重，附件不会覆盖主文或主来源任务摘要。
  经验证的 IR 主文与附件也能同时出现在原文列表中，无数据库结构迁移。
- `src/breakouts/ep/market_quality.py`：尚未收完的开盘区间显示 `NOT_YET_CLOSED`，
  不把盘前尚未发生的 09:30 K 线误报成已发生的数据缺失。
- `configs/ep_official_domains.json`：新增 SAIC、SNOW 的精确发行人记录，仍非通配域名授权。

## 成交量口径

美东 2026-09-11 03:43，SG 对三个标的同时比较了扩展报价、普通报价、前一日 EOD 和当日分钟线。

| 股票 | 扩展报价 volume | 普通报价 volume | 前一日 EOD volume |
| --- | ---: | ---: | ---: |
| AAPL | 70,011,913 | 69,820,744 | 69,820,744 |
| GTLB | 4,432,332 | 4,378,452 | 4,378,452 |
| NYAX | 13,640 | 13,639 | 13,639 |

这些扩展报价的时间戳属于前一天盘后，普通报价属于前一天收盘。
**推测**：扩展报价的量更像包含常规时段的日累计，而非纯扩展时段成交量。
这只是样本观察，不能据此确认重置时刻、交易所覆盖或用两端相减恢复盘后量。
03:43 的当日分钟返回空数组属于盘前窗口开始之前的观察，不能据此证明“不支持盘前”。

美东 **04:01 再次从 SG 实测**：

| 股票 | 扩展报价 volume | 价格 / 时间观察 |
| --- | ---: | --- |
| AAPL | 49,230.30225 | 最新成交价 325.9，成交及报价时间戳已是当日 04:01 |
| GTLB | 0 | 最新成交价 47.3，tradeSize=1；报价时间戳晚于该成交但 volume 仍为 0 |
| NYAX | 0 | 报价时间戳进入当日，但最后成交仍为前一日 |

结论分开看：盘前价格更新能力在 AAPL/GTLB 已观察到；三个报价 volume 都明显下降，
说明两次取样之间发生了重置或统计范围切换，但不能确定精确重置时刻。
报价成交量包含小数、成交与报价量可能不同步，不能因 timestamp 新鲜就认定每个字段都新鲜。
这些真实情况已加入量能诊断与回归用例，原值保留，不四舍五入为“真实股数”。

04:01 当日分钟接口仍均为空。取样靠近盘前起点，不能区分发布延迟与扩展时段未覆盖；
结合上一轮历史事件样本没有盘前分钟线，当前仍不能构建可信的同一时刻历史盘前 RVOL。
另外，前一日 GTLB EOD volume 从 4,378,452 更新为 4,432,377，NYAX 从 13,639 更新为 13,640，
普通报价仍保持旧值。必须记录接收版本，不能把后来修订的数据当成当时已知。

FMP 已查阅的 [扩展报价文档](https://intelligence.financialmodelingprep.com/developer/docs/stable/batch-aftermarket-quote)
与 [分钟线文档](https://intelligence.financialmodelingprep.com/developer/docs/stable/intraday-1-min)
未在可读页面中说明量的重置、场所覆盖及不同周期成交量差异。因此 `contract_verified` 继续为 false。

明确不做以下替代：把 `tradeSize` 当累计量；把 bid/ask size 当成交量；
把轮询到的最后一笔交易加总当完整 tape；把 `close * volume` 标为实际成交额；
把缺少的历史盘前量补零后生成 RVOL；按 EOD 比例放大分钟量。

## 验证与复现

- 最终本地 656 项通过，另有 43 个 subtests；SG 相同范围 656 项通过。
- 七个关键代码/配置/核验脚本的本地与 SG 验收副本 SHA-256 一致。
- 回归覆盖范围：EP、杯柄、盘中监控、小时提醒、盘前摘要与消息路由。
- 覆盖 EP、PDF 附件独立留档、来源缓存、队列、模型费用隔离、杯柄隔离及 Discord 路由。
- `volume_probe.py` 单次最多 9 个 FMP 请求，401/403/429 后停止；无 `--execute` 时不请求、不写文件。
- `followup_replay.py` 仅读取留档 JSON，不调用数据商。结果保留每份报告哈希，失败请求也计入次数。
- 本轮 SG 实测合计 30 个公开来源请求、18 个 FMP 请求；LLM 请求与 Discord 消息均为 0。
- SG 生产仓库仍为 `8af9686`，本轮未修改生产服务、消息通道或定时任务。
  验收目录为 `/home/projects/quant/tmp/ep-sg-capability-20260911-0700`，不能把验收通过当成已上线。

```sh
python reviews/2026-09-11-ep-sg-capability/followup_replay.py \
  --evidence tmp/ep-sg-capability-evidence \
  --output reviews/2026-09-11-ep-sg-capability/followup_report.json
```

## 接下来仍缺什么

1. AFRM 可由 SG 正常获取的官方文字版本，或经过验证的图片识别证据链。不能靠模型猜测填补。
2. FMP 对成交量范围、分钟周期差异、盘前历史覆盖的明确技术答复。
   [技术问题草稿](ep_fmp_volume_support_questions.md) 已整理，尚未替用户发送。这不是授权或升级会员问题。
3. 把已可访问正文的供应覆盖扩展到更多发行人，同时保留逐股票的来源失败诊断。
4. 量能契约通过后，再连接盘前 RVOL、催化等级和开盘确认。当前没有开放这些正式信号。
