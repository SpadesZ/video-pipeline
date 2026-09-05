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
from uuid import uuid4

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
    # 未提供 request_hash 時為 False，代表這批候選沒有可信的派工來源
    linked: bool = False

    @property
    def new_count(self) -> int:
        return sum(1 for item in self.imported if not item.duplicate)

    def summary(self) -> str:
        link = "linked" if self.linked else "unlinked"
        return (
            f"{self.new_count} new, {len(self.imported) - self.new_count} duplicate, "
            f"{len(self.skipped)} skipped for {self.shot_id}@{self.provider} ({link})"
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


class JobLinkError(ValueError):
    """提供的 request_hash 無法安全對應到一筆工作。"""


def build_provenance(job: CapabilityJob | None) -> dict:
    """由派工快照組出候選的生成血緣。

    刻意不接受 ShotPlan 作為來源。分鏡在派工後可能已被修改，
    若回推當前值，記錄下來的會是從未真正送出去的內容。
    未關聯派工時全部留空，明確表示血緣不可考。
    """
    if job is None:
        return {
            "prompt": "",
            "negative_prompt": "",
            "reference_asset_ids": [],
            "provider_parameters": {},
        }

    snapshot = job.request_snapshot or {}
    visual = snapshot.get("visual") or {}
    audio = snapshot.get("audio") or {}
    return {
        "prompt": visual.get("prompt") or audio.get("text") or "",
        "negative_prompt": visual.get("negative_prompt") or "",
        "reference_asset_ids": list(job.reference_asset_ids or []),
        "provider_parameters": dict(job.provider_parameters or {}),
    }


def check_against_request(
    job: CapabilityJob | None, media: MediaInfo
) -> list[str]:
    """比對實際規格與派工當下送出的規格，回傳落差描述。不阻擋匯入。

    基準一律取自 CapabilityJob 記錄的 requested_* 欄位，不回頭使用
    ShotPlan.target_duration_ms。後者是導演意圖，派工前會經
    ProductionProfile 的鏡頭長度政策夾住，以它計算落差會得到錯誤數值。
    未關聯工作時無可信基準，明確標示而非改用其他來源。
    """
    warnings: list[str] = []
    if media.error:
        warnings.append(media.error)

    if job is None:
        warnings.append("未關聯派工記錄，無法比對規格落差")
        return warnings

    requested_ms = job.requested_duration_ms
    if requested_ms is None:
        warnings.append("派工記錄未含請求片長，無法比對落差")
    elif media.duration_ms is not None:
        tolerance = max(200, int(requested_ms * 0.1))
        delta = media.duration_ms - requested_ms
        if abs(delta) > tolerance:
            warnings.append(
                f"片長 {media.duration_ms}ms 與請求 {requested_ms}ms "
                f"相差 {delta:+d}ms"
            )

    actual_ratio = media.aspect_ratio
    requested_ratio = job.requested_aspect_ratio
    if actual_ratio and requested_ratio and actual_ratio != requested_ratio:
        warnings.append(f"比例 {actual_ratio} 與請求 {requested_ratio} 不符")

    return warnings


def _resolve_job(
    session: Session,
    project_id: str,
    shot_id: str,
    provider: str,
    request_hash: str | None,
) -> CapabilityJob | None:
    """依 request_hash 嚴格對應工作。

    提供 hash 時必須同時符合 project_id、shot_id 與 provider，任一不符即
    拒絕匯入。刻意不提供「找不到就退回最近一筆」的行為：那會把使用者
    貼錯的 hash 悄悄接到別的工作上，讓後續統計全部失真。
    完全未提供 hash 時回傳 None，由呼叫端標示為 unlinked import。
    """
    if not request_hash:
        return None

    job = session.exec(
        select(CapabilityJob).where(CapabilityJob.request_hash == request_hash)
    ).first()
    if job is None:
        raise JobLinkError(
            f"找不到 request_hash 為 {request_hash[:16]}... 的派工記錄"
        )

    mismatches: list[str] = []
    if job.project_id != project_id:
        mismatches.append(f"project {job.project_id} != {project_id}")
    if (job.shot_id or "") != shot_id:
        mismatches.append(f"shot {job.shot_id} != {shot_id}")
    if job.provider != provider:
        mismatches.append(f"provider {job.provider} != {provider}")

    if mismatches:
        raise JobLinkError(
            f"request_hash {request_hash[:16]}... 對應的工作不符: "
            + "; ".join(mismatches)
        )

    return job


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
    if shot is None:
        raise ValueError(
            f"鏡頭 {shot_id} 不屬於專案 {artifact.project_id}，拒絕匯入"
        )

    target_dir = variants_dir(settings, artifact.project_id, shot_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    with Session(engine) as session:
        job = _resolve_job(
            session, artifact.project_id, shot_id, provider, request_hash
        )
        report.job_id = job.job_id if job else None
        report.linked = job is not None

        for filename, payload in files:
            suffix = Path(filename).suffix.lower()
            if suffix not in VIDEO_SUFFIXES:
                report.skipped.append(f"{filename}: 不支援的副檔名 {suffix}")
                continue
            if not payload:
                report.skipped.append(f"{filename}: 檔案為空")
                continue

            digest = content_hash(payload)

            # 去重必須連同來源一起比對。同樣的位元組若來自不同平台或不同
            # 派工，是兩筆各自獨立的生成結果，合併會抹掉 attribution，
            # 讓平台比較失去意義。
            existing = session.exec(
                select(AssetVariant).where(
                    AssetVariant.project_id == artifact.project_id,
                    AssetVariant.shot_id == shot_id,
                    AssetVariant.file_hash == digest,
                    AssetVariant.provider == provider,
                    AssetVariant.job_id == (job.job_id if job else None),
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

            # 全域唯一。原本以 shot_id + provider + file_hash 組成，
            # 不同專案匯入同一支檔案會撞主鍵。
            variant_id = f"var_{uuid4().hex[:20]}"
            destination = target_dir / f"{variant_id}{suffix}"
            destination.write_bytes(payload)

            media = probe_media(destination)
            warnings = check_against_request(job, media)
            provenance = build_provenance(job)
            if job is None:
                warnings.append("未關聯派工，無生成血緣可記錄")

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
                    # 血緣一律取自派工快照，不回推當前 ShotPlan
                    prompt_snapshot=provenance["prompt"],
                    negative_prompt=provenance["negative_prompt"],
                    reference_asset_ids=provenance["reference_asset_ids"],
                    provider_parameters=provenance["provider_parameters"],
                    # 基準取自派工記錄，未關聯時留空而非退回 ShotPlan 意圖值
                    requested_duration_ms=job.requested_duration_ms if job else None,
                    actual_duration_ms=media.duration_ms,
                    resolution=media.resolution,
                    fps=media.fps,
                    file_hash=digest,
                    local_path=str(destination),
                    original_filename=filename,
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


def load_owned_variant(
    session: Session, project_id: str, variant_id: str
) -> AssetVariant:
    """取得候選並驗證其歸屬。

    所有以 variant_id 為入口的操作都必須經過此處，否則帶著別的專案的
    variant_id 呼叫就能跨專案改動資料。
    """
    variant = session.get(AssetVariant, variant_id)
    if variant is None:
        raise ValueError(f"找不到候選 {variant_id}")
    if variant.project_id != project_id:
        raise ValueError(
            f"候選 {variant_id} 屬於專案 {variant.project_id}，"
            f"不可由專案 {project_id} 操作"
        )
    return variant


def select_variant(
    artifact: ProductionArtifact,
    variant_id: str,
    reason: str = "",
    actor: str = "local",
) -> AssetVariant:
    """選定某個候選作為該鏡頭的成品，同一鏡頭的其他候選一併退選。"""
    from pipeline.db import engine

    with Session(engine) as session:
        variant = load_owned_variant(session, artifact.project_id, variant_id)

        siblings = session.exec(
            select(AssetVariant).where(
                AssetVariant.project_id == artifact.project_id,
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
