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
