# 檔案路徑: video-pipeline/pipeline/stages/variant_importer.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   人工生成影片的匯入階段。
# 主要責任:
#   1. 將人工於平台產出的影片存檔並建立 AssetVariant。
#   2. 以 ffprobe 取得實際規格，與 ShotPlan 的目標值比對後記錄落差。
#   3. 以 request_hash 關聯 CapabilityJob 並推進其狀態。
#   4. 以 file_hash 提供冪等，同一支檔案重複匯入不會產生第二筆候選。
# 說明:
#   流程型態沿用 transcript_importer：解析輸入、重建下游狀態、
#   寫入 decision_log、落盤並入庫。
#   實際片長一律以檔案為準，不沿用 ShotPlan 的目標值。平台常無法精準命中
#   要求的長度，這個落差本身就是評估模型的指標。
# --------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.adapters.video.media_probe import MediaInfo, probe_media
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.review import DecisionLogEntry
from pipeline.models.shot import ShotPlan
from pipeline.models.variant import (
    AssetVariant,
    CapabilityJob,
    GenerationMode,
    JobStatus,
    VariantStatus,
)
from pipeline.settings import Settings

logger = logging.getLogger("variant_importer")

VARIANTS_DIRNAME = "variants"
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


class ImportedVariant(BaseModel):
    variant_id: str
    shot_id: str
    provider: str
    local_path: str
    file_hash: str
    actual_duration_ms: int | None = None
    resolution: str | None = None
    fps: float | None = None
    duplicate: bool = False
    warnings: list[str] = Field(default_factory=list)


class VariantImportReport(BaseModel):
    project_id: str
    shot_id: str
    provider: str
    imported: list[ImportedVariant] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    job_id: str | None = None

    @property
    def new_count(self) -> int:
        return sum(1 for item in self.imported if not item.duplicate)

    def summary(self) -> str:
        return (
            f"{self.new_count} new, {len(self.imported) - self.new_count} duplicate, "
            f"{len(self.skipped)} skipped for {self.shot_id}@{self.provider}"
        )


def variants_dir(settings: Settings, project_id: str, shot_id: str) -> Path:
    return (
        Path(settings.data_dir)
        / "projects"
        / project_id
        / VARIANTS_DIRNAME
        / shot_id
    )


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def find_shot(artifact: ProductionArtifact, shot_id: str) -> ShotPlan | None:
    return next(
        (shot for shot in artifact.shot_plans if shot.shot_id == shot_id), None
    )


def check_against_plan(shot: ShotPlan | None, media: MediaInfo) -> list[str]:
    """比對實際規格與鏡頭計畫，回傳落差描述。不阻擋匯入。"""
    warnings: list[str] = []
    if media.error:
        warnings.append(media.error)
    if shot is None:
        return warnings

    if media.duration_ms is not None:
        tolerance = max(200, int(shot.target_duration_ms * 0.1))
        delta = media.duration_ms - shot.target_duration_ms
        if abs(delta) > tolerance:
            warnings.append(
                f"片長 {media.duration_ms}ms 與目標 {shot.target_duration_ms}ms "
                f"相差 {delta:+d}ms"
            )

    actual_ratio = media.aspect_ratio
    if actual_ratio and shot.aspect_ratio and actual_ratio != shot.aspect_ratio:
        warnings.append(f"比例 {actual_ratio} 與目標 {shot.aspect_ratio} 不符")

    return warnings


def _resolve_job(
    session: Session,
    project_id: str,
    shot_id: str,
    provider: str,
    request_hash: str | None,
) -> CapabilityJob | None:
    if request_hash:
        job = session.exec(
            select(CapabilityJob).where(
                CapabilityJob.request_hash == request_hash,
                CapabilityJob.provider == provider,
            )
        ).first()
        if job is not None:
            return job
    return session.exec(
        select(CapabilityJob)
        .where(
            CapabilityJob.project_id == project_id,
            CapabilityJob.shot_id == shot_id,
            CapabilityJob.provider == provider,
        )
        .order_by(CapabilityJob.created_at.desc())
    ).first()


def import_variants(
    settings: Settings,
    artifact: ProductionArtifact,
    shot_id: str,
    provider: str,
    files: list[tuple[str, bytes]],
    request_hash: str | None = None,
    model_id: str | None = None,
    generation_mode: str | None = None,
    actor: str = "local",
    note: str | None = None,
) -> VariantImportReport:
    """匯入一顆鏡頭於單一平台產出的候選影片。"""
    from pipeline.db import engine

    report = VariantImportReport(
        project_id=artifact.project_id, shot_id=shot_id, provider=provider
    )
    if not files:
        raise ValueError("未提供任何檔案")

    shot = find_shot(artifact, shot_id)
    target_dir = variants_dir(settings, artifact.project_id, shot_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    with Session(engine) as session:
        job = _resolve_job(
            session, artifact.project_id, shot_id, provider, request_hash
        )
        report.job_id = job.job_id if job else None

        for filename, payload in files:
            suffix = Path(filename).suffix.lower()
            if suffix not in VIDEO_SUFFIXES:
                report.skipped.append(f"{filename}: 不支援的副檔名 {suffix}")
                continue
            if not payload:
                report.skipped.append(f"{filename}: 檔案為空")
                continue

            digest = content_hash(payload)

            existing = session.exec(
                select(AssetVariant).where(
                    AssetVariant.project_id == artifact.project_id,
                    AssetVariant.shot_id == shot_id,
                    AssetVariant.file_hash == digest,
                )
            ).first()
            if existing is not None:
                report.imported.append(
                    ImportedVariant(
                        variant_id=existing.variant_id,
                        shot_id=shot_id,
                        provider=existing.provider,
                        local_path=existing.local_path or "",
                        file_hash=digest,
                        actual_duration_ms=existing.actual_duration_ms,
                        resolution=existing.resolution,
                        fps=existing.fps,
                        duplicate=True,
                        warnings=["相同內容的候選已存在，未重複建立"],
                    )
                )
                continue

            variant_id = f"var_{shot_id}_{provider}_{digest[:12]}"
            destination = target_dir / f"{variant_id}{suffix}"
            destination.write_bytes(payload)

            media = probe_media(destination)
            warnings = check_against_plan(shot, media)

            session.add(
                AssetVariant(
                    variant_id=variant_id,
                    project_id=artifact.project_id,
                    shot_id=shot_id,
                    job_id=job.job_id if job else None,
                    provider=provider,
                    model_id=model_id or (job.model_id if job else None),
                    model_version=job.model_version if job else None,
                    generation_mode=generation_mode or GenerationMode.IMAGE_TO_VIDEO.value,
                    prompt_snapshot=shot.prompt if shot else "",
                    negative_prompt=shot.negative_prompt if shot else "",
                    reference_asset_ids=list(shot.reference_asset_ids) if shot else [],
                    requested_duration_ms=shot.target_duration_ms if shot else None,
                    actual_duration_ms=media.duration_ms,
                    resolution=media.resolution,
                    fps=media.fps,
                    file_hash=digest,
                    local_path=str(destination),
                    status=VariantStatus.IMPORTED.value,
                    generation_timestamp=datetime.now(timezone.utc),
                )
            )
            report.imported.append(
                ImportedVariant(
                    variant_id=variant_id,
                    shot_id=shot_id,
                    provider=provider,
                    local_path=str(destination),
                    file_hash=digest,
                    actual_duration_ms=media.duration_ms,
                    resolution=media.resolution,
                    fps=media.fps,
                    warnings=warnings,
                )
            )

        # 有新候選進來即代表該工作已由人工完成
        if job is not None and report.new_count:
            job.status = JobStatus.COMPLETED.value
            job.completed_at = datetime.now(timezone.utc)
            session.add(job)

        session.commit()

    artifact.decision_log.append(
        DecisionLogEntry(
            action="variants_imported",
            actor=actor or "local",
            note=note or report.summary(),
        )
    )
    artifact.touch()
    return report


def list_variants(project_id: str, shot_id: str | None = None) -> list[AssetVariant]:
    from pipeline.db import engine

    with Session(engine) as session:
        statement = select(AssetVariant).where(AssetVariant.project_id == project_id)
        if shot_id:
            statement = statement.where(AssetVariant.shot_id == shot_id)
        return list(session.exec(statement.order_by(AssetVariant.created_at)).all())


def select_variant(
    artifact: ProductionArtifact,
    variant_id: str,
    reason: str = "",
    actor: str = "local",
) -> AssetVariant:
    """選定某個候選作為該鏡頭的成品，同一鏡頭的其他候選一併退選。"""
    from pipeline.db import engine

    with Session(engine) as session:
        variant = session.get(AssetVariant, variant_id)
        if variant is None:
            raise ValueError(f"找不到候選 {variant_id}")

        siblings = session.exec(
            select(AssetVariant).where(
                AssetVariant.project_id == variant.project_id,
                AssetVariant.shot_id == variant.shot_id,
                AssetVariant.variant_id != variant_id,
                AssetVariant.status == VariantStatus.SELECTED.value,
            )
        ).all()
        for sibling in siblings:
            sibling.status = VariantStatus.IMPORTED.value
            sibling.selected_reason = None
            session.add(sibling)

        variant.status = VariantStatus.SELECTED.value
        variant.selected_reason = reason or None
        session.add(variant)
        session.commit()
        session.refresh(variant)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="variant_selected",
            actor=actor or "local",
            note=f"{variant.shot_id}: {variant_id}" + (f"; {reason}" if reason else ""),
        )
    )
    artifact.touch()
    return variant
