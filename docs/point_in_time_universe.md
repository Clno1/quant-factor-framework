# Point-in-time 股票池

更新日期：2026-08-29

## 1. 为什么强制

动态指数不能拿今天的成分名单回测过去。否则退市、被剔除或经营恶化的公司会从历史样本消失，
产生幸存者偏差。

当前契约：

- `SP500` 和 `US_LIQUID_5M` 是动态池，必须有 PIT 成分。
- `MAG7` 被明确声明为固定研究池，可以没有 PIT。
- Watchlist 冻结用户创建任务时的 ticker 集合；它不宣称复原历史指数。

## 2. 正式文件

主 SP500 PIT 只从以下配置目录读取：

```text
data/pit_universes/SP500.parquet
data/pit_universes/SP500.metadata.json
```

Parquet 必需列：

| 列 | 含义 |
|---|---|
| `date` | 一份完整成员快照开始生效的 XNYS 交易日 |
| `ticker` | 标准化证券代码 |
| `active` | 该完整快照中的成员状态 |

示例：

```csv
date,ticker,active
2025-12-31,AAPL,true
2025-12-31,MSFT,true
2026-03-31,AAPL,true
2026-03-31,NVDA,true
```

每个日期是一整份快照，不是只列新增/删除事件。2026-03-31 的快照没有 MSFT，表示它从该日
起不再属于股票池。

## 3. 构建过程

```bash
python scripts/run_data_pipeline.py pit
```

`src/data/sp500_pit.py` 会：

1. 拉取供应商历史变更事件和当前 constituents；
2. 标准化 ticker 与事件日期；
3. 应用 `configs/sp500_pit_corrections.yaml` 中已经人工审核的修正；
4. 从固定研究起点重建每个交易日的完整快照；
5. 校验成员数量、事件一致性和最后快照；
6. 写候选诊断到 `data/raw/pit/`；
7. 只有全部通过才原子替换 `data/pit_universes/SP500.parquet`。

随后 `MarketDataWriter` 把本次 PIT 文件复制到对应不可变行情版本中。回测真正绑定的是版本内
冻结副本及其 checksum，不会受未来 PIT 修订影响。

## 4. 硬校验

系统拒绝：

- 动态池没有 PIT；
- 首个快照晚于研究起点；
- 同一 `date+ticker` 有冲突状态；
- 快照为空或成员数异常；
- ticker 非法或规范化后重复；
- 最后快照不是目标 session；
- 最后快照与当前 constituents 不一致；
- 历史 active ticker 不在行情中；
- PIT 每日行情覆盖低于配置门槛；
- 用 `--universe N` 截断动态池后发布研究。

## 5. PIT 解决了什么、没有解决什么

它解决“当时股票池里有哪些证券”。它不自动解决：

- 当时的行业分类；
- 当时可见的市值和基本面；
- 财报实际发布日期；
- 退市收益和 corporate action 细节。

因此市场市值中性化只有拿到真实 PIT 市值序列后才应开启，不能把今天的市值回填到过去。

## 6. 有效时间和知晓时间

当前指数成分快照表示 effective-close 状态，不等于公告何时被市场知道。事件表还没有完整的
`announcement_date/known_at`，因此回测不得在 T 日通过 `membership.shift(-1)` 读取 T+1 状态。
当前保守执行规则是：T 日收盘首次确认退出，T+1 开盘提交退出订单；若缺少开盘价，必须由版本
绑定的并购、破产或接管事件解释最终结算，否则 fail closed。

停牌或退市的累计终值只在退出证据可知日记入组合账，不向前倒写到最后交易日。未来接入包含
可靠公告时间的第二数据源后，才可以在满足 `known_at <= decision_timestamp` 时提前安排订单。
