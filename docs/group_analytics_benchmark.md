# Stage-1 Group Analytics 性能基准（GA1-19）

## 结论

本次本机工程基准 **PASS**：500 个确定性合成 SP500 成员的聚合/制品发布，以及从 immutable latest 制品生成 heat/detail payload，三项 p95 均低于 GA1-19 门槛。

| 场景 | 样本 | 条件 | p50 | p95 | 最大值 | 门槛 | 结果 |
|---|---:|---|---:|---:|---:|---:|---|
| SP500 EOD 聚合 + 快照发布 | 20 | 连续运行，无额外预热 | 194.300 ms | **207.929 ms** | 219.275 ms | 5,000 ms | PASS |
| latest + heat payload | 100 | 预热 1 次后计时 | 77.964 ms | **84.941 ms** | 106.339 ms | 300 ms | PASS |
| latest + detail payload | 100 | 预热 1 次后计时 | 82.478 ms | **87.824 ms** | 96.081 ms | 500 ms | PASS |

这是当前开发机上的可重复工程基准，**不等于新加坡生产机复测，也不能代替生产发布门槛**。上线前仍需在新加坡实际部署环境，以生产依赖版本、文件系统、进程配置和真实 SP500 数据重复执行并保存结果。

## 基准口径

基准脚本：[benchmark_group_analytics.py](/Users/huozhihong/Documents/Quant/scripts/benchmark_group_analytics.py)

执行命令：

```bash
python3 scripts/benchmark_group_analytics.py --pretty
```

脚本不访问网络，默认使用临时目录，结束后自动清理制品；标准输出始终为一个严格 JSON 文档。任一 p95 超阈值时退出码为 `1`，执行异常时退出码为 `2`。

计时范围如下：

1. `aggregate_snapshot`：每次重新聚合全部 500 个成员，调用真实 `aggregate_groups` 和 service 的 frame enrichment，然后通过 `FileGroupArtifactStore.publish` 写入、fsync、哈希校验并原子发布一套新的 immutable Parquet 制品。连续计时 20 次，没有未计时的业务预热；Python 模块导入发生在计时前。
2. `latest_heat_payload`：先做 1 次未计时预热，再计时 100 次。每次重新解析 `latest_success`，校验 manifest 和文件哈希，读取 metrics/members/contributions 三个 Parquet 文件，调用 Web router 的真实 heat payload builder，并进行严格 JSON 序列化。
3. `detail_payload`：先做 1 次未计时预热，再计时 100 次。每次同样重新读取并校验 latest 制品，调用真实 detail payload builder 生成第一页 50 个成员和贡献度信息，并进行严格 JSON 序列化。

latest/detail 计时覆盖只读制品和路由 payload 业务路径，不包含外部网络、TCP/HTTP socket、反向代理和浏览器渲染开销。p95 使用 `numpy.percentile(method="linear")` 计算。

## 固定数据规模

| 字段 | 值 |
|---|---|
| Fixture | deterministic synthetic SP500 |
| 成员数 | 500 |
| 行业数 | 11 |
| 分类层级 | sector |
| 有效收益覆盖 | 500 / 500 |
| 数据日期 | 2026-07-15 |
| 外部网络调用 | 0 |
| Fixture hash | `sha256:afed39ee9f7d0a29d59fb6e72393f0b143e45b4bbd9759b3a69cf65c5a7c371e` |

收益输入由固定数学表达式生成，不使用随机数；ticker、security_id、分类和收益横截面在每次执行中一致。Fixture hash 可用于确认后续复测仍在使用同一份逻辑数据。

## 本次环境

测量日期：2026-07-16（Asia/Shanghai；运行窗口 `2026-07-16T06:39:49.049015Z` 至 `2026-07-16T06:40:09.451561Z`）

| 项目 | 值 |
|---|---|
| Host | ZHIHONGHUO-MC1 |
| OS | macOS 14.6.1, arm64 |
| CPU | arm64 / 12 logical CPUs（运行时报告 processor=`arm`） |
| 物理内存 | 36,864 MB |
| Python | CPython 3.13.9 |
| NumPy | 2.4.4 |
| pandas | 3.0.2 |
| PyArrow | 24.0.0 |
| FastAPI | 0.136.1 |
| 进程峰值 RSS | 145.2 MB |

## 判定与后续复测

本次结果支持 GA1-19 的本地 Engineering DoD：即使把 Parquet 落盘、fsync、哈希校验、latest 固定读取和 JSON 序列化都纳入计时，仍有明显余量。

生产机复测应至少做到：

- 使用相同脚本和同一 fixture hash，先确认硬件/依赖差异下的基线；
- 再用真实 500 成员快照做补充测试，但继续排除外部下载时间；
- 保存脚本的完整 JSON 输出、Git revision、systemd 资源限制及数据盘类型；
- 分别记录冷启动首轮、100 次 warm 请求、并发读请求和定时任务写入期间的读延迟；
- 生产复测若超出门槛，不得用本机结果豁免，需优化或由负责人批准新的冻结基线。
