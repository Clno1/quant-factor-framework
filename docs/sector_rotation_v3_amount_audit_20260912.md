# 板块轮动 V3 成交额口径对账

日期：2026-09-12

状态：代码门禁已锁定合成口径，并用 `amount_audit_status=SYNTHETIC_GATES_ONLY` 与“按 canonical close 计算”分开。**真实 6 只标的的 FMP 抽样对账仍待 SG 补做**。在抽样通过前，不得把 `amount_verified=true` 理解成供应商口径已核验。

相关代码：`src/group_analytics/rotation/service.py` `load_frames()`、`src/group_analytics/rotation/engine.py` `metric_frame()`。

## 1. 锁的原因（仍然成立）

旧路径用 `get_historical_ohlcv(..., dividend_adjusted=True)`，把分红复权价映射成 `close`，再算 `close × volume`。分红复权价与未经核验的成交量相乘，经济口径不成立，所以生产轨的 `amount_ratio` 被 `amount_verified=false` 整列打成 NaN。

仓库内已经有合法取数：`get_canonical_historical_ohlcv()` 用 `/full` 的拆股复权 OHLCV 作为可成交价量，只从分红复权序列取 `adj_close` 做总收益。相对收益继续用 `adj_close`；成交额改用 `execution_close × volume`，两张表禁止复用。

## 2. 本次环境做不到的活样本

计划要求先抽 3 只有拆股记录、3 只有大额分红的标的，对拆股日前后各 5 个交易日逐日核 `close × volume` 连续性，并对比 `adj_close × volume` 的差异分布。

本云端工作区没有可用的 FMP 凭证，不能把实盘 6 只标的的对账表写进本文并假装已经完成。该抽样必须在 SG 上用真实 `get_canonical_historical_ohlcv` 补做，结果追加到本文或另开 `docs/sector_rotation_v3_amount_audit_<date>.md`。

## 3. 代码门禁（合成，必须保持绿）

`tests/test_group_rotation.py` 锁定下面两条，避免以后再把分红复权价乘进成交额：

1. **拆股连续性**：最后一日 2-for-1（`close` 减半、`volume` 加倍）时，`execution_close × volume` 与前一日相等，`amount_ratio ≈ 1`。若错误地沿用未同步放大的成交量，倍数会掉到约 0.5。
2. **分红不得污染成交额**：最后一日 `adj_close` 下调 10%、`close` 与 `volume` 不变时，成交额仍按 `close × volume`，不得跟随 `adj_close` 跳变。

方向标签另有反例：主题 +1% / 基准 +2% 时必须写「相对走弱」，不得写成「下跌」；只有绝对收益为负才允许附「（绝对下跌）」。

## 4. 缓存失效

旧缓存 `data/reference/group_analytics/rotation/<SYMBOL>.parquet` 是分红复权口径（`close == adj_close`），共享 `data/raw/ohlcv` 同样未核验。V3 读取路径改为：

- 目录：`.../rotation/canonical/<SYMBOL>.parquet`
- 旁路文件：`<SYMBOL>.basis.json`，必须含 `price_basis = canonical_full_plus_dividend_adj`
- 缺失或口径不符视为缺失，不静默使用旧文件
- **停用** `data/raw/ohlcv` 回退

旧目录保留便于回退，但运行时不再读取。

## 5. 默认值与两种状态

- **计算路径**：只使用 canonical `close × volume`。缺少 `close` 时金额为 null，**禁止**回退 `adj_close`。
- **`amount_verified`**：解锁生产轨“成交活跃”列（仍要求 close 存在）。这只表示“按 canonical 字段计算”，不是供应商抽样通过。
- **`amount_audit_status`**：当前为 `SYNTHETIC_GATES_ONLY`。真实 6 标的 FMP 抽样通过后才能改为 `PASSED`。

systemd 价格层 unit 不必加 `--amount-verified` 开关。解锁成交额只服务于主表「成交活跃」列。生产 0–100 分仍要求真实广度，且 **本轮全程不在主表展示**。

快照字段：`amount_basis = split_adjusted_close_x_volume`，`amount_audit_status = SYNTHETIC_GATES_ONLY`，`amount_audit_doc` 指向本文，`price_basis = canonical_full_plus_dividend_adj`。
