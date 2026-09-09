**2026-09-05 审查问题修复结果**

根仓库基线为 `1149764d13539da6159ab0e927594d49d1f181f0`。本次完成27项代码缺陷修复；另1项原先声明不支持的自动拆股调账，已加入强制保护，跨版本单位变化时禁止继续记账。代码与测试保留在本地工作区，尚未提交、推送或部署。原审查报告和复现证据未改写。

全部修复后，全量测试 **723 passed，0 failed，0 skipped**，耗时28.25秒；相对审查基线增加62项测试。两条警告来自现有Starlette/httpx及anyio兼容接口弃用。`compileall`和`git diff --check`通过。测试在独立临时仓库和合成数据上执行，使用真实DuckDB、Parquet、SQLite、XNYS、FastAPI TestClient；含引号名称的测试使用Node执行真实模板中的JavaScript事件处理器。未读取真实账户来重放成交，未发送通知，也未触发线上数据重建。

完整测试明细及修改后源码哈希见本目录 `repair_verification/full_suite.xml` 与 `repair_verification/verification.json`。计划与逐项状态见 [repair_plan.md](/Users/huozhihong/Documents/Quant/reviews/2026-09-05-fresh-audit/repair_plan.md)。

| 审查编号 | 修复后的行为 | 主要回归依据 |
|---|---|---|
| TW-1 | 正式四价格v2入口使用共同的有状态资金账，持仓随收益漂移，再平衡生成实际买卖；收益、费用、持仓和回放共享同一结果 | A/B价格分化后的权重、80元再平衡交易、每日贡献与账面恒等式 |
| TW-2 | 两种因子方向的多空收益都扣除两条腿的费用 | 零市场收益下，两腿手续费均造成亏损 |
| TW-8 | 回撤指标及两种绘图库纳入初始本金 | 首日亏10%、再亏10%，最大回撤为19% |
| TW-9 | 双排序实际使用显式传入的执行开盘价 | 只提供四价格参数也产生正确成交与持仓 |
| DF1 | 广域日更重新抓取重叠窗口，以security_id验证各价格/成交量调整比例，并重写受影响历史月份；无锚点或非一致修订时阻断 | 拆股、分红经过真实发布与读回后不制造假收益；缺失/非一致锚点失败 |
| DF2 | 新增独立的unadjusted_close用于历史名义价格门槛；收益仍使用原有总收益序列，ADV仍用一致调整后的价量 | 未来10:1拆股不改变历史2美元股票的资格；缺少可靠名义价格时阻断 |
| DF3 | 无PIT成员表的静态股票池按is_current_member过滤，基准单独保留 | 实际MAG7发布可含8只支持资产，研究bundle只含7名成员，QQQ基准仍可用 |
| DF4 | 无新成员事件时延续既有PIT状态，保留窗口之前的退出事件 | 无事件的增量结果与完整构建一致 |
| DF5 | checkpoint绑定预处理与因子参数、类和输入契约 | 改动winsorize参数后拒绝恢复旧月份 |
| TW-3 | 成交按实际可执行日期推进，同日先卖后买；现金事件按生效日入账；中断重试不能把后来的现金投入过去日期 | 周三卖出无法资助周二买入；未来分红不能资助过去；订单投影失败重试；同秒成交维持账本顺序 |
| TW-4 | 持仓及未完成订单保留执行价锚点，跨数据版本检查同一历史日期的单位是否变化；变化或无法验证即报错 | 100→50的拆股单位变化被阻断，普通市价下跌不误报；自动拆股调账仍不支持 |
| TW-5 | frame元信息及数据行来自同一SQLite读事务 | 在两次读取之间并发覆盖frame，读取端仍返回完整旧快照 |
| TW-6 | 同日日结重用首次冻结的消息内容，重试不受生成时间变化影响 | 重复准备和发送中再次准备不产生冲突或重复消息；成交消息仍严格校验内容 |
| TW-7 | 补齐Web收益审计的json导入 | 非空记录与空值正常序列化 |
| TW-10 | 用户名称放入转义的data属性，事件处理器读取dataset | 五个模板上的单引号、双引号及脚本形状字符串均保持为名称 |
| SG-1 | 接受已有分钟线修订并重新计算派生指标 | 已计算105价格被最终95价格和1000成交量替换 |
| SG-2 | 自选池与非默认查询可提交持久化后台任务；缓存绑定参数、行情版本和自选池快照 | 网页读取不执行扫描；队列去重、后台生成、缓存命中、版本/快照变化失效、中断恢复及有限重试 |
| SG-3 | MAE/MFE只包括退出开盘前的持仓路径，并包含退出开盘跳空 | 退出当天后续50/150极值不污染结果，真实退出跳空仍计入 |
| SG-4 | 无法入场的事件补齐各期限删失字段，汇总允许全部删失 | 1个删失事件、0个已实现观察正常汇总 |
| SG-5 | 逐条验证来源时间，拒绝无时间、旧交易日及相对批次最新报价明显滞后的记录 | 昨日报价、同日落后1小时及缺时间均不进入新日信号；默认最大批次时间差900秒 |
| SG-6 | 缺乏有效基准、柄部或突破成交量时拒绝信号，不产生Infinity | 三种零量边界均可保存真实监控周期 |
| SG-7 | 默认分类提供者使用配置中的映射路径 | 自定义映射版本实际进入服务 |
| SG-8 | 缺少可选基准时保留有效股票统计；必需基准缺失及任何已发布版本损坏仍失败 | 缺失与校验错误分别处理，基准空值及诊断如实保留 |
| MR1 | AP按唯一预测阈值合并同分样本 | 四个同为0.5的分数，在所有正负例排列下AP均为0.5 |
| MR2 | 配置隔离期不得短于最大标签窗口；直接筛选入口也检查候选窗口 | 不安全配置被拒绝，真实XNYS日期下开发结果窗口不进入封存期 |
| MR3 | 全PIT覆盖率按配置声明的依赖可得日期计算，继续保留95%门槛 | 完整1990–2026数据及1993起SPY可通过；上市后缺失前缀仍被计入并拒绝 |
| MR4 | 普通prepare识别已有Cboe文件过期，刷新后重新验证 | 自动下载一次；新文件仍旧则报错并保留旧缓存 |
| OPS1 | 读取端独立检查快照年龄，展示采集失联事件、过期状态和跨页面提示 | 超过180秒后降级，原始SQLite字节不变；新快照发布后自动恢复 |

代码入口可从 [quintile_v2.py](/Users/huozhihong/Documents/Quant/src/backtest/quintile_v2.py:33)、[update_us_equity_coverage.py](/Users/huozhihong/Documents/Quant/scripts/update_us_equity_coverage.py:462)、[papertrading/runner.py](/Users/huozhihong/Documents/Quant/src/papertrading/runner.py:515)、[breakouts/application.py](/Users/huozhihong/Documents/Quant/src/breakouts/application.py:434)、[market_regime_research/pipeline.py](/Users/huozhihong/Documents/Quant/src/market_regime_research/pipeline.py:132)、[operations/store.py](/Users/huozhihong/Documents/Quant/src/operations/store.py:700)查看。

**旧产物与运行方式**

1. 宽基历史需要按新数据口径重新构建。顺序是：使用与Security Master一致的目标交易日运行 `scripts/backfill_us_equity_coverage.py`，通过质量门槛后发布coverage；随后执行 `scripts/build_us_liquid_pit.py --full-rebuild --publish`；再执行 `scripts/run_broad_factor_data.py --full-rebuild --publish`；最后重新生成依赖这些版本的研究/回测产物。新方法为 `BROAD_COVERAGE_V3_NOMINAL_PRICE` / `US_LIQUID_5M_PIT_V3_NOMINAL_PRICE`。旧断点不应继续恢复；旧Parquet可读取，但缺失名义价格不能充当新PIT输入。安全主数据自身的历史证明门槛仍然有效，不能因本次修复而跳过。
2. 名义价格使用FMP的 `historical-price-eod/non-split-adjusted` 来源；这个接口的官方说明是保留实际历史价格而不做拆股调整。[FMP官方接口说明](https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-non-split-adjusted)。本次没有调用真实账户API验证套餐权限或全市场覆盖；返回缺失时会明确失败。历史回填获取全窗口名义价格，日更主要补新完成月末，避免每天为全部证券逐只重复拉取。来源和覆盖范围写入发布lineage。
3. v2收益、费用、回撤、MAG7横截面及事件MAE/MFE发生口径修正，旧报告保留历史证据，需要重新运行才能得到修正后的数字。市场筛选算法版本升为 `0.1.4`，杯柄算法升为 `daily-cup-5m-handle-shadow-v3`。改动前后的结果不应混成同一实验。
4. 自定义动量查询由已有 `scripts/run_data_requests.py` 后台入口处理，每轮数据请求后最多构建1个扫描，失败最多尝试3次。部署环境需运行既有的 `quant-data-requests` 定时任务；Web返回“已提交后台”，随后刷新读取。队列持久化于 `data/cache/momentum_scans/requests`，绑定不可变行情版本及自选池快照。没有新增Web进程内扫描线程，也没有启用新的常驻服务。
5. 模拟账户的自动拆股调账没有在本次实现。触发 `PAPER_PRICE_UNITS_CHANGED` / `PAPER_PRICE_UNITS_UNVERIFIED` 后必须核对持仓、订单及公司行动依据再恢复；不能清空报错后直接沿用旧份额。已有错误成交也不会被改写。日结以首次成功入队内容为准，同日后续再次准备不会改写已冻结消息。
6. 运维快照允许年龄默认等于60秒采集间隔加120秒宽限。超过阈值后网页和API显式显示过期；`/healthz`仍以HTTP 200表达Web进程存活，同时通过 `status=degraded` 和 `snapshot_freshness`暴露采集失联。告警依据来自读取端，不依赖已停机的采集器运行。

**分支核验**

已执行 `git fetch --prune origin`，清除了远端已经不存在的 `origin/fix/research-integrity-20260822`。本次直接查询 `origin` 时，服务器仍返回 `main`、`master` 和 `cursor/document-main-branch-16f3` 三个分支；并非所有非main分支都已从该远端消失。

`origin/*`是远端跟踪引用，本地的`main`、`master`、`codex/*`及备份分支是独立分支。Fetch/prune会清理失效的远端跟踪引用，不会删除本地分支。其中`codex/research-integrity-followup`还在另一个worktree使用，显示上游`gone`符合Git预期。本次没有删除本地备份或占用中的分支。
