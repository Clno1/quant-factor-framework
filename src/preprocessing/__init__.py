"""
因子预处理：去极值 -> 中性化（可选） -> 最终 Z-score 标准化
"""
from src.preprocessing.winsorize import winsorize_mad, winsorize_3sigma
from src.preprocessing.standardize import zscore_cs
from src.preprocessing.neutralize import neutralize_industry
from src.preprocessing.pipeline import preprocess_factor

__all__ = [
    "winsorize_mad",
    "winsorize_3sigma",
    "zscore_cs",
    "neutralize_industry",
    "preprocess_factor",
]
