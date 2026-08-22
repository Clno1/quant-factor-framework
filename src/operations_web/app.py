"""FastAPI composition root for the independent operations website."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.operations.registry import OperationsRegistry, operations_registry
from src.operations.store import OperationsReader
from src.operations_web.security import install_operations_auth, operations_credentials


HERE = Path(__file__).resolve().parent
ASSET_VERSION = str(int(time.time()))
STATUS_LABELS = {
    "SCHEDULED": "等待运行",
    "RUNNING": "运行中",
    "SUCCESS": "正常",
    "DEGRADED": "部分异常",
    "SKIPPED": "正常跳过",
    "BLOCKED": "被阻断",
    "FAILED": "失败",
    "MISSED": "未按时运行",
    "STALE": "已过期",
    "DISABLED": "计划关闭",
    "UNKNOWN": "未知",
}
CATEGORY_LABELS = {
    "DATA": "数据生产",
    "RESEARCH": "研究发布",
    "TRADING": "交易验证",
    "MIGRATION": "上线专项",
    "DELIVERY": "业务通知",
    "MONITORING": "盘中监控",
    "INFRASTRUCTURE": "基础设施",
    "OTHER": "其他",
    "MARKET_DATA": "行情数据",
    "BROAD_US": "全美宽基",
    "RESOURCE": "服务器资源",
}
DELIVERY_STATUS_LABELS = {
    "SENT": "已发送",
    "SCHEDULED": "等待运行",
    "PENDING": "等待发送",
    "SENDING": "发送中",
    "NO_SIGNAL": "无信号",
    "SKIPPED_EMPTY": "无信号",
    "DRY_RUN": "未启用发送",
    "SHADOW": "影子模式",
    "NOT_ATTEMPTED": "未尝试",
    "DISABLED": "计划关闭",
    "MISSED": "未投递",
    "STALE": "证据过期",
    "DEGRADED": "部分异常",
    "BLOCKED": "被阻断",
    "FAILED": "发送失败",
    "UNKNOWN": "未知",
}
DELIVERY_STATUS_CLASSES = {
    "SENT": "success",
    "NO_SIGNAL": "skipped",
    "SKIPPED_EMPTY": "skipped",
    "SCHEDULED": "scheduled",
    "PENDING": "running",
    "SENDING": "running",
    "DRY_RUN": "disabled",
    "SHADOW": "disabled",
    "NOT_ATTEMPTED": "skipped",
    "DISABLED": "disabled",
    "MISSED": "missed",
    "STALE": "stale",
    "DEGRADED": "degraded",
    "BLOCKED": "blocked",
    "FAILED": "failed",
    "UNKNOWN": "unknown",
}


def _format_time(value: Any, timezone_name: str = "Asia/Singapore") -> str:
    if value in {None, ""}:
        return "-"
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
        return timestamp.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def create_app(
    *,
    registry: OperationsRegistry | None = None,
    reader: OperationsReader | None = None,
    credentials: tuple[str, str] | None | object = ...,
) -> FastAPI:
    selected_registry = registry or operations_registry()
    selected_reader = reader or OperationsReader(selected_registry.settings.snapshot_path)
    app = FastAPI(
        title="Quant Operations",
        description="Read-only operational evidence console",
        version="1.0.0",
    )
    auth = operations_credentials() if credentials is ... else credentials
    install_operations_auth(app, auth if isinstance(auth, tuple) else None)
    app.state.operations_reader = selected_reader
    app.state.operations_registry = selected_registry
    app.state.refresh_seconds = selected_registry.settings.refresh_seconds

    static_dir = HERE / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update({
        "asset_version": ASSET_VERSION,
        "status_label": lambda value: STATUS_LABELS.get(str(value or "UNKNOWN"), str(value or "未知")),
        "category_label": lambda value: CATEGORY_LABELS.get(str(value or "OTHER"), str(value or "其他")),
        "delivery_status_label": lambda value: DELIVERY_STATUS_LABELS.get(str(value or "UNKNOWN").upper(), str(value or "未知")),
        "delivery_status_class": lambda value: DELIVERY_STATUS_CLASSES.get(str(value or "UNKNOWN").upper(), "unknown"),
        "format_time": _format_time,
        "refresh_seconds": selected_registry.settings.refresh_seconds,
    })

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            template,
            {"page": "", "title": "运维总览", **context},
        )

    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "overview.html",
            page="overview",
            title="运维总览",
            payload=selected_reader.overview(),
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "jobs.html",
            page="jobs",
            title="任务中心",
            jobs=selected_reader.jobs(),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> HTMLResponse:
        payload = selected_reader.job(job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return render(
            request,
            "job_detail.html",
            page="jobs",
            title=str(payload["definition"]["display_name"]),
            payload=payload,
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> HTMLResponse:
        payload = selected_reader.run(run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return render(
            request,
            "run_detail.html",
            page="jobs",
            title="运行详情",
            run=payload,
        )

    @app.get("/freshness", response_class=HTMLResponse)
    def freshness_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "freshness.html",
            page="freshness",
            title="数据新鲜度",
            rows=selected_reader.freshness(),
        )

    @app.get("/deliveries", response_class=HTMLResponse)
    def deliveries_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "deliveries.html",
            page="deliveries",
            title="消息投递",
            rows=selected_reader.deliveries(),
        )

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "projects.html",
            page="projects",
            title="上线专项",
            rows=selected_reader.projects(),
        )

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents_page(request: Request, resolved: bool = False) -> HTMLResponse:
        return render(
            request,
            "incidents.html",
            page="incidents",
            title="异常中心",
            rows=selected_reader.incidents(include_resolved=resolved),
            include_resolved=resolved,
        )

    @app.get("/api/overview", response_class=JSONResponse)
    def api_overview() -> JSONResponse:
        return JSONResponse(_json_safe(selected_reader.overview()))

    @app.get("/api/jobs", response_class=JSONResponse)
    def api_jobs() -> JSONResponse:
        return JSONResponse(_json_safe({"jobs": selected_reader.jobs()}))

    @app.get("/api/jobs/{job_id}", response_class=JSONResponse)
    def api_job(job_id: str) -> JSONResponse:
        payload = selected_reader.job(job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(_json_safe(payload))

    @app.get("/api/runs/{run_id}", response_class=JSONResponse)
    def api_run(run_id: str) -> JSONResponse:
        payload = selected_reader.run(run_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return JSONResponse(_json_safe(payload))

    @app.get("/api/freshness", response_class=JSONResponse)
    def api_freshness() -> JSONResponse:
        return JSONResponse(_json_safe({"freshness": selected_reader.freshness()}))

    @app.get("/api/deliveries", response_class=JSONResponse)
    def api_deliveries() -> JSONResponse:
        return JSONResponse(_json_safe({"deliveries": selected_reader.deliveries()}))

    @app.get("/api/projects", response_class=JSONResponse)
    def api_projects() -> JSONResponse:
        return JSONResponse(_json_safe({"projects": selected_reader.projects()}))

    @app.get("/api/incidents", response_class=JSONResponse)
    def api_incidents(resolved: bool = False) -> JSONResponse:
        return JSONResponse(_json_safe({
            "incidents": selected_reader.incidents(include_resolved=resolved)
        }))

    @app.get("/healthz", response_class=JSONResponse)
    def health() -> JSONResponse:
        return JSONResponse({
            "status": "ok" if selected_reader.available() else "initializing",
            "snapshot_available": selected_reader.available(),
        })

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()


__all__ = ["app", "create_app"]
