# 檔案路徑: video-pipeline/pipeline/models/timeline.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   剪輯決策 EditDecision 與時間線 TimelineClip。
# 主要責任:
#   1. 以 in/out point、retime 與轉場描述如何使用一段生成素材。
#   2. 產生成片時間線，作為 CueLedger 的唯一上游。
# 層級定位:
#   剪輯不等於把生成檔案接起來。三個時長必須分離：
#     ShotPlan.target_duration_ms      導演意圖
#     AssetVariant.actual_duration_ms  生成結果
#     TimelineClip.used_duration_ms    成片佔用
#   對白戲常由音訊決定節奏，素材長度不應直接成為時間線長度。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class TransitionType(StrEnum):
    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPE = "wipe"


class RetimeMode(StrEnum):
    NONE = "none"
    SPEED = "speed"
    HOLD = "hold"


class EditDecision(BaseModel):
    """如何從一個候選素材取用一段內容。"""

    edit_id: str
    shot_id: str
    variant_id: str
    order: int = Field(ge=0)

    # 來源素材的取用區間
    in_point_ms: int = Field(default=0, ge=0)
    out_point_ms: int = Field(gt=0)

    retime_mode: RetimeMode = RetimeMode.NONE
    retime_factor: float = Field(default=1.0, gt=0)
    hold_ms: int = Field(default=0, ge=0)

    transition_in: TransitionType = TransitionType.CUT
    transition_out: TransitionType = TransitionType.CUT
    transition_duration_ms: int = Field(default=0, ge=0)

    note: str | None = None

    @model_validator(mode="after")
    def _validate_points(self) -> "EditDecision":
        if self.out_point_ms <= self.in_point_ms:
            raise ValueError(
                f"out_point_ms ({self.out_point_ms}) 必須大於 "
                f"in_point_ms ({self.in_point_ms})"
            )
        return self

    @property
    def source_duration_ms(self) -> int:
        return self.out_point_ms - self.in_point_ms

    @property
    def used_duration_ms(self) -> int:
        """成片實際佔用長度：取用區間經 retime 後再加上停格。"""
        if self.retime_mode is RetimeMode.SPEED:
            retimed = int(self.source_duration_ms / self.retime_factor)
        else:
            retimed = self.source_duration_ms
        return retimed + self.hold_ms


class TimelineClip(BaseModel):
    """時間線上的一段。由 EditDecision 展開而得，帶絕對起點。"""

    clip_id: str
    edit_id: str
    shot_id: str
    variant_id: str
    order: int = Field(ge=0)

    timeline_start_ms: int = Field(ge=0)
    used_duration_ms: int = Field(gt=0)
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)

    local_path: str | None = None

    @property
    def timeline_end_ms(self) -> int:
        return self.timeline_start_ms + self.used_duration_ms


class Timeline(BaseModel):
    project_id: str
    clips: list[TimelineClip] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @property
    def total_duration_ms(self) -> int:
        if not self.clips:
            return 0
        return max(clip.timeline_end_ms for clip in self.clips)

    @property
    def total_duration_seconds(self) -> float:
        return self.total_duration_ms / 1000.0


def build_timeline(project_id: str, decisions: list[EditDecision]) -> Timeline:
    """依 order 將剪輯決策串成連續時間線。

    每段起點為前一段終點，長度取自 EditDecision.used_duration_ms，
    刻意不採用素材檔案長度。
    """
    clips: list[TimelineClip] = []
    cursor_ms = 0
    for index, decision in enumerate(sorted(decisions, key=lambda d: d.order)):
        duration = decision.used_duration_ms
        clips.append(
            TimelineClip(
                clip_id=f"clip_{index + 1:04d}",
                edit_id=decision.edit_id,
                shot_id=decision.shot_id,
                variant_id=decision.variant_id,
                order=index,
                timeline_start_ms=cursor_ms,
                used_duration_ms=duration,
                source_in_ms=decision.in_point_ms,
                source_out_ms=decision.out_point_ms,
            )
        )
        cursor_ms += duration
    return Timeline(project_id=project_id, clips=clips)
