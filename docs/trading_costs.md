# 回测与模拟盘交易费用说明

更新日期：2026-08-29

本文记录系统内回测和模拟盘使用的成交、滑点、手续费模型。相关代码集中在
`src/execution/models.py`，默认参数在 `configs/default.yaml` 的
`backtest.execution` 段。

## 适用范围

- 回测：`src/backtest/portfolio.py` 为每个研究分组维护持仓市值、现金和 NAV。
  调仓订单来自调仓前真实漂移持仓与新目标的差额，再调用共享执行模型计算成交价、
  滑点和费用；收益、订单、成本和净值来自同一本状态账。
- 模拟盘：`src/papertrading/runner.py` 使用同一套模型；买单会在含滑点和费用后
  检查现金是否足够，卖单会按实际持仓数量成交。
- 默认成交时机：`next_open`，即 T 日决策，T+1 开盘价成交。系统不允许
  `next_open` 静默回退到 `close`。

## 默认配置

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `timing` | `next_open` | T 日决策，下一根开盘价成交 |
| `portfolio_value` | `100000` | 回测中将权重换算成股数的名义组合规模；新建回测页可覆盖并冻结到任务 |
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

回测在调仓时建立等权目标，但调仓之间不会免费恢复等权。价格变化后，每只股票按真实
持仓市值自然漂移；下一次调仓才会卖出超配股票、买入低配股票。系统按以下步骤落账：

1. 用当前开盘价标记原持仓，计算调仓前 `positions + cash = NAV`。
2. 以 `NAV - estimated_cost` 为可投资金额迭代求解目标持仓，为本次费用预留现金。
3. `estimated_notional = abs(target_value - actual_position_value)`。
4. `estimated_quantity = estimated_notional / raw_price`。
5. 校验 `estimated_quantity <= trailing_ADV × volume_limit`；引擎会扫描完整回测区间并汇总
   所有超限订单，以最严格订单反推出该历史区间可承载的最大 `portfolio_value`。只要存在
   超限就拒绝发布结果，不假装整单成交，也不在横截面回测中暗做部分成交。
6. 调用 `calculate_execution(...)` 得到成交价、滑点成本和费用明细。
7. 调仓后重新校验 `positions + cash = NAV - cost`；持有期只让仓位随证券收益变化。
8. 每日强制满足 `net_return = gross_return - cost_return` 和
   `end_nav = start_nav × (1 + net_return)`。

Long-Short 收益按“定向毛价差 - 多头成本 - 空头成本”计算；禁止用两个净收益直接相减，
因为那会错误地把空头腿成本加回组合。

容量失败会以结构化 `ADV_CAPACITY_EXCEEDED` 写入任务，页面显示当前资金、全区间容量、
最严格股票、请求/允许股数和订单参与率。页面给出的重建资金在历史容量上再保留 10% 缓冲，
但它只是同一冻结数据版本下的容量建议，不代表未来流动性保证。技术堆栈默认折叠保留，
便于运维审计。结构化全区间结果显示为“安全建议资金”；旧任务里只有首笔英文错误的，页面会
兼容解析并明确标为“首个失败订单推算 / 初步估算资金”，重跑后仍可能发现更严格的历史订单。

股票数少于目标分组数两倍时，runner 会在执行前降低有效分组数。例如 6 只股票配置 5 组会实际
使用 3 组，Top 组合为 Q3，避免大部分分组只有 0 或 1 只股票。实际分组会在成功和容量失败任务中
持久化并展示；创建时的 5 组只是请求值，不再冒充最终执行值。

自定义 Watchlist 的行情版本必须达到最近可发布交易日。版本陈旧时任务进入
`WAITING_FOR_DATA`，统一缺数 worker 发布专属版本后由 Web 后台监视器自动恢复；不会用旧版本继续
回测。2026-08-13 SG 验收中，6 票 Watchlist 从 `2026-08-10` 补到 `2026-08-12`，随后以
`14,700 USD`、3 个有效分组完成回测，实际计算 10.847 秒。

只有同时存在成交开盘价和下一次估值开盘价的调仓才进入回测绩效。区间尾部若只有
`T+1 open`、没有 `T+2 open`，页面仍可显示 T 日信号，但不会生成一笔成本与收益期限不一致的
截断交易。

### PIT 成分退出与停牌

动态股票池内持仓在两次正常调仓之间退出时，默认执行策略是
`next_open_or_last_close_to_cash`：

1. 成分快照采用 effective-close 语义；T 日首次观察到退出，只允许在 T+1 开盘卖出，
   不读取 T+1 membership 为 T 日提前下单；
2. 若下一日停牌但后来恢复报价，沿用最后一次真实可成交时的组合归属，在首个恢复开盘卖出；
3. 若并购后不再恢复交易，只有版本绑定事件账本明确为收购/合并时才允许用最后可交易收盘近似；
4. 若事件账本明确为 FDIC 接管、破产或 receivership，按 100% 损失 write-off，不虚构券商成交；
5. 未审阅的事件理由、缺事件账本或没有可验证价格时 fail closed；
6. 停牌期间持仓值冻结；最终结算损益在退出证据可知日入账，不倒记到最后交易日，避免未来
   事件改写中间 NAV 或调仓；
7. 退出后的权重保持现金直到下一次正常调仓，不对剩余股票事后放大权重。

没有 `announcement_date/known_at` 的旧 PIT 事件不会被解释成“提前已知”。当前保守合同宁可
在有效状态确认后晚一根开盘退出，也不根据未来 membership 获得理想成交。

回测产物中会保存：

- `holdings.parquet`：每次调仓后的逐票持仓集合。
- `holdings_detail.parquet`：目标权重、实际权重、持仓市值、估算数量、现金和 NAV。
- `trades.parquet`：逐票交易、估算股数、成交价、滑点、费用组件。
- `costs.parquet`：按日期和分组聚合后的滑点、费用和总成本。
- `portfolio_daily.parquet`：每日期初/期末 NAV、现金、市场损益、成本和会计误差。
- `position_daily.parquet`：每日逐票期初权重、证券收益和组合收益贡献。

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
- 市值双排序只接受版本绑定的 `date × ticker` PIT 市值矩阵。当前 profile 的
  `ticker × market_cap` 快照只用于当期展示；系统写入
  `BLOCKED_NO_PIT_MARKET_CAP`，禁止向历史广播后冒充稳健性检验。
- 模拟盘只接受目标因子日期等于指定 `as-of` 对应的 XNYS session；不传 `as-of`
  时必须等于最近一个已经正式收盘的 XNYS session。数据陈旧会直接失败。

## 参数来源

- IBKR 美股佣金、监管费、清算费和 pass-through 费用：
  https://www.interactivebrokers.com/en/pricing/commissions-stocks.php
- QuantConnect Fee/Slippage 模型设计：
  https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/transaction-fees/supported-models
  https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models
- QuantConnect 退市和证券移除处理：
  https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions
  https://www.quantconnect.com/docs/v2/writing-algorithms/securities/requesting-data
- Zipline `VolumeShareSlippage`：
  https://zipline.ml4trading.io/api-reference.html#zipline.finance.slippage.VolumeShareSlippage
- Backtrader 佣金与滑点机制：
  https://www.backtrader.com/docu/commission-schemes/commission-schemes/
  https://www.backtrader.com/docu/slippage/slippage/

费用费率会变化，尤其是 SEC、FINRA、CAT 等监管相关项目。准备切实盘或季度复盘时，
需要重新核对券商和监管机构页面，并同步更新 `configs/default.yaml`。
