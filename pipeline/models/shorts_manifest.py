# 檔案路徑: video-pipeline/pipeline/models/shorts_manifest.py
# 產生時間: 2026-06-25
# 版本: v1.0
# 模組定位:
#   短影片切片清單資料模型。
# 主要責任:
#   1. 定義 ShortItem 與 ShortsManifest 結構。
#   2. 記錄每個短片的 cue 範圍、Hook 文字、預估時長。

from pydantic import BaseModel, Field


class ShortItem(BaseModel):
    short_id: str
    parent_project_id: str
    title: str
    hook_text: str = ""  # 3-second hook text for shorts
    cue_ids: list[str] = Field(default_factory=list)
    start_ms: int = 0
    end_ms: int = 0
    estimated_duration_seconds: float = 0.0
    word_count: int = 0
    status: str = "draft"  # draft | rendered | approved


class ShortsManifest(BaseModel):
    parent_project_id: str
    shorts: list[ShortItem] = Field(default_factory=list)
    total_shorts: int = 0
    max_short_duration_seconds: float = 60.0
