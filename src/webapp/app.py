"""
FastAPI 主应用。

启动：
    uvicorn src.webapp.app:app --host 0.0.0.0 --port 8000
或：
    python scripts/run_mvp.py --serve
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)

_HERE = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    app = FastAPI(
        title=CONFIG.webapp.title,
        description="Multi-Factor Quant Research Dashboard",
        version="0.1.0",
    )

    # 挂载静态资源
    static_dir = _HERE / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 注册路由
    from src.webapp.routes import router
    app.include_router(router)

    log.info("FastAPI app created. Title=%s", CONFIG.webapp.title)
    return app


# 供 uvicorn 直接导入
app = create_app()
