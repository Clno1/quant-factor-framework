# 文档导航

更新日期：2026-08-09

不要按文件名字母顺序硬读。建议按下面路径进入。

## 第一次理解系统

1. [`../README.md`](../README.md)：项目能力、入口和最短启动方式。
2. [`code_reading_guide.md`](code_reading_guide.md)：按调用链阅读代码。
3. [`project_architecture.md`](project_architecture.md)：进程和模块边界。
4. [`unified_data_storage.md`](unified_data_storage.md)：DuckDB、Parquet、SQLite 与完整数据流。
5. [`data_foundation.md`](data_foundation.md)：行情 writer、质量门禁和 reader。

## 多因子研究

- [`research_universe_redesign_requirements.md`](research_universe_redesign_requirements.md)：研究池、目标池、跨池因子结论、NASDAQ100 和网页改版需求基线。
- [`research_universe_redesign_implementation.md`](research_universe_redesign_implementation.md)：本次改造的代码落地、当前阻断、正式发布门槛和 SG 上线顺序。
- [`factor_data_explorer_requirements.md`](factor_data_explorer_requirements.md)：按日期截面和单股历史查询 raw、clean、单因子排名的产品与数据契约。
- [`factor_data_explorer_implementation.md`](factor_data_explorer_implementation.md)：查询服务、API、页面、严格版本门禁、本地验收与 SG 上线条件。
- [`factor_preprocessing.md`](factor_preprocessing.md)：raw、去极值、中性化和 z-score。
- [`factor_confidence.md`](factor_confidence.md)：IC、ICIR、统计置信和稳定性。
- [`point_in_time_universe.md`](point_in_time_universe.md)：PIT 股票池契约。
- [`sp500_pit_and_daily_publication.md`](sp500_pit_and_daily_publication.md)：SP500 每日发布任务。
- [`trading_costs.md`](trading_costs.md)：回测和模拟盘费用、滑点与成交约束。
- [`strategy_decision_replay.md`](strategy_decision_replay.md)：策略决策回放。

## 模拟盘

- [`paper_trading_operations.md`](paper_trading_operations.md)：账户运行、账本和 fail-closed 条件。
- [`trading_costs.md`](trading_costs.md)：成交成本模型。

## 运维

- [`sg_operations_overview.md`](sg_operations_overview.md)：当前 SG 目标拓扑和已知差异。
- [`sg_manual_deploy_3a52611.md`](sg_manual_deploy_3a52611.md)：提交 `3a52611` 的 SG 手工备份、部署、数据重发与验收命令。
- [`server_daily_runbook.md`](server_daily_runbook.md)：日常检查、日志和发布命令。
- [`singapore_server_deployment.md`](singapore_server_deployment.md)：通用首次安装模板。

## 独立研究域

- [`momentum_breakout_summary.md`](momentum_breakout_summary.md)：日线动量突破总览。
- [`momentum_breakout_data_flow.md`](momentum_breakout_data_flow.md)：从定时入口到版本日线、分钟确认和 SQLite 的完整代码线路。
- [`premarket_discord.md`](premarket_discord.md)：盘前摘要数据闸门和 outbox。
- [`intraday_momentum_monitor_design.md`](intraday_momentum_monitor_design.md)：分钟 shadow 监控设计。
- [`market_turning_signals_research.md`](market_turning_signals_research.md)：大盘顶底研究方案。
- [`market_regime_research_implementation.md`](market_regime_research_implementation.md)：大盘研究实现。
- [`market_regime_effectiveness_screening.md`](market_regime_effectiveness_screening.md)：有效性筛选。
- [`market_regime_research_code_audit.md`](market_regime_research_code_audit.md)：研究代码审计。
- [`group_analytics_benchmark.md`](group_analytics_benchmark.md)：板块分析性能基线。

旧迁移脚本说明、旧 root/Discord 超长手册和早期 group analytics 需求稿已经删除。需要追溯时使用
Git 历史，不再把过期操作指南放在当前阅读路径中。
