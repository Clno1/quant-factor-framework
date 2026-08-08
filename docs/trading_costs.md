# 回测与模拟盘交易费用说明

更新日期：2026-07-30

本文记录系统内回测和模拟盘使用的成交、滑点、手续费模型。相关代码集中在
`src/execution/models.py`，默认参数在 `configs/default.yaml` 的
`backtest.execution` 段。

## 适用范围

- 回测：`src/backtest/quintile.py` 按逐票目标权重变化估算订单金额和股数，
  再调用共享执行模型计算成交价、滑点成本和费用，最后按组合资金规模扣减到组收益。
- 模拟盘：`src/papertrading/runner.py` 使用同一套模型；买单会在含滑点和费用后
  检查现金是否足够，卖单会按实际持仓数量成交。
- 默认成交时机：`next_open`，即 T 日决策，T+1 开盘价成交。系统不允许
  `next_open` 静默回退到 `close`。

## 默认配置

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `timing` | `next_open` | T 日决策，下一根开盘价成交 |
| `portfolio_value` | `100000` | 回测中将权重换算成股数的名义组合规模 |
| `fee_model` | `ibkr_us_pro_fixed` | 默认使用 IBKR Pro Fixed 风格美股费用 |
| `slippage_model` | `volume_share` | 默认使用成交量占比滑点模型 |
| `slippage_bps` | `5` | `constant_bps` 模型的单边滑点；不用于绕过成交量校验 |
| `commission_bps` | `2` | `simple_bps` 费用模型的单边手续费兜底 |
| `min_open_coverage` | `0.95` | 正式行情版本中 `open` 的最低覆盖率校验 |

## 费用模型

当前支持的 `fee_model`：

- `ibkr_us_pro_fixed`：默认值。券商佣金为 `0.005 USD/share`，每单最低
  `1.00 USD`，最高为成交金额的 `1%`。
- `ibkr_us_pro_tiered`：低月交易量档位近似，券商佣金为 `0.0035 USD/share`，
  每单最低 `0.35 USD`，最高为成交金额的 `1%`。
- `ibkr_us_lite`：券商佣金为 0，但监管、CAT、清算等第三方费用仍按配置计算。
- `simple_bps`：老式简化模型，费用为 `成交金额 * commission_bps / 10000`。

默认还会计算以下第三方费用：

| 费用项 | 默认值 | 方向 |
| --- | ---: | --- |
| SEC Transaction Fee | `成交金额 * 0.0000206` | 仅卖出 |
| FINRA Trading Activity Fee | `卖出股数 * 0.000195`，每笔最高 `9.79 USD` | 仅卖出 |
| FINRA CAT Fee | `成交股数 * 0.000003` | 买入和卖出 |
| NSCC/DTC Clearing | `成交股数 * 0.00020`，最高成交金额 `0.5%` | 买入和卖出 |
| NYSE Pass Through | `券商佣金 * 0.000175` | 买入和卖出 |
| FINRA Pass Through | `券商佣金 * 0.00056` | 买入和卖出 |
| Exchange Fee | 默认 `0 bps` | 买入和卖出 |

说明：交易所 maker/taker、具体路由、暗池、盘前盘后、做空借券费、融资利息暂未在
PnL 中模拟。后续接入真实券商订单路由后，`exchange_fee_bps` 和借券/融资模块需要细化。

## 滑点模型

当前支持的 `slippage_model`：

- `volume_share`：默认值，参考成熟回测框架常见做法，用订单参与率估算价格冲击。
- `constant_bps` / `simple_bps`：固定单边 bps 滑点。
- `none`：无滑点，主要用于排查问题。

`volume_share` 的默认参数：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `fallback_bps` | `5` | 底层直接调用的兼容值；标准回测/模拟盘缺成交量会失败 |
| `spread_bps` | `2` | 基础价差/开盘冲击 |
| `volume_limit` | `0.025` | 每个交易日最多成交过去 20 日 ADV 的 2.5% |
| `adv_window` | `20` | 只使用决策时已经知道的过去 20 个有效成交量 |
| `price_impact` | `0.10` | 价格冲击系数 |

计算公式：

```text
reference_volume = mean(valid volume over the trailing 20 sessions)
participation = abs(quantity) / reference_volume
capped = min(participation, volume_limit)
slippage_bps = spread_bps + price_impact * capped^2 * 10000
buy_fill_price = raw_price * (1 + slippage_bps / 10000)
sell_fill_price = raw_price * (1 - slippage_bps / 10000)
slippage_cost = abs(quantity) * raw_price * slippage_bps / 10000
```

## 回测落账

回测使用等权分组，因此每次调仓会得到每只股票的 `old_weight`、`new_weight` 和
`trade_weight`。系统按以下步骤扣成本：

1. `estimated_notional = abs(trade_weight) * portfolio_value`
2. `estimated_quantity = estimated_notional / raw_price`
3. 校验 `estimated_quantity <= trailing_ADV × volume_limit`；超过时拒绝回测，
   不假装整单成交，也不在横截面回测中暗做部分成交。
4. 调用 `calculate_execution(...)` 得到成交价、滑点成本、费用明细。
5. `cost = total_cost_cash / portfolio_value`
6. 在调仓生效日从对应组的日收益中扣除 `cost`。

只有同时存在成交开盘价和下一次估值开盘价的调仓才进入回测绩效。区间尾部若只有
`T+1 open`、没有 `T+2 open`，页面仍可显示 T 日信号，但不会生成一笔成本与收益期限不一致的
截断交易。

回测产物中会保存：

- `holdings.parquet`：逐票目标持仓权重。
- `trades.parquet`：逐票交易、估算股数、成交价、滑点、费用组件。
- `costs.parquet`：按日期和分组聚合后的滑点、费用和总成本。

## 模拟盘落账

模拟盘与回测共用执行模型，但使用真实账户状态：

1. 先处理等待成交的卖单，再处理买单，卖出现金可用于买入。
2. 每个交易日最多成交 `trailing_ADV × volume_limit`，超出的数量保持 pending，
   下一交易日继续尝试，形成可审计的部分成交。
3. 买单用 `max_buy_quantity_for_cash(...)` 计算在含滑点和费用后现金可承受的最大股数。
4. 持仓成本价使用实际成交价 notional 更新。
5. SQLite 中的 `fills` frame 是现金和持仓的事实账本；账户主记录和 `positions` frame 是
   可由成交账本重建的投影。失败重跑会按 `fill_id` 和 `order_id` 防止重复成交或重复扣费。
6. `fills` frame 保存成交价、费用模型、滑点模型、监管费用、清算费、总成本等字段；所有行和
   frame 元数据都在 `outputs/quant_app.sqlite3` 中并带 checksum。

## 数据与校验要求

- `next_open` 必须在绑定的正式行情版本中有完整 `open` 数据，且覆盖率至少达到
  `min_open_coverage`；缺失时不得回退 `close`。
- 当 `slippage_model=volume_share` 时必须有可用的历史成交量。成交量缺失时回测拒绝任务，
  模拟盘拒绝本轮运行；不会把成交上限静默视为无限。
- 所有成本、滑点、参与率和费率参数必须是有限非负数；`volume_limit`、费用占成交金额上限
  和 open 覆盖率必须在 `[0, 1]` 内。负值、`NaN`、无限值和非布尔开关会在任务启动前被拒绝。
- `SP500`、`US_LIQUID_5M` 是动态股票池，必须提供 point-in-time 完整成分快照。
  严格校验包括：首个快照覆盖回测起点、历史活跃股票都存在于行情/因子矩阵。
  `MAG7` 在配置中被明确声明为固定研究池，因此不要求 PIT 文件。
- 模拟盘只接受目标因子日期等于指定 `as-of` 对应的 XNYS session；不传 `as-of`
  时必须等于最近一个已经正式收盘的 XNYS session。数据陈旧会直接失败。

## 参数来源

- IBKR 美股佣金、监管费、清算费和 pass-through 费用：
  https://www.interactivebrokers.com/en/pricing/commissions-stocks.php
- QuantConnect Fee/Slippage 模型设计：
  https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/transaction-fees/supported-models
  https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models
- Zipline `VolumeShareSlippage`：
  https://zipline.ml4trading.io/api-reference.html#zipline.finance.slippage.VolumeShareSlippage
- Backtrader 佣金与滑点机制：
  https://www.backtrader.com/docu/commission-schemes/commission-schemes/
  https://www.backtrader.com/docu/slippage/slippage/

费用费率会变化，尤其是 SEC、FINRA、CAT 等监管相关项目。准备切实盘或季度复盘时，
需要重新核对券商和监管机构页面，并同步更新 `configs/default.yaml`。
