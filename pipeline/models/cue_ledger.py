# 檔案路徑: video-pipeline/pipeline/models/cue_ledger.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   時間軸 Cue Ledger 與素材類型之資料模型結構定義。
# 主要責任:
#   1. 定義 AssetType 與 CueItem 元素架構。
#   2. 提供 CueLedger 資料容器供管線階段儲存傳遞。
# --------------------------------------------------------------------------

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class AssetType(StrEnum):
    NONE = "none"
    BROLL = "broll"
    GENERATED_IMAGE = "generated_image"
    SCREENCAST = "screencast"
    TITLE_CARD = "title_card"


class CueItem(BaseModel):
    cue_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    voice_text: str
    subtitle_text: str | None = None
    visual_prompt: str | None = None
    asset_type: AssetType = AssetType.NONE
    shorts_id: str | None = None
    risk_note: str | None = None

    @model_validator(mode="after")
    def validate_timings(self) -> "CueItem":
        if self.end_ms < self.start_ms:
            raise ValueError(f"end_ms ({self.end_ms}) must be greater than or equal to start_ms ({self.start_ms})")
        return self


class CueLedger(BaseModel):
    project_id: str
    cues: list[CueItem] = Field(default_factory=list)
    subtitles_path: str | None = None

