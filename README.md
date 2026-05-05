# Multi-Factor Quant Research Framework

从零搭建的**美股多因子量化研究框架**。MVP 阶段先用一个 6 个月动量因子跑通完整闭环：
**数据获取 → 因子计算 → 预处理 → IC 分析 → 五分位回测 → 可视化 → FastAPI Web 展示**。

> 框架结构遵循"分层解耦 + 面向数据管道"设计，便于后续扩展到多因子合成、行业中性化、因子相关性分析等高级功能。

---

## 特性

- 🎯 **股票池**：S&P 500 成分股（自动从 Wikipedia 抓取）
- 📈 **数据源**：`yfinance`（免费、无需 token）
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

### 2. 运行完整 pipeline

```bash
python scripts/run_mvp.py
```

这会完成：
1. 抓取 S&P 500 成分股列表
2. 下载 5 年日线行情（Parquet 缓存，二次运行秒开）
3. 计算 MOM_6M 因子 + 预处理
4. 计算 Rank IC 序列 + 汇总指标
5. 跑五分位回测 + Long-Short 组合
6. 生成可视化图表到 `outputs/`

### 3. 启动 Web 服务

```bash
uvicorn src.webapp.app:app --host 0.0.0.0 --port 8000
```

然后浏览器访问 `http://<你的电脑IP>:8000`（手机同 WiFi 可直接打开）。

---

## 核心设计

- **FactorBase 抽象基类**：所有因子继承统一接口，新增因子只需写一个子类，主流程零修改。
- **向量化回测**：使用 `pd.qcut` + `groupby` 做五分位分组，500 股票 × 5 年回测 < 1 秒。
- **前视偏差防范**：IC 计算严格用 `factor_t` 对齐 `forward_return_{t→t+N}`。
- **零 JS 前端**：FastAPI 后端渲染 Jinja2 模板，前端用 Plotly.js CDN，无需前端构建。

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
