# 檔案路徑: video-pipeline/pipeline/models/visual_contract.py
# 產生時間: 2026-06-24 21:22 +08:00
# 版本: v1.0
# 模組定位:
#   LAVA Storyboard 與視覺合規性契約模型。
# 主要責任:
#   1. 定義 VisualStyleGuide、CharacterProfile 與各畫面 ShotSpec 規格。
#   2. 定義 VisualQualityFinding 記錄畫面合規性缺失 (Blocker/Warning)。
#   3. 定義 VisualQualityReport 計算視覺合規總得分。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field



class ShotType(StrEnum):
    TALKING_HEAD = "talking_head"
    GENERATED_IMAGE = "generated_image"
    BROLL = "broll"
    SCREENCAST = "screencast"
    TITLE_CARD = "title_card"
    UNKNOWN = "unknown"


class VisualStyleGuide(BaseModel):
    style_name: str = "documentary explainer"
    aspect_ratio: str = "16:9"
    lighting: str = "clean soft contrast"
    color_grade: str = "natural contrast, no heavy color cast"
    typography: str = "large readable sans-serif captions"
    negative_prompts: list[str] = Field(default_factory=list)


class CharacterProfile(BaseModel):
    character_id: str
    role: str
    description: str
    continuity_notes: list[str] = Field(default_factory=list)


class ShotSpec(BaseModel):
    cue_id: str
    shot_type: ShotType
    prompt: str
    camera: str
    composition: str
    continuity: str
    negative_prompt: str
    quality_checks: list[str] = Field(default_factory=list)


class VisualQualityContract(BaseModel):
    project_id: str
    style_guide: VisualStyleGuide
    characters: list[CharacterProfile] = Field(default_factory=list)
    shots: list[ShotSpec] = Field(default_factory=list)


class VisualQualityFinding(BaseModel):
    code: str
    severity: str = "warning"
    cue_id: str | None = None
    message: str


class VisualQualityReport(BaseModel):
    project_id: str
    score: int = Field(ge=0, le=100)
    findings: list[VisualQualityFinding] = Field(default_factory=list)

