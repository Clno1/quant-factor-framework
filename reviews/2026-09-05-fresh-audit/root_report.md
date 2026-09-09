**市场研究、运维与公共配置的独立核验**

基线与主报告相同。全部相关生产代码、脚本、测试、配置、前端及systemd文件已阅读全文；重点对照实际调用关系，而非沿用文档对当前状态的判断。以下全部为P2：统计/运行错误是真实的，但完整PIT和非默认参数等条件必须保留。

**MR-1：Average Precision错误处理相同预测分数**

定位：[screening.py:354](/Users/huozhihong/Documents/Quant/src/market_regime_research/screening.py:354)。代码对概率稳定排序后逐行累计precision，未把同分预测作为同一个决策阈值处理。

四个概率都为0.5、两个正例两个负例时，分类器无法区分任何样本，阈值方式的AP应为正例比例0.5。正例排前面，程序输出1；正例排后面，输出0.4166667。统计量不应受并列预测的输入排列影响。

这不等于声称实际训练器会接受所有常量特征。真实链路的winsorization尾部、离散广度特征也会产生并列预测；该函数进入`_classification_metrics`，再影响Average Precision、PR lift与G4门禁。需按唯一预测分数聚合，再计算precision-recall增量，并测试对并列样本置换的不变性。

**MR-2：合法接受的隔离长度配置可让标签跨入封存区间**

定位：[settings.py:484](/Users/huozhihong/Documents/Quant/src/market_regime_research/settings.py:484)、[screening.py:253](/Users/huozhihong/Documents/Quant/src/market_regime_research/screening.py:253)。配置分别校验标签窗口与embargo为正，却没有校验两者关系。

设置`embargo_sessions=1`、最大标签窗口60，通过真实settings校验。真实XNYS日历下，最后开发特征日期为2021-12-30，它的60日结果窗口截至2022-03-28；2022-01-01起的数据仍被摘要宣称为`SEALED_NOT_EVALUATED`。训练/验证折边界存在相同问题。

上游已生成完整未来标签，后续仅按特征日期裁剪，未按结果区间终点purge。默认60/60不会触发；修改标签horizon或embargo才会。应根据最大结果窗口执行purge，或至少拒绝不安全组合。独立审查者核对未找到额外保护。

**MR-3：当前默认完整PIT研究的全历史覆盖门槛不可达**

定位：[pipeline.py:132](/Users/huozhihong/Documents/Quant/src/market_regime_research/pipeline.py:132)、[features.py:754](/Users/huozhihong/Documents/Quant/src/market_regime_research/features.py:754)。主索引从1990开始，SPY配置从1993-01-29开始；EW-CW必须使用SPY，整体研究仍reindex至主索引，再逐列按全区间计算非空比例。

给两只合成股票完整1990–2026价格和成员数据，仅让SPY遵守配置的上市起点。真实广度函数产生的EW-CW覆盖率为`0.915556999`，全PIT门禁要求0.95，拒绝整个研究。测试用两只股票只为缩小计算规模；这个缺失区间比例与股票数量无关，其他独立特征组用完整合成列隔离了影响。

当前PIT数据本身尚未通过上游门槛；本项意味着补齐PIT后仍有第二层确定阻断。准确范围是当前默认1990–2026区间，不能说未来所有结束日期都永远不可达。需分别定义特征可得历史、共同研究区间或替代基准契约；不能将真实缺数和合法上市前空值混成同一个比例。

**MR-4：普通prepare不刷新过期的Cboe文件**

定位：[sources.py:950](/Users/huozhihong/Documents/Quant/src/market_regime_research/sources.py:950)。已有文件且schema完整时，`volatility_downloaded=False`；后续只检查新鲜度，不重新获取数据。

合成价格完整到2026-01-06，Cboe完整schema只到01-05。调用默认`prepare_market_sources(..., include_credit=False)`，下载函数调用次数0，直接报`Cboe VIX is stale: expected 2026-01-06, got 2026-01-05`。使用`--force`可以绕过，但默认刷新入口应能识别旧日期并更新，行为也与同函数中价格缓存的刷新策略不一致。

失败关闭避免了把过期数据投入研究，因此这是刷新可用性缺陷，不应说它已经把旧Cboe数据伪装成新日发布。

**OPS-1：采集器停机后的旧绿色快照不会过期**

定位：[store.py:747](/Users/huozhihong/Documents/Quant/src/operations/store.py:747)、[app.py:274](/Users/huozhihong/Documents/Quant/src/operations_web/app.py:274)。读取侧检查快照是否存在，直接沿用持久化状态。配置的watchdog heartbeat宽限没有形成读取侧的快照过期判断，前端自动刷新也只会继续读取同一旧快照。

真实临时SQLite中给全部任务写入2000-01-03的SUCCESS快照，通过真实TestClient读取：采集器仍SUCCESS，`snapshot_at`仍为2000年，事件列表为空，`/healthz`返回ok。这不是说HTTP健康接口必须代表所有业务任务成功；实质问题是运维总览没有提醒自己的证据已经停止更新，连采集器自身也保持正常状态。

应在读取侧按当前时间检验快照年龄，独立显示采集失联/快照过期，不能让需要运行的采集器成为发现自己已停机的唯一机制。

**验证边界**

脚本`repro_root.py`使用真实模块和XNYS日历、合成表格、临时Parquet/SQLite及TestClient。只mock来源下载和时钟，不访问真实provider、账户、服务或通知。完整实验结果保存为`evidence/repro_root_result.json`；阅读明细包含本次实际代码版本哈希。
