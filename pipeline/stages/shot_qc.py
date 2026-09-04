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
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import ProductionProfile, default_profile
from pipeline.models.qc import SCORE_MAX, SCORE_MIN, ContinuityQC, ContinuityScope, VariantQC
from pipeline.models.review import DecisionLogEntry
from pipeline.models.variant import AssetVariant, CapabilityJob, UNUSABLE_JOB_STATUSES
from pipeline.stages.variant_importer import load_owned_variant

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
        # 驗證歸屬，避免帶著別的專案的 variant_id 就能跨專案寫入評分
        variant = load_owned_variant(session, artifact.project_id, variant_id)

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

    # 引用的候選必須屬於本專案，否則會把別的專案的素材寫進連戲紀錄
    from pipeline.db import engine as _engine

    with Session(_engine) as session:
        for referenced in (variant_id, ref_variant_id):
            if referenced:
                load_owned_variant(session, artifact.project_id, referenced)

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


class ShotOutcome(StrEnum):
    """鏡頭的統計歸類。互斥，每顆鏡頭恰好落入一種。

    判定優先序為 usable > completed > failed > dispatched > planned，
    先定義清楚才不會在 E6 累積資料後才發現口徑要改。

    planned    有 ShotPlan，但從未派工。
    dispatched 已建立派工記錄，但尚未匯回任何候選，且工作未全數失敗。
    failed     已派工且工作全數落入終態失敗，仍無任何候選。
    completed  已匯回候選，但沒有任何一個被評為可用。
    usable     至少有一個候選被評為 usable_without_repair。
    """

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    FAILED = "failed"
    COMPLETED = "completed"
    USABLE = "usable"


class ShotQCSummary(BaseModel):
    shot_id: str
    outcome: ShotOutcome = ShotOutcome.PLANNED
    job_count: int = 0
    variant_count: int = 0
    scored_count: int = 0
    usable_count: int = 0
    best_variant_id: str | None = None
    best_score: float | None = None
    total_correction_minutes: float = 0.0
    total_retries: int = 0

    @property
    def usable_variant_rate(self) -> float | None:
        """可用候選佔該鏡頭全部候選的比例。與 usable-shot rate 不同。"""
        if not self.variant_count:
            return None
        return round(self.usable_count / self.variant_count, 3)


class ProjectQCSummary(BaseModel):
    """專案品質彙整。

    usable-shot rate 的分母刻意有兩種，因為兩者回答不同問題：
      of_planned    分鏡表上的鏡頭有多少比例最終可用，反映整支片能否完成。
      of_dispatched 實際送去生成的鏡頭有多少比例可用，反映模型好壞。
    只從「已有候選的鏡頭」建分母會漏掉完全生不出東西的鏡頭，
    把兩個指標都高估。
    """

    project_id: str
    shots: list[ShotQCSummary] = Field(default_factory=list)
    continuity_count: int = 0

    @property
    def planned_count(self) -> int:
        return len(self.shots)

    @property
    def dispatched_count(self) -> int:
        return sum(1 for shot in self.shots if shot.job_count > 0)

    @property
    def usable_count(self) -> int:
        return sum(1 for shot in self.shots if shot.outcome is ShotOutcome.USABLE)

    @property
    def failed_count(self) -> int:
        return sum(1 for shot in self.shots if shot.outcome is ShotOutcome.FAILED)

    @property
    def abandoned_count(self) -> int:
        """分鏡表上存在但從未派工的鏡頭。"""
        return sum(1 for shot in self.shots if shot.outcome is ShotOutcome.PLANNED)

    @property
    def total_variants(self) -> int:
        return sum(shot.variant_count for shot in self.shots)

    @property
    def total_usable_variants(self) -> int:
        return sum(shot.usable_count for shot in self.shots)

    @property
    def usable_shot_rate_of_planned(self) -> float | None:
        if not self.planned_count:
            return None
        return round(self.usable_count / self.planned_count, 3)

    @property
    def usable_shot_rate_of_dispatched(self) -> float | None:
        if not self.dispatched_count:
            return None
        return round(self.usable_count / self.dispatched_count, 3)

    @property
    def total_correction_minutes(self) -> float:
        return round(sum(shot.total_correction_minutes for shot in self.shots), 2)

    @property
    def correction_minutes_per_usable_shot(self) -> float | None:
        """每產出一顆可用鏡頭所需的人工時間。決定系統有無商業價值。"""
        if not self.usable_count:
            return None
        return round(self.total_correction_minutes / self.usable_count, 2)

    @property
    def retries_per_usable_shot(self) -> float | None:
        if not self.usable_count:
            return None
        return round(
            sum(shot.total_retries for shot in self.shots) / self.usable_count, 2
        )

    def outcome_counts(self) -> dict[str, int]:
        counts = {outcome.value: 0 for outcome in ShotOutcome}
        for shot in self.shots:
            counts[shot.outcome.value] += 1
        return counts


def _classify(entry: ShotQCSummary, all_jobs_failed: bool) -> ShotOutcome:
    if entry.usable_count:
        return ShotOutcome.USABLE
    if entry.variant_count:
        return ShotOutcome.COMPLETED
    if entry.job_count:
        return ShotOutcome.FAILED if all_jobs_failed else ShotOutcome.DISPATCHED
    return ShotOutcome.PLANNED


def summarize_project_qc(
    artifact: ProductionArtifact, profile: ProductionProfile | None = None
) -> ProjectQCSummary:
    """依 ProductionProfile 的權重彙整專案品質概況。

    分母取自 artifact.shot_plans，因此完全沒有候選的鏡頭也會計入，
    不會因為「沒生出東西」而從統計中消失。
    """
    from pipeline.db import engine

    project_id = artifact.project_id
    active_profile = profile or artifact.production_profile or default_profile()
    weights = active_profile.qc_weights
    summary = ProjectQCSummary(project_id=project_id)

    with Session(engine) as session:
        variants = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == project_id)
        ).all()
        qc_rows = session.exec(
            select(VariantQC).where(VariantQC.project_id == project_id)
        ).all()
        jobs = session.exec(
            select(CapabilityJob).where(CapabilityJob.project_id == project_id)
        ).all()
        summary.continuity_count = len(
            session.exec(
                select(ContinuityQC).where(ContinuityQC.project_id == project_id)
            ).all()
        )

    qc_by_variant = {row.variant_id: row for row in qc_rows}

    # 分母來自分鏡表，而非只有已產生候選的鏡頭
    by_shot: dict[str, ShotQCSummary] = {
        shot.shot_id: ShotQCSummary(shot_id=shot.shot_id)
        for shot in artifact.shot_plans
    }
    jobs_by_shot: dict[str, list[CapabilityJob]] = {}

    for job in jobs:
        if not job.shot_id:
            continue
        jobs_by_shot.setdefault(job.shot_id, []).append(job)
        entry = by_shot.setdefault(job.shot_id, ShotQCSummary(shot_id=job.shot_id))
        entry.job_count += 1

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
        if qc.retries_to_usable:
            entry.total_retries += qc.retries_to_usable

        score = qc.weighted_score(weights)
        if score is not None and (entry.best_score is None or score > entry.best_score):
            entry.best_score = score
            entry.best_variant_id = variant.variant_id

    for shot_id, entry in by_shot.items():
        shot_jobs = jobs_by_shot.get(shot_id, [])
        all_failed = bool(shot_jobs) and all(
            job.status in UNUSABLE_JOB_STATUSES for job in shot_jobs
        )
        entry.outcome = _classify(entry, all_failed)

    summary.shots = [by_shot[key] for key in sorted(by_shot)]
    return summary
