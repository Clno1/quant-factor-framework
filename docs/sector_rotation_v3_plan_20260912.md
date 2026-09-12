# 板块轮动 V3：迭代方案与开发计划

日期：2026-09-12

状态：**开发依据文档**。P0–P3 已在分支 `cursor/sector-rotation-v3-99b0`（PR #7）落地；P4 / P5 未开始。后续板块轮动的迭代严格按本文推进；偏离本文的改动需要先更新本文再实施。

适用范围：`src/group_analytics/rotation/`、`src/premarket_digest/rotation.py`、`src/webapp/group_analytics_routes.py` 及对应模板/静态资源、`deploy/systemd/` 中轮动相关 unit。

前序文档（结论仍然有效，本文不重复推导）：

- [V2 初次研究](sector_rotation_v2_research.md)
- [V2 公开源码审计修订稿](sector_rotation_v2_public_source_revision.md)
- [V2 实现与升级操作](sector_rotation_v2_upgrade.md)
- [V2 真实数据验证与上线门槛](sector_rotation_v2_validation_20260909.md)

本文合并了两条独立核查线的结论，两边一致的部分直接采用，冲突的部分在 §2 中标明并给出裁定。

---

## 1. 本次迭代要解决的问题

一句话概括当前状态：**算法比参考版更严谨，但严谨性被表达成了空白，因此可用性低于参考版。**

用户实际反馈的三个症状：

1. 页面显示"截至 2026-09-10 完整收盘"，怀疑数据滞后。
2. "真实广度"和"研究优先级"两列大量"未接入""等待确认"。
3. 整体感觉像玩具。

核查后，三个症状对应五个不同的根因，其中只有一个是"数据没接"，其余四个是**判断层与发布闭环的设计问题**，不需要任何新数据源就能修。

本次迭代的目标不是提高预测能力，而是：

> 先让它成为一个可靠、可核对、信息不互相覆盖的板块研究工具，再讨论预测能力。

这个定位由 [V2 验证文档](sector_rotation_v2_validation_20260909.md) 的结论直接决定：`promotion=NOT_APPROVED`，现有复杂状态筛选**没有**跑赢简单 RS20。所以本轮不增加任何新的评分复杂度。

---

## 2. 现状核查结论

### 2.1 时效性：截图数据是对的，但存在真实的 9 小时空窗

**更正**：此前一条核查把截图时间默认成了当前时刻，因而怀疑"周六构建失败"。实际核查时刻为北京时间 2026-09-12 约 01:28，即纽约时间 2026-09-11 约 13:28，美股当日尚未收盘（NYSE 常规收盘美东 16:00）。因此当时最新完整收盘日就是 **2026-09-10**，页面显示正确，没有漏交易日。

Discord 投递记录同样正确：

| 盘前目标日 | 使用数据 | 纽约时间发送 |
|---|---|---|
| 09-10 | 09-09 收盘 | 09:20:32 |
| 09-11 | 09-10 收盘 | 09:20:14 |

投递在 09:20 ET，此时美股未开盘，"上一个完整收盘"就是当下真实存在的最新完整数据。**Discord 没有额外滞后。**

但以下三个时效问题是真实的：

**(a) 构建排期距收盘约 9 小时。**

```5:9:deploy/systemd/quant-group-analytics-eod.timer
[Timer]
OnCalendar=Tue..Sat *-*-* 13:15:00 Asia/Singapore
Persistent=true
AccuracySec=2min
RandomizedDelaySec=30s
```

13:15 SGT = 05:15 UTC = 01:15 ET（夏令时）。美股收盘 16:00 ET = 次日 04:00 SGT。因此每个交易日存在固定的 **9 小时 15 分空窗**：新收盘数据已经存在，但要等到次日下午才进快照。对 UTC+8 的使用者，这覆盖了整个上午。

排在 13:15 的原因是要接在 11:30 SGT 宽基数据链之后做个股关联。但**主题价格层只需要约 30 个 ETF/个股的日线，不依赖宽基链**。这个依赖可以拆开。

**(b) 页面只在打开时读取一次**，长时间停留会持续显示旧快照，没有自动刷新或过期提示。

**(c) Discord 链接指向固定历史 run**，打开旧消息不会变成最新报告，页面也没有醒目区分"最新"与"历史"。

### 2.2 个股关联失败后不会重试（新发现的闭环缺口）

最新轮动快照记录 `candidate_linkage = unavailable`，但同日动量频道后来已成功生成报告 —— 轮动侧从未重新关联。

代码路径确认：`scripts/run_group_rotation.py` 只在构建时尝试一次 `CompletedSessionMomentumSource(...).load(source_session)`，失败即以 `unavailable` 发布，此后没有任何重试入口。`load_rotation_report` 在 09:20 ET 读取时也不重试。

所以只要 13:15 SGT 那一刻宽基数据没就绪，**当天盘前摘要就永久没有个股候选**，即使数据在 07:00 ET 之前已经恢复。

首次失败的具体原因需要另行核对当时的调用条件，不能笼统归因为"宽基停在 09-04"。

### 2.3 广度与优先级的结构性死结

```103:113:src/group_analytics/rotation/engine.py
    if symbols and (strict or not theme.proxy):
        ma = member_prices.rolling(20).mean()
        eligible = member_prices.notna() & ma.notna() if strict else member_prices.notna()
        n = eligible.sum(axis=1)
        result["breadth"] = (member_prices.gt(ma) & eligible).sum(axis=1).div(n.replace(0, np.nan)) * 100
        result["breadth_n"] = n
        result["breadth_expected"] = len(symbols)
    else:
        result["breadth"] = np.nan if strict else np.where(idx.isna(), np.nan, np.where(idx > idx.rolling(20).mean(), 70, 30))
```

生产轨道（`strict=True`）下，没有登记成员的主题广度直接为 NaN。

| 主题类型 | 数量 | 广度状态 |
|---|---:|---|
| 未登记持仓成员的 ETF | 17 | 无法计算真实成员广度 |
| 只有 3~4 只成员的篮子 | 3 | 可算，但永远达不到 `breadth_n >= 5` |
| 有 5 只成员的篮子 | 2 | 才可能满足现有广度门槛 |

```199:203:src/group_analytics/rotation/engine.py
        elif confirmed == "leading" and absolute_up:
            breadth_ok = row.breadth_n >= 5 and row.breadth_n >= row.breadth_expected * .8
            action = "priority" if breadth_ok and row.breadth >= 60 else "price_watch"
        elif confirmed == "improving" and row.abs5 > 0:
            action = "watch"
```

即 **22 个主题里只有 2 个（光通信、数据中心电力）可能进入最高档"优先核对"**。其余 20 个无论价格多强，封顶都是"价格领先／待广度"。

关于参考版的 70% 广度：其脚本对 ETF 采用 `价格 > 自身20期均线 → 70，否则 → 30` 的二值映射，不是成分股比例。参考截图中多个 ETF 同为 70% 与此机制一致。**我们应借鉴其清晰呈现，但不能把这种代理数字填进"真实广度"。**

### 2.4 算法反例：稳定领先被永久判为"等待"（最严重的设计缺陷）

```165:171:src/group_analytics/rotation/engine.py
    confirmed, pending, count, since = "unavailable", None, 0, None
    records = []
    for date, row in frame.iterrows():
        x = np.log1p(row.rs20 / 100)
        y = np.log1p(row.rs5 / 100) - (x - np.log1p(row.rs5 / 100)) / 3
        # Stable equality at the band edge, independent of log round-off.
        boundary = abs(x) <= .005 + 1e-12 or abs(y) <= .002 + 1e-12
```

设主题以恒定几何速率跑赢基准，记每日相对对数收益为 `c`：

```text
x  = ln(1 + RS20/100) = 20c
ln(1 + RS5/100)       = 5c
y  = 5c − (20c − 5c)/3 = 5c − 5c = 0
```

推导前提：`q = 主题指数 / 基准`，`RS_k = 100 × (q_t/q_{t−k} − 1)`，因此 `ln(1 + RS_k/100) = ln(q_t/q_{t−k})` 恰好等于 k 个交易日相对对数收益之和。恒定速率即该日收益恒为 `c`。

**`y` 对任何恒定速率的相对趋势都恒等于 0**，不是近似为 0，是构造上必然为 0。

数值验证：20 日跑赢 2% → `c = ln(1.02)/20 = 0.00099013`，`RS20 = 2.0000%`、`RS5 = 0.4963%`，`y = 9.4e-17` —— 机器精度量级，即严格为零。`boundary` 判定为 `True`。

（`δx = 0.005` 对应 `RS20 ≈ 0.501%`，即月度相对强弱死区约 ±0.5%。）

而 `boundary` 是 **OR** 逻辑，`|y| <= 0.002` 成立即整体落入边界带 → `candidate = "neutral"` → `action = "wait"`。

结论：**一个每天稳定跑赢、无缺数、无过度延伸的主题，会被永久标记为"等待确认"。** 而这恰恰是最值得优先研究的形态。

根因是把"没有加速"当成了"无法判断"。`y` 是加速度，稳定趋势的加速度本来就是零；对加速度设死区，等于系统性地排除所有最干净的趋势。

这不是阈值调参问题，是 X 轴与 Y 轴被 AND 压缩成单一状态造成的。

### 2.5 一天噪声抹掉已确认状态

```195:198:src/group_analytics/rotation/engine.py
        elif row.extension:
            action = "extended"
        elif boundary or pending:
            action = "wait"
```

`pending` 只要当日候选状态 ≠ 已确认状态就被置上，**哪怕它永远确认不了**。于是已稳定确认"领先且加速"的主题，出现一天反向波动，研究优先级立刻掉成"等待确认"。

截图中半导体标注"已确认：领先且加速 · 新状态待确认 1/2"，优先级却是"等待确认"，即此逻辑。

### 2.6 生产分数全部为空

```127:128:src/group_analytics/rotation/engine.py
    if strict and not amount_verified:
        result[["amount_proxy", "amount_ma20", "amount_ratio"]] = np.nan
```

```151:153:src/group_analytics/rotation/engine.py
        sufficient = r.history_valid & r.breadth.notna() & (r.breadth_n >= 5) & r.amount_ratio.notna()
        sufficient &= r.breadth_n.ge(r.breadth_expected * .8)
        r["score"] = r.score.where(sufficient)
```

`amount_verified` 默认 false → `amount_ratio` 全 NaN → `sufficient` 恒 False → **22 个主题生产分数全部为 null**。

锁的原因是真实的取数口径问题：

```29:31:src/group_analytics/rotation/service.py
            if fetcher is None:
                from src.data.fmp import get_historical_ohlcv
                fetcher = get_historical_ohlcv
            frame = fetcher(symbol, start, end, dividend_adjusted=True)
```

用 dividend-adjusted 端点，`adjClose` 被映射成 `close`，再用 `close × volume` 当成交额 —— 分红复权价 × 复权状态未核验的成交量，口径不成立。

**修法已在仓库内**：`src/data/fmp.py` 的 `get_canonical_historical_ohlcv()` 用 `/full`（拆股复权，价量经济一致）提供可成交 OHLCV，只从 dividend-adjusted 取 `adj_close`。切换 fetcher 后成交额口径即可合法成立。

### 2.7 两套模型并存，主站用的不是参考版复刻

`source_v1_compat` 是参考版规则的对照实现；主站展示的是我们另行设计的 `research_v2`。两者共存于同一快照，但 compat 的趋势文字、成交额倍数、五项积分、原版状态**都被藏在详情抽屉里**，主表只显示 research_v2 且其中三列为空。

这是"玩具感"的直接来源：参考版 5 个展示维度我们关掉 3 个，而关掉的那 3 个其实都已经算好了。

---

## 3. 设计原则（开发期间不可违反）

1. **一个标签只回答一个问题。** 强弱、速度、风险、数据完备度、个股确认必须分别可读，不得互相覆盖。
2. **缺失必须说明是哪一种缺失。** "等待""未接入""暂无候选"必须带可枚举的原因码。
3. **不用代理数字冒充真实测量。** ETF 70/30 趋势代理不进"真实广度"；`close × volume` 不叫资金流。
4. **观察资格与回测资格分离。** 无供应商生效日的持仓数据只能用于当日展示，`point_in_time=false`，不倒填历史、不进回测。
5. **不增加评分复杂度。** 在跑赢 RS20 基线之前，新维度只作为展示证据，不进入排序或选股条件。
6. **领域隔离不变。** `src/group_analytics/` 不导入 factors / backtest / papertrading / breakouts / alerts；核心域不反向依赖它。个股关联留在上层 `src/premarket_digest/rotation.py`。
7. **fail-closed 不变。** 快照校验失败不回退旧单日榜；不可变 run 不覆盖；失败不覆盖成功指针。
8. **不碰其他模块。** 多因子、回测、模拟盘、茶杯柄、EP 一行不动。

---

## 4. 目标信息架构

首页必须可靠回答三个问题：

> 谁持续领先？谁正在修复？谁持续落后？

然后把广度、净申赎、个股确认、风险作为**分别可核对的证据**并列展示。

### 4.1 主表列结构（目标态）

| 列 | 内容 | 来源 |
|---|---|---|
| 主题 | 名称 + 代理/篮子 | themes |
| 强弱 | 领先 / 持平 / 落后（X 轴） | research_v2 |
| 速度 | 加速 / 平稳 / 减速（Y 轴） | research_v2 |
| 5/20/60 日相对 | 数值 | research_v2 |
| 趋势 | 强 / 偏强 / 中性 / 偏弱 / 弱 | compat（升为主表） |
| 成交活跃 | 倍数 + 方向（放量相对走强/走弱） | P1 解锁后 |
| 真实广度 | 百分比 + 等权/加权 + 样本 N/期望 + 口径标签 | P2 接入后 |
| 净申赎 | 日/周净流入金额 + 数据日期 | P3 接入后 |
| 风险 | 延伸偏大 / 数据不足（可多个，独立标记） | research_v2 |
| 研究优先级 | 单一排序建议 | 由上述派生 |

### 4.2 强弱 × 速度九宫格

X 轴（月度相对强弱）与 Y 轴（近 5 日相对前 15 日速度）**独立输出，不再 AND 压缩**：

| | 加速 | 平稳 | 减速 |
|---|---|---|---|
| **领先** | 领先且加速 | **稳定领先** | 领先但降温 |
| **持平** | 持平转强 | 与基准同步 | 持平转弱 |
| **落后** | 落后但改善 | 稳定落后 | 落后且减速 |

死区语义修正：

- `|x| <= δx` → **持平**（不是"无法判断"）
- `|y| <= δy` → **平稳**（不是"无法判断"）

"边界"一词不再用于状态命名，只保留在审计字段中描述数值落在死区内。

这直接修复 §2.4 的反例：稳定领先 = 领先 + 平稳，是一个明确且有用的结论。

### 4.3 "等待"原因必须拆开

当前所有缺失共用"等待确认"。目标枚举：

| 原因码 | 含义 | 展示文案 |
|---|---|---|
| `ETF_HOLDINGS_NOT_LINKED` | 未登记 ETF 持仓 | 真实广度未接入 |
| `SMALL_BASKET` | 篮子仅 3~4 家 | 小样本（N 家） |
| `LOW_PARTICIPATION` | 广度已算但偏低 | 广度 X%，参与度低 |
| `STATE_CONFIRMING` | 新状态确认中 | 新状态确认 n/2 |
| `NO_QUALIFIED_CANDIDATE` | 扫描正常但无合格突破候选 | 扫描正常，暂无合格形态 |
| `LINKAGE_FAILED` | 扫描或关联失败 | 个股关联失败（原因码） |
| `INSUFFICIENT_HISTORY` | 不足 61 个连续收盘点 | 历史不足 |

### 4.4 用现有 09-10 数据的表达对照

| 主题 | 现有数据 | 当前显示 | 目标显示 | 证据来源 |
|---|---|---|---|---|
| 存储（相对 QQQ） | 5日 +4.01%，20日 +4.67%，60日 −13.39%；样本 3/3 | 价格领先／待广度 | 短中期修复 · 长期仍弱 · 仅 3 只篮子股票 | 页面截图，已核对 |
| 半导体（相对 QQQ） | 5日 +1.86%，20日 −2.17%，60日 −9.20% | 等待确认 | 短期改善 · 中期未转强 · 真实成员广度未知 | 页面截图，已核对 |
| 云计算（相对 QQQ） | 5日 −1.62%，20日 −1.62%，60日 +20.11% | 延伸偏大 | 长期领先 · 近期回落 · 延伸风险（RS60 > 18） | 页面截图，已核对 |
| 能源（相对 SPY） | 20日 +8.45%，60日 +16.97% | 延伸偏大 | 待补 RS5 与 dist50 后确定 | **待 SG 核对** |

前三行可由截图字段完整推导。**能源行不能直接采用**：`extension = (rs60 > 18) or (dist50 > 8)`，而 `rs60 = 16.97 < 18`，因此"延伸偏大"必然来自 `dist50 > 8`，该字段未在截图中给出；"近期降温"也需要 RS5，同样缺失。P1 开工前必须从 SG 快照补齐 `rs5`、`rs1`、`dist50`、`abs20`、`absolute_ma20` 再回填本行，不得凭两个数字给结论。

同样的数据，表达清晰度差距明显。这是 P1 不需要任何新数据就能拿到的收益。

---

## 5. 分阶段开发计划

六个阶段。每阶段独立可验收、可回退、可单独发 PR。**前一阶段验收通过才开始下一阶段。**

### P0 — 时效与发布闭环

**目标**：收盘后 1.5 小时内出快照；个股关联失败可重试；页面区分最新与历史。

**不改算法。**

#### P0.1 拆分构建阶段

`scripts/run_group_rotation.py` 增加 `--stage {price,linkage,all}`，默认 `all`（保持向后兼容）。

- `price`：执行 `run_rotation(...)`，用 `attach_candidates(snapshot, None, reason="个股关联待后续阶段")` 发布。不调用 `CompletedSessionMomentumSource`。
- `linkage`：不重新计算主题。从 `RotationStore.load()` 读取最新快照，校验 `source_session` 与目标一致，尝试关联，成功则重新 `publish`。
- `all`：现有行为。

**幂等与不可变性**：`store.publish` 以 `sha256(snapshot)[:16]` 生成 `run_id`，重新关联后 digest 改变 → 产生新 run，旧 run 原样保留；`latest.json` 的 `old["source_session"] <= new` 判断允许同日前移。**架构已支持，无需改 store。**

`linkage` 阶段必须满足：

- 若 `candidate_linkage.status == "available"` 且 `input_fingerprint` 未变 → 直接返回 `NOOP`，不产生新 run。
- 若动量报告仍不可用 → 记录失败原因码，**不覆盖**现有快照。
- 重复执行不产生重复 run。
- `store.load()` 返回值带 `run_id`，重新 `publish` 前必须 `snapshot.pop("run_id", None)`（现有 `run_group_rotation.py` 已有此处理，拆分阶段后不能丢）。

#### P0.1.1 重复 run 抑制（必做，否则每天产生冗余快照）

快照包含 `"generated_at": now.isoformat()`，因此**即使输入完全相同，每次构建的 digest 也不同**，会生成一个新的 `run_id`。在美股休市日、或同一交易日重复触发时，`price` 阶段会为同一个 `source_session` 反复产出内容等价的 run。

`price` 阶段发布前必须先比对：

```text
若 latest 快照的 source_session 相同
且 latest 快照的 input_fingerprint 相同
→ 跳过发布，返回 NOOP
```

`input_fingerprint` 已经是输入面板的 sha256（不含 `generated_at`），正是这个判断的正确键。休市日因此自然收敛为 NOOP，不需要在 timer 里单独判断交易所假日。

#### P0.2 定时器调整

新增 `deploy/systemd/quant-group-rotation-price.timer` / `.service`：

```ini
[Timer]
OnCalendar=Mon..Fri *-*-* 17:30:00 America/New_York
Persistent=true
AccuracySec=2min
RandomizedDelaySec=60s
Unit=quant-group-rotation-price.service
```

使用 `America/New_York` 让 systemd 自动处理夏令时，收盘后固定 1.5 小时。对应 SGT 夏令时 05:30、冬令时 06:30。

service 沿用现有资源约束（root、`flock` 宽基锁、`MemoryHigh=400M`、`MemoryMax=550M`、`CPUQuota=100%`），`ExecStart` 加 `--stage price`。

`quant-group-analytics-eod.timer` 保持 13:15 SGT，`ExecStart` 改为 `--stage linkage`。

在 `quant-premarket-prepare.service`（07:00 ET）之前追加一次 `--stage linkage` 重试，作为 `ExecStartPre` 或独立 oneshot。允许失败不阻断盘前流程。

`configs/operations.yaml` 增加 `group_rotation_price` job 登记，`enabled_expected: true`。

#### P0.3 页面时效表达

`src/webapp/static/js/group_rotation.js`：

- 头部改为三行：数据截至哪个收盘 / 快照生成时间 / 距今时长 + 下次更新时间。
- `run` 查询参数存在时，显著标注"固定历史快照"并提供"查看最新"按钮。
- `visibilitychange` 事件 + 5 分钟轮询，检测到新 `run_id` 时提示"有更新的快照"，由用户点击刷新，不自动跳变。
- `freshness !== "current"` 的黄色告警保留并加强。

#### P0.4 Discord

`src/premarket_digest/rotation.py` 的 embed：

- `url` 保留固定 run 链接（可追溯）。
- footer 或独立 field 增加 `/group-analytics` 最新入口。
- 明确标注"本消息数据截至 {source_session} 收盘，链接为该时刻固定快照"。

**交付物**：`--stage` 三态；2 个新 unit；operations 登记；页面时效区；Discord 双链接。

**测试**：

- `linkage` 阶段幂等（连续两次不产生新 run）
- `linkage` 在动量不可用时不覆盖现有快照
- `linkage` 成功后 `latest.json` 前移且旧 run 可读
- `price` 阶段不触碰 `CompletedSessionMomentumSource`
- `tests/test_systemd_units.py` 覆盖新 unit

**验收**：SG 上观察连续 3 个交易日，收盘后 1.5 小时内出快照；至少复现一次"13:15 关联失败 → 07:00 ET 重试成功"。

**回退**：`--stage all` 保持可用；停用新 timer 即回到原行为。

---

### P1 — 判断层重构（不需要任何新数据源）

**目标**：修复 §2.4 / §2.5 / §2.6 / §2.7。这是本轮收益最大的一步。

#### P1.1 X/Y 双轴独立输出

`src/group_analytics/rotation/engine.py` 的 `add_states` 重构：

```text
δx = 0.005   # 月度相对强弱死区（对数）
δy = 0.002   # 速度死区（对数）

strength_axis = "leading"  if x >  δx
                "flat"     if |x| <= δx
                "lagging"  if x < -δx

speed_axis    = "accelerating" if y >  δy
                "steady"       if |y| <= δy
                "decelerating" if y < -δy
```

新增快照字段：`strength_axis`、`speed_axis`、`strength_label`、`speed_label`、`combined_label`（九宫格中文名）。

**保留** `x`、`y`（当前只存 `acceleration_log`，需补存 `strength_log`）以便审计。

两日确认机制**只作用于 `strength_axis`**（月度强弱应当稳定），`speed_axis` 每日直出（速度本来就该敏感）。这一点必须在字段上体现：`strength_confirmed` / `strength_candidate` / `strength_confirmation_count` vs `speed_current`。

#### P1.2 风险标记独立

`risk_flags` 改为数组，不再与优先级互斥：

- `EXTENDED`：`rs60 > 18` 或 `dist50 > 8`（保留原阈值）
- `BELOW_ABSOLUTE_MA20`：`index <= absolute_ma20`
- `ABSOLUTE_DOWNTREND`：`abs20 <= 0`

能源那种"中期领先 + 近期降温 + 延伸风险"必须三条同时可见。

#### P1.3 数据完备度独立

`evidence_gaps` 数组，使用 §4.3 的原因码。与 `risk_flags`、优先级完全分开。

#### P1.4 研究优先级重新定义

优先级只做**排序建议**，不再吸收风险和缺失信息。按顺序判断，首个命中即停止：

```text
0. unavailable  数据不足    history_valid = false
1. focus        持续领先    strength=leading 且 abs20>0 且 index>absolute_ma20
2. defensive    相对抗跌    strength=leading 且 (abs20<=0 或 index<=absolute_ma20)
3. recover      正在修复    strength=lagging 且 speed=accelerating 且 abs5>0
4. weak         持续落后    strength=lagging
5. neutral      与基准同步  strength=flat
```

**完备性证明**（必须在测试中断言）：`history_valid=true` 时 `strength ∈ {leading, flat, lagging}`。leading 被规则 1/2 完全覆盖且互斥；lagging 被规则 3/4 完全覆盖；flat 由规则 5 覆盖。**无未命中分支。**

三点必须注意：

- `unavailable` **排在第一位**，不是最后。历史不足时其余字段无意义，先判断才能避免用残缺数据得出方向性结论。
- **没有 `cooling` 档**。"领先但降温"由 `speed_axis=decelerating` 独立表达，不占用优先级档位 —— 这正是"一个标签只回答一个问题"。降温的领先主题仍属 `focus`，排序时靠 RS20 和速度列自然下沉。
- `defensive`（相对抗跌）对应 [V2 研究稿 §6.3](sector_rotation_v2_research.md) 已确立的约束：相对排名第一但绝对趋势向下时只报"相对抗跌"，不进默认强势方向。此档不可省略，否则"跑赢但在跌"会被误读成可研究方向。

档内排序沿用当前默认的 RS20 降序，可切 RS5/RS60。优先级只决定分组，不决定组内顺序。

关键差异：

- **不再有 `breadth_ok` 门槛**。广度作为独立证据列展示，不再决定优先级档位。这解除 §2.3 的死结。
- **`pending` 不再覆盖优先级**。已确认状态照常给出建议，新候选以 `strength_confirmation_count` 角标呈现。这修复 §2.5。
- **延伸不再吞掉优先级**。能源仍是"持续领先"，同时挂 `EXTENDED` 风险标记。

#### P1.5 解锁成交额

`src/group_analytics/rotation/service.py` 的 `load_frames` 改用 `get_canonical_historical_ohlcv()`：

- `close/open/high/low/volume` 来自 `/full`（拆股复权，价量一致）→ 用于 `close × volume` 成交额
- `adj_close` 来自 dividend-adjusted → 用于相对收益计算

`metric_frame` 中相对收益继续用 `adj_close`，成交额改用 `close × volume`。二者在同一 frame 内共存，字段名必须区分。

**对账要求（必须先做，通过才改默认值）**：

1. 选 3 个有拆股记录的标的（含近两年拆股）、3 个有大额分红的标的
2. 拆股日前后各 5 个交易日逐日核对 `close × volume` 连续性
3. 对比 `adj_close × volume` 与 `close × volume` 的差异分布
4. 结果写入 `docs/sector_rotation_v3_amount_audit_<date>.md`

对账通过后，`amount_verified` 默认改为 `true`，`--amount-verified` 参数保留但反转为 `--no-amount-verified`。快照中必须记录 `amount_basis`（取值 `split_adjusted_close_x_volume`）与对账文档的 commit，使任一历史 run 都能追溯其成交额口径。

成交额方向标签按 [V2 修订稿 §5.1](sector_rotation_v2_public_source_revision.md) 的反例要求：必须写"放量相对走强/走弱"，需要写"放量下跌"时另行检查绝对收益。

**解锁成交额不等于恢复 0–100 分。** `sufficient` 还要求 `breadth.notna() & breadth_n>=5`，17 个 ETF 主题在 P2 之前仍然拿不到分数。而且按设计原则 5 与 §4.1 的目标主表，**生产 0–100 分在本轮全程保持不展示**；成交额解锁只服务于"成交活跃"这一展示列。开发时不要顺手把分数列加回主表。

#### P1.5.1 三个价格表与函数签名变更

`get_canonical_historical_ohlcv` 返回的 frame 里，`close` 是拆股复权可成交价，`adj_close` 是分红复权总收益价。当前 `service.py` 只向下游传 `prices`（取 `adj_close`）与 `volumes` 两张表：

```81:82:src/group_analytics/rotation/service.py
            prices[symbol] = frame.get("adj_close", frame.get("close", pd.Series(dtype=float)))
            volumes[symbol] = frame.get("volume", pd.Series(dtype=float))
```

必须新增第三张 `execution_close` 表并贯穿 `analyze()` → `metric_frame()`。相对收益继续用 `adj_close`，成交额改用 `execution_close × volume`。两者字段名必须区分，禁止复用 `prices`。

连带影响：`input_panel` 增加一张表 → `input_fingerprint` 改变 → `replay.py` 的冻结重放口径改变。**v2.1 旧 run 只能按 v2 规则重放**，`replay.py` 需按快照的 `schema_version` 分派，不得用 v3 规则重放 v2 快照并报告 MATCH。

#### P1.5.2 价格缓存必须失效（否则首次部署静默取到旧口径）

```20:45:src/group_analytics/rotation/service.py
def load_frames(symbols, start, end, *, refresh=False, cache_root=None, fetcher=None):
    """Group-owned cache; existing shared OHLCV files remain read-only fallback."""
    root = Path(cache_root) if cache_root else PROJECT_ROOT / "data" / "reference" / "group_analytics" / "rotation"
    shared = CONFIG.abs_path(CONFIG.data.raw_dir) / "ohlcv"
```

现存缓存 `rotation/<SYMBOL>.parquet` 全部是 dividend-adjusted 口径（`close == adj_close`），共享回退目录 `data/raw/ohlcv/` 口径同样未经核验。换 fetcher 后若沿用同一路径：

- 单只标的刷新失败时旧文件保留 → 与新文件混合口径
- 走共享回退的标的永远是旧口径
- 两种情况都**不会报错**，只会产出错误的成交额

必须做的处理：

1. 缓存路径加口径版本段：`rotation/canonical/<SYMBOL>.parquet`
2. 缓存文件内必须含 `price_basis` 标记，读取时校验，不符即视为缺失
3. 共享 `data/raw/ohlcv` 回退在新口径下**停用**（读不到就报缺失，不降级）
4. 部署时旧目录保留但不再读取，便于回退

#### P1.6 compat 维度升到主表

`analyze()` 已将 `compatibility: compat.iloc[-1].to_dict()` 存入每行快照，**纯前端改动**：

- `source_trend`（强/偏强/中性/偏弱/弱）升为主表"趋势"列
- compat 的五项积分与原版状态保留在详情"方法对照"区，标注"参考版规则对照，非本项目结论"
- 原版状态名（资金撤出／拥挤主升／派发）**只在对照区出现**，不进主表

#### P1.7 Schema 版本

`src/group_analytics/rotation/__init__.py`：`SCHEMA_VERSION = "rotation.v3.0"`，`PRODUCTION_PROFILE = "research_v3"`，`SOURCE_PROFILE` 不变。

`RotationStore` 需要区分读写版本：

- `publish()` 只接受当前 `SCHEMA_VERSION`
- `load()` 接受 `READABLE_SCHEMA_VERSIONS = {"rotation.v2.1", "rotation.v3.0"}`，读到旧版本时在返回值上标 `schema_legacy=true`，页面据此降级展示，不报错

**这不是可选项，否则 v3 首次发布必然失败。** `publish()` 内部会调用 `self.load()` 读取旧指针：

```52:56:src/group_analytics/rotation/store.py
            pointer = {"run_id": run_id, "sha256": digest, "source_session": snapshot["source_session"]}
            old = self.load() if (self.root / "latest.json").exists() else None
            if old is None or old["source_session"] <= snapshot["source_session"]:
                _atomic_json(self.root / "latest.json", pointer)
```

而 `load()` 当前对版本是硬校验：

```89:90:src/group_analytics/rotation/store.py
        if snapshot.get("schema_version") != SCHEMA_VERSION or not isinstance(snapshot.get("rows"), list):
            raise ValueError("Unsupported rotation schema")
```

`SCHEMA_VERSION` 一旦升到 `rotation.v3.0`，SG 上 `latest.json` 仍指向 v2.1 run，`publish()` → `load()` → `ValueError` → **第一次 v3 构建直接失败**，而且 `store.failure()` 会把 `last_attempt` 标成 FAILED。

必须在**同一个提交**里完成读版本放宽与写版本升级，不能分两次合并。部署前在 SG 上用真实 `outputs/group_analytics/rotation/` 目录做一次只读 `load()` 验证。

对应测试：以 v2.1 快照为 latest 时，v3 `publish()` 必须成功且指针正确前移。

**交付物**：双轴状态、独立风险/缺失数组、新优先级、成交额解锁、趋势列、v3 schema。

**测试**（新增，`tests/test_group_rotation.py` 扩展）：

- **§2.4 反例回归**：构造恒定速率相对趋势序列，断言 `strength_axis == "leading"`、`speed_axis == "steady"`、`priority == "focus"`，且**不出现** `wait`
- **优先级完备性**：对 `strength × speed × abs20符号 × index/MA20符号` 的全部组合遍历，断言每一组都命中且仅命中一个档位，无 `None`
- `leading` + `abs20 <= 0` → `defensive`，不得落入 `focus`，也不得无命中
- 一天噪声不改变已确认 `strength_axis`，也不改变 `priority`
- 延伸主题仍能获得 `focus`，同时 `EXTENDED` 在 `risk_flags` 中
- 无成员 ETF 的 `evidence_gaps` 含 `ETF_HOLDINGS_NOT_LINKED`，且不影响 `priority`
- 3 只成员篮子 `evidence_gaps` 含 `SMALL_BASKET`
- `speed_axis` 每日直出，不受两日确认影响
- **v2.1 快照作为 latest 时，v3 `publish()` 成功且指针前移**
- v2.1 旧快照仍可 `load()` 并标 `schema_legacy`
- `replay.py` 对 v2.1 快照按 v2 规则重放，不用 v3 规则报 MATCH
- 缓存文件 `price_basis` 不符时视为缺失，不静默使用
- `input_fingerprint` 相同时 `price` 阶段返回 NOOP，不产生新 run
- 主题涨 1% / 基准涨 2% 时标签为"相对走弱"而非"下跌"

**验收**：用 09-10 真实快照重算，逐条核对 §4.4 三个主题的目标表达；22 个主题中"等待确认"占比显著下降且每个剩余的"等待"都有明确原因码。

**回退**：schema 双版本可读，回退代码后旧页面仍能读 v3 快照的兼容子集。

---

### P2 — 真实成员广度

**目标**：把 17 个 ETF 主题的广度从"未接入"变成真实测量。

#### P2.1 接入持仓观测

`src/group_analytics/rotation/holdings.py` 已实现 `normalize_observation` / `observation_breadth`，SMH 试点结论（25/25 覆盖、权重 99.91%、等权 48.00%、加权 67.85%）见 [V2 验证文档 §4](sector_rotation_v2_validation_20260909.md)。

本阶段把它从独立脚本接进日常构建：

- `service.py` 新增 `holdings_root` 参数，`run_rotation` 在计算前加载各 ETF 的最近一次持仓观测
- 持仓观测作为**独立产物**存储，与价格缓存分离：`data/reference/group_analytics/rotation/holdings/<ETF>/<captured_at>.json`
- 观测刷新独立排期（每周一次足够，持仓变动慢），失败不阻断价格层构建

#### P2.2 双口径必须同时展示

SMH 试点中等权 48.00% 与加权 67.85% 相差近 20 个百分点，只给一个数是误导。快照字段：

```text
breadth_equal_weight_pct      等权站上 20 日线比例
breadth_weighted_pct          按持仓权重加权比例
breadth_eligible_members      有效成员数
breadth_mapped_members        可映射权益成员数
breadth_member_coverage       有效/可映射
breadth_weight_coverage       有效权重/可映射权重
breadth_kind                  口径枚举
holdings_captured_at          持仓观测时间
holdings_effective_at         持仓生效日（供应商未提供时为 null）
point_in_time                 恒为 false
```

生产轨道的 `breadth_kind` 枚举：`member_above_ma`（自建篮子）、`etf_holdings_observation`（ETF 当前持仓观测）、`unavailable`。**不设 `etf_trend_proxy`** —— 参考版的 70/30 永远不进这个字段。对照轨道产出的 70/30 继续保留在行内 `compatibility` 字段中，只在"方法对照"区展示。

#### P2.3 资格边界

- `holdings_effective_at` 为 null 时保持 `status = OBSERVATION_ONLY_NO_PROVIDER_DATE`、`point_in_time = false`
- **只用于当日展示**。不倒填历史、不进 `validation.py` 的回测样本、不参与任何前向研究标签
- 页面文案："当前持仓观测广度（持仓生效日未披露）"，不叫"历史成分广度"
- 海外上市成分、现金、衍生品继续进 `excluded` 证据，不静默丢弃

#### P2.4 优先级仍不使用广度

P1 已把广度从优先级中移除，本阶段**不恢复**。广度是并列证据，不是门槛。是否允许它影响排序，属于 P5 的验证课题。

**数据源前置审计**（写代码前完成）：

1. `/stable/etf/holdings` 对全部 17 个 ETF 的可用性与响应完整性
2. 各 ETF 的成分数量（决定请求成本与内存）
3. 权重合计是否落在 95~105 区间
4. 海外上市成分占比（影响交易日历对齐）
5. 请求配额消耗估算
6. 结果写入 `docs/sector_rotation_v3_holdings_audit_<date>.md`

**测试**：双口径同时输出；权重合计越界拒绝；成分映射失败进 `excluded`；持仓观测过期时降级为 `unavailable` 而非使用旧数据；`point_in_time` 恒 false；持仓数据不进 validation 样本。

**验收**：17 个 ETF 全部有真实广度或明确的不可用原因；SMH 结果与试点一致。

---

### P3 — ETF 净申赎（真实资金流）

**目标**：补上参考版和我们共同缺失的唯一真正衡量"钱"的维度。

**这是本轮唯一需要新数据能力的阶段，因此前置审计的权重最高。**

#### P3.1 口径定义

```text
日净申赎 ≈ 拆股调整后的 ETF 份额变化 × 当日 NAV
```

必须严格区分四个概念（参考版把它们混作一谈）：

| 概念 | 含义 | 能否说"资金流入" |
|---|---|---|
| ETF 二级市场成交额 | `close × volume` | **否** |
| 篮子成员合计成交额 | `Σ close_i × volume_i` | **否** |
| ETF 净申赎 | `Δ份额 × NAV` | 是 |
| 底层资金净流入 | 需穿透持仓 | 本阶段不做 |

明确边界：净申赎**不是成交量**，**不是 AUM 增长**（AUM 变化含价格涨跌），且通常 **T+1 才可得**，没有盘中资金流。

#### P3.2 前置接口审计（不写业务代码）

必须逐项回答并写入 `docs/sector_rotation_v3_flows_audit_<date>.md`：

1. FMP 是否提供 ETF 历史份额（shares outstanding）？端点、字段名、历史长度
2. 是否提供历史 NAV？与收盘价的差异（溢价/折价）
3. 份额序列是否已拆股调整？ETF 拆股/反向拆股如何体现
4. 数据发布延迟（T+1？T+2？）
5. 覆盖哪些 ETF（17 个是否全覆盖）
6. 传播/展示授权范围
7. 若份额不可得，是否可由 AUM 与 NAV 倒推？精度与可得性实测

**审计不通过则本阶段整体推迟**，不做降级替代，不用成交额冒充。

#### P3.3 实现约束（审计通过后）

- 净申赎列只在有份额数据时出现，没有就留空
- 独立标注数据日期（与价格日期可能不同，通常落后一天）
- 同时展示日净申赎与 5 日/20 日累计
- 与价格方向组合成解释性标签：`价格涨+净流入`（含金量高）、`价格涨+净流出`（可能是反弹出货）、`价格跌+净流入`（可能是逢低吸纳）、`价格跌+净流出`
- 这些标签是**描述**，不是评分项，不进优先级
- 自建篮子无 ETF 份额概念，该列恒为"不适用"，不用成员成交额顶替

**验收**：17 个 ETF 的净申赎与至少一个公开来源（如 ETF.com 或发行商官网）抽样对账 5 个交易日，偏差与原因可解释。

---

### P4 — 呈现层

**目标**：把已有数据变成可读的产品。**不新增任何算法。**

#### P4.1 相对轮动轨迹图

X/Y 已在快照中，`history` 已存 120 个交易日，仅缺前端。

- 散点 + 尾迹，默认显示最近 10 周
- 四象限底色，可切换观察范围（科技/行业）
- **命名约束**：JdK RS-Ratio / RS-Momentum 公式为专有。本图命名为"相对强弱—速度轨迹"，文案明确"借鉴相对趋势与轨迹表达，非 RRG 复刻"

#### P4.2 板块热力图

数据源：`US_EQUITY_COVERAGE`（约 3000+ 只，含行业分类与市值），我们的底层覆盖优于 Finviz。

- 市值加权 treemap，按 sector / sub-industry 两级下钻
- 周期切换：1 日 / 5 日 / 20 日
- 点击下钻到个股，复用既有 `/breakouts/{ticker}` 链接
- **约束**：行业分类当前为 `LATEST_KNOWN_BACKFILL_NOT_PIT`。做**当日**热力图合规（只需今天的分类），必须页面标注，且**不得用于历史回看**

#### P4.3 跨资产背景条

新增 6 个 ETF 代理进现有取数循环，成本极低：

| 代理 | 观察对象 |
|---|---|
| TLT / IEF | 长/中期美债 |
| UUP | 美元 |
| GLD | 黄金 |
| USO / DBC | 原油 / 商品 |

- 显示 5/20/60 日绝对收益，与主题表并列
- **必须标"ETF 代理"**：不把 UUP 叫美元指数，不把 GLD 叫黄金现货
- 纯展示，不进任何评分

**验收**：桌面 + 390px 移动端验收；轨迹图与快照数值一致；热力图行业合计与 coverage 总数对账。

---

### P5 — 宏观背景与增量验证

**目标**：让"为什么换方向"这一层从空壳变成可用，并验证前四阶段是否带来真实增量。

#### P5.1 FRED 接入

`src/group_analytics/rotation/context.py` 的框架（证据家族、`known_at`、许可校验、支持/压制/混合/不足）设计正确，缺的是数据源。

候选序列：

| 序列 | 含义 | 家族 |
|---|---|---|
| DGS10 | 名义 10 年 | rates |
| DFII10 | 实际 10 年 | rates |
| T10YIE | 盈亏平衡通胀 | inflation |
| DTWEXBGS | 广义美元 | dollar |
| BAMLH0A0HYM2 | 高收益利差 | credit |

三个前置条件：

1. **许可**：ICE HY OAS 有传播限制，FRED 页面仅保留 3 年历史。主站/Discord 展示前必须确认授权，写入审计文档
2. **已知时点**：H.15 工作日 16:15 发布、H.10 每周一发布上周。`observed_at` 与 `published_at` 必须分开存，不能用观察日当可得日。使用 ALFRED vintage 时需注意日期级 vintage 不能证明某个时刻可用
3. **不做因果归因**：只输出"实际利率上行""美元走强""信用利差走阔"这类窄标签，不输出"机构从硬件撤资转入软件"

宏观层只做上下文：**不改主题分数、不控仓位**。参考截图中最值得学的正是这一点 —— "多方 76、空方 45"配的行动是"等待"，因为"支撑未兑现"。外部证据与价格确认是两回事。

#### P5.2 增量验证

用既有 `scripts/research_group_rotation.py` + `src/group_analytics/rotation/validation.py`，对每个阶段重跑：

**基线固定为简单 RS20。**

对照组：

1. RS20（基线）
2. RS60
3. RS20/60 组合
4. `source_v1_compat` 积分
5. V3 双轴优先级（P1 产物）
6. V3 + 真实广度（P2 产物）
7. V3 + 广度 + 净申赎（P3 产物）

V2 生产价格状态**不重新实现**。P1 之后该代码路径已移除，其历史对照数值直接引用 [V2 验证文档 §3](sector_rotation_v2_validation_20260909.md) 的已记录结果（科技 −2.57pp、行业 −0.45pp，相对 RS20，25bp 成本）。为做对照而保留一份死代码违反"不增加复杂度"。

要求沿用既有实验设计：T+1 复权开盘入场、T+h 复权收盘退出，h = 20 为主检验、5 为辅助；不重叠持有期；0/10/25/50bp 双边成本；交易日块自助法；开发/验证/回看三段切分且标签跨段剔除。

**准入规则**：

- 任何新维度若不能在冻结规则、相同有效样本、扣成本后**稳定跑赢 RS20**，只能保留为展示证据，**不得进入排序或选股条件**
- 允许结论为负。[V2 验证](sector_rotation_v2_validation_20260909.md) 已经给出负面结果，本轮不得通过调参掩盖
- `promotion` 状态在有新证据前保持 `NOT_APPROVED`

---

## 6. 阶段依赖与追踪

```text
P0 时效与闭环          （独立，不依赖任何其他阶段）

P1 判断层重构 ──┬──> P2 真实广度 ──> P3 净申赎
                └──> P4 呈现层

P5.1 宏观接入         （独立，只依赖 FRED 审计）

P5.2 增量验证         （在 P1 / P2 / P3 各自结束时分别跑一次）
```

- **P0 与 P1 在逻辑上互不依赖，但会改动同一批文件**（`run_group_rotation.py`、`service.py`、`group_rotation.js`）。不要并行开发后再合并，**按 P0 → P1 顺序串行合并**。原计划「P0 上线并观察满 3 个交易日后再开 P1 分支」已由负责人豁免：本轮在同一分支连续交付 P0–P3，SG 三日观察改为上线后补做，而不是开发门禁。
- P4 只依赖 P1（轨迹图需要 `strength_log` / `acceleration_log` 字段），不依赖 P2/P3。
- P5.1 与价格链完全独立，可在任意阶段并行推进，但其上线受许可审计约束。
- P5.2 不是最后才做：P1、P2、P3 各自结束时都要跑一次对 RS20 的对照。

| 阶段 | 需要新数据源 | 前置审计 | 主要风险 |
|---|---|---|---|
| P0 | 否 | 无 | timer 重启副作用；重复 run |
| P1 | 否 | 成交额复权对账 | schema 迁移（见 P1.7）；缓存口径混用（见 P1.5.2） |
| P2 | 是（ETF 持仓） | 持仓接口审计 | 供应商无生效日 |
| P3 | 是（份额/NAV） | 资金流接口审计 | 数据可能不可得 |
| P4 | 否（+6 个 ETF） | 无 | 分类非 PIT |
| P5 | 是（FRED） | 许可与 known_at 审计 | 传播授权 |

关于 timer 重启副作用：[main/SG 同步记录](main_sg_sync_20260911.md) 已记录过一次恢复 timer 导致计划外立即执行的事件（`Persistent=false`、无 drop-in，原因未定位）。P0 涉及新增和修改 unit，**不得假设重启日历 timer 无副作用**；维护期需要显式执行护栏，并在操作前后核对 `systemctl list-timers` 与实际执行记录。

---

## 7. 明确不做的事

- 不抄参考版的 ETF 70/30 假广度
- 不把 `close × volume` 叫资金流，不在主表使用"派发/撤出/拥挤"等无据措辞
- 不为填满分数而降低门槛或倒填历史持仓
- 不把自建对数象限称为 RRG 复刻
- 不声称复刻扩展 13 主题版或 NQ 动因状态机（其公式未知）
- 不让 `group_analytics` 反向依赖核心域
- 不在验证通过前把轮动结论变成任何自动选股或仓位依据
- 不碰多因子、回测、模拟盘、茶杯柄、EP 的任何逻辑
- 不在本轮增加新的评分复杂度

---

## 8. 待核实事项

1. **SG 上线后的 P0 三日观察**：核对 `quant-group-rotation-price` 是否在收盘后 1.5h 出快照；至少复现一次「13:15 关联失败 → 07:00 ET 重试成功」。需只读 `systemctl` / `journalctl` / `outputs/group_analytics/rotation/last_attempt.json`
2. **个股关联首次失败的具体调用条件**：不能继续笼统归因为「宽基停在 09-04」，需定位 `CompletedSessionMomentumSource.load()` 当时抛出的确切原因码
3. **§4.4 能源行的缺失字段**：需从 SG 快照补齐 `rs5`、`rs1`、`dist50`、`abs20`、`absolute_ma20`，确认「延伸偏大」来自 `dist50 > 8` 而非 `rs60 > 18`
4. **TradingView 外部数值对账**：仍未完成，`source_v1_compat` 目前只能称「规则级实现候选」
5. **P1 成交额 6 标的实盘抽样**：合成门禁已绿；3 只有拆股、3 只有大额分红的 FMP 抽样仍待 SG，见 [成交额对账](sector_rotation_v3_amount_audit_20260912.md)
6. **P2 17 个 ETF `/etf/holdings` 活样本**：本环境无 FMP key；SMH 试点仍是唯一实盘锚点，见 [持仓对账](sector_rotation_v3_holdings_audit_20260912.md)
7. **P3 FMP ETF 份额/NAV**：公开文档 **NOT_PASSED**（无历史份额/NAV 序列）。已禁止降级替代。若 SG 发现未文档化端点，先更新 [净申赎审计](sector_rotation_v3_flows_audit_20260912.md) 再写取数
8. **P5.2 对 RS20 的增量对照**：P1/P2/P3 各自结束后应跑一次；本环境没有冻结实盘面板，未跑。上线后用 `scripts/research_group_rotation.py` 补。广度/净申赎在对照跑赢前不得进排序

已关闭：计划原文「FMP ETF 份额/NAV 未审计前不承诺 P3」——文档审计已做完，结论是不可得，P3 只保留空列管线。

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-12 | 首版。合并两条核查线结论，确立 P0–P5 六阶段计划 |
| 2026-09-12 | 自查修订。修正优先级阶梯未命中分支与 `unavailable` 判断顺序；补回 `defensive`（相对抗跌）档；补充 schema 迁移会导致 v3 首次发布失败的隐患；补充价格缓存口径失效、第三张价格表签名变更；重复 run 抑制；澄清成交额解锁不恢复 0–100 分；能源示例行标为待核对；修正阶段依赖图与合并顺序 |
| 2026-09-12 | P0–P3 在同一分支落地（见 §10）。P0 三日观察门禁由负责人豁免为上线后补做 |
| 2026-09-12 | P0–P3 复查：修复盘前关联重试未开 `GROUP_ANALYTICS_ENABLED`、失败会话写成 `latest`、周持仓截断主题 canonical、overlay 可打断价格层、过期持仓仍显示「未接入」 |
| 2026-09-12 | 二次复查：关联层不得把 `NO_QUALIFIED_CANDIDATE` / `LINKAGE_FAILED` 写入 `production.evidence_gaps`（否则 `audit_group_rotation.py` 对已关联快照报 DIFFERENT）；盘前 `kind` 按 schema 标 `rotation_v3`，渲染器同时接受 v2/v3，避免误走旧单日板块摘要 |

---

## 10. 实施状态（2026-09-12，PR #7）

唯一合入分支：`cursor/sector-rotation-v3-99b0`。不按阶段拆 PR。隔离边界未变：只动 `src/group_analytics/rotation/`、`src/premarket_digest/rotation.py`、轮动 Web、轮动 systemd；不碰多因子 / 回测 / 模拟盘 / 茶杯柄 / EP。

### 10.1 已交付

| 阶段 | 状态 | 要点 |
|---|---|---|
| 计划 | 已写 | 本文 + 自审修订 |
| P0 | 代码完成，SG 三日观察未做 | `--stage {price,linkage,all}`；17:30 ET 价格层；13:15 SGT 关联；07:00 ET 盘前 `ExecStartPre` 重试（已补 `GROUP_ANALYTICS_ENABLED`）；页面新鲜度 / `?run=` 固定快照；Discord 双链接；`group_rotation_price` 已登记 operations |
| P1 | 代码完成，6 标的实盘抽样未做 | schema `rotation.v3.0`；双轴强弱×速度；`pending` 不覆盖优先级；EXTENDED 不吞档；canonical `close×volume`；主表趋势/成交活跃/风险；生产 0–100 分不在主表 |
| P2 | 代码完成，17 ETF 活样本未做 | 独立 `rotation/holdings/<ETF>/`；主表等权/加权；14 日历日过期停用；不进优先级 / validation / `production.breadth`；周更写入 `rotation/holdings_canonical/`，**不覆盖**主题 `rotation/canonical/` |
| P3 | 审计 NOT_PASSED，只保留管线 | 篮子「不适用」，ETF「—」；`amount_proxy` 恒 null；无份额/NAV 拉取 |
| P4 | 未开始 | 轨迹图 / 热力图 / 跨资产条 |
| P5 | 未开始 | FRED；把广度/净申赎纳入排序前必须先跑赢 RS20 |

### 10.2 复查已修的缺陷

1. `quant-premarket-prepare` 未设置 `GROUP_ANALYTICS_ENABLED`，07:00 ET 关联重试是空转。已在 `EnvironmentFile` 之后写入 true。
2. CLI `--asof latest` 失败时 `last_attempt.source_session` 写成字面 `"latest"`。现解析为 XNYS ISO 日期。
3. `observe_rotation_holdings.py` 曾用 70 日历日 refresh 进主题 canonical，可能截断 61 根历史。现独立 leaf `holdings_canonical`，回看 550 日历日。
4. `attach_holdings_breadth` 异常（含无 `adj_close`）曾中断整个价格层。现按行/外层兜底；无 `adj_close` 时读 `close`。
5. 过期/测量失败的持仓仍展示 `ETF_HOLDINGS_NOT_LINKED`。页面与 Discord 已按状态过滤。
6. `attach_candidates` 曾把关联缺口写进 `production.evidence_gaps`，已关联快照的冻结重放会报 DIFFERENT。现只写行级 `evidence_gaps`。
7. `load_rotation_report` 的 `kind` 是 Discord 分流键（不是 schema）。v3 快照现标 `rotation_v3`；`build_sector_rotation_payload` 同时接受 `rotation_v2` / `rotation_v3`，避免误渲染成旧单日板块涨跌摘要。

### 10.3 刻意保持的行为

- 生产 0–100 分本轮全程不在主表
- 广度、净申赎不进优先级
- 周持仓任务不进 `operations.yaml`（周日历 vs `latest_publishable_xnys` 会误报 MISSED）
- 盘前报告 `kind` 即使在 v3 也必须走 `rotation_payload`，不能只改名字不改渲染器
- 首次部署因 `input_panel` 增加 holdings/flows fingerprint，价格层会多发布一次新 run，随后指纹稳定则 NOOP

### 10.4 下一步

等负责人指示。默认顺序是 P4（呈现层，只依赖 P1 字段）或 P5.1（FRED 审计，独立）。不要在 P3 审计通过前写份额/NAV 取数，也不要把广度/净申赎塞进排序。
