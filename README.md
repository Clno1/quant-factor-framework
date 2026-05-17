# Multi-Factor Quant Research Framework

从零搭建的**美股多因子量化研究框架**。MVP 阶段先用一个 6 个月动量因子跑通完整闭环：
**数据获取 → 因子计算 → 预处理 → IC 分析 → 五分位回测 → 可视化 → FastAPI Web 展示**。

> 框架结构遵循"分层解耦 + 面向数据管道"设计，便于后续扩展到多因子合成、行业中性化、因子相关性分析等高级功能。

---

## 特性

- 🎯 **股票池**：S&P 500 成分股（FMP 直拉，含 GICS sector）
- 📈 **数据源**：**FMP（Financial Modeling Prep）**（推荐，稳定）/ yfinance（免费但常被限流）
- 🏭 **因子库**：先实现 MOM_6M，预留 VOL / REVERSAL / TURNOVER 等扩展槽位
- 🧪 **有效性检验**：Rank IC（Spearman）+ IC_IR + t 统计量（对齐研报 IC 汇总表）
- 📊 **五分位回测**：Quintile Analysis + Long-Short（Q5 - Q1）
- 🎨 **可视化**：matplotlib 静态图 + Plotly 交互图（Web 嵌入）
- 📱 **Web 端**：FastAPI + 深色响应式页面，手机浏览器友好

---

## 目录结构

```
Quant/
├── configs/default.yaml   # 全局配置
├── data/                  # 数据缓存 (raw / processed)
├── logs/                  # 运行日志
├── src/
│   ├── config.py          # 配置加载
│   ├── utils/             # 日志 & IO
│   ├── data/              # 股票池 & yfinance 下载 & 清洗
│   ├── factors/           # 因子基类 + MOM_6M
│   ├── preprocessing/     # 去极值 / 标准化 / 中性化
│   ├── analysis/          # IC 分析
│   ├── backtest/          # 五分位回测引擎
│   ├── visualization/     # matplotlib / plotly 图表
│   └── webapp/            # FastAPI 服务 + Jinja2 模板
├── scripts/run_mvp.py     # 一键运行脚本
└── requirements.txt
```

---

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置数据源（FMP，推荐）

注册 FMP 拿到 API key：https://site.financialmodelingprep.com/

**两种方式配置 key（任选一种）：**

```bash
# 方式 A：环境变量（推荐，不会进 git）
export FMP_API_KEY="你的_key"
# 永久生效写到 ~/.zshrc 或 ~/.bashrc
```

```yaml
# 方式 B：configs/default.yaml
data:
  provider: "fmp"
  fmp:
    api_key: "你的_key"   # 注意不要 commit
```

切换数据源只需改 `data.provider` 字段：`"fmp"` 或 `"yfinance"`。

### 3. 运行完整 pipeline

```bash
# 烟雾测试（20 只）
python scripts/run_mvp.py --universe 20 --no-web

# 全量 + 启动 Web
python scripts/run_mvp.py
```

这会完成：
1. 抓取 S&P 500 成分股列表（FMP 直接带 sector）
2. 下载 5 年日线行情（Parquet 缓存，二次运行秒开）
3. 计算 MOM_6M 因子 + 预处理
4. 计算 Rank IC 序列 + 汇总指标
5. 跑五分位回测 + Long-Short 组合
6. 生成可视化图表到 `outputs/`

### 4. 启动 Web 服务

```bash
python scripts/run_mvp.py --serve-only
```

浏览器访问 `http://<服务器IP>:18823`（手机/电脑均可）。

---

## 核心设计

- **FactorBase 抽象基类**：所有因子继承统一接口，新增因子只需写一个子类，主流程零修改。
- **向量化回测**：使用 `pd.qcut` + `groupby` 做五分位分组，500 股票 × 5 年回测 < 1 秒。
- **前视偏差防范**：IC 计算严格用 `factor_t` 对齐 `forward_return_{t→t+N}`。
- **零 JS 前端**：FastAPI 后端渲染 Jinja2 模板，前端用 Plotly.js CDN，无需前端构建。

---

## 因子库 / 策略库 / 回测（v2）

系统三层解耦：**因子（计算定义）→ 策略（因子加权配方）→ 回测（策略 × 股票池 × 日期 → 异步执行）**。

### Web 入口

| 路径 | 说明 |
|---|---|
| `/factors` | 因子库（只读，按分类卡片展示）|
| `/strategies` / `/strategies/new` / `/strategies/{id}` | 策略库 CRUD |
| `/watchlists` / `/watchlists/new` / `/watchlists/{id}` | 自定义股票组（Watchlist）CRUD |
| `/backtests` / `/backtests/new` / `/backtests/{id}` | 回测任务 CRUD + 异步执行 |

### 因子库元信息

`configs/factor_library.yaml` 记录每个因子的中文展示名、分类、公式、描述与风险提示，与 `src/factors/` 中代码注册的 `FACTOR_REGISTRY` 通过 `id` 关联。启动时做一致性校验。

### 策略 = 因子配方（不绑定股票池）

```
outputs/strategies/<UUID>/definition.json
```

通过 Web 表单创建，可选若干因子并赋予权重（允许任意数字、负值、自动归一化）。

### 回测异步执行

```
outputs/backtests/<UUID>/
  ├─ task.json        # 状态 pending/running/success/failed + strategy_snapshot 冻结
  ├─ returns.parquet  # Top 组日收益
  ├─ nav.parquet      # 净值曲线
  ├─ metrics.json     # AnnReturn / Sharpe / MaxDD / Calmar / WinRate
  ├─ holdings.parquet # 每个调仓日的 Top 组持仓
  └─ log.txt          # 任务执行日志
```

**合成算法**：`Σ wᵢ · Zscore(因子ᵢ)` → 五分位回测 → 取 Top 组（Q_n）作为策略持仓与收益。

**成交模型（v3 新增）**：让回测尽量贴近实盘可达上限。

| 模式 | 决策时刻 | 成交价 | 持有期收益 |
|---|---|---|---|
| `close`（理论上限）| T 日收盘 | T 日收盘价 | close-to-close |
| `next_open`（推荐，默认）| T 日收盘 | **T+1 日开盘价** | open-to-open |

**摩擦成本**：调仓日按"换手率 × (slippage + commission) × 2"扣除。  
- 默认：滑点 5 bps + 手续费 2 bps，单边 7 bps；**双边 14 bps**。
- 在新建回测页的"高级选项"里可临时覆盖。
- 详情页诊断模块会展示当前任务用的成交参数和年化摩擦成本（bps/年）。

**典型 A/B 差距**（同策略 + 同股票池跑两次）：从 `close+0/0` 到 `next_open+5/2bps`，  
Sharpe 通常下降 0.1~0.3，年化收益下降 2~6%（含 ~150-200 bps 摩擦 + ~4% T+1 滞后）。  
这部分差距才是"实盘和回测之间的真实 gap"。

**异步**：`ThreadPoolExecutor(max_workers=2)`，前端 1 秒轮询 `/api/backtests/{id}/status`。服务重启时遗留 running 任务会被 startup_recovery 标记为 failed。

### Watchlist（自定义股票组）

- 存储：`outputs/watchlists/<UUID>/definition.json` + `_index.json`
- 每行一个 `{ticker, weight, name}`——回测时**只用 ticker 集合**（忽略权重），留着权重给未来模拟盘按权重下单
- **两种添加方式**：
  - 上传 `.csv` / `.txt`（每行一个 ticker，或 `ticker,weight` 两列）
  - 搜索下拉（调 FMP `/search-symbol` + `/search-name`）
  - 所有 ticker 会先经过 `/api/ticker_verify` 校验存在性
- 支持编辑（改名 / 增减 ticker / 改权重）。**编辑对历史回测无影响**——回测任务创建时冻结快照（`watchlist_snapshot`）。

### 回测两种股票池路径

| universe 值 | 合成方式 | 速度 | 能否支持任意 ticker |
|---|---|---|---|
| `SP500` / `MAG7` | 读预算好的 `factor_values.parquet` | ms 级 | 否（必须先跑 pipeline）|
| `watchlist:<uuid>` | `src/backtest/adhoc.py` 即时拉价格 + 现算因子 | 秒级 | 是 |

### 重要：升级到 v2/v3 需重跑 pipeline

回测合成依赖每个因子的 `factor_values.parquet`（v2）和股票池的 `open.parquet`（v3，next_open 模式用）。**请用以下命令重建一次**：

```bash
# 全量重建（SP500 + MAG7）
python scripts/run_mvp.py --update

# 仅 MAG7（快速验证）
python scripts/run_mvp.py --update --only-universe MAG7
```

> **重要**：v3（成交模型）新增了 `open.parquet` 宽表。如果 SP500/MAG7 还没用 v3 重跑过，预设池跑 next_open 模式时会**自动降级回 close 模式**并在日志里告警。Watchlist 路径不受影响（adhoc 实时拉的数据已包含 open）。

---

## 扩展新因子

在 `src/factors/` 新建一个文件，继承 `FactorBase`：

```python
from src.factors.base import FactorBase
import pandas as pd

class Volatility20D(FactorBase):
    name = "VOL_20D"
    direction = +1  # IC 自动判断，可留 0

    def compute(self, price_df: pd.DataFrame) -> pd.DataFrame:
        returns = price_df.pct_change()
        return returns.rolling(20).std()
```

然后在 `configs/default.yaml` 的 `factors.enabled` 中加入 `VOL_20D` 即可。

---

## License

MIT
