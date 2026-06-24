from fastapi import APIRouter
from pydantic import BaseModel

from app.services.task_client import enqueue_preview_job

router = APIRouter()


class PreviewJobRequest(BaseModel):
    project_id: str


@router.post("/preview")
def create_preview_job(payload: PreviewJobRequest) -> dict:
    task = enqueue_preview_job(payload.project_id)
    return {"task_id": task.id, "project_id": payload.project_id}

