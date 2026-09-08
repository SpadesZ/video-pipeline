# 檔案路徑: video-pipeline/pipeline/capability/transports/manual.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   人工派工 transport。
# 主要責任:
#   1. 產出 job package 並建立 CapabilityJob 記錄，回傳 pending_manual。
#   2. 以 request_hash 提供冪等：同一份工作重複派送不會產生第二筆記錄。
#   3. 依 ProviderSpec 的 transport 欄位資料驅動註冊，不使用平台名稱分支。
# 說明:
#   execute() 刻意不阻塞等待人工完成。工作交付後即回傳 pending_manual，
#   由匯入流程接續。這使得第一階段以人工驗證模型品質時，上層 orchestration
#   與未來改用 API transport 完全一致，屆時只需替換本模組。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from pipeline.capability.base import CapabilityRequest, CapabilityResult
from pipeline.capability.job_package import (
    JobPackage,
    build_job_package,
    read_job_manifest,
)
from pipeline.capability.model_registry import ModelEntry, model_registry
from pipeline.capability.provider_spec import ProviderSpec, get_provider, provider_specs
from pipeline.models.capability import Capability
from pipeline.models.reference_asset import ReferenceAsset
from pipeline.models.variant import CapabilityJob, JobStatus, TransportKind

logger = logging.getLogger("capability.manual")

JOB_PACKAGE_DIRNAME = "job_packages"


def default_output_root(project_id: str | None) -> Path:
    from pipeline.settings import get_settings

    settings = get_settings()
    base = Path(settings.data_dir) / "projects"
    if project_id:
        return base / project_id / JOB_PACKAGE_DIRNAME
    return Path(settings.data_dir) / JOB_PACKAGE_DIRNAME


def resolve_reference_paths(asset_ids: list[str]) -> dict[str, Path]:
    """由 reference_assets 表解析素材實體路徑。"""
    if not asset_ids:
        return {}
    from pipeline.db import engine

    resolved: dict[str, Path] = {}
    with Session(engine) as session:
        rows = session.exec(
            select(ReferenceAsset).where(ReferenceAsset.asset_id.in_(asset_ids))
        ).all()
        for row in rows:
            if row.local_path:
                resolved[row.asset_id] = Path(row.local_path)
    return resolved


def _collect_request_reference_ids(request: CapabilityRequest) -> list[str]:
    ids: list[str] = []
    if request.visual is not None:
        if request.visual.first_frame_ref:
            ids.append(request.visual.first_frame_ref)
        ids.extend(request.visual.reference_asset_ids)
    if request.audio is not None and request.audio.voice_ref:
        ids.append(request.audio.voice_ref)
    return ids


def _job_id(request_hash: str, provider: str) -> str:
    return f"job_{provider}_{request_hash[:16]}"


def upsert_manual_job(
    request: CapabilityRequest,
    provider: str,
    model: ModelEntry,
    request_hash: str,
    package: JobPackage | None = None,
) -> tuple[str, bool]:
    """建立或取回工作記錄。回傳 (job_id, 是否為新建)。

    以 request_hash 與 provider 為冪等鍵：同一份工作重複派送時沿用既有
    記錄，避免 generations_attempted 被灌水。
    """
    from pipeline.db import engine

    job_id = _job_id(request_hash, provider)
    with Session(engine) as session:
        existing = session.exec(
            select(CapabilityJob).where(
                CapabilityJob.request_hash == request_hash,
                CapabilityJob.provider == provider,
            )
        ).first()
        if existing is not None:
            return existing.job_id, False

        # 凍結派工當下的完整請求。匯入候選時的血緣只讀這份快照，
        # 分鏡日後被修改也不會污染已送出工作的來源記錄。
        visual = request.visual
        manifest = read_job_manifest(Path(package.package_dir)) if package else {}
        session.add(
            CapabilityJob(
                job_id=job_id,
                project_id=request.project_id or "unassigned",
                shot_id=request.shot_id,
                capability=request.capability.value,
                provider=provider,
                model_id=model.model_id,
                model_version=model.model_version,
                transport=TransportKind.MANUAL.value,
                status=JobStatus.PENDING_MANUAL.value,
                request_hash=request_hash,
                requested_duration_ms=visual.duration_ms if visual else None,
                requested_aspect_ratio=visual.aspect_ratio if visual else None,
                request_snapshot=request.model_dump(mode="json"),
                provider_parameters=manifest.get("provider_parameters", {}),
                reference_asset_ids=(
                    list(visual.reference_asset_ids) if visual else []
                ),
                manifest_path=package.job_path if package else None,
                submitted_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return job_id, True


class ManualTransportAdapter:
    """以人工操作完成生成的 transport。

    一個實例對應一組 (capability, provider)，由 install_manual_adapters
    依 ProviderSpec 資料自動建立。
    """

    def __init__(
        self,
        capability: Capability,
        provider: str,
        output_root: Path | None = None,
        record_job: bool = True,
    ) -> None:
        self.capability = capability
        self.provider = provider
        self._output_root = output_root
        self._record_job = record_job

    def _resolve_model(self, request: CapabilityRequest) -> ModelEntry | None:
        model_id = request.parameters.get("model_id")
        if not model_id:
            return None
        return model_registry().get(str(model_id))

    def _resolve_spec(self) -> ProviderSpec | None:
        return get_provider(self.provider)

    def _resolve_output_root(self, request: CapabilityRequest) -> Path:
        if self._output_root is not None:
            return self._output_root
        return default_output_root(request.project_id)

    async def execute(self, request: CapabilityRequest) -> CapabilityResult:
        spec = self._resolve_spec()
        if spec is None:
            return CapabilityResult.failure(
                f"平台 {self.provider} 未登錄", "unknown_provider"
            )

        model = self._resolve_model(request)
        if model is None:
            return CapabilityResult.failure(
                "路由未提供 model_id，無法產生 job package", "missing_model"
            )

        reference_paths = resolve_reference_paths(
            _collect_request_reference_ids(request)
        )

        try:
            package = build_job_package(
                request=request,
                provider=spec,
                model=model,
                output_root=self._resolve_output_root(request),
                reference_paths=reference_paths,
            )
        except Exception as error:  # noqa: BLE001 - 產包失敗需回報而非中斷派送
            return CapabilityResult.failure(
                f"產生 job package 失敗: {error}", "package_error"
            )

        job_id = None
        created = False
        if self._record_job:
            try:
                job_id, created = upsert_manual_job(
                    request, self.provider, model, package.request_hash, package
                )
            except Exception as error:  # noqa: BLE001 - 需回報明確原因而非拋出
                # job package 已產出，但沒有工作記錄就無法在匯回時對應，
                # 因此視為失敗並附上明確原因，而非讓例外冒泡成 "exception"。
                return CapabilityResult(
                    ok=False,
                    status=JobStatus.FAILED,
                    provider=self.provider,
                    model_id=model.model_id,
                    outputs=[package.package_dir],
                    error_code="job_record_failed",
                    error_message=f"job package 已產出但工作記錄失敗: {error}"[:300],
                )
            if not created:
                logger.info(
                    "Manual job already dispatched: shot=%s provider=%s hash=%s",
                    request.shot_id,
                    self.provider,
                    package.request_hash[:16],
                )

        return CapabilityResult(
            ok=False,
            status=JobStatus.PENDING_MANUAL,
            job_id=job_id,
            provider=self.provider,
            model_id=model.model_id,
            model_version=model.model_version,
            outputs=[package.package_dir],
            submitted_at=datetime.now(timezone.utc),
            error_message="; ".join(package.warnings) or None,
        )


def install_manual_adapters(
    output_root: Path | None = None, record_job: bool = True
) -> list[tuple[Capability, str]]:
    """依 ProviderSpec 資料註冊所有人工 transport。

    平台與能力的組合完全來自 catalog，新增平台只需新增 YAML。
    """
    # 於函式內 import，避免與 router 形成模組層級循環相依
    from pipeline.capability.router import register_adapter

    installed: list[tuple[Capability, str]] = []
    for spec in provider_specs().values():
        if spec.transport is not TransportKind.MANUAL:
            continue
        for capability in spec.supported_capabilities:
            register_adapter(
                ManualTransportAdapter(
                    capability=capability,
                    provider=spec.provider_id,
                    output_root=output_root,
                    record_job=record_job,
                )
            )
            installed.append((capability, spec.provider_id))
    return installed
