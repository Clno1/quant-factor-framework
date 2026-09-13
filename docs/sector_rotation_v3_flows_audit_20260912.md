# 板块轮动 V3 净申赎接口审计

日期：2026-09-12

状态：**NOT_PASSED**。按计划 §P3.2，审计不通过则本阶段整体推迟，**不做降级替代，不用成交额冒充**。代码只保留展示管线与合成算术，不调用任何份额/NAV 供应商接口。

相关代码：`src/group_analytics/rotation/flows.py`。常量 `FLOWS_AUDIT_STATUS = "NOT_PASSED"`。

## 1. 口径（不变）

```text
日净申赎 ≈ 拆股调整后的 ETF 份额变化 × 当日 NAV
```

| 概念 | 含义 | 能否说「资金流入」 |
|---|---|---|
| ETF 二级市场成交额 | `close × volume` | **否** |
| 篮子成员合计成交额 | `Σ close_i × volume_i` | **否** |
| ETF 净申赎 | `Δ份额 × NAV` | 是 |
| 底层资金净流入 | 需穿透持仓 | 本阶段不做 |

净申赎不是成交量，不是 AUM 增长（AUM 含价格），通常 T+1 才可得。自建篮子没有 ETF 份额，该列恒为「不适用」。

## 2. 逐项审计

公开 FMP 文档（2026-09-12 查阅）能回答的部分如下。本环境没有 API key，不能用实盘字段名/历史长度/延迟做最终确认；文档缺口本身已足够判 **不通过**。

### 2.1 历史份额（shares outstanding）

**未找到** ETF 份额的日频历史序列端点。

- [ETF & Mutual Fund Information](https://site.financialmodelingprep.com/developer/docs/stable/information) `/stable/etf/info`：当前档案（名称、费率、AUM 等），不是时间序列。
- [All Shares Float](https://site.financialmodelingprep.com/developer/docs/stable/all-shares-float)：公司流通股本，**不是** ETF 份额。
- [ETF Holdings](https://site.financialmodelingprep.com/developer/docs/stable/holdings)：成分股持仓数量，不是基金自身发行份额。

字段名、历史长度、拆股调整方式因此都无法从 FMP 文档给出。

### 2.2 历史 NAV

**未找到** ETF NAV 日频历史端点。`etf/info` 若含当前 NAV，也只是截面。溢价/折价（NAV vs 收盘价）无法在没有 NAV 序列时计算。对比：Intrinio 等第三方有独立的 historical NAV flows API，不在本仓库许可范围内，本阶段不引入第二供应商。

### 2.3 拆股调整

无份额序列则无从核验 ETF 拆股/反向拆股如何体现在 shares 上。不能假设 `close` 的拆股复权可代替份额复权。

### 2.4 发布延迟

无官方份额/NAV 序列，无法核验 T+1 / T+2。行业惯例常为 T+1，只能作为以后接入时的假设，不是现有数据契约。

### 2.5 17 个 ETF 覆盖

在没有历史份额端点的前提下，覆盖度实测无意义。不得用 17 个 ETF 的成交额或 AUM 截面冒充「已覆盖」。

### 2.6 传播/展示授权

未使用任何新的份额供应商，无新增传播条款。页面「净申赎」列展示「—」或「不适用」，不展示金额。

### 2.7 AUM / NAV 倒推

`shares ≈ AUM / NAV` 只在**同一时点的两个历史序列**上都存在时才可能。`etf/info` 是当前截面；AUM 变化包含价格涨跌，倒推日份额会把涨跌算进「申赎」。计划已禁止这种降级。**不实现倒推。**

## 3. 代码落点（管线，不是数据）

在审计通过前允许存在、且测试锁定的行为：

- 自建篮子：`net_creation.status = not_applicable`，不用成员成交额
- ETF：`status = unavailable`，`daily` / 累计 / 标签均为 null，`amount_proxy` 恒为 null
- `compute_net_creation(shares, nav)` 只服务合成测试与未来 `rotation/flows/<ETF>.parquet`（`date, shares, nav`）
- 描述标签（价格涨+净流入 等）只有在 `audit_status=PASSED` 且测试注入序列时才出现；**不进优先级**
- 主表有「净申赎」列，审计未通过时为「—」

**明确不写：** FMP 份额/NAV 拉取、AUM 倒推、把 `production.amount_proxy` 复制进净申赎。

## 4. 验收（推迟）

计划要求 17 个 ETF 与 ETF.com 或发行商官网抽样对账 5 个交易日。在 FMP（或另审过的供应商）提供可审计的拆股调整份额与 NAV 序列之前，这项验收**不能开始**。SG 若日后证实 FMP 有未文档化的端点，应先更新本文并改 `FLOWS_AUDIT_STATUS`，再写取数代码。
