# 檔案路徑: video-pipeline/apps/api/app/routes/projects.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   FastAPI 專案管理 API 路由模組。
# 主要責任:
#   1. 提供建立專案與取得專案資料的 API 端點。
#   2. 管理與資料庫的直接互動，確保存檔狀態持久化。
# --------------------------------------------------------------------------

from pathlib import Path

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.deps import settings_dep, get_session
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.settings import Settings
from pipeline.stages.run_mvp import run_mvp_pipeline
from pipeline.utils.files import read_json_model

router = APIRouter()

PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class CreateProjectRequest(BaseModel):
    title: str = Field(min_length=1)
    script_markdown: str = Field(min_length=1)
    language: str = "en"
    persona: str | None = None


@router.post("")
async def create_project(payload: CreateProjectRequest, settings: Settings = Depends(settings_dep), session: Session = Depends(get_session)) -> ProductionArtifact:
    return await run_mvp_pipeline(
        session=session,
        settings=settings,
        title=payload.title,
        script_markdown=payload.script_markdown,
        language=payload.language,
        persona=payload.persona,
    )


@router.get("/{project_id}")
def get_project(project_id: str, settings: Settings = Depends(settings_dep), session: Session = Depends(get_session)) -> ProductionArtifact:
    if not PROJECT_ID_RE.match(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID format")
    artifact = session.get(ProductionArtifact, project_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Project not found")
    return artifact

