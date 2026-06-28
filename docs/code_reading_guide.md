# 代码阅读指南

这份文档是给第一次系统性阅读项目的人看的。目标不是解释每一行代码，而是让你知道：

- 每个文件在系统里负责什么
- 一个函数为什么存在
- 读到某个配置项时应该跳到哪个模块
- 当前系统的数据是怎么从 FMP 行情一路流到网页上的

## 先抓住三条主线

### 1. 离线研究主线

```text
configs/default.yaml
  -> scripts/run_mvp.py
  -> src/data/
  -> src/factors/
  -> src/preprocessing/
  -> src/analysis/
  -> src/backtest/quintile.py
  -> src/webapp/results_store.py
  -> outputs/universes/<UNIVERSE>/factors/<FACTOR>/
  -> Web 因子页面
```

这条线回答的问题是：

> 一个因子从行情数据开始，怎么被计算、检验、回测、保存、展示？

### 2. 策略回测主线

```text
策略库
  -> src/strategies/
  -> src/backtest/composer.py
  -> src/backtest/runner.py
  -> src/backtest/quintile.py
  -> src/execution/models.py
  -> outputs/backtests/<TASK_ID>/
  -> Web 回测详情页
```

这条线回答的问题是：

> 多个因子按权重融合成一个策略后，怎么回测？

### 3. 模拟盘主线

```text
模拟盘账户
  -> src/papertrading/definition.py
  -> src/papertrading/target.py
  -> src/papertrading/runner.py
  -> src/execution/models.py
  -> outputs/papertrading/<ACCOUNT_ID>/
  -> Web 模拟盘页面
```

这条线回答的问题是：

> 一个策略如果每天模拟交易，订单、成交、持仓和权益曲线怎么更新？

## 第一阶段：只读两个文件

### `configs/default.yaml`

你可以把这个文件理解成“全系统旋钮”。先不要纠结每一个数字，先看大块：

| 配置块 | 作用 | 主要影响 |
|---|---|---|
| `universes` | 跑哪些股票池 | `scripts/run_mvp.py` 会循环这些股票池 |
| `date_range` | 研究时间范围 | 数据下载、IC、回测都会使用这个范围 |
| `data` | FMP 数据源和缓存 | `src/data/loader.py`、`src/data/fmp.py` |
| `preprocessing` | 因子预处理 | `src/preprocessing/pipeline.py` |
| `ic_analysis` | IC 计算参数 | `src/analysis/ic.py` |
| `factor_confidence` | 因子置信评估阈值 | `src/analysis/confidence.py` |
| `backtest` | 分组回测、调仓、成交设置 | `src/backtest/quintile.py` |
| `backtest.execution` | 手续费、滑点、成交时点 | `src/execution/models.py` |
| `factors.enabled` | 当前启用哪些因子 | `src/factors/` |
| `webapp` | Web host/port/output_dir | `src/webapp/app.py` |

读配置时最重要的是问一句：

> 这个值被谁读取？

例如：

- `backtest.execution.timing = "next_open"` 被 `quintile_backtest()` 读取，决定能不能用当天收盘价成交。
- `factor_confidence.thresholds.min_t_stat_pass` 被 `build_factor_confidence()` 读取，决定因子置信状态。
- `factors.enabled` 被 `run_pipeline_for_universe()` 读取，决定循环计算哪些因子。

### `scripts/run_mvp.py`

这个文件是离线 pipeline 的总入口。推荐只按这几个函数读：

1. `main()`
   - 程序从这里开始。
   - 只做三件事：读参数、决定是否跑 pipeline、决定是否启动 Web。

2. `parse_args()`
   - 解释命令行参数。
   - 例如 `--serve-only`、`--no-web`、`--update`。

3. `run_pipeline()`
   - 决定要跑哪些股票池。
   - 真正干活的是下一层 `run_pipeline_for_universe()`。

4. `run_pipeline_for_universe()`
   - 这是最重要的函数。
   - 它完成一个股票池的全流程：
     1. 取股票池
     2. 下载/整理行情宽表
     3. 循环每个因子
     4. 计算 raw factor
     5. 预处理成 clean factor
     6. 计算 IC
     7. 跑五分位回测
     8. 生成图
     9. 保存产物
     10. 生成因子置信评估

5. `serve_web()`
   - 只启动网页。
   - 不更新数据，不计算因子。

## 第二阶段：顺着 `run_mvp.py` 往下跳

看到 `get_universe()`，跳到：

```text
src/data/universe.py
```

它负责股票池成分股，例如 SP500、MAG7。

看到 `build_wide_tables()`，跳到：

```text
src/data/cleaner.py
```

它负责把一只只股票的 OHLCV 行情整理成宽表：

```text
date x ticker
```

例如：

- `adj_close.parquet`
- `open.parquet`
- `returns.parquet`
- `volume.parquet`

看到 `get_factor()` 和 `factor.compute_from_wide()`，跳到：

```text
src/factors/base.py
src/factors/momentum.py
src/factors/volatility.py
src/factors/reversal.py
src/factors/turnover.py
```

它们负责把行情宽表变成因子值宽表。

看到 `preprocess_factor()`，跳到：

```text
src/preprocessing/pipeline.py
```

它负责去极值、标准化、中性化。

看到 `compute_ic()`，跳到：

```text
src/analysis/ic.py
```

它负责计算因子值和未来收益的 Rank IC。

看到 `quintile_backtest()`，跳到：

```text
src/backtest/quintile.py
```

它负责分组回测、next_open 成交、可交易过滤、交易成本扣减。

看到 `calculate_execution()`，跳到：

```text
src/execution/models.py
```

它负责手续费、滑点、成交价、最大可买数量。

看到 `build_factor_confidence()`，跳到：

```text
src/analysis/confidence.py
```

它负责因子置信评估：p-value、q-value、稳定性、单调性、换手、覆盖率。

看到 `save_factor_artifacts()`，跳到：

```text
src/webapp/results_store.py
```

它负责把结果写入 `outputs/`，Web 页面再从这里读。

## 第三阶段：理解 outputs

离线研究产物大致长这样：

```text
outputs/universes/SP500/factors/MOM_12M/
  meta.json
  factor_values.parquet
  ic.parquet
  ic_summary.json
  group_nav.parquet
  ls_nav.parquet
  ls_returns.parquet
  group_metrics.parquet
  backtest_config.json
  confidence.json
  confidence_checks.parquet
```

这些文件的含义：

| 文件 | 作用 |
|---|---|
| `factor_values.parquet` | 预处理后的因子矩阵，策略融合会读它 |
| `ic.parquet` | 每天一条 IC |
| `ic_summary.json` | IC 均值、IC_IR、t 统计量 |
| `group_nav.parquet` | Q1..Q5 分组净值 |
| `ls_nav.parquet` | Long-Short 净值 |
| `group_metrics.parquet` | 年化收益、Sharpe、回撤等 |
| `confidence.json` | 因子置信总报告 |
| `confidence_checks.parquet` | 因子置信逐项检查清单 |

Web 页面不是凭空算出来的，它大部分是在读这些文件。

## 第四阶段：再读 Web

Web 入口：

```text
src/webapp/app.py
```

它只做三件事：

1. 创建 FastAPI app
2. 挂载 `/static`
3. 注册 `routes.py` 和 `routes_v2.py`

旧主线页面在：

```text
src/webapp/routes.py
```

包括：

- 首页
- 因子详情
- 因子 IC/NAV API
- 单股诊断

新主线页面在：

```text
src/webapp/routes_v2.py
```

包括：

- 因子库
- 策略库
- 回测任务
- 模拟盘
- 股票池

页面模板在：

```text
src/webapp/templates/
```

样式在：

```text
src/webapp/static/css/style.css
```

## 建议你怎么读

不要一次打开 20 个文件。每次只读一个问题：

1. 股票池怎么来的？
   - `src/data/universe.py`

2. 行情怎么变成宽表？
   - `src/data/cleaner.py`

3. 因子怎么算？
   - `src/factors/base.py`
   - 再看一个具体因子，比如 `src/factors/momentum.py`

4. 因子怎么检验？
   - `src/analysis/ic.py`
   - `src/analysis/confidence.py`

5. 回测怎么防止前视？
   - `src/backtest/quintile.py`

6. 成本怎么扣？
   - `src/execution/models.py`

7. Web 怎么展示？
   - `src/webapp/results_store.py`
   - `src/webapp/routes.py`
   - `src/webapp/templates/factor.html`

## 最重要的一句话

这个系统的核心不是某一个页面，而是这条链：

```text
行情宽表 -> 因子矩阵 -> 预处理因子矩阵 -> IC/回测/置信评估 -> outputs -> Web
```

读代码时只要始终问：

> 这个函数输入什么 DataFrame？输出什么 DataFrame 或文件？

整个系统就会慢慢清楚。
