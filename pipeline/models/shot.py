# 檔案路徑: video-pipeline/pipeline/models/shot.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   鏡頭計畫 ShotPlan 與角色身份包 CharacterIdentityPack。
# 主要責任:
#   1. 以導演意圖描述每顆鏡頭：拍什麼、誰在畫面、如何運鏡、目標多長。
#   2. 定義 provider-neutral 的角色身份，平台專屬設定隔離於 provider_bindings。
# 層級定位:
#   ShotPlan 是「導演意圖真相」。target_duration_ms 是意圖而非結果；
#   實際生成長度記於 AssetVariant.actual_duration_ms，
#   成片佔用長度記於 TimelineClip.used_duration_ms。三者不得混用。
#   VisualContract 的 ShotSpec 應視為 ShotPlan 的生成/QC view，非獨立來源。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field

from pipeline.models.capability import Capability
from pipeline.models.visual_contract import ShotType


class ShotFraming(StrEnum):
    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    MEDIUM = "medium"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"
    OVER_THE_SHOULDER = "over_the_shoulder"
    POINT_OF_VIEW = "point_of_view"


class CameraMovement(StrEnum):
    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    DOLLY = "dolly"
    TRACKING = "tracking"
    ZOOM = "zoom"
    HANDHELD = "handheld"
    CRANE = "crane"


class CameraSpec(BaseModel):
    framing: ShotFraming = ShotFraming.MEDIUM
    movement: CameraMovement = CameraMovement.STATIC
    lens: str | None = None
    notes: str | None = None

    def describe(self) -> str:
        parts = [self.framing.value.replace("_", " "), self.movement.value]
        if self.lens:
            parts.append(self.lens)
        return ", ".join(parts)


class ProviderBinding(BaseModel):
    """平台專屬的身份錨定設定。

    seed、LoRA、平台 reference ID 一律放在這裡，不放進 CharacterIdentityPack
    本體。多數商用平台的 seed 不可控，不能作為角色身份的核心欄位。
    """

    provider: str
    model_id: str | None = None
    seed: int | None = None
    lora_ref: str | None = None
    reference_id: str | None = None
    parameters: dict = Field(default_factory=dict)


class CharacterIdentityPack(BaseModel):
    """provider-neutral 的角色身份。所有 *_ref 欄位皆為 ReferenceAsset.asset_id。"""

    character_id: str
    role: str = ""
    identity_description: str = ""
    continuity_notes: list[str] = Field(default_factory=list)

    canonical_face_ref: str | None = None
    canonical_fullbody_ref: str | None = None
    wardrobe_refs: list[str] = Field(default_factory=list)
    voice_identity_ref: str | None = None

    provider_bindings: dict[str, ProviderBinding] = Field(default_factory=dict)

    @property
    def reference_asset_ids(self) -> list[str]:
        ids = [
            self.canonical_face_ref,
            self.canonical_fullbody_ref,
            self.voice_identity_ref,
            *self.wardrobe_refs,
        ]
        return [asset_id for asset_id in ids if asset_id]

    def binding_for(self, provider: str) -> ProviderBinding | None:
        return self.provider_bindings.get(provider)


class ShotPlan(BaseModel):
    shot_id: str
    beat_id: str
    scene_id: str
    order: int = Field(ge=0)

    shot_type: ShotType = ShotType.UNKNOWN
    capability: Capability = Capability.VIDEO_I2V
    camera: CameraSpec = Field(default_factory=CameraSpec)

    prompt: str = ""
    negative_prompt: str = ""

    character_refs: list[str] = Field(default_factory=list)  # CharacterIdentityPack.character_id
    first_frame_ref: str | None = None  # ReferenceAsset.asset_id
    reference_asset_ids: list[str] = Field(default_factory=list)

    target_duration_ms: int = Field(default=4000, gt=0)
    aspect_ratio: str = "9:16"
    continuity_notes: str | None = None
    constraints: dict = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)

    @property
    def target_duration_seconds(self) -> float:
        return self.target_duration_ms / 1000.0
