# 板块轮动 V3 持仓观测对账

日期：2026-09-12

状态：代码路径与合成门禁已锁定；**17 个 ETF 的实盘 FMP 探测仍待 SG 补做**。本环境没有可用的 FMP 凭证，不能把未实测的成分数量、权重合计或海外占比写成已完成对账。

相关代码：`src/group_analytics/rotation/holdings.py`、`scripts/observe_rotation_holdings.py`、`src/group_analytics/rotation/service.py` 的 overlay。

## 1. 接口

公开文档确认的当前持仓端点是 [FMP ETF & Fund Holdings](https://site.financialmodelingprep.com/developer/docs/stable/holdings)：

```text
GET https://financialmodelingprep.com/stable/etf/holdings?symbol=SPY
```

仓库客户端走 `_get("/etf/holdings", {"symbol": ...})`，与 V2 SMH 试点相同。文档把该接口描述为当前组合（资产、份额、市值、权重），**没有要求也没有返回持仓生效日**。响应里可能有 `updatedAt`，这只是供应商更新时间字符串，**不得改名为 `holdings_effective_at`**。

另有 v4 按日持仓（`/api/v4/etf-holdings` + `date`）和 portfolio-date 列表，那是历史成分快照候选，不是本阶段的“当前观测”口径，也**不能**据此声称已有 PIT 生效日。本阶段不调用 v4。

`/stable/etf/info` 提供当前基金档案（含 AUM 等），不是持仓名单，也不进广度。

## 2. 本环境做不到的活样本

计划要求在写业务代码前实测：

1. `/stable/etf/holdings` 对全部 17 个代理的可用性与响应完整性
2. 各 ETF 成分数量（请求成本与内存）
3. 权重合计是否落在 95–105
4. 海外上市成分占比
5. 配额消耗估算

本云端工作区没有 FMP key，上述五项不能用实盘数字填充。17 个代理名单来自 `proxy_etf_symbols()`：SMH、IGV、SKYY、CIBR、BOTZ、AIQ、XLK、XLF、XLV、XLI、XLY、XLP、XLE、XLB、XLU、XLRE、XLC。

V2 已完成的 **SMH 试点**（见 [V2 验证文档 §4](sector_rotation_v2_validation_20260909.md)）仍然是唯一实盘锚点：25/25 可映射、权重约 99.91%、等权 48.00%、加权 67.85%、`point_in_time=false`、`OBSERVATION_ONLY_NO_PROVIDER_DATE`。SG 上线后应对 17 个 ETF 各打一次 `/etf/holdings`，把成分数量、权重合计、`excluded` 原因追加到本文，而不是另写一份假装已完成的表。

## 3. 代码门禁（合成，必须保持绿）

`tests/test_group_rotation_holdings.py` 锁定：

- 双口径同时输出；等权与加权都可以独立为 0
- 权重合计越出 95–105 拒绝（沿用 `normalize_observation`）
- 海外/现金/无法映射进入 `excluded`，权重不消失
- `updatedAt` 永不成为 `holdings_effective_at`；`point_in_time` 恒为 false
- 观测超过 **14 个日历日** → `HOLDINGS_OBSERVATION_STALE`，**不使用旧持仓**
- overlay 写在行上的 `holdings_breadth`，**不写入** `production.breadth`，**不改变** `priority`
- 缺失观测时生产轨仍带 `ETF_HOLDINGS_NOT_LINKED`
- 等权 &lt; 60 只进入 `observation_gaps.LOW_PARTICIPATION`，不写回 `production.evidence_gaps`（避免污染 replay / 关联层）
- `validation.py` 不引用持仓观测；`make_panel` 继续拒绝 current-member 回看
- 带 overlay 的快照 `replay_snapshot` 仍为 MATCH（只比 production/compatibility）

## 4. 存储与排期

独立产物，不进价格 parquet：

```text
data/reference/group_analytics/rotation/holdings/<ETF>/<YYYYMMDDTHHMMSSZ>.json
```

日常 `run_rotation(..., holdings_root=)` **只读**最近一次观测，失败不阻断价格层。成员价格优先从独立缓存 `rotation/holdings_canonical/` 读取，缺失时才回退到当日已注入/主题 canonical 的重叠标的；**不得**把周更 refresh 写进主题 `rotation/canonical/`，否则会截断 61 根历史。

每周一 **12:00 America/New_York**（早于 17:30 价格层）跑 `scripts/observe_rotation_holdings.py --all --refresh-members`：

- `deploy/systemd/quant-group-rotation-holdings.timer` / `.service` / `-root.service`
- 独立 flock：`.rotation-holdings.lock`，不占用宽基 `.broad-production.lock`
- MemoryHigh=700M / MemoryMax=900M（成员 canonical 刷新比 30 只主题价格重）
- 单 ETF 失败继续其余 16 个；全部失败才非零退出
- 默认 `--max-members 120`，超过则该 ETF 失败而不是截断权重

未加入 `configs/operations.yaml`：现有 watchdog 的 `target_policy=latest_publishable_xnys` 按交易日对齐，周任务会在周二至周五误报 MISSED。先 systemd-only，避免假警报。

配额粗算（待 SG 用实盘 N 修正）：每周 17 次 holdings + 每成分 2 次 canonical OHLCV（`/full` + dividend-adjusted）。成分在 SMH/XLK 等之间重叠，实际 ticker 数低于 17×N。

## 5. 展示

主表「真实广度」对 ETF 显示「等权 / 加权 · 样本 n/N」，详情与方法区写 **当前持仓观测广度（持仓生效日未披露）**。参考版 70/30 仍只在 `compatibility`，`breadth_kind` 枚举只有 `member_above_ma` | `etf_holdings_observation` | `unavailable`，不设 `etf_trend_proxy`。

广度不进优先级（P2.4）。P5.2 对 RS20 的增量对照因此与 P1 相同：持仓观测不是历史标签输入。
