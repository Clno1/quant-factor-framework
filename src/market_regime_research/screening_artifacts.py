"""Immutable artifact publication and report rendering for Stage B screens."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import html
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping
from uuid import uuid4

import pandas as pd

from src.market_regime_research import (
    SCREENING_ALGORITHM_VERSION,
    SCREENING_SCHEMA_VERSION,
)
from src.market_regime_research.artifacts import (
    file_sha256,
    write_strict_json,
)
from src.market_regime_research.models import ScreeningRunResult
from src.market_regime_research.screening import ScreeningOutputs
from src.market_regime_research.settings import ScreeningSettings

CANDIDATE_REGISTRY_FILE = "candidate_registry.parquet"
EVENT_STUDIES_FILE = "univariate_event_studies.parquet"
FOLD_RESULTS_FILE = "walk_forward_folds.parquet"
PREDICTIONS_FILE = "walk_forward_predictions.parquet"
SCORECARD_FILE = "candidate_scorecard.parquet"
MANIFEST_FILE = "screening_manifest.json"
SUMMARY_FILE = "screening_summary.json"
REPORT_FILE = "research_report.html"
RUN_FILE = "screening.json"

_SCREENING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_screening_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"screen_{stamp}_{uuid4().hex[:8]}"


def _format_metric(value: Any, *, percentage: bool = False) -> str:
    if value is None or pd.isna(value):
        return "-"
    numeric = float(value)
    if percentage:
        return f"{numeric:.2%}"
    return f"{numeric:.4f}"


def _render_score_rows(scorecard: pd.DataFrame) -> str:
    confirmatory = scorecard.loc[
        scorecard["hypothesis_tier"] == "confirmatory"
    ].copy()
    confirmatory["_status_order"] = confirmatory["screening_status"].map(
        {
            "STAGE_1_PASS": 0,
            "STAGE_1_FAIL": 1,
            "INSUFFICIENT_EVIDENCE": 2,
        }
    ).fillna(3)
    confirmatory = confirmatory.sort_values(
        ["_status_order", "side", "horizon", "fdr_q_value"],
        kind="stable",
    )
    rows: list[str] = []
    for item in confirmatory.itertuples(index=False):
        status = html.escape(str(item.screening_status))
        css_class = (
            "pass"
            if item.screening_status == "STAGE_1_PASS"
            else "insufficient"
            if item.screening_status == "INSUFFICIENT_EVIDENCE"
            else "fail"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.feature_name))}</td>"
            f"<td>{html.escape(str(item.side))}</td>"
            f"<td>{int(item.horizon)}d</td>"
            f"<td>{html.escape(str(item.expected_direction))}</td>"
            f"<td>{int(item.development_event_episodes)}</td>"
            f"<td>{int(item.walk_forward_folds)}</td>"
            f"<td>{_format_metric(item.average_precision)}</td>"
            f"<td>{_format_metric(item.prevalence)}</td>"
            f"<td>{_format_metric(item.brier_skill, percentage=True)}</td>"
            f"<td>{_format_metric(item.fdr_q_value)}</td>"
            f'<td><span class="status {css_class}">{status}</span></td>'
            "</tr>"
        )
    return "\n".join(rows)


def render_research_report(
    outputs: ScreeningOutputs,
    *,
    screening_id: str,
    source_run_id: str,
) -> str:
    """Build a small self-contained audit report, not a production UI."""
    summary = outputs.summary
    status_counts = summary.get("status_counts", {})
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>大盘顶底信号有效性筛选</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #17202a;
      background: #f5f7f8;
    }}
    body {{ margin: 0; }}
    header {{ background: #102a2e; color: white; padding: 28px 32px; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; letter-spacing: 0; }}
    header p {{ margin: 4px 0; color: #d8e7e6; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 24px 32px 40px; }}
    .summary {{ display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr)); gap: 12px; }}
    .metric {{ background: white; border: 1px solid #dce3e5; border-radius: 6px; padding: 14px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 5px; }}
    .notice {{ margin: 18px 0; padding: 14px 16px; border-left: 4px solid #bd7a13; background: #fff9ed; }}
    .table-wrap {{ overflow-x: auto; background: white; border: 1px solid #dce3e5; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e8edef; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #edf3f3; position: sticky; top: 0; }}
    .status {{ display: inline-block; padding: 3px 7px; border-radius: 4px; font-weight: 650; }}
    .pass {{ color: #0c6637; background: #e5f5ec; }}
    .fail {{ color: #9b2c2c; background: #fbeaea; }}
    .insufficient {{ color: #805b10; background: #fff3d8; }}
    @media (max-width: 820px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .summary {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>大盘顶底信号有效性筛选</h1>
    <p>筛选运行：{html.escape(screening_id)}</p>
    <p>研究数据：{html.escape(source_run_id)}</p>
  </header>
  <main>
    <section class="summary">
      <div class="metric">全部测试<strong>{int(summary["candidate_tests"])}</strong></div>
      <div class="metric">事前登记<strong>{int(summary["confirmatory_tests"])}</strong></div>
      <div class="metric">阶段通过<strong>{int(summary["stage_1_pass_count"])}</strong></div>
      <div class="metric">阶段失败<strong>{int(status_counts.get("STAGE_1_FAIL", 0))}</strong></div>
      <div class="metric">证据不足<strong>{int(status_counts.get("INSUFFICIENT_EVIDENCE", 0))}</strong></div>
    </section>
    <div class="notice">
      2022-01-01 起的数据仍为封存集，本报告没有评估它。
      “STAGE_1_PASS”只表示通过 G1-G6，仍需参数扰动、增量信息、
      经济价值和影子运行，当前没有任何指标获准进入生产信号。
    </div>
    <section>
      <h2>事前登记候选</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>特征</th><th>方向</th><th>期限</th><th>预期</th>
              <th>事件数</th><th>WF folds</th><th>PR-AUC</th>
              <th>基准率</th><th>Brier Skill</th><th>FDR q</th><th>结论</th>
            </tr>
          </thead>
          <tbody>
            {_render_score_rows(outputs.scorecard)}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""


def publish_screening_run(
    *,
    output_root: Path,
    outputs: ScreeningOutputs,
    settings: ScreeningSettings,
    source_manifest: Mapping[str, Any],
    screening_id: str | None = None,
) -> ScreeningRunResult:
    """Atomically publish one immutable screening directory and latest pointer."""
    screening_id = str(screening_id or generate_screening_id())
    if (
        not _SCREENING_ID.fullmatch(screening_id)
        or screening_id in {".", ".."}
    ):
        raise ValueError("screening_id contains unsafe characters")
    source_run_id = str(source_manifest.get("run_id", "")).strip()
    if not source_run_id:
        raise ValueError("source_manifest requires run_id")

    screenings_root = Path(output_root) / "screenings"
    screenings_root.mkdir(parents=True, exist_ok=True)
    final_dir = screenings_root / screening_id
    if final_dir.exists():
        raise FileExistsError(f"Screening run already exists: {screening_id}")
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{screening_id}.", dir=str(screenings_root))
    )
    try:
        paths = {
            CANDIDATE_REGISTRY_FILE: temporary_dir / CANDIDATE_REGISTRY_FILE,
            EVENT_STUDIES_FILE: temporary_dir / EVENT_STUDIES_FILE,
            FOLD_RESULTS_FILE: temporary_dir / FOLD_RESULTS_FILE,
            PREDICTIONS_FILE: temporary_dir / PREDICTIONS_FILE,
            SCORECARD_FILE: temporary_dir / SCORECARD_FILE,
        }
        outputs.candidate_registry.to_parquet(
            paths[CANDIDATE_REGISTRY_FILE],
            compression="snappy",
            index=False,
        )
        outputs.event_studies.to_parquet(
            paths[EVENT_STUDIES_FILE],
            compression="snappy",
            index=False,
        )
        outputs.fold_results.to_parquet(
            paths[FOLD_RESULTS_FILE],
            compression="snappy",
            index=False,
        )
        outputs.predictions.to_parquet(
            paths[PREDICTIONS_FILE],
            compression="snappy",
            index=False,
        )
        outputs.scorecard.to_parquet(
            paths[SCORECARD_FILE],
            compression="snappy",
            index=False,
        )

        summary = {
            **outputs.summary,
            "screening_id": screening_id,
            "source_research_run_id": source_run_id,
            "created_at": _utc_now(),
            "schema_version": SCREENING_SCHEMA_VERSION,
            "algorithm_version": SCREENING_ALGORITHM_VERSION,
        }
        summary_path = temporary_dir / SUMMARY_FILE
        write_strict_json(summary_path, summary)
        report_path = temporary_dir / REPORT_FILE
        report_path.write_text(
            render_research_report(
                outputs,
                screening_id=screening_id,
                source_run_id=source_run_id,
            ),
            encoding="utf-8",
        )
        artifact_paths = {
            **paths,
            SUMMARY_FILE: summary_path,
            REPORT_FILE: report_path,
        }
        artifact_hashes = {
            name: file_sha256(path) for name, path in artifact_paths.items()
        }
        manifest_path = temporary_dir / MANIFEST_FILE
        write_strict_json(
            manifest_path,
            {
                "screening_id": screening_id,
                "created_at": summary["created_at"],
                "schema_version": SCREENING_SCHEMA_VERSION,
                "algorithm_version": SCREENING_ALGORITHM_VERSION,
                "source": dict(source_manifest),
                "settings": asdict(settings),
                "artifacts": artifact_hashes,
                "holdout_status": "SEALED_NOT_EVALUATED",
            },
        )
        write_strict_json(
            temporary_dir / RUN_FILE,
            {
                "screening_id": screening_id,
                "status": "SUCCESS",
                "created_at": summary["created_at"],
                "source_research_run_id": source_run_id,
                "stage_1_pass_count": summary["stage_1_pass_count"],
                "production_approved_count": 0,
            },
        )
        os.replace(temporary_dir, final_dir)

        pointer_temp = Path(output_root) / f".latest_screening.{uuid4().hex}.tmp"
        write_strict_json(
            pointer_temp,
            {
                "screening_id": screening_id,
                "screening_path": f"screenings/{screening_id}",
                "source_research_run_id": source_run_id,
                "published_at": _utc_now(),
            },
        )
        os.replace(pointer_temp, Path(output_root) / "latest_screening.json")
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise

    return ScreeningRunResult(
        screening_id=screening_id,
        screening_dir=final_dir,
        candidate_registry_path=final_dir / CANDIDATE_REGISTRY_FILE,
        event_studies_path=final_dir / EVENT_STUDIES_FILE,
        fold_results_path=final_dir / FOLD_RESULTS_FILE,
        predictions_path=final_dir / PREDICTIONS_FILE,
        scorecard_path=final_dir / SCORECARD_FILE,
        manifest_path=final_dir / MANIFEST_FILE,
        summary_path=final_dir / SUMMARY_FILE,
        report_path=final_dir / REPORT_FILE,
    )


__all__ = [
    "CANDIDATE_REGISTRY_FILE",
    "EVENT_STUDIES_FILE",
    "FOLD_RESULTS_FILE",
    "MANIFEST_FILE",
    "PREDICTIONS_FILE",
    "REPORT_FILE",
    "SCORECARD_FILE",
    "SUMMARY_FILE",
    "generate_screening_id",
    "publish_screening_run",
    "render_research_report",
]
