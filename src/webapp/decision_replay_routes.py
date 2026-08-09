"""HTML and JSON routes for frozen backtest/paper decision replay."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.backtest import store as bt_store
from src.decision_replay.query import (
    date_snapshot,
    get_snapshot,
    replay_meta,
    stock_history,
)
from src.papertrading.store import (
    account_dir,
    list_accounts,
    load_account,
    load_account_artifacts,
)
from src.webapp.results_store import list_universes


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()


def _universes() -> list[str]:
    return list_universes()


def _source_context(kind: str, source_id: str) -> dict[str, Any]:
    if kind == "backtest":
        source = bt_store.load_task(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Backtest not found")
        run_dir = bt_store.BACKTEST_ROOT / source_id
        strategy = source.get("strategy_snapshot") or {}
        return {
            "kind": kind,
            "kind_label": "回测",
            "source": source,
            "run_dir": run_dir,
            "name": source.get("name") or source_id[:8],
            "strategy": strategy,
            "universe": source.get("universe") or "",
            "back_href": f"/backtests/{source_id}",
            "rerun_href": (
                f"/backtests/new?strategy_id={source.get('strategy_id')}"
                if source.get("strategy_id")
                else "/backtests/new"
            ),
        }

    if kind == "paper":
        source = load_account(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Paper account not found")
        strategy = source.get("strategy_snapshot") or {}
        return {
            "kind": kind,
            "kind_label": "模拟盘",
            "source": source,
            "run_dir": account_dir(source_id),
            "name": source.get("name") or source_id[:8],
            "strategy": strategy,
            "universe": source.get("universe") or "",
            "back_href": f"/paper/{source_id}",
            "rerun_href": f"/paper/{source_id}",
        }

    raise HTTPException(status_code=404, detail="Unknown replay source")


def _events(kind: str, source_id: str) -> dict[str, Any]:
    if kind == "backtest":
        artifacts = bt_store.load_task_artifacts(source_id)
        return {
            "trades": artifacts.get("trades"),
            "orders": None,
            "fills": None,
        }
    artifacts = load_account_artifacts(source_id)
    return {
        "trades": None,
        "orders": artifacts.get("orders"),
        "fills": artifacts.get("fills"),
    }


def _snapshot_or_404(kind: str, source_id: str):
    context = _source_context(kind, source_id)
    snapshot = get_snapshot(context["run_dir"])
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="该运行记录没有决策回放快照",
        )
    return snapshot


def _page(request: Request, kind: str, source_id: str):
    context = _source_context(kind, source_id)
    snapshot = get_snapshot(context["run_dir"])
    available = snapshot is not None
    manifest = snapshot.manifest if snapshot is not None else {}
    return templates.TemplateResponse(request, "decision_replay.html", {
        "title": f"策略决策回放 · {context['name']}",
        "available": available,
        "manifest": manifest,
        "source_id": source_id,
        "source_kind": kind,
        "source_kind_label": context["kind_label"],
        "source_name": context["name"],
        "source": context["source"],
        "strategy": context["strategy"],
        "universe_label": context["universe"],
        "back_href": context["back_href"],
        "rerun_href": context["rerun_href"],
        "universes": _universes(),
    })


@router.get("/decision-replay", response_class=HTMLResponse)
def decision_replay_index(request: Request):
    sources: list[dict[str, Any]] = []
    for task in bt_store.list_tasks():
        source_id = str(task.get("id") or "")
        if not source_id:
            continue
        sources.append(
            {
                "kind": "backtest",
                "kind_label": "回测",
                "id": source_id,
                "name": task.get("name") or source_id[:8],
                "status": task.get("status") or "—",
                "universe": task.get("universe") or "—",
                "available": get_snapshot(bt_store.BACKTEST_ROOT / source_id) is not None,
                "href": f"/backtests/{source_id}/decisions",
            }
        )
    for account in list_accounts():
        source_id = str(account.get("id") or "")
        if not source_id:
            continue
        sources.append(
            {
                "kind": "paper",
                "kind_label": "模拟盘",
                "id": source_id,
                "name": account.get("name") or source_id[:8],
                "status": account.get("status") or "—",
                "universe": account.get("universe") or "—",
                "available": get_snapshot(account_dir(source_id)) is not None,
                "href": f"/paper/{source_id}/decisions",
            }
        )
    return templates.TemplateResponse(
        request,
        "decision_replay_index.html",
        {"title": "决策回放", "sources": sources},
    )


@router.get("/backtests/{source_id}/decisions", response_class=HTMLResponse)
def backtest_replay_page(request: Request, source_id: UUID):
    return _page(request, "backtest", str(source_id))


@router.get("/paper/{source_id}/decisions", response_class=HTMLResponse)
def paper_replay_page(request: Request, source_id: UUID):
    return _page(request, "paper", str(source_id))


def _meta(kind: str, source_id: str):
    snapshot = _snapshot_or_404(kind, source_id)
    return JSONResponse(replay_meta(snapshot))


def _date(kind: str, source_id: str, date: str | None):
    snapshot = _snapshot_or_404(kind, source_id)
    try:
        payload = date_snapshot(
            snapshot,
            date,
            **_events(kind, source_id),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(payload)


def _stock(kind: str, source_id: str, ticker: str):
    snapshot = _snapshot_or_404(kind, source_id)
    try:
        payload = stock_history(snapshot, ticker)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(payload)


@router.get("/api/backtests/{source_id}/decision-replay/meta")
def backtest_replay_meta(source_id: UUID):
    return _meta("backtest", str(source_id))


@router.get("/api/backtests/{source_id}/decision-replay")
def backtest_replay_date(
    source_id: UUID,
    date: str | None = Query(None),
):
    return _date("backtest", str(source_id), date)


@router.get("/api/backtests/{source_id}/decision-replay/stocks/{ticker}")
def backtest_replay_stock(source_id: UUID, ticker: str):
    return _stock("backtest", str(source_id), ticker)


@router.get("/api/paper/{source_id}/decision-replay/meta")
def paper_replay_meta(source_id: UUID):
    return _meta("paper", str(source_id))


@router.get("/api/paper/{source_id}/decision-replay")
def paper_replay_date(
    source_id: UUID,
    date: str | None = Query(None),
):
    return _date("paper", str(source_id), date)


@router.get("/api/paper/{source_id}/decision-replay/stocks/{ticker}")
def paper_replay_stock(source_id: UUID, ticker: str):
    return _stock("paper", str(source_id), ticker)


__all__ = ["router", "templates"]
