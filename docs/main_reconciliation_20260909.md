# 本地、SG 与 GitHub main 版本核对

## 合并输入

- 共同基线 / 当时 GitHub main：`39e2236`。
- 本地 main：`328cd4a`，比基线多 2 个杯柄修复/记录提交；另有 Kimi 适配和运维文档未提交。
- SG main：`27439d7`，比基线多 6 个板块轮动 v2 提交；另有 6 个已跟踪文件修改及 2 个杯柄诊断文件未提交。
- SG 两份诊断文件与本地一致，分钟缺口修复也是同一套实现，不应选择一边覆盖另一边。

本轮将 SG 未提交成果固化为 `aef1918`，本地成果固化为 `a13d4ff`，以 `5bcedbf` 保留双方历史完成三方合并。
没有文本冲突。合并结果相对于 SG 实际工作树只多出 Kimi 的 10 个文件，本报告为额外发布记录。
板块轮动、杯柄缺口审计、Kimi 提案层及双方最新运维文档均保留，没有回退某个需求来换取合并成功。

## 验证

- 本地：`python -m pytest tests -q --tb=short`，1048 passed、4 failed、63 subtests passed。
- 四项失败在共同基线 `39e2236`、相同本地环境原样复现：三个 broad PIT 分类列 fillna 测试和一个 market regime 日期 ns/us 精度测试。
  它们不是此次合并回归；本轮未扩大范围修改数据层。
- SG 使用 `/home/projects/quant/.venv/bin/python` 在独立 staging 目录运行完整 `tests`：1036 passed、16 skipped、1 warning，无失败。
- 首次在本地根目录直接执行 pytest 意外收集了被忽略的嵌套旧仓库，导致重复模块收集错误；改为显式 `tests` 后得到上述结果，没有删除旧仓库或修改测试来绕过。
- 针对新增提交的常见密钥/Webhook token 格式扫描未发现命中；密钥、运行数据库、tmp 和部署临时标记不加入提交。

## SG 发布边界

备份/验收目录：`/home/projects/quant/tmp/main-reconcile-gRuNqmR2`。

该目录保存 SG 原 main 的 Git bundle、隔离测试源码、发布清单，以及发布时的工作树补丁和文件 preimage。
部署器先核验旧 HEAD、全部预期源码的 Git blob 哈希、执行权限和未提交补丁哈希；发现并行修改即停止。
随后仅 stash 指定的 8 个代码/文档路径并保留该 stash，用 `git merge --ff-only` 更新主目录，逐文件核验发布树。
具体最终提交和保留的 stash ID 写入该目录的 `receipt.json`，不需要通过清理运行文件来制造全目录干净。

不覆盖或重建数据湖、SQLite、生产配置密钥；不改 systemd 已安装 unit、不重启服务、不发送 Discord。
SG 原有 tmp、locks 和部署标记保持本地运行状态，不纳入 GitHub。
部署前分钟监控已为 failed，Web 和运维 Web 为 running；代码同步不代表上游 RML/coverage 阻断已修复，不能把失败日改算通过。

## Kimi 下一步

已配置的 `/etc/quant/ep-llm-trial-kimi-cn.key` 经元数据检查为 root、0600，本轮不读取内容或发起付费调用。
仍使用国内开放平台 `kimi-k2.6`，不自动升级到 K3。
独立试验代码目录及累计账本保持不变：`/home/projects/quant/tmp/ep-llm-trial-20260909`。

下一步是一次手动 GTLB 提取，随后查看原文引用、实际/指引、基本/稀释 EPS、单位、期间、遗漏与真实 token 用量。
引用通过只代表文本支持，不自动认定财务语义正确；先审核这一份，再决定是否依次运行 ANF、NYAX、PLAB。
累计应用预算仍为 USD 10，不开启定时提取、评级发布或 Discord。具体命令和费用口径见 [Kimi 说明](ep_llm_kimi.md)。
