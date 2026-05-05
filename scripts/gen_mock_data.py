"""
离线生成模拟行情，用于在 Yahoo 被限流时本地验证整个 pipeline。

运行：
    python scripts/gen_mock_data.py --n-tickers 30 --years 3

产物与 yfinance 真实下载一致：data/raw/ohlcv/<ticker>.parquet
之后 `python scripts/run_mvp.py --universe 30 --no-web` 会直接读缓存，跳过联网。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.universe import get_universe  # noqa: E402
from src.utils.io import ensure_dir, write_parquet  # noqa: E402


def _simulate_one(n_days: int, seed: int) -> pd.DataFrame:
    """几何布朗运动模拟一只股票的日线（含跨股票共同风险因子，便于 IC 有信号）。"""
    rng = np.random.default_rng(seed)
    # 个股日收益：漂移 + 特质波动
    mu = rng.normal(0.0004, 0.0003)           # 日均 ~0.04%
    sigma = abs(rng.normal(0.018, 0.004))     # 日波动 ~1.8%
    rets = rng.normal(mu, sigma, size=n_days)
    close = 20.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n_days)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n_days)))
    open_ = close * (1 + rng.normal(0, 0.003, n_days))
    volume = rng.integers(1_000_000, 10_000_000, size=n_days).astype(float)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "adj_close": close, "volume": volume,
    })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-tickers", type=int, default=30, help="模拟股票数")
    parser.add_argument("--years", type=float, default=3.0, help="模拟年数")
    args = parser.parse_args()

    n_days = int(args.years * 252)
    end = pd.Timestamp(CONFIG.date_range.end)
    dates = pd.bdate_range(end=end, periods=n_days)

    # 从 S&P 500 成分股里取前 N 个 ticker（复用已缓存的 universe）
    uni = get_universe().head(args.n_tickers)
    tickers = uni["ticker"].tolist()

    out_dir = PROJECT_ROOT / CONFIG.data.raw_dir / "ohlcv"
    ensure_dir(out_dir)

    for i, t in enumerate(tickers):
        df = _simulate_one(n_days, seed=1000 + i)
        df.index = dates
        df.index.name = "date"
        write_parquet(df, out_dir / f"{t}.parquet")

    print(f"[mock] Generated {len(tickers)} tickers × {n_days} days into {out_dir}")
    print(f"[mock] Next: python scripts/run_mvp.py --universe {len(tickers)} --no-web")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
