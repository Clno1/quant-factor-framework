# 从零代码审查：breakouts / live / alerts / group analytics / premarket

审查源码为根仓库 HEAD 1149764。已全量逐行阅读分配的 81 个源文件/脚本/测试，共 27,218 行；`signals_groups_read.json` 逐文件记录行数及 SHA256。另接手 data_factors 的 25 个测试/fixture，共 7,141 行，独立记录于 `data_factors_tests_read.json`。未引用本地记忆、历史审查结论或运行结果缓存；未更改业务代码或真实行情数据，也未运行真实 worker、抓取行情、投递通知。

## 框架与整体流程

1. 日线突破模块通过版本绑定的 `BreakoutDailyDataset` 读取发布日线。`US_ACTIVE` 映射至 `US_LIQUID_5M`，广域适配器绑定父级覆盖版本、PIT 成分、Security Master generation 及校验和。scanner 对至少 65 行日线计算 20D 收益、ADR、流动性、均线、整理结构、pivot 和状态。application 负责股票池选择、市场过滤及扫描缓存，Web 只读既有缓存。
2. 小时告警先取广域日线候选，并合并 watchlist/paper holdings 强制关注名单，再读批量报价生成当天临时日线，通过严格门槛后按信号等级排序；SQLite 保存已观察/已投递等级，Discord 发送摘要。可选分钟增强只处理有限数量候选。
3. 分钟监控冻结上一 XNYS 交易日的日线候选与数据契约，以最多 40（硬上限 60）个活跃标的为核心，轮询 REST 报价和一分钟 OHLCV。rolling 聚合开盘区间、VWAP、均线及相对量；主突破 detector 与独立日线杯型/5m 柄型 detector 输出版本化信号。SQLite 保存候选、状态、观察周期和 outbox；多个完整 shadow 交易日决定是否具备 live 晋级资格。
4. 历史事件研究在每个信号日仅给 scanner 截至当日的历史，发生状态转换且经过 cooldown 才生成事件；下一交易日开盘入场，指定 horizon 后开盘退出，以 total-return open 计算收益，并计算 MAE/MFE、删失和年度/市场状态汇总。
5. Group analytics Stage 1 为 SP500/FMP 当前分类的 EOD 行业/子行业描述统计。分类缓存带 reviewed group ID、来源、时间与 hash；ticker 去重可按 issuer override 合并多股类，代表股优先用 t-1 市值，缺市值退至 t-1 ADV60。收益按统一 XNYS 相邻交易日计算，缺失不填充；组内采用 MAD winsor ROBUST_EW，按成员/价格覆盖和质量门槛排榜。发布使用不可变 generation、完整一致性校验、原子 latest pointer 和独立 attempt ledger；显式历史 asof 属于 research dry-run，strict PIT 请求被拒绝。
6. Premarket 将重计算 prepare 与 09:20–09:29 ET sender 分离。动量和行业摘要都要求精确 T-1 数据、覆盖和来源门槛；两个频道独立状态与重试。file lock、SQLite durable outbox、请求截止保护、不确定结果不自动重发，共同维护投递边界。dry-run 只写临时预览。

## 已识别问题及最小验证

所有合成输入和断言在 `repro_signals_groups.py`。已在 `/private/tmp/quant_fresh_audit_20260905/light-venv/bin/python` 中以隔离 repo 为 cwd 顺序执行全部 8 项，exit code 0；`--only NAME` 可单独运行。使用真实业务模块与真实 pandas/SQLite，仅供应商/股票池等输入使用合成数据或 Mock，没有模拟核心计算实现。实际输出记录于 `repro_signals_groups_results.json`。

### SG-1 [P1] 正在形成的一分钟线首次入库后，最终 OHLCV 永久无法更新

- 位置：`src/breakouts/live/rolling.py:59-64`，调用链 `src/breakouts/live/service.py:243-251` 和 `src/breakouts/live/feeds/fmp_rest.py:84-90`。
- merge 只保留索引从未出现的新行；同一分钟的后续完整值/供应商修订都直接丢弃。初次 merge 允许当前未收盘分钟，`completed_frame` 只在读取时排除，因此并不能避免提前冻结。
- 触发：10:00:08 接受时间戳 10:00 的 partial close=105、volume=100，10:01:08 再接受同 timestamp 最终 close=95、volume=1000。当前代码 merge 返回 0，已完成分钟及指标仍为 105/100，可错误判定突破、VWAP、相对量和均线。
- **已用实际 unmodified rolling.py 与真实 pandas 复现**：`{"correction_merge_count":0,"retained_close":105.0,"expected_final_close":95.0,"retained_volume":100.0,"expected_final_volume":1000.0,"metrics_last_price":105.0}`。
- 已有 `test_forming_minute_is_excluded` 只验证当时不读取 partial；`test_duplicate_and_out_of_order_exact_bars_are_reconciled` 只验证完全相同副本与补缺 timestamp，没有覆盖同 timestamp 更新。
- 修复方向：拒绝尚未完成的输入，或支持同 timestamp 更新并使派生指标失效重算。

### SG-2 [P2] Web 自选列表和绝大多数筛选选项没有可完成的扫描路径

- 位置：`src/breakouts/application.py:389-410`。Web 两个入口 `src/webapp/breakout_routes.py:78-94,202-225` 固定 `allow_build=False`；路由仍向模板提供 watchlists，UI 提供 view/asof/阈值等控件。
- watchlist 永远跳过缓存读取，`allow_build=False` 紧接着必然抛 `BreakoutScanNotReadyError`；即便后台显式 build watchlist，409 行也不存缓存。
- 扩大范围：缓存参数包括 view/asof/市场标的及各阈值，`scripts/refresh_us_active.py:466-479` 仅预生成一组固定默认参数（US_ACTIVE、view=all、asof=None、QQQ 等）。修改 READY view 或任一阈值、选择其他股票池的 UI 请求，没有常规后台预生成路径；页面让用户等下次发布也不会解决。
- 验证 `watchlist`：合成已准备好的缓存响应在 US_ACTIVE 成功，watchlist 请求 cache 调用次数为 0 并报未就绪。已在 light-venv 真实模块复现。
- 修复方向：让 UI 可提供的请求有明确受控计算/预计算路径，或针对不可变默认扫描进行适用的内存筛选；watchlist 应有版本绑定缓存或小规模受控执行路径。

### SG-3 [P2] MAE/MFE 纳入开盘退出后的整个交易日

- 位置：`src/breakouts/historical_backtest.py:136-141`。
- 收益退出价使用 exit_position 的 open，但 path_low/high 切片到 exit_position+1，包含退出当天 high/low，泄漏交易结束后的波动进入持有期风险指标。
- 合成：信号日后100开盘入场，持有当天 low99/high101，次日100开盘退出，退出后才 low50/high150。真实持有期 MAE/MFE 为 -1%/+1%，当前计算为 -50%/+50%，gross_return 仍 0%。验证 `outcomes` 已在 light-venv 真实模块复现。
- 修复方向：只包含入场日至退出前一日完整日线，并单独考虑退出开盘价。

### SG-4 [P2] 全部信号都在数据末日时，删失结果汇总直接崩溃

- 位置：`src/breakouts/historical_backtest.py:103-109,265-268`。
- 没有下一开盘时 `_event_outcomes` 提前返回，只写 entry_censored，未写任何 hN 字段。若结果全部为此类事件，summary 的 `events.get(hN_net_return)` 为 None，`pd.to_numeric(None)` 为标量 NaN，调用 `.dropna()` 抛 AttributeError。
- 合成：81个交易日，只在第81日输出 BREAKOUT。预期一条 entry-censored、零 realized observation；当前 public `backtest_breakout_frames` 会报错。验证 `censored` 已在 light-venv 真实模块复现。
- 修复方向：entry censored 也填完整各 horizon schema，汇总对缺列使用 index 对齐的空/NaN Series。

### SG-5 [P2] 小时扫描把某只股票的旧报价写成其他股票报价所在的新交易日

- 位置：`src/alerts/engine.py:304-307,323-341,361-364`，缺 timestamp 的 fallback 为161-165行。
- 整批报价取 max timestamp 推断 session_date，循环处理每只股票时却都使用这个统一 quote_date，未验证该股票报价是否同日/过期/未来。来自停牌、稀疏成交或缓存滞后的前日快照会追加为当天日线，并进入严格筛选与告警等级观察。
- 合成：两股票拥有截至7月27日的80日真实形状合成 OHLCV；STALE 报价时间7月27日15:59，FRESH时间7月28日10:30。默认严格阈值下 STALE 能被标为 data_date=7月28日并 base_pass=True，而 quote_timestamp仍为前日。验证 `stale_quote` 已在 light-venv 真实模块复现。
- 修复方向：以明确当前 XNYS session 校验每条 quote 的日期和年龄，再决定临时日线；缺时间戳应列为 unavailable。

### SG-6 [P2] 柄型零量使放量倍数变成 Infinity，产生假 MATCH 并让监控持久化失败

- 位置：`src/breakouts/live/cup_handle.py:461-482,494-501`，持久化`src/breakouts/live/state.py:21-28,785`，调用`src/breakouts/live/service.py:394-405`。
- handle 平均量为0时 breakout_volume_ratio 被设为 inf。只要几何和价格突破成立，即使 breakout本身量也为0，放量判断依然通过。inf还原样写入details/pattern；SQLite写入使用 `json.dumps(allow_nan=False)`，导致整个 cup cycle 抛 ValueError。即使信号被其他条件 REJECTED，details含inf仍使cycle保存失败。
- 合成：使用仓库已确认可MATCH的杯柄几何fixture，前6条baseline量保留100，后6条handle及breakout全设0。实际 MATCH + inf，保存cycle报 Out of range float values are not JSON compliant: inf。验证 `zero_cup_volume` 已在 light-venv 真实模块复现。
- 修复方向：量无效/不足应输出可序列化的 UNEVALUABLE/REJECTED，零分母不应表达为无限放量。

### SG-7 [P2] 配置的 reviewed group ID 映射路径被默认服务忽略

- 位置：`src/group_analytics/service.py:208-211`；可配置路径`src/group_analytics/settings.py:17,76-78`；provider接受此参数`src/group_analytics/adapters.py:157,164-167`。
- service持有自定义settings，却默认无参数构造 `FMPCurrentClassificationProvider()`，所以它总读仓库固定 fmp_group_ids.yaml。自定义映射无法生效，运行配置/hash却声称使用所配置的路径。
- 合成：将有效reviewed YAML复制到临时目录，只把version改为audit-custom-v2；直接provider(path=...)用v2，service(settings)仍使用2026-07-16。验证 `mapping_ignored` 已在 light-venv 真实模块复现。
- 修复方向：构造默认provider时传self.settings.group_id_mapping_path。

### SG-8 [P2] 可选基准缺少发布版本时，行业模块仍整体失败

- 位置：`src/group_analytics/adapters.py:799-825`，默认契约[src/group_analytics/settings.py:43](/Users/huozhihong/Documents/Quant/src/group_analytics/settings.py:43)为require_benchmark=False。
- 默认市场adapter把SP500股票数据和外部SPY基准放在同一 required-version try里。即使股票数据完整、配置允许无benchmark，coverage基准发布缺失仍抛 PublishedMarketDataError，无法到达service的可选benchmark逻辑（425行后）。
- 合成reader可返回有效SP500股票两日日线，但请求US_EQUITY_COVERAGE时抛DataFoundationError。验证 `optional_benchmark` 已在 light-venv 真实模块复现。具体症状为fresh install/局部发布中SP500可用而基准publication尚不存在；不要将此结论泛化为损坏基准artifact必须被忽略。
- 修复方向：将missing optional benchmark与股票主输入缺失区分，并将require_benchmark策略传入adapter或服务协调层。

## 未列为已确认漏洞的事项

- live rolling 的5m源分钟少于5但有成交被测试明确允许，测试声称仅使用已观察成交；不单独将这一设计认定为缺陷。
- 分钟SQLite outbox假定单进程发送者（下一claim把现有SENDING改UNKNOWN）。未证明部署会并发启动同一监控，因此只视为适用前提。
- legacy小时worker缺少与premarket相同的durable outbox/unknown状态，但代码未宣告相同交付保证；不把较弱恢复能力直接扩充为确定业务bug。
- 当前分类非PIT已被代码显式标记，历史asof限制也存在；不因出现当前分类本身而声称程序未防未来泄漏。
- premaket模块在本次逐行审查中未发现同等可证明的业务逻辑错误；此不等于无缺陷保证。

## 执行证据

- 全部 8 个复现断言均通过，已确认当前实现存在所述现象（并非期望行为通过）。
- Python 运行环境为临时 light-venv，未更改业务仓库和真实数据；第三方依赖通过已下载 wheels 与经批准的最小依赖安装。
- `repro_signals_groups_results.json` 为本次完整模块运行输出记录，`repro_signals_groups_components.py` 另保留先前依赖不全时的原函数组件验证。
- 本报告不声称生产实际已经发生所有问题，只声明列出的合成触发条件可使当前实现复现问题。根代理负责全量回归测试结果。


## 既有测试运行

临时 light-venv 中对13个相关文件运行 pytest，结果 **184 passed，56 subtests passed，1 warning，2.59s**；测试文件清单和runtime/cwd记录于 `signals_groups_test_run.json`，原始日志为 `signals_groups_pytest.log`。唯一 warning 是 pandas 后续版本对 object fillna downcast 的兼容提示。最初附加 websocket probe 测试因为缺少 websockets 而在收集阶段失败，剔除该文件后完成上述运行；不将缺依赖记为代码缺陷，也不声称此项测试已通过。

此结果与8项合成缺陷复现可以同时成立：现有回归覆盖主要路径和已有门禁，尚未覆盖这些特定边界。
