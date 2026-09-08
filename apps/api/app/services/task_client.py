# 檔案路徑: video-pipeline/apps/api/app/services/task_client.py
# 產生時間: 2026-06-24 21:26 +08:00
# 版本: v1.0
# 模組定位:
#   FastAPI API 伺服器端的 Celery Task 客戶端連線層。
# 主要責任:
#   1. 提供向 Celery worker 投遞任務的介面 (如 enqueue_preview_job)。
# --------------------------------------------------------------------------

from celery import Celery

from pipeline.settings import get_settings


settings = get_settings()
celery_client = Celery(
    "video_pipeline_client",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)


def enqueue_preview_job(project_id: str):
    return celery_client.send_task("video_pipeline.preview_project", args=[project_id])


def enqueue_asr_job(project_id: str):
    return celery_client.send_task("video_pipeline.run_asr_job", args=[project_id], queue="asr")
