"""
FastAPI 主应用。

启动：
    uvicorn src.webapp.app:app --host 0.0.0.0 --port 8000
或：
    python scripts/run_mvp.py --serve
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger(__name__)

_HERE = Path(__file__).resolve().parent

# 静态资源版本号：进程启动时间戳，每次重启 Web 服务都会刷新缓存
ASSET_VER = str(int(time.time()))


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

    # 注册路由 + 注入模板全局变量
    from src.webapp.routes import router, templates
    templates.env.globals["asset_ver"] = ASSET_VER
    app.include_router(router)

    log.info("FastAPI app created. Title=%s asset_ver=%s", CONFIG.webapp.title, ASSET_VER)
    return app


# 供 uvicorn 直接导入
app = create_app()
