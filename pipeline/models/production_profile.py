# 檔案路徑: video-pipeline/pipeline/models/production_profile.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   製片政策集合 ProductionProfile 定義與預設組合。
# 主要責任:
#   1. 以政策欄位描述一部片的製作方式，而非以內容種類分類。
#   2. 提供 preset 作為政策預設值的載入來源。
#   3. 定義各 QC 維度在此製作方式下的權重。
# 重要約束:
#   preset_id 只用於載入預設值，不得參與任何執行期邏輯判斷。
#   Router、renderer 與 QC 一律只讀政策欄位，禁止出現
#   `if profile.preset_id == "comic_drama"` 這類分支，否則漫劇、短劇、
#   電影會各自長出一套 pipeline。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field


class MotionPolicy(StrEnum):
    STATIC = "static"
    SELECTIVE_I2V = "selective_i2v"
    FULL_MOTION = "full_motion"


class DialoguePolicy(StrEnum):
    NARRATION = "narration"
    MULTI_VOICE = "multi_voice"


class LipSyncPolicy(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class QualityTier(StrEnum):
    DRAFT = "draft"
    HIGH = "high"


class RenderMode(StrEnum):
    SLIDESHOW = "slideshow"
    SHOT_ASSEMBLY = "shot_assembly"


class ShotDurationPolicy(BaseModel):
    min_seconds: float = Field(default=2.0, gt=0)
    max_seconds: float = Field(default=6.0, gt=0)

    def clamp_ms(self, duration_ms: int) -> int:
        low = int(self.min_seconds * 1000)
        high = int(self.max_seconds * 1000)
        return max(low, min(high, duration_ms))


class QCWeights(BaseModel):
    """各 QC 維度在此製作方式下的相對權重。0 表示該維度不列入加權。"""

    # 單鏡頭
    prompt_adherence: float = 1.0
    temporal_stability: float = 1.0
    motion_quality: float = 1.0
    camera_control: float = 1.0
    artifact_severity: float = 1.0
    identity_consistency: float = 1.0
    facial_acting: float = 1.0
    # 跨鏡頭
    cross_shot_identity: float = 1.0
    wardrobe_continuity: float = 1.0
    location_continuity: float = 1.0
    lip_sync_quality: float = 1.0


class ProductionProfile(BaseModel):
    preset_id: str = "slideshow_legacy"

    aspect_ratio: str = "16:9"
    motion_policy: MotionPolicy = MotionPolicy.STATIC
    dialogue_policy: DialoguePolicy = DialoguePolicy.NARRATION
    lip_sync_policy: LipSyncPolicy = LipSyncPolicy.NONE
    shot_duration: ShotDurationPolicy = Field(default_factory=ShotDurationPolicy)
    quality_tier: QualityTier = QualityTier.DRAFT
    render_mode: RenderMode = RenderMode.SLIDESHOW
    target_duration_seconds: int = Field(default=90, gt=0)
    qc_weights: QCWeights = Field(default_factory=QCWeights)

    @property
    def requires_lip_sync(self) -> bool:
        return self.lip_sync_policy is LipSyncPolicy.REQUIRED

    @property
    def uses_shot_assembly(self) -> bool:
        return self.render_mode is RenderMode.SHOT_ASSEMBLY


# --------------------------------------------------------------------------
# Preset：僅為政策欄位的預設值組合，不具任何執行期語義
# --------------------------------------------------------------------------

_PRESETS: dict[str, dict] = {
    # 既有 FFmpeg 單圖投影片路徑。保留為一組 preset，而非獨立的程式分支。
    "slideshow_legacy": {
        "aspect_ratio": "16:9",
        "motion_policy": MotionPolicy.STATIC,
        "dialogue_policy": DialoguePolicy.NARRATION,
        "lip_sync_policy": LipSyncPolicy.NONE,
        "shot_duration": {"min_seconds": 8.0, "max_seconds": 8.0},
        "quality_tier": QualityTier.DRAFT,
        "render_mode": RenderMode.SLIDESHOW,
        "target_duration_seconds": 600,
    },
    # 第一階段主要驗證目標：60-90 秒直式 AI 漫劇。
    # 角色一致性遠比嘴型重要，故 lip_sync 權重壓低。
    "comic_drama_high": {
        "aspect_ratio": "9:16",
        "motion_policy": MotionPolicy.SELECTIVE_I2V,
        "dialogue_policy": DialoguePolicy.MULTI_VOICE,
        "lip_sync_policy": LipSyncPolicy.OPTIONAL,
        "shot_duration": {"min_seconds": 2.0, "max_seconds": 6.0},
        "quality_tier": QualityTier.HIGH,
        "render_mode": RenderMode.SHOT_ASSEMBLY,
        "target_duration_seconds": 90,
        "qc_weights": {
            "cross_shot_identity": 3.0,
            "identity_consistency": 3.0,
            "wardrobe_continuity": 2.0,
            "temporal_stability": 2.0,
            "prompt_adherence": 1.5,
            "location_continuity": 1.0,
            "motion_quality": 1.0,
            "facial_acting": 1.0,
            "camera_control": 0.5,
            "artifact_severity": 1.5,
            "lip_sync_quality": 0.3,
        },
    },
    # 短劇：臉部表演與嘴型開始重要。
    "short_drama_high": {
        "aspect_ratio": "9:16",
        "motion_policy": MotionPolicy.FULL_MOTION,
        "dialogue_policy": DialoguePolicy.MULTI_VOICE,
        "lip_sync_policy": LipSyncPolicy.REQUIRED,
        "shot_duration": {"min_seconds": 2.0, "max_seconds": 8.0},
        "quality_tier": QualityTier.HIGH,
        "render_mode": RenderMode.SHOT_ASSEMBLY,
        "target_duration_seconds": 180,
        "qc_weights": {
            "cross_shot_identity": 3.0,
            "identity_consistency": 3.0,
            "facial_acting": 2.5,
            "lip_sync_quality": 2.5,
            "wardrobe_continuity": 2.0,
            "temporal_stability": 2.0,
            "prompt_adherence": 1.5,
            "motion_quality": 1.5,
            "location_continuity": 1.0,
            "camera_control": 1.0,
            "artifact_severity": 1.5,
        },
    },
    # 電影模式：運鏡與場景連戲權重提高。
    "film_high": {
        "aspect_ratio": "16:9",
        "motion_policy": MotionPolicy.FULL_MOTION,
        "dialogue_policy": DialoguePolicy.MULTI_VOICE,
        "lip_sync_policy": LipSyncPolicy.REQUIRED,
        "shot_duration": {"min_seconds": 3.0, "max_seconds": 12.0},
        "quality_tier": QualityTier.HIGH,
        "render_mode": RenderMode.SHOT_ASSEMBLY,
        "target_duration_seconds": 600,
        "qc_weights": {
            "cross_shot_identity": 3.0,
            "identity_consistency": 3.0,
            "camera_control": 2.5,
            "location_continuity": 2.5,
            "temporal_stability": 2.0,
            "wardrobe_continuity": 2.0,
            "motion_quality": 2.0,
            "facial_acting": 2.0,
            "lip_sync_quality": 1.5,
            "prompt_adherence": 1.5,
            "artifact_severity": 2.0,
        },
    },
}

PRESET_IDS: tuple[str, ...] = tuple(_PRESETS)


def load_preset(preset_id: str) -> ProductionProfile:
    """依 preset 建立 ProductionProfile。回傳後即為純政策物件。"""
    if preset_id not in _PRESETS:
        raise ValueError(
            f"未知的 preset_id: {preset_id}. 可用: {', '.join(PRESET_IDS)}"
        )
    return ProductionProfile(preset_id=preset_id, **_PRESETS[preset_id])


def default_profile() -> ProductionProfile:
    """既有專案未指定 profile 時的預設值，維持原本投影片行為。"""
    return load_preset("slideshow_legacy")
