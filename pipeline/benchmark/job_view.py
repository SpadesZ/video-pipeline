# 檔案路徑: video-pipeline/pipeline/benchmark/job_view.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   單一派工的人工操作視圖。
# 主要責任:
#   1. 把派工快照攤平成人工在平台操作時需要的欄位。
#   2. 提供以派工為脈絡的匯入所需的一切識別，使用者不必手抄 request_hash。
# 說明:
#   原本要完成一次生成，使用者得下載 zip、找到 job.json、讀出 request_hash、
#   再回到匯入表單貼上，中間任何一步貼錯都會把影片掛到別的派工上，
#   而且不會有錯誤訊息——統計只會悄悄算錯。
#
#   識別一律從派工快照讀，不回推目前的 ShotPlan 或 catalog：
#   分鏡與 target 版本在派工後都可能被改過，那樣顯示的會是從未送出的內容。
# --------------------------------------------------------------------------

from __future__ import annotations

from pydantic import BaseModel, Field

from pipeline.benchmark import v1_pack
from pipeline.benchmark.attribution import TARGET_PARAMETER_KEY

MODEL_VERSION_KEY = "_bm_model_version"
UI_LABEL_KEY = "_bm_ui_label"
PROVISIONAL_KEY = "_bm_provisional"


class ReferenceView(BaseModel):
    asset_id: str
    label: str = ""
    sha256: str | None = None
    available: bool = False
    is_first_frame: bool = False


class JobView(BaseModel):
    """一份派工在 Workbench 上的全部內容。"""

    job_id: str
    project_id: str
    shot_id: str
    scenario: str = ""
    target_id: str | None = None
    provider: str
    model_id: str | None = None
    # 派工當下的版本。刻意不取 catalog 現值，避免舊派工被顯示成新版本。
    model_version: str | None = None
    ui_label: str = ""
    provisional_at_dispatch: bool = True

    capability: str = ""
    status: str = ""
    request_hash: str | None = None
    transport: str = ""

    prompt: str = ""
    negative_prompt: str = ""
    camera: str = ""
    duration_ms: int | None = None
    aspect_ratio: str | None = None
    parameters: dict = Field(default_factory=dict)
    references: list[ReferenceView] = Field(default_factory=list)

    attempts_recorded: int = 0
    variants_imported: int = 0

    @property
    def duration_seconds(self) -> float | None:
        return None if self.duration_ms is None else round(self.duration_ms / 1000, 2)

    @property
    def model_display(self) -> str:
        return f"{self.model_id or '—'}@{self.model_version or '版本未確認'}"

    @property
    def complete(self) -> bool:
        return self.variants_imported > 0


def _public_parameters(parameters: dict) -> dict:
    """過濾掉底線前綴的內部標記，只留下真正要填進平台的參數。"""
    return {
        key: value
        for key, value in (parameters or {}).items()
        if not key.startswith("_")
    }


def build_job_view(job, reference_labels: dict[str, str] | None = None) -> JobView:
    snapshot = job.request_snapshot or {}
    visual = snapshot.get("visual") or {}
    parameters = snapshot.get("parameters") or {}
    hashes = visual.get("reference_hashes") or {}

    first_frame = visual.get("first_frame_ref")
    asset_ids: list[str] = []
    if first_frame:
        asset_ids.append(str(first_frame))
    for item in visual.get("reference_asset_ids") or []:
        if str(item) not in asset_ids:
            asset_ids.append(str(item))

    labels = reference_labels or {}
    references = [
        ReferenceView(
            asset_id=asset_id,
            label=labels.get(asset_id, ""),
            sha256=hashes.get(asset_id),
            available=bool(hashes.get(asset_id)),
            is_first_frame=asset_id == first_frame,
        )
        for asset_id in asset_ids
    ]

    target_id = parameters.get(TARGET_PARAMETER_KEY)
    return JobView(
        job_id=job.job_id,
        project_id=job.project_id,
        shot_id=job.shot_id or "",
        scenario=v1_pack.SHOT_SCENARIOS.get(job.shot_id or "", ""),
        target_id=str(target_id) if target_id else None,
        provider=job.provider,
        model_id=job.model_id,
        model_version=job.model_version or parameters.get(MODEL_VERSION_KEY) or None,
        ui_label=str(parameters.get(UI_LABEL_KEY) or ""),
        provisional_at_dispatch=bool(parameters.get(PROVISIONAL_KEY, True)),
        capability=job.capability,
        status=job.status,
        request_hash=job.request_hash,
        transport=job.transport,
        prompt=visual.get("prompt") or "",
        negative_prompt=visual.get("negative_prompt") or "",
        camera=visual.get("camera") or "",
        duration_ms=visual.get("duration_ms"),
        aspect_ratio=visual.get("aspect_ratio"),
        parameters=_public_parameters(parameters),
        references=references,
    )


def list_jobs(project_id: str) -> list[JobView]:
    """列出專案內所有 benchmark 派工，附上嘗試與匯入計數。"""
    from sqlmodel import Session, select

    from pipeline.db import engine
    from pipeline.models.reference_asset import ReferenceAsset
    from pipeline.models.variant import AssetVariant, CapabilityJob

    with Session(engine) as session:
        jobs = session.exec(
            select(CapabilityJob).where(CapabilityJob.project_id == project_id)
        ).all()
        variants = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == project_id)
        ).all()
        labels = {
            item.asset_id: item.label
            for item in session.exec(select(ReferenceAsset)).all()
        }

    counts: dict[str, int] = {}
    for variant in variants:
        if variant.job_id:
            counts[variant.job_id] = counts.get(variant.job_id, 0) + 1

    views = []
    for job in jobs:
        view = build_job_view(job, labels)
        view.variants_imported = counts.get(job.job_id, 0)
        views.append(view)

    order = {shot.shot_id: index for index, shot in enumerate(v1_pack.shots())}
    views.sort(
        key=lambda item: (
            item.target_id or "~",
            order.get(item.shot_id, 99),
        )
    )
    return views


def load_job(project_id: str, job_id: str) -> JobView:
    """讀取單一派工。不屬於該專案的一律拒絕，不靜默回傳空值。"""
    from sqlmodel import Session, select

    from pipeline.db import engine
    from pipeline.models.reference_asset import ReferenceAsset
    from pipeline.models.variant import AssetVariant, CapabilityJob

    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        if job is None or job.project_id != project_id:
            raise LookupError(f"專案 {project_id} 中找不到派工 {job_id}")
        imported = len(
            session.exec(
                select(AssetVariant).where(AssetVariant.job_id == job_id)
            ).all()
        )
        labels = {
            item.asset_id: item.label
            for item in session.exec(select(ReferenceAsset)).all()
        }

    view = build_job_view(job, labels)
    view.variants_imported = imported
    return view


def reference_path(asset_id: str) -> str | None:
    """參考素材的實際位置。供下載端點使用，不對使用者顯示。"""
    from sqlmodel import Session

    from pipeline.db import engine
    from pipeline.models.reference_asset import ReferenceAsset

    with Session(engine) as session:
        asset = session.get(ReferenceAsset, asset_id)
    return asset.local_path if asset else None
