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

