# 因子数据浏览器实现说明

更新日期：2026-08-09  
状态：本地功能与回归验收完成；正式数据重发、SG 部署和生产性能验收待完成

## 1. 用户入口

左侧导航的“研究”组新增“因子数据”：

```text
GET /research/factor-data
```

页面有两个视图：

- 日期截面：某个正式交易日中，完整 PIT 股票池内所有历史证券的 raw、clean、单因子排名和百分位；
- 单股历史：某只股票在 factor generation 全区间内逐日的 raw、clean、排名、百分位和 PIT 状态。

因子详情和研究股票池详情可以进入该页面。旧 `/stock/{ticker}`、旧 `/api/stock/{ticker}`、旧
`single_stock.py` 和旧单股模板已经删除，不再保留 FMP 临时重算或 raw 升序排名口径。

## 2. 代码边界

| 文件 | 职责 |
|---|---|
| `src/factors/observations.py` | 校验 publication、generation、行情版本和 PIT；派生 rank、percentile、quintile |
| `src/webapp/research_routes.py` | meta、snapshot、history、CSV 和页面路由 |
| `src/webapp/templates/factor_data.html` | 两个视图的结构与可访问控件 |
| `src/webapp/static/js/factor_data.js` | URL 状态、查询、表格、Plotly、分页和 fail-closed 状态 |
| `src/webapp/static/css/style.css` | 桌面、移动端、宽表滚动和状态样式 |
| `tests/test_factor_observations.py` | 方向、PIT、版本篡改、并发切换、API、导出和旧入口回归 |

Web 路由不读取 Parquet 路径，也不自己计算排名。页面、JSON API 和 CSV 全部经过同一个
`FactorObservationReader`。

## 3. 严格数据流

```mermaid
flowchart LR
    WEB["因子数据页面/API"] --> READER["FactorObservationReader"]
    READER --> PUB["research_publication.json"]
    PUB --> FACTOR["raw/clean Parquet + factor manifest"]
    PUB --> VERSION["显式 DatasetVersion"]
    VERSION --> MARKET["DuckDB catalog + immutable market Parquet"]
    MARKET --> PIT["PIT membership + security metadata"]
    FACTOR --> DERIVE["clean × direction"]
    PIT --> DERIVE
    DERIVE --> RESULT["rank / percentile / quintile"]
```

每次冷读取会校验：

1. 当前正式 publication 的 schema、状态、股票池和截止交易日；
2. 因子 generation ID、manifest SHA-256、raw/clean 文件哈希和矩阵严格对齐；
3. publication 绑定的明确行情 version，不读取另一个 latest；
4. bars、universe、membership、manifest 四类行情版本完整性；
5. 当前日期的 PIT membership；
6. 因子注册表方向与 generation manifest 中方向一致。

查询返回前再次核对 publication 身份。若查询期间发布切换，完整重试一次；仍变化则返回
`409 PUBLICATION_CHANGED`。缓存键包含 universe、publication、factor、generation、factor
manifest 和 dataset version，不能跨版本复用。

## 4. 排名口径

```text
eligible = 当日 PIT 成分 AND clean 为有限数
oriented = clean × direction
rank = oriented 降序排名，ties=min
percentile = oriented 升序百分位，ties=average
```

- `rank=1` 永远表示按预设方向最优；
- 正向因子 clean 越高越优，负向因子 clean 越低越优；
- 同值共享数学排名，同排名只用 ticker 升序确定显示顺序；
- 搜索、状态筛选和分页发生在完整截面排名之后，不改变分母；
- rank 不写 SQLite，由 clean、direction 和 PIT membership 确定性派生。

## 5. API

```text
GET /api/research/factor-data/meta
GET /api/research/factor-data/snapshot
GET /api/research/factor-data/history
GET /api/research/factor-data/export
```

snapshot 与 history 共用同一逐行构造函数，所以同一个 `date × ticker` 的 raw、clean、rank、
percentile、quintile、PIT 和状态完全一致。CSV 每行附带 publication ID、factor generation ID、
dataset version ID 和 factor manifest SHA-256。

## 6. Fail-closed 行为

下列情况不会调 FMP、不会读旧 `data/raw/ohlcv`、不会换股票池，也不会换成另一个行情版本：

- 研究未发布、已过期或完整性无效；
- 因子不在当前 publication；
- raw/clean 或 PIT 文件哈希不匹配；
- 日期不是该 generation 的正式观测日；
- 股票从未出现在该 generation；
- 查询过程中 publication 切换。

页面显示中文业务状态。非交易日会展示前后正式观测日并允许显式跳转；NASDAQ100 未发布时会
说明仍需 PIT、行情版本和因子研究发布。

## 7. 本地验收

- 完整测试：`399 passed`；
- 隔离的已签名 publication 样本：正向/负向排名、PIT 加入退出、分页、单股历史、图表、日期回跳通过；
- 本地真实数据：SP500/MAG7 为完整性无效，NASDAQ100 未发布，页面和 API 均 fail closed；
- 浏览器控制台：无未处理错误；
- 1280px 页面无整体横向溢出；
- 390px 页面无整体横向溢出，宽表仅在自己的滚动容器内横向滚动，筛选器和按钮均在视口内。

## 8. 尚未完成的生产门槛

在 SG 宣布可用前必须：

1. 按当前四哈希合同重发 SP500 正式行情版本和 8 个因子 generation；
2. 解决 NASDAQ100 PIT 来源不一致，发布其正式 PIT、行情和 8 因子研究；
3. 发布 MAG7 当前参考结果和跨池结论；
4. 部署精确代码版本并重启 `quant-web.service`；
5. 抽查一个正向因子、一个负向因子和一个 PIT 加入/退出证券；
6. 在 SG 实测 snapshot/history 冷缓存 p95 小于 2 秒、热缓存 p95 小于 500 毫秒；
7. 检查 Web journal 无 500、哈希回退或未处理前端错误。

正式数据未重发前，页面显示红色/黄色门禁状态是正确行为，不得临时放宽校验来制造可用结果。
