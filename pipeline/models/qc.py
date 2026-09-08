# 檔案路徑: video-pipeline/pipeline/models/qc.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   單鏡頭品質 VariantQC 與跨鏡頭連戲 ContinuityQC 資料表。
# 主要責任:
#   1. 分離「單一 clip 自身品質」與「鏡頭之間的關係品質」兩種語義。
#   2. 以可為 NULL 的評分欄位表達 N/A，而非以 0 分混淆。
#   3. 提供依 ProductionProfile 權重計算的加權分數。
# 為何分兩張表:
#   temporal_stability 是一支 clip 自己的性質；cross_shot_identity 是
#   shot A 對 shot B 的關係。若混在同一列，「Shot 8 的一致性」究竟是
#   對照 Shot 7 還是 Shot 1 將無法回答。ContinuityQC 因此帶 ref_shot_id
#   與 scope。
# 評分方向:
#   除 artifact_severity 外，所有評分皆為 0-100 且越高越好。
#   artifact_severity 越高代表瑕疵越嚴重，加權時以 (100 - severity) 計入。
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Column, String
from sqlmodel import Field, SQLModel

from pipeline.models.production_profile import QCWeights

SCORE_MIN = 0
SCORE_MAX = 100


def _weighted(pairs: list[tuple[int | None, float]]) -> float | None:
    """對 (分數, 權重) 配對計算加權平均。None 分數與零權重不列入。"""
    total_weight = 0.0
    total_score = 0.0
    for score, weight in pairs:
        if score is None or weight <= 0:
            continue
        total_score += score * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return round(total_score / total_weight, 2)


class VariantQC(SQLModel, table=True):
    """單一 clip 自身的品質評分。與 AssetVariant 一對一。"""

    __tablename__ = "variant_qc"

    qc_id: str = Field(primary_key=True)
    variant_id: str = Field(index=True, unique=True)
    project_id: str = Field(index=True)
    shot_id: str = Field(index=True)

    # 0-100，None 表示此鏡頭不適用該維度
    prompt_adherence: int | None = None
    temporal_stability: int | None = None
    motion_quality: int | None = None
    camera_control: int | None = None
    artifact_severity: int | None = None  # 越高越糟
    # 單一鏡頭內角色身份是否穩定。與 ContinuityQC.cross_shot_identity 不同：
    # 一顆鏡頭內臉部漂移屬於此處，兩顆鏡頭像不像同一人屬於跨鏡頭連戲。
    identity_consistency: int | None = None
    # 表情演技。對話類鏡頭的主要判準之一，非對話鏡頭留空。
    facial_acting: int | None = None

    # 商業指標，獨立於品質分數
    usable_without_repair: bool | None = Field(default=None, index=True)
    human_correction_minutes: float | None = None
    retries_to_usable: int | None = None
    # 平台生成耗時，用於比較各平台效率
    generation_seconds: float | None = None

    reviewer: str = "local"
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def weighted_score(self, weights: QCWeights) -> float | None:
        cleanliness = (
            None if self.artifact_severity is None else SCORE_MAX - self.artifact_severity
        )
        return _weighted(
            [
                (self.prompt_adherence, weights.prompt_adherence),
                (self.temporal_stability, weights.temporal_stability),
                (self.motion_quality, weights.motion_quality),
                (self.camera_control, weights.camera_control),
                (cleanliness, weights.artifact_severity),
                (self.identity_consistency, weights.identity_consistency),
                (self.facial_acting, weights.facial_acting),
            ]
        )


class ContinuityScope(StrEnum):
    PAIR = "pair"
    SCENE = "scene"
    SEQUENCE = "sequence"


class ContinuityQC(SQLModel, table=True):
    """鏡頭之間的連戲評分。scope 決定比較範圍。"""

    __tablename__ = "continuity_qc"

    qc_id: str = Field(primary_key=True)
    project_id: str = Field(index=True)

    # 值域為 ContinuityScope
    scope: str = Field(
        default=ContinuityScope.PAIR.value,
        sa_column=Column(String, nullable=False, index=True),
    )

    shot_id: str = Field(index=True)
    # scope=pair 時為必要的比較對象；scene/sequence 時可為空
    ref_shot_id: str | None = Field(default=None, index=True)
    scene_id: str | None = Field(default=None, index=True)

    variant_id: str | None = Field(default=None, index=True)
    ref_variant_id: str | None = None

    cross_shot_identity: int | None = None
    wardrobe_continuity: int | None = None
    location_continuity: int | None = None
    lip_sync_quality: int | None = None

    reviewer: str = "local"
    notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def weighted_score(self, weights: QCWeights) -> float | None:
        return _weighted(
            [
                (self.cross_shot_identity, weights.cross_shot_identity),
                (self.wardrobe_continuity, weights.wardrobe_continuity),
                (self.location_continuity, weights.location_continuity),
                (self.lip_sync_quality, weights.lip_sync_quality),
            ]
        )
