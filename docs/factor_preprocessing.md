# 因子预处理说明

本文档记录项目中单因子进入 IC、置信评估、回测和多因子融合之前的清洗流程。

## 当前默认流程

代码入口：

```text
src/preprocessing/pipeline.py
```

当前顺序为：

```text
raw factor
  -> winsorize 去极值
  -> neutralize 行业/市值中性化
  -> z-score 横截面标准化
  -> clean factor
```

这里的输入和输出都是 `date x ticker` 宽表：

```text
index   = date
columns = ticker
value   = 因子值
```

## 为什么是这个顺序

### 1. 先去极值

极端值会污染回归和标准化，所以要先处理。

默认方法为 MAD winsorization：

```text
median = 当天截面中位数
MAD = median(|x_i - median|)
upper = median + n * 1.4826 * MAD
lower = median - n * 1.4826 * MAD
```

超出上下界的因子值会被压回边界。

### 2. 再中性化

中性化的目标是剥离行业、市值等已知风险暴露。

每天做一次截面回归：

```text
factor_i = const + industry_dummies_i + log_mcap_i + residual_i
```

然后用 `residual_i` 作为中性化后的因子值。

### 3. 最后 z-score

中性化后得到的是回归残差，残差尺度会随日期和因子变化，所以必须最后再做横截面标准化：

```text
z_i = (x_i - mean_day) / std_day
```

这样后续多因子融合时，不同因子才处在可比较尺度。

## 当前打开的开关

配置位置：

```text
configs/default.yaml
```

当前默认：

```yaml
preprocessing:
  winsorize_method: "mad"
  winsorize_n: 3
  standardize: true
  neutralize_industry: true
  neutralize_mcap: false
  neutralize_min_obs: 30
```

也就是说：

- 去极值：开启
- z-score：开启
- 行业中性化：开启
- 市值中性化：暂未开启

## 为什么市值中性化暂未开启

市值中性化需要一套版本化、point-in-time 的历史市值矩阵，并且必须和行情版本按日期、ticker
严格绑定。当前正式行情版本只有 OHLCV：

```text
open / high / low / close / adj_close / volume
```

没有历史市值矩阵。如果现在把 `neutralize_mcap` 直接设为 `true`，系统会因为缺少输入而拒绝或
跳过，造成“配置看似开启但研究并未真正完成”的误解。不能用今天的市值回填历史。

因此当前只打开有可靠数据支撑的行业中性化。等后续补齐 point-in-time 历史市值数据后，再打开市值中性化。

## 小股票池注意事项

中性化需要足够的截面样本。默认 `neutralize_min_obs = 30`。

对于 MAG7 这类只有 7 只股票的小股票池，行业回归样本过少，系统会跳过该日中性化并记录日志。这是有意设计，避免用极少样本做不稳定回归。

## 关键代码位置

| 步骤 | 文件 |
|---|---|
| 预处理总入口 | `src/preprocessing/pipeline.py` |
| MAD / 3sigma 去极值 | `src/preprocessing/winsorize.py` |
| Z-score 标准化 | `src/preprocessing/standardize.py` |
| 行业/市值中性化 | `src/preprocessing/neutralize.py` |
| 配置开关 | `configs/default.yaml` |
