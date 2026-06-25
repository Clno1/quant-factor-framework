# 因子置信评估说明

本文档记录项目内因子置信评估系统的计算口径、默认阈值和产物路径。

## 产物位置

每个股票池、每个因子都会在以下目录生成置信评估产物：

```text
outputs/universes/<UNIVERSE>/factors/<FACTOR>/
  confidence.json
  confidence_checks.parquet
  rank_autocorr.parquet
  quantile_turnover.parquet
```

`confidence.json` 是总报告；`confidence_checks.parquet` 是逐项检查清单；另外两个 Parquet 用于后续画 Rank 自相关和分位换手图。

## 评分维度

综合评分为 0-100 分，默认权重在 `configs/default.yaml` 的 `factor_confidence.score_weights` 中配置：

| 维度 | 默认权重 | 说明 |
|---|---:|---|
| predictive | 35% | IC 均值、IC_IR、t 统计量、p-value、FDR q-value、IC 同向占比 |
| stability | 25% | 月度同向占比、63D 滚动 IC 同向占比、三段样本一致性 |
| economic | 20% | 分组收益单调性、扣费后多空年化收益、扣费后多空 Sharpe |
| tradability | 10% | Rank 自相关、Top 分位换手、年化交易摩擦 |
| data_quality | 10% | 平均覆盖率、最近覆盖率、零截面标准差占比 |

## 默认结论

系统输出三个结论：

| 结论 | 含义 |
|---|---|
| 通过 | 综合得分、FDR 和 t 统计量都达到可进入策略研究的默认门槛 |
| 观察 | 有一定信号，但统计置信、稳定性或经济意义还不够稳 |
| 拒绝 | 当前样本下不建议进入策略组合 |

等级为 A/B/C/D，主要用于快速排序；真正判断时建议同时查看检查清单。

## 多重检验

同一股票池内所有因子会在 pipeline 结束时统一做 Benjamini-Hochberg FDR 校正，得到 `q_value`。默认通过阈值为 `q_value <= 0.10`，观察阈值为 `q_value <= 0.20`。

## 运行方式

重新生成因子分析产物时会自动生成置信评估：

```bash
python scripts/run_mvp.py --only-universe SP500 --no-web
```

只启动网页查看已有产物：

```bash
python scripts/run_mvp.py --serve-only --host 127.0.0.1 --port 18823
```

## 当前边界

第一版已覆盖成熟多因子研究中的核心置信检查，但仍有可扩展空间：

- Newey-West/HAC t 统计量
- Deflated Sharpe Ratio
- 样本外 walk-forward 置信
- 行业/市值分桶后的因子置信稳定性
- 策略组合级别的融合因子置信评估
