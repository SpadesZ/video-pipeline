# 檔案路徑: video-pipeline/pipeline/models/narrative.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   敘事中介表示 NarrativeIR 與其 Scene / Beat 結構。
# 主要責任:
#   1. 以敘事單位描述故事，不含任何時間軸或鏡頭資訊。
#   2. 作為 ShotPlan 的唯一上游來源。
# 層級定位:
#   NarrativeIR 是「故事真相」。它不知道有幾顆鏡頭、每顆幾秒，
#   那是 ShotPlan 的職責；也不知道成片如何剪接，那是 EditDecision 的職責。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field

from pipeline.models.visual_contract import VisualStyleGuide


class BeatIntent(StrEnum):
    """節拍在故事中的功能，而非它的長度或畫面。"""

    SETUP = "setup"
    DEVELOPMENT = "development"
    TURN = "turn"
    REVEAL = "reveal"
    PAYOFF = "payoff"
    TRANSITION = "transition"


class DialogueLine(BaseModel):
    character_id: str
    text: str
    delivery_note: str | None = None


class Beat(BaseModel):
    """最小敘事單位。一個 Beat 可能對應零至多顆鏡頭。"""

    beat_id: str
    scene_id: str
    order: int = Field(ge=0)
    intent: BeatIntent = BeatIntent.DEVELOPMENT
    summary: str = ""
    narration: str | None = None
    dialogue: list[DialogueLine] = Field(default_factory=list)
    character_ids: list[str] = Field(default_factory=list)
    emotional_note: str | None = None

    @property
    def has_dialogue(self) -> bool:
        return bool(self.dialogue)


class Scene(BaseModel):
    scene_id: str
    order: int = Field(ge=0)
    summary: str = ""
    location: str | None = None
    time_of_day: str | None = None
    mood: str | None = None
    location_ref: str | None = None  # ReferenceAsset.asset_id
    beats: list[Beat] = Field(default_factory=list)


class NarrativeIR(BaseModel):
    project_id: str
    logline: str = ""
    style_guide: VisualStyleGuide = Field(default_factory=VisualStyleGuide)
    scenes: list[Scene] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @property
    def beat_count(self) -> int:
        return sum(len(scene.beats) for scene in self.scenes)

    def iter_beats(self):
        """依場景與節拍順序走訪全部 Beat。"""
        for scene in sorted(self.scenes, key=lambda s: s.order):
            for beat in sorted(scene.beats, key=lambda b: b.order):
                yield scene, beat

    def find_beat(self, beat_id: str) -> Beat | None:
        for _scene, beat in self.iter_beats():
            if beat.beat_id == beat_id:
                return beat
        return None
