# 从零代码审查：数据、PIT、因子与统计链路

基线：`/Users/huozhihong/Documents/Quant`，HEAD `1149764`。仅本轮读取源码、配置契约及测试；未读取历史审查报告、本地记忆或旧修复结论；未修改业务代码、真实数据或外部系统。

## 完整阅读与方法

- 本代理逐文件完整读取所有分配的 [src/data](/Users/huozhihong/Documents/Quant/src/data)、[src/factors](/Users/huozhihong/Documents/Quant/src/factors)、[src/preprocessing](/Users/huozhihong/Documents/Quant/src/preprocessing)、[src/analysis](/Users/huozhihong/Documents/Quant/src/analysis)、[src/research_universes](/Users/huozhihong/Documents/Quant/src/research_universes) 与脚本：73 文件、31,625 行，准确清单 `data_factors_read.json`。
- 协作代理 signals_groups 补充完整读取 25 个测试/fixture、7,141 行，清单 `data_factors_tests_read.json`，说明 `data_factors_tests_report.md`。两部分覆盖原分配 98 文件、38,766 行。
- 未以函数名、`rg`命中或目录扫描替代全文阅读。检索仅用于读完后的跨模块定位。
- 合成复现 `repro_data_factors.py` 与结果 `repro_data_factors_output.json` 位于本目录。因环境依赖尚在安装，先使用 bundled Python 的 pandas/numpy，通过 AST 加载原始完整函数体；缺失依赖所在的无关 imports 被剔除，生产函数逻辑未修改。Calendar 使用业务日日历模拟，选取真实月末/交易日。宽表复现仅替换 I/O；广域合并执行的是原脚本两条 `combined` 赋值语句。此阶段是逻辑单元复现，不能称为完整生产管线或真实 FMP 数据验证。
- 已随后完成正常 import 的原生验证，脚本 `repro_data_factors_native.py`，结果 `repro_data_factors_native_output.json`。运行时 `/private/tmp/quant_fresh_audit_20260905/light-venv/bin/python`，cwd和源码为独立副本`repo_data`。使用真实 pandas、XNYS、DuckDB、Parquet 和原始模块，未替换核心依赖或逻辑；所有输入为本轮生成的合成数据。5项均复现，DF1实际经过quality gates并生成PUBLISHED版本，DF3实际经过普通Writer发布和Reader读回，DF5确认当前真实factor fingerprint已改变但checkpoint仍接受旧分区。

## 框架和整体流程

1. `fmp.py` 将 full endpoint 的拆股调整 OHLCV 与 dividend-adjusted endpoint 的总收益 close 合成为 canonical bars；`price_semantics.py` 明确分离执行价和总收益价，并推导总收益开盘价。
2. 普通 `MarketDataWriter` 获取完成的 XNYS session，拉取有限 overlap，重基准父版本历史，验证数据、PIT及支持基准，写 immutable parquet/manifest，通过 DuckDB 原子更新 published pointer。Reader 校验 parent/child hash 后提供宽表和 typed prices，研究代码不能回退旧原始缓存。
3. 核心 SP500/NASDAQ100 由 source-backed 增删事件逆推 PIT，MAG7 是固定名单。安全主数据保留当前及历史成员和基准支持资产，分类来源明确标记最新快照或 PIT。
4. 广域 Security Master 使用稳定 security_id、有效期 ticker aliases、退市和 issue continuity；无法证明历史的实体受批准的 research history policy 限制。回填按 security batch/year，发布整合为 monthly parquet；广域日更专用脚本用 EOD bulk 补父版本之后的日期。
5. `US_LIQUID_5M` 从月末价格与 ADV20 筛选普通股票，保存完整月末名单和稀疏 forced exits，与 coverage/master 版本绑定。因子按 security_id 和月份分块，保留充分 warmup，raw factor 在coverage上算，clean仅PIT成员；factor-data和正式研究门禁分开。
6. `run_mvp` 基于发布版本构造可交易/成员mask，计算raw与预处理因子，审计forward outcomes，计算IC/HAC，按 next-open执行分组回测，可选size双重排序，并输出confidence/FDR及审计发布。
7. 普通和广域 observation service 为只读 explorer，通过manifest、generation、数据version、PIT和security identity约束查询；research_universes按primary/secondary/reference组织跨股票池对照。

## 已证实问题

### DF1 / P1：广域日更直接拼接不同复权基准，拆股或分红制造假收益

主定位：`scripts/update_us_equity_coverage.py:806-810`；相关 `:437-492`（只抓parent之后日期）、`:879-895`（宣称增量总收益语义）。

- 普通 writer 有 `_rebase_parent_to_fetched_scale`；广域专用日更没有相同处理。旧月份只保留/重写原有数值，新日期由 EOD bulk取得，`pd.concat([old,delta])` 后去重便发布。
- `src/data/fmp.py:994-1005` 及 `price_semantics.py:1-12`明确OHLC为split-adjusted、adj_close为总收益复权close。parent在corporate action发生前已冻结，新增数据处于新基准，不能直接拼接。
- 复现：无市场盈亏的2:1拆股，母版100、下一交易日50，原合并保留 `[100,50]`，真实`PriceSemantics.total_returns`输出 `-0.5`，经济收益应为0。现金分红1元的100→99亦输出 `-1%`而非0。
- 门禁不拦：`broad_coverage.py:151-210`仅行内OHLC/数值/日历；`:746-949`为重复、空值、非正值、OHLC上下界、日历、未来行、覆盖率和身份presence。`:1007-1037`仅认证parent语义manifest并执行这些gate。上述两行都是有效OHLC、身份完整且目标覆盖100%，不存在跨日复权连续性检查。
- 原生验证进一步确认：真实`split_coverage_bar_quality`隔离0行，8项`_validate_partitions`全部通过，真实`BroadCoverageStore.publish_partitions`成功生成PUBLISHED child，Reader读回总收益仍为-50%。
- 影响：广域因子值、波动率、总收益及后续研究被污染。历史月份fingerprint还会继续复用错误基准；后续常规append无法修复。该问题不适用于已具备overlap重基准的普通writer。

### DF2 / P1：PIT一美元门槛使用后见拆股调整价，历史成员随未来拆股改变

主定位：`src/data/derived_universe.py:454-469,493`；契约声明`:917`为`UNADJUSTED_CLOSE`；上游`fmp.py:994-1005`和`backfill_us_equity_coverage.py:270-290`实际供应split-adjusted close。

- `_evaluate_month_end`以输入`close`作为`selection_price`直接比较固定美元门槛，没有还原当日名义价格。
- 复现同一历史2024-01-31：AAA当日价格2元、量300万、ADV600万元，合格；以后10:1拆股使提供商历史变为0.2元、量3000万，ADV仍600万元，当前代码对同一天将资格改为False。
- 这是独立于DF1的缺陷：即使完整canonical重建解决收益连续性，历史价格门槛仍会受到之后发生的split/reverse split影响，导致PIT股票池前视偏差。
- 需使用当日真实名义价格或带生效日期的split ledger恢复可知历史；不能仅把manifest的`UNADJUSTED_CLOSE`标签改名。

### DF3 / P2：MAG7默认研究把支持基准QQQ当成第八只成员

主定位：`scripts/run_mvp.py:243-260,300-306`；产生链路`foundation.py:1705-1733`、`access.py:599-628`、`foundation.py:2530-2580`。

- Writer按registry为MAG7加QQQ支持基准，并将其写入bars和metadata，标识`is_current_member=False`。这是合理的基准支持数据。
- 默认研究不传ticker子集；bundle加载完整8列宽表，MAG7无PIT membership；`run_mvp`只在`--universe N`限制模式使用current members，默认取all_version_members并把缺省成员mask全部置True。
- 原metadata builder→原wide-table builder→原PIT缺省分支复现：8列含QQQ，metadata显示QQQ当前成员False，但QQQ的全部因子成员mask为True。
- 原生验证实际`MarketDataWriter.update_universe('MAG7')`使用本地合成fetcher发布后，`Reader.load_membership`为None，`load_wide_tables`含8列，`load_universe(current_only=True)`明确只有7只；随后成员mask中QQQ为True。
- 影响：MAG7横截面标准化、排名、IC、分组、因子观察掺入QQQ。MAG7是reference，confidence评分默认不发布，因此优先级低于主广域价格错误；并不代表SP500/NASDAQ100具有同样PITmask问题。

### DF4 / P2：没有新事件的月中PIT增量误报“没有合格股票”

主定位：`src/data/derived_universe.py:415-416`；调用`:1049-1067`；脚本`build_us_liquid_pit.py:202-230`。

- 增量先取得合法`initial_active`，只重建refresh_start之后的完整月末/退出事件。若区间没有事件，`membership_rows=[]`，helper把“没有新事件”误判成“没有任何合格证券”，抛出DataFoundationError。
- 复现：Jan31两只合格股票，前版Feb22，目标Feb23，refreshFeb1，无退市。增量抛`no securities passed broad PIT eligibility`；相同数据和目标做完整重建通过。
- 精确适用条件：增量路径要求Security Master generation一致。标准整链日更通常会发布新master并转全量，故不能称为每个月日常管线必然失败；同一master下回补多个target或直接调用增量API才命中。测试只覆盖Feb末→Mar末有事件的情况。

### DF5 / P2：广域因子断点续跑不绑定预处理参数，可能混合不同方法生成的月份

主定位：`scripts/run_broad_factor_data.py:141-156,578-598`；比较[src/factors/broad_pipeline.py:241](/Users/huozhihong/Documents/Quant/src/factors/broad_pipeline.py:241)。

- 单个新分区fingerprint包含`CONFIG.preprocessing`，但是整轮checkpoint identity未包含预处理/因子参数。恢复已completed分区时只验证已有parquet hash后continue，没有计算当前fingerprint。
- 复现将真实配置键`preprocessing.winsorize_n`从3改成1：调用原`_checkpoint_identity`所得identity完全相同，原`_load_checkpoint`接受包含旧completed分区的断点。
- 原生验证改变仅当前进程`CONFIG['preprocessing']['winsorize_n']`、运行完恢复：真实`factor_input_fingerprint`哈希改变，但`_checkpoint_identity`完全相同，`_load_checkpoint`仍接受旧completed。
- 若某月完成后中断，修改参数再`--auto-resume`/`--generation-id`续跑，已完成月保留旧处理、后续月用新处理，一个publication内部混合计算规则。旧文件hash正确不能证明与当前方法匹配。
- 不应以“fingerprint已有preprocessing”判定安全，因为completed分支在计算fingerprint之前退出。

## 已检查但未列为确定主问题

- `pit.load_point_in_time_membership`丢弃`snapshot_type`，compact forced exit可被当成完整快照。但当前广域生产主要用DerivedUniverseStore + membership_override，未证明直接生产触发，暂不升级为核心发现。
- `ic.compute_ic`先intersect列，完整缺失outcome列可能静默缩小横截面；主线生成factor/returns轴通常一致，未取得主路径触发证据。
- frozen factor矩阵路径可被后续publication替换、paper持仓数量是否需要跨split基准换算已反馈其他代理，其系统级影响由相应负责人验证。
- double_sort四价参数分支与quintile_v2问题交叉核对：run_mvp确实调用四价接口；size双重排序目前受PIT market cap gate阻挡，不能将未开启路径说成默认必然失败。
- 普通writer的分红/拆股重基准测试存在且与DF1不是同一实现。协作代理完整读取测试后未发现对DF1–DF5的反证，亦未发现对应边界回归覆盖。

## 执行限制

已对关键API正常import、DuckDB/Parquet实际质量门禁完成验证，所有测试写入本轮临时目录。复现证明代码在明确的输入下产生错误，不声称真实仓库历史数据中已经发生这些事件；没有拉取网络数据或触碰真实catalog。广域更新主脚本的网络fetch流程没有整轮运行，精确合并语句由第一份AST脚本验证，其后生产quality/publication/reader用第二份原生脚本端到端通过。
