# 回测与模拟盘交易费用说明

更新日期：2026-06-16

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
| `slippage_bps` | `5` | 缺少成交量或 `constant_bps` 时的单边滑点兜底 |
| `commission_bps` | `2` | `simple_bps` 费用模型的单边手续费兜底 |
| `min_open_coverage` | `0.95` | `open.parquet` 最低覆盖率校验 |

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
| `fallback_bps` | `5` | 成交量缺失时的兜底滑点 |
| `spread_bps` | `2` | 基础价差/开盘冲击 |
| `volume_limit` | `0.025` | 单根日线 bar 最高按 2.5% 成交量参与率计算冲击 |
| `price_impact` | `0.10` | 价格冲击系数 |

计算公式：

```text
participation = abs(quantity) / bar_volume
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
3. 调用 `calculate_execution(...)` 得到成交价、滑点成本、费用明细。
4. `cost = total_cost_cash / portfolio_value`
5. 在调仓生效日从对应组的日收益中扣除 `cost`。

回测产物中会保存：

- `holdings.parquet`：逐票目标持仓权重。
- `trades.parquet`：逐票交易、估算股数、成交价、滑点、费用组件。
- `costs.parquet`：按日期和分组聚合后的滑点、费用和总成本。

## 模拟盘落账

模拟盘与回测共用执行模型，但使用真实账户状态：

1. 先处理等待成交的卖单，再处理买单，卖出现金可用于买入。
2. 买单用 `max_buy_quantity_for_cash(...)` 计算在含滑点和费用后现金可承受的最大股数。
3. 持仓成本价使用实际成交价 notional 更新。
4. `fills.parquet` 保存成交价、费用模型、滑点模型、监管费用、清算费、总成本等字段。

## 数据与校验要求

- `next_open` 必须有 `open.parquet`，且覆盖率至少达到 `min_open_coverage`。
- 当 `slippage_model=volume_share` 时，优先使用 `volume.parquet`；如果某票某日成交量缺失，
  该笔交易使用 `fallback_bps`。
- 股票池支持 point-in-time 成分股快照；开启
  `backtest.require_point_in_time_universe=true` 后，没有 PIT 股票池会拒绝回测。

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
