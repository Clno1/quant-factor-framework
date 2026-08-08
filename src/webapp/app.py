"""
FastAPI 主应用。

启动：
    uvicorn src.webapp.app:app --host 127.0.0.1 --port 8000
或：
    python scripts/run_mvp.py --serve-only
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import CONFIG
from src.utils.logger import get_logger
from src.webapp.security import (
    basic_auth_credentials,
    install_basic_auth_middleware,
)

log = get_logger(__name__)

_HERE = Path(__file__).resolve().parent

# 静态资源版本号：进程启动时间戳，每次重启 Web 服务都会刷新缓存
ASSET_VER = str(int(time.time()))


def _recover_application_state() -> tuple[int, int]:
    """Recover interrupted jobs and activate the WAITING_FOR_DATA monitor."""
    interrupted = 0
    submitted = 0
    try:
        from src.backtest.store import startup_recovery

        interrupted = startup_recovery()
        if interrupted:
            log.warning(
                "startup_recovery: %d stale backtest tasks marked as failed.",
                interrupted,
            )
    except Exception as exc:  # noqa: BLE001
        log.error("startup_recovery failed: %s", exc)

    try:
        from src.backtest.runner import get_runner

        submitted = get_runner().reconcile_waiting()
        if submitted:
            log.info(
                "startup_recovery: submitted %d backtests whose data is ready.",
                submitted,
            )
    except Exception as exc:  # noqa: BLE001
        log.error("WAITING_FOR_DATA startup reconciliation failed: %s", exc)
    return interrupted, submitted


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _recover_application_state()
    yield


def _strict_config_flag(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError("feature flags must be booleans")


def create_app() -> FastAPI:
    app = FastAPI(
        title=CONFIG.webapp.title,
        description="Multi-Factor Quant Research Dashboard",
        version="0.1.0",
        lifespan=_lifespan,
    )
    install_basic_auth_middleware(app, basic_auth_credentials())

    # 挂载静态资源
    static_dir = _HERE / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 注册路由 + 注入模板全局变量
    from src.webapp.breakout_routes import (
        router as breakout_router,
        templates as breakout_templates,
    )
    from src.webapp.routes import router, templates
    from src.webapp.routes_v2 import router_v2, templates as templates_v2
    from src.webapp.decision_replay_routes import (
        router as decision_replay_router,
        templates as decision_replay_templates,
    )
    try:
        writer_enabled = _strict_config_flag(
            os.environ.get(
                "GROUP_ANALYTICS_ENABLED",
                CONFIG.group_analytics.enabled,
            )
        )
        group_analytics_enabled = writer_enabled and _strict_config_flag(
            os.environ.get(
                "GROUP_ANALYTICS_WEB_ENABLED",
                getattr(CONFIG.group_analytics, "web_enabled", False),
            )
        )
    except AttributeError:
        group_analytics_enabled = False
    templates.env.globals["asset_ver"] = ASSET_VER
    templates_v2.env.globals["asset_ver"] = ASSET_VER
    decision_replay_templates.env.globals["asset_ver"] = ASSET_VER
    breakout_templates.env.globals["asset_ver"] = ASSET_VER
    templates.env.globals["group_analytics_enabled"] = group_analytics_enabled
    templates_v2.env.globals["group_analytics_enabled"] = group_analytics_enabled
    decision_replay_templates.env.globals["group_analytics_enabled"] = group_analytics_enabled
    breakout_templates.env.globals["group_analytics_enabled"] = group_analytics_enabled
    app.include_router(router)
    app.include_router(router_v2)
    app.include_router(decision_replay_router)
    app.include_router(breakout_router)

    # Optional composition-root registration.  No factor/backtest/paper module
    # imports the group domain, and disabled deployments do not import its Web
    # adapter at all.
    if group_analytics_enabled:
        from src.webapp.group_analytics_routes import (
            router as group_analytics_router,
            templates as group_analytics_templates,
        )
        group_analytics_templates.env.globals["asset_ver"] = ASSET_VER
        group_analytics_templates.env.globals["group_analytics_enabled"] = True
        app.include_router(group_analytics_router)

    log.info(
        "FastAPI app created. Title=%s asset_ver=%s group_analytics=%s",
        CONFIG.webapp.title,
        ASSET_VER,
        group_analytics_enabled,
    )
    return app


# 供 uvicorn 直接导入
app = create_app()
