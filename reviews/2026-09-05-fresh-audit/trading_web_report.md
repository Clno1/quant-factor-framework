# 回测、模拟盘、存储与 Web：首次接触审查

审查对象：`/Users/huozhihong/Documents/Quant`，根仓库 HEAD `1149764`。按本轮清单逐文件完整阅读 112 个代码/模板/样式/测试/脚本文件，35,371 行；另完整阅读根代理指定 5 个当前设计文档。实际阅读清单见 `trading_web_read.json`。没有修改业务代码、读取密钥、使用旧审查结论或运行真实 worker。

## 架构与整体流程

1. 策略是具备固定因子 ID、有符号权重与方向的配方，按绝对权重和归一化。Watchlist 是独立目标证券集合，股票集合摘要决定专属数据集身份。SQLite 保存可变定义、任务和账户，定义快照随任务/账户冻结。
2. 回测任务冻结策略、目标池、研究证据、执行与风险参数。Runner 读取不可变行情版本及因子 generation，验证 PIT/完整性/日期/可交易性，缺数则入队等待。逐因子经过完整成分合成后进入 `quintile_backtest_v2`，next-open 成交价格和总回报归因价格分开。完成后保存收益、交易、成本、持仓和决策快照。
3. 存在两套实际分组回测路径：旧 `quintile_backtest` 已连接 `simulate_group_portfolios` 有状态资金账；正式 `quintile_backtest_v2` 仍连接旧等权收益与独立订单工具。正式 runner 与 run_mvp 使用后者。这是本轮最严重的流程不一致。
4. 模拟盘以 `fills` 和 `cash_events` 为事实账本，账户/仓位/订单为投影。每轮账户锁中生成最新目标、重建现金与持仓、处理 pending、计算分红、收盘估值、生成调仓单、写回放及运行记录。成交事实先写、订单投影后写，实现失败重跑去重；整轮由多次 SQLite 事务构成。
5. SQLite WAL 中每个 frame 写入时同时更新逐行 JSON、元数据和 checksum。行情与研究主要用版本绑定 Parquet，回放采用独立文件锁、文件哈希与 manifest 最后发布。Web 是 FastAPI/Jinja，提供研究浏览、策略/Watchlist/任务/账户 CRUD、手工运行、回放及突破/行业观察；配置 Basic Auth 或限制回环连接，并对跨域修改做拦截。

## 检查与复现方法

数值案例只使用 4 只合成证券和短日期区间，没有读取真实行情、账户或既有产物。脚本：`repro_trading_web_numpy.py`，结果：`repro_trading_web_numpy_result.json`。在依赖安装等待期，实际回测/指标/SQLite 模块完整原样加载，仅替换 CONFIG/logger 依赖；模拟盘、通知与 Web 的实际函数从源 AST 原样编译，替换存储为隔离内存字典或 /tmp SQLite。随后已在 `light-venv` 和独立 `repo_trading` 副本用 `--normal` 正常 import 原模块完成全部 9 项交叉验证（含真实通知 payload 校验和 Web 路由完整导入）；结果在 `repro_trading_web_normal_result.json`。模板问题另外通过实际 Jinja 渲染、HTML 属性解码及 Node vm 执行复现，总计 10 项已证实发现。两个脚本均退出 0。

读过的现有测试覆盖较多错误输入、成本、PIT、manifest、账户恢复、并发记录写及页面展示，但经济回测测试主要落在旧 `quintile_backtest`。这使旧入口修正的行为没有约束正式 v2。

## 已证实发现（按影响排序）

### TW-1 / P1：正式回测每天免费恢复等权，交易账与实际绩效脱离

位置：`src/backtest/quintile_v2.py:172-187`；正式调用 [src/backtest/runner.py:730](/Users/huozhihong/Documents/Quant/src/backtest/runner.py:730)。`src/decision_replay/builder.py:257-277` 在缺少每日仓位账时又回退等权，从而同样错误的持仓/收益可相互通过审计。

`gross_ret` 由每日期末证券收益的简单等权平均生成，成交明细另由旧工具按初始名义资金与成员变化生成。价格走势改变组内实际仓位时，v2 既没有让权重漂移，也没有记录每日恢复等权所需的交易；到下次正式调仓且成员不变时仍不交易。`portfolio_daily`、`position_daily` 全空。

复现：Q1 买入 A、B 各 500，A 开盘价 100→120→132，B 保持 100，调仓周期 3。第二个持有区间实际 A 权重 600/1100，应赚 5.454545%，v2 报 5%；下次调仓需要卖 A 买 B，v2 输出 0 单，而有状态旧入口输出 2 单。真实 `build_backtest_snapshot` 使用该结果仍成功，冻结 A 权重 0.5，并报告组合贡献最大审计误差为 0.0。这个误差会累计影响 NAV、费用、容量、收益贡献与策略判断。

契约：[docs/trading_costs.md](/Users/huozhihong/Documents/Quant/docs/trading_costs.md) 明确要求自然漂移和同一本资金账；[docs/strategy_decision_replay.md](/Users/huozhihong/Documents/Quant/docs/strategy_decision_replay.md) 明确要求从 position_daily 读取真实逐票权重。应让正式入口使用同一有状态引擎，并为正式入口建立经济行为回归测试。

### TW-2 / P1：正式多空收益把空头腿成本加回，方向还会翻转成本

位置：`src/backtest/quintile_v2.py:219-221`。

`group_ret` 已分别扣费，随后 `(net_top - net_bottom) * direction`，因此空头腿费用以正号回到组合；direction=-1 还改变费用方向。

复现：所有股票价格不变，两组建仓各付 10 bps。Q1=Q2=-0.001，v2 LongShort=0；经济预期为 -0.002。正式研究展示的扣费后多空绩效由此偏高，且原设计明确禁止此公式。应使用定向毛价差减两腿成本。

### TW-3 / P1：模拟盘按方向先卖后买，却没有按实际成交日排序，可使用未来现金

位置：[src/papertrading/runner.py:507](/Users/huozhihong/Documents/Quant/src/papertrading/runner.py:507)、`:553-559`、`:603`、`:614-635`。

pending 全部 SELL 优先，每只股票再独立寻找各自首个有效开盘。若卖出股票某日缺报价，卖出会落在之后的日期，而另一个买单仍落在较早交易日；共享 cash 已包含未来卖款。

复现：周一决策，现金 0，持有 A 10 股；周二 A 无报价、B=100，周三两只=100。周三补跑，代码先成交周三 SELL A 10，再成交周二 BUY B 10，最终现金显示 0；按真实日期回放周二现金为 -1000。`_state_from_fill_ledger` 也不拒绝这段负现金路径。应按实际成交时间推进账户状态，SELL 优先仅限同一成交日。

### TW-4 / P1（已声明但未安全隔离的边界）：模拟盘跨拆股版本继续使用旧股数，产生虚假亏损

位置：`src/papertrading/runner.py:100-125`、`:460`、`:858`、`:914-936`；[src/papertrading/target.py:230](/Users/huozhihong/Documents/Quant/src/papertrading/target.py:230) 读取最新版本的 execution_close。

成交账本冻结 quantity 与成交价；后续轮次直接用新版本 split-adjusted close 乘旧 quantity，没有按版本调整股数/成本或拒绝单位变化。数据组已独立核对 [src/data/foundation.py:904](/Users/huozhihong/Documents/Quant/src/data/foundation.py:904) 附近 `_rebase_parent_to_fetched_scale` 会在新拆股后缩放旧历史 OHLC 与 volume，故不同 immutable version 的数量单位会变化。

复现：旧版本 100 股×100，现金 0，权益 10000；2:1 拆股后的最新可执行价 50，经济股数应为 200。实际 `_state_from_fill_ledger` 仍输出 100 股，`_mark_equity` 正常发布 5000 权益。没有经济损失却出现 -50%。

[docs/paper_trading_operations.md](/Users/huozhihong/Documents/Quant/docs/paper_trading_operations.md) 明确承认尚未包含拆股处理，不能将其描述成作者已承诺券商级 corporate actions。问题在于这个已知边界没有检测/阻断，普通持仓穿过拆股后会静默生成错误账户状态。至少须检测单位变化并停止发布，或实现跨版本拆股调整与公司行动账。

### TW-5 / P2：SQLite frame 读取不是一致快照，正常并发更新会被当成损坏

位置：`src/storage/app_db.py:465-488`，连接在 `:136-138` 设置 `isolation_level=None`。

get_frame 分两次 SELECT 读 metadata 与 rows，没有 BEGIN 读事务。另一个连接可在两次 SELECT 之间提交新的整张 frame；前一代行数/哈希与后一代数据拼接，读者抛 row-count/checksum mismatch，即使每一代都完整有效。网页与 paper worker 正常并发即可触发。

复现通过连接代理在 metadata.fetchone 后同步提交第二个真实 SQLite 写事务：旧表 1 行，新表 2 行，get_frame 报 `SQLite frame row count mismatch: paper/test/equity`；紧接着无并发重读得到合法 2 行。应把 metadata 与 rows 放入同一读事务快照。

### TW-6 / P2：同一交易日日结重复运行总会碰撞不可变消息，不能幂等重试

位置：[src/papertrading/notifications.py:405](/Users/huozhihong/Documents/Quant/src/papertrading/notifications.py:405)、`:477-489`；`src/papertrading/notification_state.py:159-171`。

日结 ID 固定为 paper-daily:session，但每次 stage_daily_summary 重建 payload 都生成当前微秒 timestamp；状态库把同 ID 的新 hash 判为不可变内容冲突。即使账户完全未变、第一次只是排队尚未发送，第二次日结命令也在 drain 前报错。

复现使用无 active 账户、禁用实际投递的 service，对 2026-01-05 连续调用两次：首次 True，第二次 `Paper notification identity already exists with different immutable content: paper-daily:2026-01-05`。应重用已冻结 payload，或先判断 ID 是否存在再构建新的内容。

### TW-7 / P2：因子未来收益审计页面/API 处理非空审计时缺 json 导入

位置：[src/webapp/routes.py:238](/Users/huozhihong/Documents/Quant/src/webapp/routes.py:238)。

`_json_records` 调用 json.loads，但该模块没有 import json。空数据返回 [] 因而基础无产物烟测不暴露；存在实际审计数据时抛 NameError。`/api/factor/{name}/outcomes` 的非空记录、单池 factor 页中的异常收益记录均会经过该辅助函数。

复现：实际 `_json_records(pd.DataFrame([{'ticker':'AAPL'}]))` 抛 `name 'json' is not defined`。应补导入并覆盖非空审计产物的页面/API 测试。

### TW-8 / P2：最大回撤漏算初始本金到首个收益的损失

位置：`src/backtest/metrics.py:95-98`；[src/visualization/plots_mpl.py:113](/Users/huozhihong/Documents/Quant/src/visualization/plots_mpl.py:113) 与 [src/visualization/plots_plotly.py:444](/Users/huozhihong/Documents/Quant/src/visualization/plots_plotly.py:444) 采用同类计算。

累积净值先计算第一天收益，然后以该值开始 cummax，初始净值 1 没有进入高水位。第一次观测亏损被当成新的起点，所有以该点为历史峰值的回撤偏小。

复现：收益 [-10%,-10%]，实际本金 1→0.9→0.81，最大回撤应 -19%，当前函数给 -10%；[-10%,0] 返回 0。会影响回测 MaxDD/Calmar 和风险图。应将初始本金纳入高水位。

### TW-9 / P2（正式路径暂被 PIT 市值门禁挡住）：双排序四价格入口传错开盘变量

位置：[src/backtest/double_sort.py:265](/Users/huozhihong/Documents/Quant/src/backtest/double_sort.py:265)。

函数已将显式 execution_open_df 解析为 execution_open，但调用有状态模拟器仍传可选旧参数 open_df。正式 run_mvp 只传四个显式价格，open_df=None，因此第一笔交易因缺成交价格报错；补了 PIT 市值后正式双排序仍不可用。

复现：完整非空四价格、PIT date×ticker control、n_control=1/n_factor=2，旧 open_df 未提供，报 `Missing execution price for required backtest trade ... ticker=A side=BUY`。当前无 PIT market cap 会提前阻断，所以不要描述成今天每个正式研究任务都会在此失败。修正传入解析后的 execution_open。

### TW-10 / P2：合法名称中的撇号破坏删除按钮，名称还可成为点击时执行的脚本

位置：[src/webapp/templates/strategy_list.html:38](/Users/huozhihong/Documents/Quant/src/webapp/templates/strategy_list.html:38)、`strategy_detail.html:18`、`backtest_list.html:62`、`paper_list.html:53`、`paper_detail.html:23`。

模板把用户名称直接放进 onclick 中的 JavaScript 单引号字符串，只依赖 Jinja HTML 自动转义。浏览器解析属性时会把实体解码回单引号，然后才编译 JavaScript，因此 HTML 转义不能保护这里的 JS 字符串。策略创建 API 接收原名称；`src/strategies/definition.py:65-70` 仅限制名称非空及长度，普通 `O'Brien` 是允许输入。

真实 Jinja 模板渲染后，经 HTMLParser 提取并解码属性，再用 Node vm 执行该原始 handler：`O'Brien` 产生 `SyntaxError: missing ) after argument list`，所以该策略删除按钮不可用；名称 `x');globalThis.auditMarker=1;//` 使测试 VM 中 auditMarker 变成 1，证实点击时脚本注入。仅替换 deleteStrategy 为无操作函数，没有访问页面、外部网络或执行真实删除。脚本为 `repro_template_names.py`、`repro_template_names.js`，结果在 `repro_template_names_result.json`。应使用 data 属性和事件监听器，或同时正确处理 HTML 属性和 JavaScript 序列化两层上下文。

## 未证实疑点与限制

- 快照 upsert 会读取先前 Parquet 并生成新 hashes，似乎没有先验证原 manifest，可能把损坏的历史行重新合法化；本轮未执行验证，不列为已证实。
- 有历史 asof 参数的 paper 轮次从全量未来 fills/cash_events 重建账户，没有先按 asof 截断；属于历史回放可见性风险，未做完整账户级最小复现，暂不单列。
- Breakout 自定义 Watchlist/UI 参数与 allow_build=False 的无结果路径已交叉反馈给 signals_groups 代理，由该代理独立复现和报告。
- 全量项目测试由根代理在隔离副本执行。没有启动真实服务、访问线上应用/券商、调用行情服务或 Discord。没有浏览器视觉验收；前端全量通读不等于跨浏览器端到端运行。
- 文档中夹带的既往部署时间/通过数量不是本轮事实依据；本轮结论来自源代码和本轮新生成合成数据。
