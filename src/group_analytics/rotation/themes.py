"""Explicit, versioned observation baskets, not historical index constituents."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import re

SOURCE_URL = "https://x.com/Corsica267/status/2060844170370277377"


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    cohort: str
    benchmark: str
    proxy: str | None = None
    members: tuple[str, ...] = ()
    known_at: str = "2026-05-31"
    version: str = "public-v1-defaults"
    source: str = SOURCE_URL
    effective_from: str | None = None

    def __post_init__(self):
        if self.cohort not in {"technology", "sectors"}:
            raise ValueError("Unsupported cohort")
        if self.benchmark != {"technology": "QQQ", "sectors": "SPY"}[self.cohort]:
            raise ValueError("Cohort benchmark mismatch")
        if not self.name or len(self.name) > 48 or not self.version or not self.source:
            raise ValueError("Invalid theme provenance or name")
        date.fromisoformat(self.known_at)
        if self.effective_from is not None:
            date.fromisoformat(self.effective_from)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.id):
            raise ValueError("Invalid theme id")
        symbols = (*self.members, self.benchmark, *((self.proxy,) if self.proxy else ()))
        if not symbols or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", s) for s in symbols):
            raise ValueError("Invalid theme symbol")
        if not self.proxy and not self.members:
            raise ValueError("Basket needs members")
        if len(set(self.members)) != len(self.members):
            raise ValueError("Duplicate member")

    def record(self):
        return {**asdict(self), "effective_from": self.effective_from or self.known_at}


def default_themes() -> tuple[Theme, ...]:
    tech = (
        ("semiconductors", "半导体", "SMH"), ("software", "软件", "IGV"),
        ("cloud", "云计算", "SKYY"), ("cybersecurity", "网络安全", "CIBR"),
        ("robotics", "机器人与自动化", "BOTZ"), ("ai", "AI综合", "AIQ"),
    )
    baskets = (
        ("optical", "光通信", ("ANET", "CIEN", "COHR", "LITE", "AAOI")),
        ("memory", "存储", ("MU", "WDC", "STX")),
        ("infrastructure", "数据中心电力与散热", ("VRT", "ETN", "PWR", "CEG", "GEV")),
        ("hyperscalers", "云巨头／AI买方", ("MSFT", "AMZN", "GOOGL", "META")),
        ("hardware", "AI硬件卖方", ("NVDA", "AVGO", "AMD", "ANET")),
    )
    sectors = (
        ("technology", "信息技术", "XLK"), ("financials", "金融", "XLF"),
        ("healthcare", "医疗保健", "XLV"), ("industrials", "工业", "XLI"),
        ("discretionary", "可选消费", "XLY"), ("staples", "必需消费", "XLP"),
        ("energy", "能源", "XLE"), ("materials", "原材料", "XLB"),
        ("utilities", "公用事业", "XLU"), ("realestate", "房地产", "XLRE"),
        ("communications", "通信服务", "XLC"),
    )
    return (
        *(Theme(i, n, "technology", "QQQ", proxy=p) for i, n, p in tech),
        *(Theme(i, n, "technology", "QQQ", members=m) for i, n, m in baskets),
        *(Theme("sector_" + i, n, "sectors", "SPY", proxy=p,
                known_at="2026-09-09", version="sector-etf-v1",
                source="project:sector-etf-proxies") for i, n, p in sectors),
    )


def required_symbols(themes):
    return sorted({s for t in themes for s in (*t.members, t.benchmark, *((t.proxy,) if t.proxy else ()))})
