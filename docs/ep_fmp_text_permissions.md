# FMP 文本使用权限核查

用户澄清（2026-09-08）：Discord 空间只有本人使用，用于个人通知。下文关于内部多人群的询问是基于先前误解保留的历史草稿，不再作为当前开发的阻塞项，也无需按该前提联系 FMP。第三方 LLM 尚未接入，相关文本处理范围在实际接入时核实。

核查日期：2026-09-08。仅核对公开规则，未查看账户订单或收到 FMP 针对此账户的授权答复。没有代发邮件、订购服务或删除现有数据。

## 简单解释

API key 证明可以调用接口，不等于证明任何使用方式都被许可。我们需要区分自己阅读、保存、交给外部 AI 分析，以及把结果提供给多个群成员。

[FMP Terms of Service](https://intelligence.financialmodelingprep.com/terms-of-service) 的 2.1/2.2 将许可与账户/订单用途关联；2.2.1 限制个人许可用于他人或群体，2.2.2 对内部或外部多用户展示要求特定协议。2.6.1 也涵盖来源于服务的衍生信息，因此不能只凭“没有转发全文”就认定摘要不受约束。2.8 和 6.2/6.3 涉及存储控制与终止后的数据处置。

这是公开条款摘要，不是对你账户的最终法律判断。所查条款没有给出足以确认当前账户允许第三方 LLM 推理的明确许可，不能自行推导为允许或一概禁止。条款引用的 Acceptable Data Use Policy 页面本轮未能读取，所以这部分也不能算完成核查。

## 目前的状态

| 用途 | 当前结论 |
|---|---|
| 现有 API 读取电话会文本 | 上轮技术访问成功；不等于全用途授权 |
| 本地留存与备份 | 保存期限、用途与账户协议待确认 |
| 第三方模型抽取事实/总结，不训练模型 | 待 FMP 明确确认；本轮未传输 |
| 内部 Discord 展示简短衍生摘要或数字 | 需确认适用的展示/再分发许可；未取得账户答复 |
| 转发完整原文或分享 API key | 本方案不采用 |
| 直接来自公司 IR/SEC 的材料 | 单独记录来源与适用规则，不因此默认任意再分发 |

**“尚未取得确认”不等于“你的订阅一定不允许”。** 订阅订单若已有额外约定，应依据实际协议核查，不必重复购买。

## 可提交的询问草稿

收件渠道：官网条款列出的 FMP support（info@financialmodelingprep.com）。以下仅为草稿，没有发送；联系邮箱没有自动作为 FMP 账户邮箱使用。不要在邮件里附 API key。

Subject: Confirm permitted use of earnings/news text under my subscription

Hello FMP Support,

Please confirm which of the following uses are covered by my current subscription, and identify any additional written agreement required:

1. Store earnings-call transcripts and press-release text in a private local/server evidence archive, including backups. What retention and deletion requirements apply?
2. Send selected text to a third-party hosted LLM for inference-only factual extraction and summarization, without model training. Please specify any provider, retention or processing-location restrictions.
3. Show brief derived summaries, selected metrics and source links to a private Discord group with multiple members, without distributing full source text or API credentials. Is a Data Display or Redistribution agreement required?
4. Is complete issuer press-release text available under my plan, rather than the 500-character responses observed? Are first-availability timestamps and historical revisions available for releases and transcripts?

Please also provide the applicable Acceptable Data Use Policy and confirm whether the permissions differ between news and earnings-call transcripts.

Thank you.

## 工程上如何处理

在确认前不新增 FMP 文本到外部 LLM 或群消息的管道，不把授权状态猜成 true。本轮没有变动已有生产推送。未来权限应按来源、数据集与用途分别记录，官方公开材料和 FMP 授权文本不能混为一份不注明来源的文本库。
