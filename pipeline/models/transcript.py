# 檔案路徑: video-pipeline/pipeline/models/transcript.py
# 產生時間: 2026-06-24 21:21 +08:00
# 版本: v1.0
# 模組定位:
#   ASR 語音辨識轉寫與時間軸對齊資料結構模型。
# 主要責任:
#   1. 定義 TranscriptSegment 語音片段結構，記錄時間範圍與文字。
#   2. 定義 TranscriptImport 表示匯入後的轉寫檔案狀態。
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field



class TranscriptFormat(StrEnum):
    AUTO = "auto"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"
    TEXT = "text"


class TranscriptSegment(BaseModel):
    segment_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class TranscriptImport(BaseModel):
    format: TranscriptFormat
    source_name: str = "paste"
    imported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    segments: list[TranscriptSegment] = Field(default_factory=list)
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)

