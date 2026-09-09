# 行业风险、历史分类与基本面时点契约

## 当前报告

运行 `python scripts/update_industry_risk.py --benchmark SPY`。它只读取账户和行情供应商，不运行模拟盘、不下单、不改策略。

`--accounts-export` 可读取 SG 只读导出的 `{accounts: [{account, positions}]}`。当前观测按内容哈希保存在 `data/lake/industry_risk/observation_<sha256>.json`，`latest.json` 绑定文件哈希。页面 `/paper/<account_id>` 和 `/api/paper/accounts/<account_id>/industry-risk` 只读这些文件，禁止在 Web 请求中访问 FMP。

基准优先使用账户的 `industry_benchmark` 显式监测设置，其次使用命名研究池的注册基准；未指定基准的自选池保留缺失状态。报告 CLI 的 `--benchmark` 只覆盖本次报告，不修改账户。

已有 `run_paper.py` 正常运行结束后刷新当前风险报告；显式 `--asof` 历史运行不抓取当前标签。完整的当日观测会被复用，避免重复消耗供应商请求。报告失败只记录告警，不重放交易，也不改变交易运行的成功/失败结果。无需新增定时器。

- 行业权重 = 该行业持仓市值 / 账户净资产；股票资产内权重另列。
- 股票市值 + 现金必须与净资产核对一致。缺失估值不能当零。
- 未分类持仓进入 UNKNOWN；FMP 的 Cash & Others 也不能机械假设全部是现金。
- 基准完整但某行业不存在时，该行业基准权重才是零；整个基准缺失时是空值。
- 日期过旧、未来时间、分类不完整、基准不完整或分类体系不一致时不生成偏离数值。
- 当前基准接口未提供生效日期时，明确标记 OBSERVATION_ONLY_NO_PROVIDER_DATE，只作当前观测对照。不能把抓取时间冒充权重生效时间。

FMP 官方接口依据：

- https://site.financialmodelingprep.com/datasets/etf-mutual-funds （列出 `/stable/etf/sector-weightings` 和 `/stable/etf/holdings`）
- https://site.financialmodelingprep.com/developer/docs/stable/profile-symbol （当前公司 profile）

## PIT 分类输入

现有 `LATEST_KNOWN_BACKFILL_NOT_PIT` 数据不会被提升为 PIT。当前持仓观测只覆盖本次抓取股票，从观测时刻向前积累；不能据此宣称补齐全市场历史。

历史分类 CSV/Parquet 必须包含：

| 字段 | 含义 |
|---|---|
| security_id | 稳定证券身份，不能只依赖今天的 ticker |
| sector | 对应时点的行业类别 |
| effective_from / effective_to | 经济有效日期，闭区间；结束日期可空 |
| knowledge_date | 分类信息实际可得日期，不是抓取日期或任意回填日期 |
| classification_policy | PIT_EFFECTIVE_DATED，必须有来源支持 |
| taxonomy | 统一的分类体系与版本 |
| source / source_evidence | 数据来源和可复核证据定位 |

另提供同证券身份体系的 ticker 历史：`security_id, ticker, effective_from, effective_to`。区间采用现有 SecurityMasterStore 的闭区间约定，改名日前后不能重叠到不同证券。

日期级 `knowledge_date` 从下一交易日才可使用，避免把同日盘后信息放入收盘决策。后续修订只在当时已知的范围内生效；未来修订不能重写旧决策。冲突分类和不明确的证券身份报错。结构验证不能替代供应商来源的人工核实。

独立对照命令：

```bash
python scripts/run_industry_neutral_comparison.py \
  --universe SP500 --factor MOM_6M --start 2020-01-01 --end 2026-09-04 \
  --history /path/to/reviewed_classifications.parquet \
  --symbols /path/to/reviewed_symbol_history.parquet \
  --output outputs/industry_comparison/reviewed_run
```

输出 baseline_all、baseline_matched、industry_neutral 三套因子、IC 和分组回测。主要 A/B 比较使用相同因子有效样本与可评估日期；完整基线另列以揭示删样本影响。分组回测仍使用真实 PIT 成员、明确开盘成交价格、总收益归因价格和版本绑定基准。

该入口采用固定的 MAD 3 倍清洗研究口径，在审计中注明；不会改变生产 CONFIG、正式因子发布指针或账户仓位。分类覆盖不足或尚未提供源文件时输出 BLOCKED，不产生伪造的中性化结论。

## 未来估值与盈利因子

正式实现前，基本面观测至少绑定：稳定公司/证券身份、财务期间、财报原始数值与币种、公告/受理时间、可得时间、抓取时间、来源及证据、修订版本。

- 按 available_at 与交易决策时刻连接，不能按 fiscal_period_end 把财报提前使用。
- 保留原始公告与后续修订；历史模型只能消费当时已知版本。
- TTM 必须由当时已公布的期间组成；价格、市值与财务分母使用一致的证券和币种口径。
- 负盈利、负净资产、极小分母与缺失值制定单独规则，不靠 winsorize 掩盖无经济意义的比率。
- 金融、保险、REIT 等业务单独定义指标适用范围；不能直接套用一般企业的 EV/EBITDA、杠杆或利润率定义。
- 原始分数、行业相对分数、组合行业约束作为不同研究版本验证；行业中性化不等于组合行业配置中性。
