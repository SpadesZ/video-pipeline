# 檔案路徑: video-pipeline/apps/api/app/main.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   FastAPI 應用程式主入口模組。
# 主要責任:
#   1. 初始化應用程式與資料庫結構。
#   2. 設定全域路由、異常處理器與生命週期生命期管理。
# --------------------------------------------------------------------------

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.routes import health, projects, render, web
from app.routes.web import WebException, error_page
from pipeline.db import init_db, log_schema_status

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log_schema_status()
    yield

app = FastAPI(title="Video Pipeline API", version="0.1.0", lifespan=lifespan)

@app.exception_handler(WebException)
async def web_exception_handler(request: Request, exc: WebException):
    return error_page(
        title="Operation Failed",
        message=exc.detail,
        back_link=exc.back_link,
        status_code=exc.status_code,
    )

app.include_router(web.router)
app.include_router(health.router)
app.include_router(projects.router, prefix="/projects", tags=["projects"])
app.include_router(render.router, prefix="/render", tags=["render"])
