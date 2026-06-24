# 檔案路徑: video-pipeline/pipeline/models/review.py
# 產生時間: 2026-06-24 21:20 +08:00
# 版本: v1.0
# 模組定位:
#   專案審核生命週期狀態與決策日誌追蹤模型。
# 主要責任:
#   1. 定義 ReviewStatus (Draft, Cues_Ready, Assets_Review, Approved 等)。
#   2. 定義 DecisionLogEntry 提供每一步管線或人工審核狀態轉移之可觀測日誌軌跡。
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field



class ReviewStatus(StrEnum):
    DRAFT = "draft"
    CUES_READY = "cues_ready"
    ASSETS_REVIEW = "assets_review"
    PREVIEW_READY = "preview_ready"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class DecisionLogEntry(BaseModel):
    action: str
    actor: str = "local"
    note: str | None = None
    from_status: ReviewStatus | None = None
    to_status: ReviewStatus | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
