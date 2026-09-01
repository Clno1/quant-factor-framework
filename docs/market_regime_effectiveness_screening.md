# 大盘顶底信号：第一轮有效性筛选报告

> 状态：Stage B / G1-G6 已完成
> 审计日期：2026-08-02
> 当前基准运行：`stage_b_effectiveness_audited_20260802_v6`
> 筛选算法版本：`0.1.3`
> 重要结论：1 个事前登记候选阶段性通过，0 个信号获准进入生产

## 一、这一步完成了什么

Stage A 已经生成 1990 年以来的标签和 139 个市场核心特征。本阶段负责回答：

> 单独看某一个指标，它对顶部或底部标签是否有可重复的样本外信息？

系统对以下组合逐一保留结果：

```text
139 个特征
× 2 个方向（top / bottom）
× 3 个期限（5 / 20 / 60 日）
= 834 次检验
```

其中：

- 28 个基础经济假设在首次完整筛选前冻结；
- 展开期限后共有 62 个事前登记检验；
- 其余 772 个组合属于探索扫描；
- 探索扫描即使数值通过，也不能直接进入正式短名单。

候选假设登记在：

```text
configs/market_regime_screening_candidates.yaml
```

## 二、时间切分和防泄漏

### 2.1 封存集

`2022-01-01` 起的数据被定义为最终封存集。本轮没有计算封存集指标表现，也没有用它
选择方向、阈值或候选。

因为最长标签会读取未来 60 个交易日，所以开发集不能直接用到 2021 年最后一天。
系统从封存集边界再向前隔离 60 个交易日，最终开发样本截止：

```text
2021-10-06
```

这样 2021-10-06 的 60 日标签也不会读到 2022 年。

回归测试会把 2022 年后的特征和标签全部篡改，再要求 scorecard 和 fold 结果与原始
运行完全相同。

### 2.2 Walk-forward

验证窗口从 1996 年起，每两年一个窗口：

```text
过去至少 5 年训练
-> 留出 60 个交易日 embargo
-> 验证未来 2 年
-> 扩大训练集
-> 进入下一窗口
```

不使用随机 KFold。每个 fold 的以下数据只从该 fold 的训练期计算：

- 1% / 99% 截尾边界；
- 均值和标准差；
- 单变量 ridge logistic 参数；
- 指标预警方向；
- 90% 报警分位阈值；
- event-study 五分位边界；
- 训练期事件基准率。

探索指标的方向只允许在第一个合格训练 fold 中确定一次，随后冻结。方向一致性使用
验证期 oriented AUC，而不是训练期回归系数。

### 2.3 重叠事件

连续多天的同一轮下跌不能被当成多个独立危机。系统采用两层处理：

1. 显著性检验中，同一 horizon 内相连的正标签合并为一个 episode，负样本按
   horizon 间隔抽取；
2. 样本外 Brier 改善使用 60 个候选观测行的 moving-block bootstrap，保留连续
   压力状态的相关性。

这里的 60 行是“标签有效状态中的连续候选观测”，不是固定 60 个日历交易日。

## 三、统计量和门槛

### 3.1 样本外概率

每个 fold 使用一个可审计的单变量概率模型：

```text
z_t = (winsor(x_t) - train_mean) / train_std

p_t = sigmoid(intercept + coefficient × z_t)
```

训练期事件发生率是基准概率。核心比较是：

```text
Brier Skill = 1 - Brier(model) / Brier(train_prevalence_baseline)
```

同时记录：

- PR-AUC 和事件基准率；
- ROC-AUC；
- calibration error；
- 事件级 precision / recall；
- 每年错误报警 episode；
- 首次触及屏障的中位交易日；
- 训练分位在验证期的单调性；
- MFE、MAE 和未来收益。

### 3.2 多重检验

每个 `side × horizon` 构成一个 FDR family。每个 family 包含全部 139 个测试，
使用 Benjamini-Hochberg 校正：

```text
top_5d:     139
top_20d:    139
top_60d:    139
bottom_5d:  139
bottom_20d: 139
bottom_60d: 139
```

事前登记候选使用登记方向的单侧 rank test；探索候选使用双侧检验。

### 3.3 当前 G1-G6

| Gate | 当前实现 |
| --- | --- |
| G1 数据 | 标签有效期内特征覆盖率至少 95% |
| G2 样本 | 至少 30 个去重事件、3 个五年 era、3 个有正负样本的 WF folds |
| G3 方向 | 预期方向在至少 75% 验证 folds 中成立 |
| G4 概率 | OOS Brier Skill > 0，且 PR-AUC > OOS 基准率 |
| G5 稳健 | 95% block-bootstrap 改善下界 > 0，leave-one-era-out 最差改善 > 0 |
| G6 多测 | BH FDR `q <= 0.10` |

`STAGE_1_PASS` 只表示同时通过 G1-G6，不等于生产有效。

## 四、真实运行结果

### 4.1 总体

```text
Stage A 输入: stage_a_audited_20260801_v6
Stage B 运行: stage_b_effectiveness_audited_20260802_v6
全部测试: 834
事前登记测试: 62
探索测试: 772
STAGE_1_PASS: 1
STAGE_1_FAIL: 42
INSUFFICIENT_EVIDENCE: 19
EXPLORATORY_ONLY: 772
生产批准: 0
```

62 个事前候选的主要失败数量为：

```text
G1 数据失败: 0
G2 样本深度失败: 19
G3 验证期方向失败: 29
G4 样本外概率失败: 51
G5 依赖稳健性失败: 61
G6 多重检验失败: 38
```

最难通过的是 G5。许多指标在全样本 rank test 中很显著，但样本外 Brier 改善的
block-bootstrap 下界仍小于零。这正是不能只看一张历史相关性图的原因。

### 4.2 唯一阶段性通过者

```text
candidate_id: bottom_spx_return_5d__5d
特征: SPX 过去 5 日收益
目标: 回撤状态下未来 5 日 Bottom first-touch
登记方向: 过去 5 日收益越低，未来反转概率越高
```

样本外结果：

| 指标 | 结果 |
| --- | ---: |
| Walk-forward folds | 3 |
| OOS 行数 | 753 |
| OOS 正例率 | 20.19% |
| PR-AUC | 28.92% |
| PR-AUC lift | 8.73 个百分点 |
| ROC-AUC | 0.6383 |
| Brier Skill | 2.93% |
| Brier 改善 95% block-bootstrap 下界 | 0.000090 |
| 验证期方向一致 | 3 / 3 |
| BH FDR q | `4.46e-15` |
| 去重开发事件 | 94 |
| 五年 era | 6 |
| 五分位单调性 | 0.90 |
| 最低风险箱事件率 | 11.11% |
| 最高风险箱事件率 | 30.34% |
| 事件级 precision | 60.00% |
| 事件级 recall | 28.30% |
| 错误报警 episode / 年 | 0.54 |
| 报警正例首次触及屏障中位日 | 第 2 个交易日 |

三个样本外窗口分别覆盖：

```text
2002-2003: validation AUC 0.6804, Brier Skill 5.08%
2008-2009: validation AUC 0.6304, Brier Skill 2.02%
2020:      validation AUC 0.6374, Brier Skill -0.16%
```

因此它只能称为“阶段性候选”，不能称为已经可靠：

- 只有三个压力期 fold；
- 2020 fold 的 Brier Skill 略为负；
- bootstrap 下界虽然大于零，但距离零很近；
- 它可能只是标签前置条件和短期超卖的自然延伸；
- 尚未证明它在其他底部状态变量之外提供增量信息；
- 尚未用 next-open 成交、滑点和费用检验经济价值；
- 2022 年后的封存集仍未打开。

### 4.3 顶部信号

本轮没有任何事前登记顶部指标通过 G1-G6。

一些指标在样本内 rank test 显著，但 PR-AUC、Brier Skill、验证期方向或 block
bootstrap 没有同时通过。当前不能在页面上画“经验证顶部信号”。

### 4.4 探索发现

8 个未登记组合数值上通过 G1-G6，包括若干 NDX 短期超卖变量、`VIX level ->
top_5d` 和 `SPY drawdown -> top_5d`。它们仍标记为 `EXPLORATORY_ONLY`，原因是：

- 方向由第一个训练 fold 发现，而不是筛选前登记；
- 不能用同一次扫描既提出假设又宣称确认；
- 若要继续，必须登记为新一代候选，再使用未参与发现的数据验证。

## 五、产物

每次成功筛选位于：

```text
outputs/market_regime_research/screenings/<SCREENING_ID>/
```

当前基准目录：

```text
outputs/market_regime_research/screenings/
  stage_b_effectiveness_audited_20260802_v6/
```

产物：

```text
candidate_registry.parquet
univariate_event_studies.parquet
walk_forward_folds.parquet
walk_forward_predictions.parquet
candidate_scorecard.parquet
screening_summary.json
screening_manifest.json
research_report.html
screening.json
```

`walk_forward_predictions.parquet` 只保存事前登记候选的逐日预测，避免把 772 个探索
组合扩成没有必要的超大文件；所有探索尝试仍完整保留在 registry、fold、event
study 和 scorecard 中。

## 六、运行方式

默认读取 Stage A 的 `latest.json`：

```bash
python scripts/run_market_regime_research.py screen
```

指定不可变输入和输出 ID：

```bash
python scripts/run_market_regime_research.py screen \
  --research-run-id stage_a_audited_20260801_v6 \
  --screening-id my_stage_b_run
```

## 七、复现和防错

`v5` 与 `v6` 的五张核心 Parquet 逐字节一致：

```text
candidate_registry.parquet       identical
univariate_event_studies.parquet identical
walk_forward_folds.parquet       identical
walk_forward_predictions.parquet identical
candidate_scorecard.parquet      identical
```

针对性测试覆盖：

- 封存期数据突变不改变任何筛选结果；
- 每个训练/验证边界满足 embargo；
- FDR 保序并正确处理缺失值；
- 未登记组合只能是 exploratory；
- 未知特征和重复假设失败关闭；
- Stage A 文件哈希被篡改时拒绝运行；
- 筛选目录不可覆盖；
- HTML 报告和 latest pointer 可追溯。

## 八、下一步

### G7 参数扰动

围绕唯一候选测试邻近定义，例如：

```text
return_3d / return_5d / return_10d
报警分位 85% / 90% / 95%
winsor 0.5% / 1% / 2%
```

预先冻结“保留至少 70% 效果”的计算口径。不能看到结果后挑最优参数。

### G8 增量信息

建立只含价格状态和标签前置条件的简单基线模型，再加入候选：

```text
baseline
vs.
baseline + spx_return_5d
```

在完全相同的 walk-forward 切分上比较 Brier、PR-AUC、校准和事件表现。只有增量
改善稳定为正才保留。

### G9 经济价值

将概率转成预先定义的风险覆盖或 SPY next-open 交易规则，强制使用现有执行层的：

- next-open 成交；
- 滑点；
- IBKR 费用模型；
- 不可成交和现金约束；
- 逐笔交易与成本明细。

### G10 影子运行

完成 G7-G9 后再每日记录信号，不驱动真实仓位。至少运行 60 个交易日，检查数据
延迟、漂移、错误报警和可解释性。

现有只读研究状态页只能展示 2022 年前的开发样本证据，并明确标记为未获生产批准。
只有 G7-G10 也通过，才讨论打开 2022 年封存集、展示实时概率或增加生产信号提示。
