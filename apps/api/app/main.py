from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.routes import health, projects, render, web
from app.routes.web import WebException, error_page

app = FastAPI(title="Video Pipeline API", version="0.1.0")


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
