# EP Kimi 手动验收

更新：2026-09-09。只替换模型提案适配层，不改变杯柄、原文校验、评级资格或 Discord 状态。

本轮状态：用户补充普通开放平台文档并要求部署后，已将 Kimi K2.6 国内平台适配部署到 SG 独立验收目录。
本地相关回归 191 项通过；SG 快照中的六组 EP 回归 182 项通过。完整 Web/策略隔离测试只在本地运行，未扩展独立快照来部署 Web 测试。
十个指定代码、测试与文档文件经内容校验一致；没有覆盖 SG 主项目、生产数据库或其他开发改动。
旧代码备份：`/home/projects/quant/tmp/ep-kimi-predeploy-kgF3dbBb`。
四份已有公告的 `plan` 均完整覆盖归档段落；部署后 `status` 仍为 0 次调用、0 预算预留、投递禁用。
`/etc/quant` 权限为 0700，新的 Kimi 密钥文件尚未创建。没有读取旧密钥、付费调用、服务重启或定时任务变更。

## 平台与模型

| 配置 | 固定 API 地址 | 模型 | 密钥环境变量 |
|---|---|---|---|
| `openai` | `https://api.openai.com/v1/responses` | 原有白名单 | `EP_LLM_API_KEY` |
| `kimi-cn` | `https://api.moonshot.cn/v1/chat/completions` | `kimi-k2.6` | `EP_KIMI_API_KEY` |
| `kimi-intl` | `https://api.moonshot.ai/v1/chat/completions` | `kimi-k2.6` | `EP_KIMI_API_KEY` |

必须根据密钥所属平台选择，不会换域名试密钥，不支持任意代理 URL、Coding Plan 或第三方转售密钥。
Kimi Code 与开放平台是独立系统，密钥不通用。官方社区规范明确 Code 订阅只用于个人交互场景，不用于脚本批处理或数据标注流水线；
本项目不通过伪造客户端 User-Agent 来绕过限制。若确为 Code 订阅，需要改用适合后台调用的开放平台 API；若是第三方，需要另行核实具体接口、模型、费用与数据接收方。
来源：[Kimi Code 平台比较](https://www.kimi.com/code/docs/)、[社区使用规范](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html)。
Kimi 官网当前把 `kimi-k2.5` 标为 2026-08-31 退役，因此没有采用旧教程的该模型。
来源：[官方模型列表](https://platform.kimi.ai/docs/models)、[国内 Chat API](https://platform.kimi.com/docs/api/chat)、[国际平台概览](https://platform.kimi.ai/docs/overview)。

K2.6 使用非思考、单结果、非流式请求；不启用搜索或工具，不自动重试、换模型或修补坏 JSON。
其结构化输出对复杂 Schema 的支持存在限制，因此将固定契约的 `$ref` 展开，将字符串/null 的 `anyOf` 化为等价类型联合。
本地仍以原始 Pydantic 契约和确定性引用校验器检查，截断、工具调用、拒答、错误模型和缺失 usage 均不放行。
来源：[官方结构化输出说明](https://platform.kimi.ai/docs/guide/response_format)。

## 预算口径

2026-09-09 官方页面每百万 token 标价：

| 平台 | 输入（未缓存） | 输出 | 缓存输入 |
|---|---:|---:|---:|
| 国内 | CNY 6.50 | CNY 27.00 | CNY 1.10 |
| 国际 | USD 0.95 | USD 4.00 | USD 0.16 |

来源：[国内 K2.6 价格](https://platform.kimi.com/docs/pricing/chat-k26)、[国际 K2.6 价格](https://platform.kimi.ai/docs/pricing/chat-k26)。
价格表嵌于官网页面组件，普通正文抓取可能不显示数值。这里只使用未缓存价格做预留，不依赖缓存折扣。
国际页面注明不含适用税费。以上不是账户最终账单，也不保证 Kimi 总费用低于原模型。

现有账本单位保持 microUSD。国内费用采用保守预算系数 `1 CNY = 0.20 budget USD`，不是实时汇率或实际结算价；
例如国内估计 CNY 1 消耗 USD 0.20 的应用预算。原币费率、换算系数和价格版本写入每次预留记录，不能直接把人民币当成美元。
实际账单应在平台核对；应用预留不是绝对账单保证，平台侧充值额度/消费控制仍需独立设置。

共用原库 `/home/projects/quant/tmp/ep-llm-trial-20260909/data/ep/llm_trial_20260909.sqlite3`。
累计上限 USD 10、每日 USD 3；OpenAI 和两个 Kimi 区域的预留相加，不重建账本规避额度。
失败也保留预留；模型身份不一致或估计用量超预留，触发共用的计费复核锁。

## SG 命令

以下命令假设确认使用国内开放平台。国际平台把所有 `kimi-cn` 改为 `kimi-intl`，不能混用密钥。
先执行不读密钥、不发 HTTP 的预检：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn plan'
```

交互隐藏输入密钥，默认保存到 `/etc/quant/ep-llm-trial-kimi-cn.key`（国际为 `ep-llm-trial-kimi-intl.key`），权限 0600：

```sh
ssh -t root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn configure-key'
```

该命令不覆盖已存在的密钥。不要把密钥贴到聊天或命令参数里；不要继续用旧 OpenAI 默认路径执行 Kimi。
如果已经把 Kimi 密钥放进旧文件，无需删除旧文件，重新隐藏输入到独立 Kimi 文件即可。

核准平台和预算后，一次只执行 GTLB，**此命令会产生真实模型调用与费用**：

```sh
ssh root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/tmp/ep-llm-trial-20260909/reviews/2026-09-09-ep-llm-trial/run.py --provider kimi-cn run GTLB --execute'
```

`status` 替换末尾的 `run GTLB --execute` 可只读查看累计账本。`plan`、`status` 不读取密钥。
付费 `run` 和密钥配置要求显式 `--provider`，试验脚本忽略环境中的模型/预算覆盖。

## 验收边界

先检查一份公告的完整性、漏提、单位、期间、主体、GAAP/non-GAAP、实际/指引和引用，随后再决定是否运行 ANF/NYAX/PLAB。
`VALIDATED` 仅表示输出完成既定检查流程，仍应查看每条 accepted/rejected；不能当成全部财务语义已核准。
模型生成中文分析或与截图相似不算验收通过。此次没有自动发现、自动评级、定时调用或真实 EP 推送。

本地单元测试与 SG 无密钥预检只证明适配代码路径，不证明密钥可用、账户模型权限或真实提取质量。
