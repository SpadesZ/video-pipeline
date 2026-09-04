# 檔案路徑: video-pipeline/pipeline/stages/shot_qc.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   鏡頭品質評分階段。
# 主要責任:
#   1. 記錄單一 clip 的 VariantQC 與跨鏡頭的 ContinuityQC。
#   2. 依 ProductionProfile 的權重彙整專案品質概況。
# 說明:
#   評分欄位可為 None 表示該維度不適用。無對白鏡頭的嘴型分數必須留空，
#   若記為 0 分會與「嘴型極差」混淆，並在加權時把整體拉低。
#   商業指標（usable_without_repair、human_correction_minutes、
#   retries_to_usable）獨立於品質分數，是 Benchmark 的主要排序依據。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import ProductionProfile, default_profile
from pipeline.models.qc import SCORE_MAX, SCORE_MIN, ContinuityQC, ContinuityScope, VariantQC
from pipeline.models.review import DecisionLogEntry
from pipeline.models.variant import AssetVariant

logger = logging.getLogger("shot_qc")

VARIANT_SCORE_FIELDS = (
    "prompt_adherence",
    "temporal_stability",
    "motion_quality",
    "camera_control",
    "artifact_severity",
)

CONTINUITY_SCORE_FIELDS = (
    "cross_shot_identity",
    "wardrobe_continuity",
    "location_continuity",
    "lip_sync_quality",
)


class QCValidationError(ValueError):
    """評分超出容許範圍。"""


def validate_scores(scores: dict[str, int | None], allowed: tuple[str, ...]) -> dict:
    """檢查評分範圍。None 代表 N/A，一律放行。"""
    cleaned: dict[str, int | None] = {}
    for field in allowed:
        value = scores.get(field)
        if value is None:
            cleaned[field] = None
            continue
        try:
            numeric = int(value)
        except (TypeError, ValueError) as error:
            raise QCValidationError(f"{field} 必須為整數或留空") from error
        if not SCORE_MIN <= numeric <= SCORE_MAX:
            raise QCValidationError(
                f"{field} 應介於 {SCORE_MIN} 與 {SCORE_MAX} 之間，實際 {numeric}"
            )
        cleaned[field] = numeric
    return cleaned


def record_variant_qc(
    artifact: ProductionArtifact,
    variant_id: str,
    scores: dict[str, int | None],
    usable_without_repair: bool | None = None,
    human_correction_minutes: float | None = None,
    retries_to_usable: int | None = None,
    reviewer: str = "local",
    notes: str | None = None,
) -> VariantQC:
    """記錄單一候選的品質評分。同一候選重複評分會覆寫既有記錄。"""
    from pipeline.db import engine

    cleaned = validate_scores(scores, VARIANT_SCORE_FIELDS)

    with Session(engine) as session:
        variant = session.get(AssetVariant, variant_id)
        if variant is None:
            raise ValueError(f"找不到候選 {variant_id}")

        record = session.exec(
            select(VariantQC).where(VariantQC.variant_id == variant_id)
        ).first()
        if record is None:
            record = VariantQC(
                qc_id=f"vqc_{uuid4().hex[:12]}",
                variant_id=variant_id,
                project_id=variant.project_id,
                shot_id=variant.shot_id,
            )

        for field, value in cleaned.items():
            setattr(record, field, value)
        record.usable_without_repair = usable_without_repair
        record.human_correction_minutes = human_correction_minutes
        record.retries_to_usable = retries_to_usable
        record.reviewer = reviewer or "local"
        record.notes = notes

        session.add(record)
        session.commit()
        session.refresh(record)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="variant_qc_recorded",
            actor=reviewer or "local",
            note=f"{variant_id}: usable={usable_without_repair}",
        )
    )
    artifact.touch()
    return record


def record_continuity_qc(
    artifact: ProductionArtifact,
    shot_id: str,
    scores: dict[str, int | None],
    scope: str = ContinuityScope.PAIR.value,
    ref_shot_id: str | None = None,
    scene_id: str | None = None,
    variant_id: str | None = None,
    ref_variant_id: str | None = None,
    reviewer: str = "local",
    notes: str | None = None,
) -> ContinuityQC:
    """記錄跨鏡頭連戲評分。

    scope=pair 時必須指定 ref_shot_id，否則「與誰比較」無從得知。
    """
    if scope == ContinuityScope.PAIR.value and not ref_shot_id:
        raise QCValidationError("scope=pair 必須指定 ref_shot_id")

    cleaned = validate_scores(scores, CONTINUITY_SCORE_FIELDS)

    record = ContinuityQC(
        qc_id=f"cqc_{uuid4().hex[:12]}",
        project_id=artifact.project_id,
        scope=scope,
        shot_id=shot_id,
        ref_shot_id=ref_shot_id,
        scene_id=scene_id,
        variant_id=variant_id,
        ref_variant_id=ref_variant_id,
        reviewer=reviewer or "local",
        notes=notes,
        **cleaned,
    )

    from pipeline.db import engine

    with Session(engine) as session:
        session.add(record)
        session.commit()
        session.refresh(record)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="continuity_qc_recorded",
            actor=reviewer or "local",
            note=f"{shot_id} vs {ref_shot_id or scope}",
        )
    )
    artifact.touch()
    return record


class ShotQCSummary(BaseModel):
    shot_id: str
    variant_count: int = 0
    scored_count: int = 0
    usable_count: int = 0
    best_variant_id: str | None = None
    best_score: float | None = None
    total_correction_minutes: float = 0.0

    @property
    def usable_rate(self) -> float | None:
        if not self.variant_count:
            return None
        return round(self.usable_count / self.variant_count, 3)


class ProjectQCSummary(BaseModel):
    project_id: str
    shots: list[ShotQCSummary] = Field(default_factory=list)
    continuity_count: int = 0

    @property
    def total_variants(self) -> int:
        return sum(shot.variant_count for shot in self.shots)

    @property
    def total_usable(self) -> int:
        return sum(shot.usable_count for shot in self.shots)

    @property
    def usable_shot_rate(self) -> float | None:
        """至少有一個可用候選的鏡頭比例。這是第一階段的核心指標。"""
        if not self.shots:
            return None
        with_usable = sum(1 for shot in self.shots if shot.usable_count)
        return round(with_usable / len(self.shots), 3)

    @property
    def total_correction_minutes(self) -> float:
        return round(
            sum(shot.total_correction_minutes for shot in self.shots), 2
        )


def summarize_project_qc(
    project_id: str, profile: ProductionProfile | None = None
) -> ProjectQCSummary:
    """依 ProductionProfile 的權重彙整專案品質概況。"""
    from pipeline.db import engine

    active_profile = profile or default_profile()
    weights = active_profile.qc_weights
    summary = ProjectQCSummary(project_id=project_id)

    with Session(engine) as session:
        variants = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == project_id)
        ).all()
        qc_rows = session.exec(
            select(VariantQC).where(VariantQC.project_id == project_id)
        ).all()
        summary.continuity_count = len(
            session.exec(
                select(ContinuityQC).where(ContinuityQC.project_id == project_id)
            ).all()
        )

    qc_by_variant = {row.variant_id: row for row in qc_rows}
    by_shot: dict[str, ShotQCSummary] = {}

    for variant in variants:
        entry = by_shot.setdefault(
            variant.shot_id, ShotQCSummary(shot_id=variant.shot_id)
        )
        entry.variant_count += 1

        qc = qc_by_variant.get(variant.variant_id)
        if qc is None:
            continue
        entry.scored_count += 1
        if qc.usable_without_repair:
            entry.usable_count += 1
        if qc.human_correction_minutes:
            entry.total_correction_minutes += qc.human_correction_minutes

        score = qc.weighted_score(weights)
        if score is not None and (entry.best_score is None or score > entry.best_score):
            entry.best_score = score
            entry.best_variant_id = variant.variant_id

    summary.shots = [by_shot[key] for key in sorted(by_shot)]
    return summary
