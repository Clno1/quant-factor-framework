# 分钟级茶杯柄检测、回放与影子验收

## 1. 当前状态

茶杯柄检测已作为盘中动量服务内的一条独立算法链实现。它复用同一份 FMP 报价和一分钟行情请求，但不复用旧动量突破的算法版本、信号去重键或五交易日验收台账。

- 算法版本：`daily-cup-5m-handle-shadow-v1`
- 参数版本：`2026-08-29.1`
- 信号族：`CUP_HANDLE_BREAKOUT`
- 默认模式：`shadow`
- 默认投递：关闭
- 独立验收：五个完整且通过门槛的 XNYS 交易日

代码部署后即开始影子计算，但不代表可以发送消息。只有新的茶杯柄台账达到 `5/5`，并且人工检查误报、延迟与拒绝分布后，才能单独打开 `cup_handle.delivery_enabled`。旧动量突破已经积累的观察日不能算入这条新算法。

## 2. 为什么拆成日线与五分钟两层

经典茶杯柄不是只看几根分钟线。杯体通常跨越数周或数月，柄和突破才适合在盘中确认。因此系统采用：

1. T-1 收盘后的日线数据识别杯体。
2. T 日盘中只使用已经完整结束的五分钟 K 线识别柄。
3. 柄缩量、深度和长度合格后，再等待收盘价放量越过杯沿。

这样既不会用到尚未收完的五分钟 K 线，也不会用当天盘中结果反过来修改昨天的杯体。

## 3. 日线杯体候选

候选预计算会遍历正式 `US_ACTIVE` 入口所绑定的宽基可交易股票，默认要求当前美元成交额至少 500 万美元，排除 ETF。每只股票只读取截至 `source_data_date` 的日线。

默认参数：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| 日线回看 | 126 个交易日 | 限制搜索范围和计算量 |
| 杯宽 | 20 至 100 个交易日 | 排除过短波动和过久历史形态 |
| 右杯沿最近窗口 | 15 个交易日 | 只保留接近当前的杯体 |
| 杯深 | 8% 至 35% | 排除浅噪声和深度破坏 |
| 理想杯深 | 18% | 只参与候选评分，不是额外硬门槛 |
| 左右杯沿差 | 不超过 8% | 确认价格已经回到左杯沿附近 |
| 杯底位置 | 全宽的 25% 至 75% | 避免 V 形或严重偏斜 |
| 右侧恢复比例 | 至少 85% | 右侧必须完成足够恢复 |
| 右侧/左侧成交量 | 不超过 1.10 | 排除右侧明显放量失控的杯体 |

算法同时保存左杯沿、右杯沿、杯底的日期与价格，以及杯深、杯宽、杯底位置、量能比和 0 至 100 分的杯体评分。候选快照最多保留 600 只；合格杯体优先进入快照，再由实时报价选出最多 40 只活跃监控股票。

## 4. 有界五分钟序列

行情源仍是完整一分钟 OHLCV。`RollingIntradayBars` 按常规交易时段聚合五分钟 K 线，并执行三项硬约束：

1. 一个五分钟桶必须具有五根完整的一分钟 bar。
2. 桶结束时间必须不晚于当前已完成分钟。
3. 对检测器最多输出 96 根，超过上限按错误处理。

美股完整常规交易日只有 78 根五分钟 bar，96 的上限留出边界空间，但禁止无界增长。输出字段固定为 `timestamp/open/high/low/close/volume`。

## 5. 盘中柄与突破

默认参数：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| 柄长 | 3 至 18 根五分钟 bar | 约 15 至 90 分钟 |
| 柄深 | 1% 至 12% | 排除没有回撤或破坏过深 |
| 柄深/杯深 | 不超过 50% | 柄不能吞掉大半杯体 |
| 杯沿接近容差 | 3% | 柄必须从杯沿附近开始 |
| 成交量基线 | 柄前 6 根 bar | 与柄内平均量比较 |
| 柄量/基线量 | 不超过 0.85 | 要求柄内缩量 |
| 突破缓冲 | 10 bps | 收盘价需越过杯沿而非刚好触碰 |
| 突破量/柄量 | 至少 1.20 | 要求突破 bar 相对放量 |

系统还要求柄低点位于杯体中点之上、上一根收盘尚未提前突破、当前完整五分钟 bar 首次收盘越过触发线。每次评估都会记录一个明确结果：`MATCH`、`REJECTED`、`NOT_READY` 或 `ERROR`，以及稳定的拒绝原因代码。

## 6. SQLite 与 outbox

状态库仍是：

`outputs/intraday_momentum_monitor/state.sqlite3`

新增表：

| 表 | 数据 |
|---|---|
| `cup_handle_evaluations` | 每分钟、每股票的结果、拒绝原因、耗时、bar 数和参数明细 |
| `cup_handle_cycles` | 每个五分钟刷新周期的评估数、命中数、错误数、P95 延迟和最大序列长度 |
| `cup_handle_session_observations` | 每个交易日独立的 PASS/FAIL 与门槛证据 |

逐股票与逐周期明细默认只保留最近 30 个交易日，防止 SQLite 无界增长；每日汇总验收、信号和 outbox 不随这项清理删除。

命中仍写入通用 `signals` 和 `signal_outbox`。主键包含 `algorithm_version` 与 `trigger_family`，所以茶杯柄不会与 `MOMENTUM_BREAKOUT` 相互覆盖。影子期 outbox 状态固定为 `SHADOW`，不会进入发送领取流程。

## 7. 历史回放与误报代理

命令：

```bash
.venv/bin/python scripts/replay_cup_handle.py \
  --ticker MDB --ticker AEVA \
  --minute-dir data/raw/intraday/1min \
  --start 2026-08-01 --end 2026-08-28
```

回放必须已有本地一分钟 Parquet；缺文件会直接失败，禁止静默调用 FMP。日线读取正式 MarketDataReader 合同。每个历史交易日只使用前一交易日及更早的日线，分钟线按当时顺序逐根送入检测器。

默认误报代理定义是：信号出现后的六根完整五分钟 bar 内，价格若先达到信号价上方 2%，记为 `CONFIRMED_PROXY`；若先跌破柄低点，或完整窗口结束仍未达到目标，记为 `FALSE_POSITIVE_PROXY`；窗口数据不足则记为 `UNRESOLVED`。这是算法筛查指标，不是收益率、成交模拟或真实交易胜率。

## 8. 运维站

打开“盘中动量持续监控”任务详情，可以看到：

- 茶杯柄独立影子进度；
- 当日命中、拒绝、等待与错误数；
- 检测 P95 延迟；
- 最大五分钟序列长度；
- 前八类拒绝原因；
- 当前算法版本和 shadow/live 模式。

命令行状态：

```bash
.venv/bin/python scripts/run_intraday_momentum_monitor.py \
  --env-file /etc/quant/intraday-momentum-monitor.env --status
```

返回中的 `cup_handle_promotion` 是独立门禁，不能用旧的 `promotion` 替代。

## 9. 五交易日门槛

一个交易日只有同时满足下列条件才计为 PASS：

- 五分钟刷新周期覆盖率达到配置门槛（完整交易日通常应有 78 个周期）；
- 当日实际产生茶杯柄评估记录；
- 当日日线杯体筛选确实运行；
- 行情版本合同完整；
- 错误周期比例没有超限；
- 检测延迟 P95 不超过 250 ms；
- 任一检测序列都不超过 96 根。

没有茶杯柄命中不是失败，因为市场可能没有合格形态；没有运行评估才是失败。失败日和缺失日都不能计入 `5/5`。

## 10. 2026-08-29 SG 首次部署

茶杯柄 shadow 代码已部署到 `/home/projects/quant`，部署前备份位于：

```text
/home/projects/quant-backups/cup-handle-shadow-20260829T152237CST
```

备份包含本次覆盖文件、覆盖前 SHA-256 清单和通过 SQLite backup API 生成的
`state.sqlite3` 一致性快照。部署后 22 个文件与本地逐项 SHA-256 一致；本地完整回归为
`594 passed`，SG 正式 `tests/` 回归为 `620 passed, 1 warning`。SQLite
`integrity_check=ok`，三张茶杯柄表已完成前向初始化。systemd unit 校验通过；唯一提示来自腾讯云
`tat_agent` 的旧 `/var/run` 路径，与本项目无关。

运维站任务详情已经实际返回 HTTP 200，并展示独立算法版本、`0/5`、命中/拒绝/等待/错误、P95
延迟和最大序列长度。`quant-intraday-candidate-prepare.timer` 将在 2026-08-31 18:30 SGT
预计算第一批杯体候选，`quant-intraday-momentum-monitor.timer` 将在同日 21:20 SGT 启动盘中
shadow。若 2026-08-31 至 2026-09-04 五个完整 XNYS 交易日全部通过，最早可在
2026-09-05 SGT 看到 `5/5`。达到 `5/5` 只代表具备人工验收条件；
`cup_handle.delivery_enabled` 仍保持 false，不会自动发送消息。

同日使用真实 target 做了周一盘前候选验收：绑定 `US_EQUITY_COVERAGE` 版本
`d4c85d16084143ecbccda73497465a7c` 和 source 2026-08-28，49.062 秒内评估 2,772 只流动
股票，1,343 只通过日线杯体门槛，按评分保留 600 只候选。较高的通过比例本身就是必须继续 shadow
的理由，不能据此直接发送。

SG 已有的 MDB 一分钟文件还完成了一次真实回放：2026-08-10 至 2026-08-11 共两个交易日、110
根完整五分钟 bar，信号数为 0；92 次评估因 `HANDLE_TOO_SHALLOW` 被拒绝，18 次处于
`INSUFFICIENT_COMPLETED_5M_BARS`。没有信号时误报率代理保持 null，不用 0% 冒充有效统计。
报告与哈希为：

```text
outputs/data_audits/cup_handle_replay_mdb_20260810_20260811.json
sha256: 7bd130910239fdaa16d591d4f31d19ed552eb602790f79ba0379d8a4c1c5119a
```

## 11. 2026-08-30 首个交易日前检查

SG 状态检查确认茶杯柄独立台账仍为 `0/5`，这是预期状态：代码在周末部署，首个可计数的完整 XNYS
交易日是 2026-08-31。三张 SQLite 表已存在但当前均为 0 行；旧动量观察没有被复制到
`daily-cup-5m-handle-shadow-v1`。实时指标因此为候选 0、命中 0、拒绝 0、等待 0、错误 0，P95
和最大 bar 数均尚无统计值。

候选准备、盘中监控和运维 watchdog timer 均为 enabled；下一次分别是 2026-08-31 18:30、21:20
SGT 和每分钟。运维 Web 持续运行且 `NRestarts=0`。上一个盘中监控交易日正常退出，峰值内存约
606.2 MiB、无 swap；候选准备在茶杯柄部署前的 2026-08-28 曾被 TERM 停止，该历史运行不能计入
新 shadow，也不代表 2026-08-31 的任务结果。

配置再次确认 `cup_handle.enabled=true`、`delivery_enabled=false`、检测 P95 门槛 250 ms、序列上限
96 根。MDB 回放仍是 0 个信号、误报代理 null；拒绝原因为 `HANDLE_TOO_SHALLOW=92` 和
`INSUFFICIENT_COMPLETED_5M_BARS=18`，不能写成 0% 误报。若 2026-08-31 至 2026-09-04 均满足
完整周期、实际评估、版本合同和延迟门槛，最早在 2026-09-05 SGT 进入人工验收，发送不会自动开启。

## 12. 2026-08-31 首个交易日盘前检查

12:01 SGT 检查时，`daily-cup-5m-handle-shadow-v1` 仍为 `0/5`，通过日期为空，剩余 5 个完整
XNYS 交易日。三张专属表 `cup_handle_cycles`、`cup_handle_evaluations`、
`cup_handle_session_observations` 行数均为 0；这是美股开盘前的正确状态，不得复用旧动量台账，也
不得把 2026-08-24 至 2026-08-28 补记为新算法观察。

2026-08-31 候选快照已提前就绪：source 为 2026-08-28，绑定数据版本
`d4c85d16084143ecbccda73497465a7c`；日线杯体评估 2,772 只、通过 1,343 只，最终选择 600 只进入
分钟 shadow。日线前五拒绝原因为 `RIGHT_RIM_RECOVERY_INCOMPLETE=386`、`RIM_MISMATCH=380`、
`CUP_DEPTH_OUT_OF_RANGE=309`、`CUP_VOLUME_NOT_CONTRACTING=179`、
`BOTTOM_POSITION_INVALID=175`。盘中命中、拒绝、等待、错误仍均为 0，P95 和最大 bar 数尚不存在，
不能写成 0 ms 或 0 根。

候选 timer 与监控 timer 均 enabled，下一次分别为 18:30 和 21:20 SGT。上一个 legacy 监控交易日
退出成功，峰值约 606.2 MiB；候选准备的 534.8 MiB/TERM 是 8 月 28 日茶杯柄部署前人工停止记录，
不计入新 shadow。当前系统可用内存约 1,176 MiB，运维 Web 峰值约 43.1 MiB，watchdog 最近峰值
约 100.9 MiB。发送开关继续为 false。

MDB 两日回放仍为 110 根完整五分钟 bar、0 信号，误报代理为 null；这表示没有可评估信号，不是
0% 误报。首个可计数结果将在 2026-09-01 约 04:05 SGT 完整收盘后产生；若 8 月 31 日至 9 月 4 日
五日全部通过，最早 9 月 5 日仅进入人工验收，仍不会自动开启推送。

## 13. 2026-09-01 首个完整交易日结果

`daily-cup-5m-handle-shadow-v1` 的首个完整交易日 2026-08-31 判定为 FAIL，不能计入观察，当前仍为
`0/5`、剩余 5 个通过日。日线候选准备和盘中服务都正常完成；盘中共记录 70/78 个预期五分钟周期，
周期覆盖率 89.74%，实际评估 2,760 次，其中命中 0、拒绝 1,616、等待 489、错误 655。检测 P95
为 0.568 ms，最大序列 77 根，分别满足 250 ms 和 96 根门槛；失败门槛是
`EXCESSIVE_DETECTOR_ERRORS`，有错误的周期占 59/70，即 84.29%。

前八个结果原因是：`HANDLE_TOO_SHALLOW=1116`、`NON_CONTIGUOUS_5M_SEQUENCE=655`、
`INSUFFICIENT_COMPLETED_5M_BARS=417`、`HANDLE_VOLUME_NOT_CONTRACTING=320`、
`RIM_NOT_BROKEN=180`、`STALE_QUOTE=56`、`HANDLE_NOT_FORMED=10`、
`STALE_COMPLETED_5M_BAR=6`。全部 655 个错误集中在 17 只股票；一只股票首次出现五分钟缺口后，
后续每次评估都继续命中同一个非连续序列错误。

根因位于当前分钟序列合同：聚合器只为实际收到的一分钟 bar 创建五分钟桶，而检测器要求整个有界
窗口内每两个相邻桶严格相差五分钟。现有证据尚不能把缺桶一律解释成“该时段无成交”或“FMP
漏数”，因此不能伪造 OHLCV、前向填充成交量，也不能把错误直接降级为等待。修复应先区分无成交和
供应商缺数；确认缺口后将该证券标记为当日不可评估并保留数据质量失败，避免同一不可逆缺口在后续
周期重复累计，同时新增证券评估覆盖率和缺口比例门槛。修复及回放通过前，不启动新的计数。

现网服务自身健康：候选准备退出码 0；盘中监控运行约 6 小时 44 分，退出码 0，CPU 约 2 分 32 秒，
峰值内存 306.3 MiB、无 swap，FMP 精确请求 2,800 次且失败 0 次。这不是内存、网络或 systemd
中断。发送仍为 `delivery_enabled=false`。MDB 回放仍为 0 个信号、误报代理 null，不能写成 0%
误报。修复后若从 2026-09-01 起五个有效交易日全部通过，最早的五个 XNYS 日期是 9 月 1、2、3、
4、8 日，最早于 2026-09-09 SGT 收盘后进入人工验收。

## 14. 2026-09-01 五分钟数据质量合同 v2

算法合同升级为 `daily-cup-5m-handle-shadow-v2`，参数版本为 `2026-09-01.1`，五日观察从新版本
重新计数。聚合器不再错误地要求每个五分钟桶必须恰好包含 5 根一分钟 bar：只要桶内存在真实来源
bar，就仅使用这些真实成交聚合 OHLCV，并记录 `source_minute_count`、分钟覆盖率和部分桶数量；不会
补造价格，也不会前向填充成交量。

只有两个真实五分钟桶之间完全缺少一个或多个桶时才产生数据缺口。系统使用每分钟批量报价的累计
成交量和最后成交时间进行保守分类：累计成交量不变为 `NO_TRADE_CONFIRMED`；累计成交量增加且最后
成交确实落在空桶内为 `PROVIDER_GAP_CONFIRMED`；证据不能闭合为
`UNRESOLVED_SOURCE_GAP`。三类都不会生成虚假 K 线，相关证券统一标记为当日 `UNEVALUABLE`。

唯一缺口写入 `cup_handle_data_gaps`，主键包含交易日、股票、算法版本和缺口开始时间。同一缺口在
后续周期只增加观察次数，不再反复制造 detector ERROR。每日门禁新增两项：可评估股票覆盖率至少
95%，缺口股票比例不超过 5%；缺口过多时分别产生
`INSUFFICIENT_EVALUABLE_TICKER_COVERAGE` 和 `EXCESSIVE_MINUTE_DATA_GAPS`，因此错误去重不会
降低质量门槛。发送开关继续保持 false。

## 15. 2026-09-02 v2 首个运行日前的上游恢复

`daily-cup-5m-handle-shadow-v2` 当前仍为 `0/5`，不是已经失败 0 次，也不是沿用 v1 的
2026-08-31 失败结果。2026-09-01 没有产生 v2 完整日结，原因是候选准备依赖的全美宽基数据链在
Security Master 身份门禁处 fail closed；缺少真实候选快照时不得启动或补记盘中观察。

上游根因是 FMP 没有提供 `UGRO -> FLZH` 和 `SVII -> NUCL` 的可靠换码历史，同时当前证券资料把
后继代码标为 OTC。系统已根据 SEC 文件增加精确纠正规则，并修复 PIT 交易所口径：历史日期使用
当时生效 ticker 的交易所，而不是用当前后继 ticker 的 OTC 状态覆盖整段历史。该修复不会让 OTC
阶段进入 `US_LIQUID_5M`，也不会猜测缺失的身份关系。

2026-09-01 的正式上游版本已恢复到 Security Master
`b99fc58963604831b9534af9600e75f2`、coverage
`a8c3814e7fd444e9b5f0a12cb047aa7f` 和 PIT
`bbe1288de3684cc3ab6849954cbd9507`。八因子正在从认证 checkpoint 重建；候选准备必须在其自己的
资源窗口内完成，盘中监控仍按 21:20 SGT 启动。只有完整收盘后的 v2 日结同时满足周期覆盖、实际
评估、错误率、P95、序列上限、可评估覆盖率和缺口比例，才可记为第一个通过日。发送继续保持
`delivery_enabled=false`。

18:30 SGT 的 v2 候选准备已按时触发并于 18:57:10 成功完成。快照 session 为 2026-09-02、
source 为 2026-09-01，精确绑定 coverage `a8c3814e7fd444e9b5f0a12cb047aa7f` 和 bars index
SHA-256 `3364b06f795790e2a93182461d70f5739b5af47e6382ede96b3a6f9e296b3b5f`；日线评估
2,848 只、合格 1,314 只、冻结 600 只。候选计算耗时 1,606.824 秒，systemd 峰值 604.5 MiB、
swap 0。原 `MemoryHigh=500M` 触发持续 cgroup reclaim，运行中仅把软高水位临时提高到 620 MiB，
`MemoryMax=700M` 和禁用 swap 未变；完成后已恢复 500 MiB。该候选成功只满足盘前输入门槛，不能
代替盘中评估或收盘后的 session PASS。

## 16. 2026-09-02 盘中前最终交接

20:28 SGT 核查时，`quant-intraday-momentum-monitor.service` 尚未运行，timer 明确等待 21:20
SGT；这属于盘前正常等待。2026-09-02 候选快照已成功冻结 600 只，v2 算法和参数版本已写入快照，
发送配置仍为 false。状态接口继续展示 v1 的 2026-08-31 FAIL，是因为 v2 尚无完整日结，不能用
“等待下一次运行”覆盖最后失败证据。

上游八因子虽然完成计算，publication 因暖机窗口 off-by-one 被严格拒绝。该问题不影响今天已经
冻结的候选和今晚分钟监控；修复后的八因子重建安排在 2026-09-03 04:20 SGT，即盘中服务正常收盘
日结之后。若今晚服务没有实际产生 `daily-cup-5m-handle-shadow-v2` 的 cycles、evaluations、
data_gaps 和 session observation，则 2026-09-02 仍不得计数。

明日验收必须报告 v2 的候选、命中、拒绝、等待、不可评估、错误、唯一缺口分类、可评估覆盖率、
缺口比例、P95 和最大 bar 数。满足全部门槛才记为 1/5；无信号仍不能表述为 0% 误报。

## 17. 2026-09-03 v2 首个完整交易日通过

`daily-cup-5m-handle-shadow-v2` 的 2026-09-02 完整日结为 PASS，因此独立观察正式记为 `1/5`，
还需要 4 个不同且连续运行的完整 XNYS 交易日。候选快照绑定 coverage
`a8c3814e7fd444e9b5f0a12cb047aa7f`、PIT `US_LIQUID_5M` 版本
`bbe1288de3684cc3ab6849954cbd9507`，并保存 membership、eligibility、Security Master 与 manifest
哈希。日线阶段评估 2,848 只、合格 1,314 只、冻结 600 只；盘中记录 71/78 个五分钟周期，
周期覆盖率 91.03%。

盘中共评估 2,840 次：命中 0、拒绝 2,242、等待 548、不可评估 50、错误 0。可评估证券为
56/58，即 96.55%，超过 95% 门槛；缺口证券为 2/58，即 3.45%，低于 5% 门槛；检测 P95 为
0.595 ms，最大序列 77 根，也分别满足 250 ms 和 96 根门槛。前八原因是
`HANDLE_TOO_SHALLOW=1961`、`INSUFFICIENT_COMPLETED_5M_BARS=319`、`STALE_QUOTE=196`、
`HANDLE_VOLUME_NOT_CONTRACTING=156`、`RIM_NOT_BROKEN=125`、
`UNRESOLVED_5M_SOURCE_GAP=50`、`NO_COMPLETED_5M_BARS=32`、
`STALE_COMPLETED_5M_BAR=1`。

`cup_handle_data_gaps` 保存 15 个唯一缺口：UAN 有 5 个 `NO_TRADE_CONFIRMED` 和 9 个
`UNRESOLVED_SOURCE_GAP`，AD 有 1 个 `UNRESOLVED_SOURCE_GAP`；
`PROVIDER_GAP_CONFIRMED=0`。同一缺口后续只更新观察次数，没有重复制造错误，也没有补造 OHLCV。

状态命令与运维适配器此前使用 `previous_xnys_sessions()`，在纽约午夜前会错误排除已经收盘并完成
日结的当前交易日，因此新加坡上午曾显示 `0/5`。现已统一改用“XNYS 收盘加 5 分钟后即视为完整”
的 `completed_xnys_sessions()`；SG 定向测试 2 项通过，CLI 与运维快照均已显示 `1/5`。部署前备份：

```text
/home/projects/quant-backups/cup-shadow-completed-session-20260903T115615CST
```

同一服务中的 legacy 动量日结在 2026-09-02 因 70 个错误周期判定 FAIL，所以运维任务总卡片仍可能
显示 DEGRADED；这不改变茶杯柄 v2 的 PASS 和 `1/5`。两条观察不能混合计数。发送继续保持关闭。
MDB 回放仍为 v1、110 根完整五分钟 bar、0 信号且误报代理为 null，不能解释为 0% 误报。

## 18. 2026-09-04 v2 第二个完整交易日通过

`daily-cup-5m-handle-shadow-v2` 的 2026-09-03 日结为 PASS，独立观察为 `2/5`，还需 3 个通过日。
日线候选快照绑定 coverage `fc81ee7a559b4509a74576791633c3ba`、PIT
`1750d58d3160438093f03a0360f692c9`、Security Master
`5748aeacb53142f4ade15038f0b98ba2` 及 membership、eligibility、manifest 哈希。日线评估 2,848 只，
合格 1,329 只，冻结 600 只。

盘中记录 71/78 个五分钟周期，共评估 2,840 次：命中 1、拒绝 2,356、等待 476、不可评估 7、
错误 0。可评估覆盖率为 53/54，即 98.15%；缺口证券比例为 1/54，即 1.85%；P95 为
0.583 ms，最大序列 77 根。两个唯一缺口都来自 CQP，分类均为
`UNRESOLVED_SOURCE_GAP`；没有伪造或前向填充 OHLCV。前八拒绝原因为
`HANDLE_TOO_SHALLOW=2075`、`INSUFFICIENT_COMPLETED_5M_BARS=349`、
`HANDLE_VOLUME_NOT_CONTRACTING=148`、`RIM_NOT_BROKEN=132`、`STALE_QUOTE=123`、
`UNRESOLVED_5M_SOURCE_GAP=7`、`NO_COMPLETED_5M_BARS=3`、
`BREAKOUT_ALREADY_OCCURRED=1`。

CTNM 在 15:10 ET 的完整五分钟 bar 形成首个 v2 shadow 命中，信号和 outbox 均保存为
`SHADOW`，没有向 Discord 发送。现有 MDB 回放仍是 v1、0 信号、误报代理 null；CTNM 尚未形成
已完成的后续结果，因此也不能声称误报率为 0%。

18:30 SGT 候选服务本次在 1 小时后超时，峰值 558.1 MiB、CPU 41 分 13 秒、swap 0。它受
`MemoryHigh=500M` 持续回收影响，未在盘前窗口保存快照；盘中服务随后以更高内存额度重建快照，
导致只覆盖 71/78 个五分钟周期。虽然本日仍满足所有严格门槛并可计数，但候选准备 SLA 已失败，
下一个交易日前应优化构建内存或调整受控资源窗口，不能依赖盘中回退。

已把候选服务的持久 `MemoryHigh` 从 500 MiB 提高到经过 2026-09-02 生产验证的 620 MiB；
`MemoryMax=700M`、单核、禁用 swap 和 1 小时超时保持不变。该调整只减少软高水位回收，不放宽
算法或数据门槛；2026-09-04 18:30 SGT 的下一次候选运行用于验证 SLA 是否恢复。

## 19. 2026-09-05 上游阻断与 v2 观察进度

`daily-cup-5m-handle-shadow-v2` 当前仍只有 2026-09-02、2026-09-03 两个 PASS，进度为 `2/5`，
剩余 3 个通过日。2026-09-04 不是“检测后失败”，而是没有形成候选快照、盘中评估或完整日结，
因此不得计数，也不得把全零指标解释为零信号。直接原因是正式 `US_EQUITY_COVERAGE` 停在
2026-09-02；候选服务于 18:30 SGT fail closed，盘中服务从 21:20 起重试四次后触发 start limit，
错误均为“coverage 过期，期望 2026-09-03”。

共享上游停滞的根因位于 Security Master。FMP 在两份独立冻结源中都把 FLZH 从活跃改为不活跃，
同时继续提供 `UGRO -> FLZH` 换码、完全一致的 CUSIP/ISIN，以及 FLZH 于 2026-08-26 从 OTC
退市的记录。原纠正规则只允许换码后的活跃基线，因此正确地拒绝了未审阅的生命周期变化。修复没有
允许任意 `is_active` 漂移，而是要求目标日在 2026-08-26 之后时同时精确匹配 ticker、退市日、
OTC 和公司名，缺少任一冻结证据仍报错。

同一冻结源两次候选构建均 PASS，五张 Parquet 的 SHA-256 逐表完全一致；第二份独立冻结源也 PASS，
活跃普通股数量同为 5,350。FLZH 审计明确保存 `inactive_on_or_after=2026-08-26`、
`delisted_exchange=OTC`、供应商公司名和 `provider_status=INACTIVE`。SG 定向测试 96 项通过，完整
回归 `655 passed`。部署前备份位于：

```text
/home/projects/quant-backups/flzh-lifecycle-20260905T004551CST
```

修复后的日更已于 2026-09-05 11:31 SGT 运行并在 11:40:43 成功完成。正式 Security Master 为
`3ea8a269a67a4797be8bfcbfb2d7ae78`，coverage 已推进到 2026-09-04 版本
`2f31ea50b7484e038ca977b252679f43`，PIT 为 `25cec81b68304b3a85e7829b31313567`，全历史日线
覆盖门禁通过。八因子后续链正在从认证 checkpoint 重建，核查时为 483/648；这证明共享上游已经恢复，
但不会补造 2026-09-04 的茶杯柄观察。候选和盘中 timer、watchdog、运维 Web 均为 enabled/active，
发送继续保持 `delivery_enabled=false`。

最近两个有效 v2 日的汇总不变：2026-09-02 为 2,840 次评估、0 命中、2,242 拒绝、548 等待、
50 不可评估、0 错误；2026-09-03 为 2,840 次评估、1 命中、2,356 拒绝、476 等待、7 不可评估、
0 错误。唯一缺口分类合计为 `NO_TRADE_CONFIRMED=5`、`UNRESOLVED_SOURCE_GAP=12`、
`PROVIDER_GAP_CONFIRMED=0`。MDB 回放仍为 v1、0 信号且误报代理为 null；CTNM 后续结果尚未成熟，
不能表述为 0% 误报。

## 20. 2026-09-07 v3 合同生效与观察重新开始

现网部署标记 `7afed9ca6593ded424a3b7639028a1d8ba24e636` 已包含
`daily-cup-5m-handle-shadow-v3`。v3 在杯柄基准区、柄部或突破 bar 的成交量不为正时明确返回
`INSUFFICIENT_VOLUME_EVIDENCE`，不再把缺失成交量换算成无穷比例继续参与判定。这会改变同一输入的
拒绝原因和潜在信号结果，因此属于算法合同变化，v2 的两个 PASS 只能保留为历史证据，不能计入 v3。

2026-09-07 11:47 SGT 的现网状态确认 v3 为 `0/5`，五个候选完成交易日均缺 v3 记录；SQLite 中
仍只有 v1 的一个 FAIL 和 v2 的 2026-09-02、2026-09-03 两个 PASS，v3 的 cycles、evaluations、
session observations 和 data gaps 均为 0。2026-09-07 是 XNYS 休市日，不产生或补记观察；若
2026-09-08、09、10、11、14 五个连续完整交易日全部通过，最早在 2026-09-14 收盘日结后达到
`5/5`。发送继续保持 `delivery_enabled=false`，达到门槛后也只进入人工验收。

候选、盘中、watchdog timer 与运维 Web 当前均为 enabled/active。候选和盘中 service 仍保留
2026-09-04 上游阻断的最后失败结果，这是不可覆盖的事故证据，不代表 timer 已停用。MDB 回放仍是
v1 的 110 根完成五分钟 bar、0 信号、`false_positive_rate_proxy=null`，不能解释为 0% 误报。

## 21. 2026-09-08 休市日候选误报修复与首日待运行

2026-09-07 是 XNYS 休市日。盘中 monitor 已按 `not_a_trading_session` 正常退出，但 18:30 SGT
的候选预计算仍直接调用 `expected_source_session()`，把休市日抛出的 `ValueError` 作为 systemd
失败。这是调度适配缺陷，不是行情、PIT 或茶杯柄算法失败；该日没有候选快照，也没有 v3 cycle、
evaluation、observation 或 data-gap 记录，因此没有污染或补记 shadow 台账。

候选入口现与盘中 monitor 使用同一 XNYS 交易日判断。休市日返回
`phase=not_a_trading_session`、候选数 0 和 exit code 0，不调用候选构建器，也不写空快照。SG 定向
回归为 `26 passed`，部署前备份位于：

```text
/home/projects/quant-backups/cup-holiday-skip-20260908T1218CST
```

运维事件已转为 RESOLVED，候选任务恢复为 SCHEDULED。2026-09-08 11:31 SGT 上游检查确认正式
coverage `562967c01bb54e2ab39454804cc4ac73` 与 PIT
`c1329fcd14dd4521911976b21fa6be22` 均绑定 2026-09-04，符合劳动节后 2026-09-08 交易日的前一
XNYS 数据日。首个 v3 完整日仍需等待 2026-09-08 的 18:30 候选、21:20 盘中监控及收盘日结；
当前 `0/5` 合理，发送保持关闭。

## 22. 2026-09-09 v3 首个交易日盘中核验

截至 2026-09-09 00:26 SGT，即 2026-09-08 美股盘中，v3 首个可观察交易日仍在运行，尚未生成
`cup_handle_session_observations`，因此正式进度仍为 `0/5`，不能提前记为通过或失败。盘前候选于
18:30 SGT 启动、18:31:35 成功退出，耗时 50.933 秒，systemd 峰值 588.3 MiB、swap 0；日线共
评估 2,846 只、合格 1,300 只并冻结 600 只盘中候选。

候选合同已独立重新校验通过：coverage `562967c01bb54e2ab39454804cc4ac73`、bars SHA-256
`350bc406683b95cd58c4d15efdf0397308701d5ce342bf2ec501c91876b97cee`、PIT
`c1329fcd14dd4521911976b21fa6be22`，membership、eligibility、PIT manifest 和 Security Master
manifest 哈希均与正式发布一致，source data date 为 2026-09-04。

00:26 SGT 的只读盘中快照为 32/78 个五分钟周期、1,240 次评估、3 次 MATCH、859 次 REJECTED、
364 次 NOT_READY、14 次 UNEVALUABLE、0 次 ERROR；53 只实际评估股票中 2 只有数据缺口，暂算
可评估覆盖率 96.23%、缺口比例 3.77%。共有 5 个唯一缺口事件，全部为
`UNRESOLVED_SOURCE_GAP`，`NO_TRADE_CONFIRMED=0`、`PROVIDER_GAP_CONFIRMED=0`。当前检测 P95
为 0.324636 ms，最大序列 34 根。

前八个非 MATCH 原因依次为 `HANDLE_TOO_SHALLOW=717`、
`INSUFFICIENT_COMPLETED_5M_BARS=311`、`RIM_NOT_BROKEN=83`、
`HANDLE_VOLUME_NOT_CONTRACTING=58`、`STALE_QUOTE=48`、
`UNRESOLVED_5M_SOURCE_GAP=14`、`HANDLE_NOT_FORMED=4`、
`INSUFFICIENT_VOLUME_EVIDENCE=1`。ABG 的 breakout volume 为 0，v3 明确拒绝且没有生成成交量比例；
全量扫描未发现非正成交量继续参与比例或非有限比值。VTS 和 NKTR 仅写入 SHADOW 信号，未投递。
MDB 回放仍为 v1、110 根 bar、0 信号且误报代理为 null；生产信号也尚无完整后续窗口，均不能称为
0% 误报。发送保持关闭，需等待 2026-09-08 收盘加 5 分钟后的正式日结。

## 23. 2026-09-09 v3 首个完整日结未通过

2026-09-08 收盘后的正式日结为 FAIL，不能计入 v3 的五交易日观察。当前通过日期为空，进度为
`0/5`，剩余 5 个通过日。失败不是服务、内存或 FMP 请求整体中断：候选服务和盘中服务均 exit 0，
盘中服务完整运行到 16:05 ET；准确失败项只有
`INSUFFICIENT_EVALUABLE_TICKER_COVERAGE` 与 `EXCESSIVE_MINUTE_DATA_GAPS`。

本日冻结 600 只盘中候选，实际进入茶杯柄评估的唯一股票为 56 只，其中 53 只可评估、3 只存在
不可恢复分钟缺口。可评估覆盖率为 53/56，即 94.6429%，低于 95% 门槛；缺口股票比例为 3/56，
即 5.3571%，高于 5% 上限。两个门槛都只差一只股票，但仍必须 fail closed，不能四舍五入为通过。
70/78 个五分钟周期的覆盖率为 89.7436%，高于 85% 门槛；错误数为 0、P95 为 0.563372 ms、
最大序列 76 根，均通过各自门槛。

最终共有 2,760 次评估：3 次 MATCH、2,183 次 REJECTED、483 次 NOT_READY、91 次
UNEVALUABLE、0 次 ERROR。前八个非 MATCH 原因为 `HANDLE_TOO_SHALLOW=1843`、
`INSUFFICIENT_COMPLETED_5M_BARS=311`、`HANDLE_VOLUME_NOT_CONTRACTING=170`、
`RIM_NOT_BROKEN=166`、`STALE_QUOTE=104`、`UNRESOLVED_5M_SOURCE_GAP=91`、
`HANDLE_NOT_FORMED=67`、`INSUFFICIENT_VOLUME_EVIDENCE=4`。

13 个唯一缺口事件涉及 OPY、TEN、WBI 三只股票：`UNRESOLVED_SOURCE_GAP=12`、
`NO_TRADE_CONFIRMED=1`、`PROVIDER_GAP_CONFIRMED=0`。这是旧分类器写入的历史结果；第 24 节复核发现
OPY 的“确认无成交”证据不足，不应把这个标签当作已证实的事实。缺口前后累计成交量增加也不能
定位成交发生在缺失的五分钟内，因为报价观测区间跨越桶边界。历史记录保留，但其证据局限必须
同时说明。重复观察只增加 `observation_count`，没有复制成新的唯一事件。

ABG 的四次 `INSUFFICIENT_VOLUME_EVIDENCE` 均由 breakout volume 为 0 触发。payload 只保留原始
baseline、handle 和 breakout volume，不生成 `handle_volume_ratio` 或
`breakout_volume_ratio`；全部 v3 cycle 的 `data_contract_complete=1`。VTS、NKTR 的两个唯一信号
仍只写入 SHADOW outbox，没有投递。MDB 回放仍是 v1 的 0 信号样本，误报代理为 null；现有生产
shadow 信号也没有成熟后续窗口，不能声称误报率为 0%。

候选服务峰值 588.3 MiB、盘中服务峰值 438.1 MiB、swap 均为 0；watchdog 持续成功，运维 Web
保持 active。运维任务卡已固定显示 2026-09-08 `DEGRADED` 和上述两个失败原因，开放事故指纹绑定
`daily-cup-5m-handle-shadow-v3`，没有被下一次 `SCHEDULED` 覆盖。发送继续保持
`delivery_enabled=false`；2026-09-09 必须作为新的独立交易日重新满足全部门槛。

## 24. 2026-09-09 缺口复查及证据分类修复

通过 SG 对 OPY、TEN、WBI 各查询 FMP 原生 1min 和 5min 历史接口，两轮复查均确认 13 段区间
全部没有返回行。最终独立报告为：

```text
/home/projects/quant/outputs/data_audits/cup_handle_gaps/2026-09-08_b80e243c01104313bdd05ebeba13ba4b.json
```

报告保留六份规范化响应和各自 SHA-256。1min 总行数分别为 OPY 208、TEN 224、WBI 260；5min
分别为 71、75、75。故本次直接原因是已取到的 FMP 数据无法提供连续序列，无法靠重新聚合或切换
同一供应商的 5min 接口补齐。仍不能仅由空行断言真实无成交或供应商漏报；收盘后的响应也不能
倒推盘中可用性。本次查询前后四张 cup_handle 生产表的内容哈希完全一致，未改写历史观察。

发现并修复证据分类问题：原代码用两次观测的累计成交量相等直接标为 NO_TRADE_CONFIRMED，但
OPY 14:25-14:30 ET 后侧报价的 provider timestamp 仍为 14:23:25，重复陈旧报价没有提供数据完整性
保证。累计量增加也可能发生在左边界之前或右边界之后。新证据口径 `quote-window-evidence-v2`
只在两个有效 provider timestamp 都落在缺口内部且累计量增加时确认有成交缺失；累计量不变、
负数、非有限数、回退或只有跨边界增量时保留 unresolved，并写明确 reason。真实无成交以后需要
更强的供应商完整性证据才能确认，不能从重复 last-trade quote 推断。

这是缺口证据标签修复；所有缺口仍为 UNEVALUABLE，OHLCV、交易信号判定和 shadow 门槛均不变。
检测算法仍为 v3，证据子版本随每个新缺口写入；旧 v3 FAIL 原样保留。日更不会通过此修复自动
变成 PASS，历史 0/5 也不变。本地与 SG 定向回归均为 51 passed，部署前备份为
`/home/projects/quant-backups/cup-gap-evidence-20260909T1230CST`。盘中服务当前 inactive，下一次 timer
启动读取新代码，不需提前启动盘中任务。

可复用核查命令（只读取现有台账，独立生成新报告）：

```bash
.venv/bin/python scripts/diagnose_cup_handle_data_gaps.py \
  --session 2026-09-08 --env-file /etc/quant/intraday-momentum-monitor.env
```

请求失败或空响应为 INCONCLUSIVE_REQUEST；重新查询出现行只记 ROWS_PRESENT_ON_REQUERY，不修改
live 结果。要消除持续的数据覆盖问题，需要供应商补齐证据或接入经过合同验证的第二分钟数据源。
不能事后剔除 OPY/TEN/WBI、扩大分母或降低 95%/5% 门槛来获得通过。盘前按历史分钟质量重新定义
候选池是另一项策略输入变更，需要单独定义和重新验收，不能混作此次数据修复。

## 25. 2026-09-09 夜间复核：上游 RML 历史数据阻断

现场时间为 2026-09-09 23:17 SGT，XNYS 当日尚未收盘。只读检查 status CLI、systemd、journal、
四张 cup_handle 表及候选快照；未修改历史记录、验收门槛或发送配置。

- 当前算法为 `daily-cup-5m-handle-shadow-v3`。最近五个完整 XNYS 交易日为 09-01、09-02、
  09-03、09-04、09-08；前四日没有 v3 证据，09-08 FAIL。通过日期为空，0/5，仍需五个连续
  合格交易日。09-07 休市；09-09 尚未完整且没有实际评估，不计数，不复用 v1/v2。
- 09-08 日线筛选 2,846 只、合格 1,300 只、冻结候选 600 只，盘中实际评估 56 只。
  70/78 周期（89.74%）通过 85% 门槛；2,760 次评估中命中 3、拒绝 2,183、等待 483、
  不可评估 91、错误 0。可评估 53/56（94.6429%）低于 95%，缺口 3/56（5.3571%）高于 5%。
  P95 为 0.5634 ms，最大 76 根，错误周期比例 0，后三项通过，但不能抵消覆盖率失败。
- 唯一缺口事件 13 条，涉及 OPY/TEN/WBI。历史分类为 UNRESOLVED_SOURCE_GAP 12、
  NO_TRADE_CONFIRMED 1、PROVIDER_GAP_CONFIRMED 0；上一节已说明原 NO_TRADE 标签证据不足，
  不能把它作为真实无成交事实。修复后的 quote-window-evidence-v2 已部署，但今天没有新评估
  可用于生产验证，旧记录未重新分类或补记。
- 前八原因：HANDLE_TOO_SHALLOW 1843、INSUFFICIENT_COMPLETED_5M_BARS 311、
  HANDLE_VOLUME_NOT_CONTRACTING 170、RIM_NOT_BROKEN 166、STALE_QUOTE 104、
  UNRESOLVED_5M_SOURCE_GAP 91、HANDLE_NOT_FORMED 67、INSUFFICIENT_VOLUME_EVIDENCE 4。
  四条成交量证据不足均为 ABG，breakout_volume=0、ratios={}；扫描 v3 评估未发现非正成交量
  被写成有效比例。不能把这些拒绝状态算成有效成交量确认。

09-08 候选绑定 coverage `562967c01bb54e2ab39454804cc4ac73`、PIT
`c1329fcd14dd4521911976b21fa6be22`、Security Master `3ea8a269a67a4797be8bfcbfb2d7ae78`，
输入目标日为 09-04。本次调用 `validate_breakout_daily_data_contract` 重新通过不可变合同及
PIT 哈希核验；旧版本完整不等于可以拿来充当 09-09 所需的 09-08 行情。

当天阻断链如下，时间均为 SGT：

1. 11:31 宽基任务启动，11:37 失败；12:07 重试，12:08 再失败，12:38 达到启动频率限制。
   新主表发布成功，但 RML 在 2019-01-02 至 2026-09-04 的历史区间没有取得有效行情，
   identity delta 审计失败，coverage/PIT 后续没有更新。
2. 18:30 候选预计算失败；21:20 盘中进程启动，开盘后及后续重试均报
   `target 2026-09-04 is stale; expected 2026-09-08`，21:38 达到启动频率限制。
3. 09-09 候选快照及四张 cup_handle 表均无当日记录。CLI 中 09:29:45 ET 的
   waiting_for_open 是最后一次旧心跳，不代表服务此刻运行中。

上游初次失败峰值 595.1 MiB、重试 229.6 MiB，均无 swap；盘中失败日志记录峰值 84 MiB，
不是资源耗尽。候选/盘中 timer 仍 enabled，下一次计划为 09-10 18:30/21:20 SGT。
watchdog 正常完成（约 111.5 MiB），运维站存活、快照新鲜且保留 09-08 FAIL 与 v3 版本；
同时存在当日服务失败和心跳中断 OPEN 事件。

MDB 回放仍为旧 v1 的 110 根、零信号、误报代理 null，不能作为 v3 或 0% 误报证明。
09-08 的三次命中对应 VTS/NKTR 两条 SHADOW 信号，结果观察窗口尚无完整标签，不能评估误报率。
发送继续为 false。本次未重启或重跑被同一上游门槛阻断的任务。恢复前必须先核实 RML 的真实
美国 ADR 上市/历史身份和供应商行情，再做有证据的定点纠正或经批准的历史排除；严禁把缺失行情
当空成功。宽基正式发布并核验后才能生成新的绑定候选，错过的盘中周期不得事后补成通过日。

## 26. 2026-09-10 RML 上游修复的边界

项目负责人已授权执行第 25 节恢复建议。发行人确认 Nasdaq RML ADS 于 09-09 开始交易，
存托凭证目录确认原 OTC ADR RSMIY 的同一 CUSIP/ISIN。FMP 对截至 09-08 的 RML 历史查询仅
返回一条 09-07 休市日、volume=0 的记录，RSMIY/RLMLF 历史为空，不能充当真实美国交易历史。
来源、响应与哈希保存在 `outputs/data_audits/rml_identity/20260909T155422Z/`。

修复增加精确 RML 身份的 PROSPECTIVE_ONLY 09-09 研究起始边界，不改原上市日期，不把普通股
或 OTC 历史拼接为 Nasdaq 历史；原排除台账保持原样。代码允许提前登记未来生效的 prospective
边界，但 coverage 在生效前必须排除该证券。09-08 两次同冻结源候选五张表内容/哈希完全一致，
quality PASS、身份覆盖 100%。本地完整测试 1,048 passed、6 skipped，SG 相关测试 57 passed。

本次修复的是“上游日线无法发布”，不等于修复 09-08 的 OPY/TEN/WBI 分钟缺口，也不证明其误报率。
daily-cup-5m-handle-shadow-v3、95%/5% 门槛及 delivery_enabled=false 均未改变。09-09 已漏跑
周期不能补记成实际评估，后续恢复后的部分交易日只能记录真实覆盖率。具体恢复版本与阶段结果见
SG 运维文档后续记录，不应仅因主表发布就把整条监控链标为成功。

本轮最终状态：RML 主表已正式发布为 `65fc7779e7524461810d10633db63dd4`，历史补齐零别名失败。
但后续发现 CALC 的当前 full 端点与 eod-bulk 成交量精度不一致（如 20724 vs 20723.6），
重叠窗口认证失败。coverage 仍停在 09-04，PIT 和候选未运行，盘中没有强行重启。已保存逐日差异
与原始 full 响应；详见宽基实施第 29 节及 SG 运维第 44 节。RML 修复不能被表述为“茶杯柄已恢复”，
0/5、发送关闭和缺跑事实保持不变。

## 27. 2026-09-10 共享行情的整票历史恢复

上游已从单只 CALC 排查扩展到冻结全池审计：5,437 只通过，321 只发生不能用单一比例认证的
历史/bulk 差异。已部署显式完整历史恢复路径，每票重取所有获准历史 ticker 区间，保留接口返回帧、
哈希和失败台账；绝不只覆盖近期，也不修改 1e-5 容差。
SG `quant-full-history-repair.service` 正在执行 2026-09-08 的受控恢复。详细合同、测试与恢复
报告路径见宽基实施第 30 节、SG 运维第 45 节。某票验证成功不等于全池已发布，更不等于
茶杯柄候选和完整交易日验收通过。只有全部修复、正式 coverage/PIT 和候选合同均通过后才可
恢复实际评估；v3 通过日不补记，`delivery_enabled=false` 不变。

本轮已结束，并非仍在运行：321 只全部检查，318 只通过，3 只拒绝，未发布 coverage。
STRR 是供应商查询换码窗口缺 9 日；ATCX/BGLC 共 4 条已知隔离坏行尚未接入新恢复分支。
不允许通过忽略这三票、篡改真实 ticker 日期或修改坏价格恢复候选。旧 coverage/PIT 仍停留
09-04，后续候选和盘中评估未启动；本次不产生任何新的 v3 合格观察日。
详见宽基实施 30.1 和 SG 运维 45 的终态记录；恢复机制代码测试通过不代表生产数据已恢复。

## 28. 2026-09-10 11:45 SGT 定期核验

SSH status 与 SQLite 四表一致：v3 最近五个完整 XNYS 交易日为 09-02、09-03、09-04、
09-08、09-09。通过日期为空，0/5，剩余五个完整合格交易日；09-02/03 只有 v2，09-04/09
无 v3 运行，09-08 FAIL，不能复用旧版本或补记。delivery 仍关闭，未启动重算或改配置。

最近有评估的 09-08：日线筛选 2,846、候选 1,300，实际盘中评估 56 只、2,760 次；
命中 3、拒绝 2,183、等待 483、不可评估 91、错误 0。70/78 周期，合同完整标记均为 1；
可评估 53/56=94.64% 低于 95%，缺口股 3/56=5.36% 高于 5%，因此失败。
P95 0.5634 ms，最大 76 根。唯一缺口 13 个：NO_TRADE_CONFIRMED 1、
UNRESOLVED_SOURCE_GAP 12、PROVIDER_GAP_CONFIRMED 0；未决缺口 OPY 6、TEN 3、WBI 3。
前八原因：柄浅 1843、分钟数据不足 311、成交量未收缩 170、未破杯沿 166、报价过期 104、
源缺口不可评估 91、柄未形成 67、成交量证据不足 4。ABG 四次 breakout_volume=0 均 REJECTED，
signal=null，未计算有效成交量比例。MDB 仍是 v1、110 根、零信号、误报代理 null，不能证明
v3 或 0% 误报；本轮没有新增后续误报标签。

新变化：今日 11:32 至 11:43:55，target=09-09 主表发布成功，但行情重叠认证 324 只失败，
后续 PIT/候选未推进。不能把凌晨 target=09-08 的 318/321 恢复缓存直接冒充本日通过结果。
详情见 SG 运维 46、宽基实施 30.2。候选/盘中 service 保留昨日失败，timer 下次为今日
18:30/21:20 SGT；上游未恢复前仍有缺跑风险。未盲目重启。运维快照保留 09-08 FAIL、
v3 和 09-09 缺跑，没有被 SCHEDULED 遮盖。

## 29. 2026-09-11 11:48 SGT 巡检：新增主表阻断

SSH status、四张 cup_handle SQLite 表和 journal 核对一致。v3 最近五个完整交易日为
09-03、09-04、09-08、09-09、09-10；通过日期为空，0/5，剩余五个完整合格交易日。
09-03 只有 v2，09-04/09/10 无 v3 评估；09-08 FAIL。09-10 日线候选在 18:30:31
报 coverage=09-04 stale、expected=09-09；盘中 21:30 后同错，21:38:24 达到重试限制。
不存在可补记的昨日候选/周期；候选数不可用，实际评估、命中、拒绝、等待、不可评估和错误记录均为零，
不是“零错误且通过”。旧心跳 waiting_for_open 不表示当前服务健康。

最近有运行的 09-08 统计不变：筛选 2846、日线候选 1300、实际评估 56 只/2760 次，
命中3、拒绝2183、等待483、不可评估91、错误0；70/78周期、合同完整标记均1。
覆盖率94.64%低于95%，缺口股5.36%高于5%；P95=0.5634ms、最大76根。
唯一缺口13（NO_TRADE_CONFIRMED=1、PROVIDER_GAP_CONFIRMED=0、UNRESOLVED_SOURCE_GAP=12）。
前八拒绝/等待原因和数量见第28节，本轮无新增；ABG四次零突破成交量继续确认被拒绝且signal=null，
没有有效成交量比例。MDB回放仍为v1、110根、零信号、误报代理null，不能作为v3或0%误报证据。

今日新增阻断更早发生在证券主表，HYMC/HYMCZ、BDX/BDXA 的同身份历史区间重叠，未发布新主表，
没有运行本日coverage/PIT。具体冻结证据及修复建议见宽基实施30.3、SG运维47。
候选/盘中服务failed，下一次timer为今日18:30/21:20；watchdog正常，运维站保留09-08 FAIL、
v3和09-10缺跑，没有被SCHEDULED遮盖。未重启、未降低门槛、未开启发送或补记通过。
