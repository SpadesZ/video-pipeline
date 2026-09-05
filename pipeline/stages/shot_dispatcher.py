# 檔案路徑: video-pipeline/pipeline/stages/shot_dispatcher.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   將 ShotPlan 派送至 Capability Router 的階段。
# 主要責任:
#   1. 把 ShotPlan 與 CharacterIdentityPack 展開為 CapabilityRequest。
#   2. 逐鏡頭派工並彙整結果，記錄至 decision_log。
# 說明:
#   ShotPlan 只引用 character_id，實際參考素材存放於 CharacterIdentityPack。
#   派工前必須在此展開為 ReferenceAsset id，否則 job package 的 refs/ 會是空的。
#   第一階段所有影片能力都走人工 transport，因此正常結果是 pending_manual
#   而非 completed，這不是錯誤。
# --------------------------------------------------------------------------

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.capability.base import CapabilityRequest, VisualPayload
from pipeline.capability.router import dispatch_capability
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import ProductionProfile, default_profile
from pipeline.models.reference_asset import ReferenceAsset
from pipeline.models.review import DecisionLogEntry
from pipeline.models.shot import CharacterIdentityPack, ShotPlan
from pipeline.models.variant import JobStatus

NOT_DISPATCHED = "not_dispatched"


class ShotReadinessState(StrEnum):
    """派工前的完備度。

    ready      所有必要資訊與參考素材皆齊備。
    incomplete 資訊齊備但部分參考素材檔案缺失，送出去 refs/ 會不完整。
    blocked    缺少必要資訊，不具備派工條件。
    """

    READY = "ready"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


class ShotReadiness(BaseModel):
    shot_id: str
    state: ShotReadinessState = ShotReadinessState.READY
    issues: list[str] = Field(default_factory=list)

    @property
    def can_dispatch(self) -> bool:
        return self.state is ShotReadinessState.READY


class ShotDispatchResult(BaseModel):
    shot_id: str
    status: str
    readiness: ShotReadinessState = ShotReadinessState.READY
    provider: str | None = None
    model_id: str | None = None
    job_id: str | None = None
    package_dir: str | None = None
    message: str | None = None

    @property
    def dispatched(self) -> bool:
        return self.status in {
            JobStatus.PENDING_MANUAL.value,
            JobStatus.SUBMITTED.value,
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
            JobStatus.COMPLETED.value,
        }


class ShotDispatchReport(BaseModel):
    project_id: str
    results: list[ShotDispatchResult] = Field(default_factory=list)

    @property
    def dispatched_count(self) -> int:
        return sum(1 for item in self.results if item.dispatched)

    @property
    def blocked_count(self) -> int:
        return sum(
            1
            for item in self.results
            if item.readiness is not ShotReadinessState.READY and not item.dispatched
        )

    @property
    def failed_count(self) -> int:
        return len(self.results) - self.dispatched_count

    def summary(self) -> str:
        return (
            f"{self.dispatched_count} dispatched, {self.blocked_count} blocked, "
            f"{self.failed_count - self.blocked_count} failed "
            f"of {len(self.results)} shots"
        )


def resolve_reference_availability(asset_ids: list[str]) -> dict[str, bool]:
    """檢查參考素材是否已登錄且具備實體檔案。"""
    if not asset_ids:
        return {}
    from pipeline.db import engine

    available: dict[str, bool] = {asset_id: False for asset_id in asset_ids}
    with Session(engine) as session:
        rows = session.exec(
            select(ReferenceAsset).where(ReferenceAsset.asset_id.in_(asset_ids))
        ).all()
        for row in rows:
            available[row.asset_id] = bool(
                row.local_path and Path(row.local_path).exists()
            )
    return available


def resolve_reference_hashes(asset_ids: list[str]) -> dict[str, str]:
    """取得素材的檔案內容雜湊，供納入請求識別。

    以檔案實際內容重算，不採用資料庫既有值：素材可能在登錄後被替換，
    若沿用舊值，換圖後的請求會與換圖前得到相同的 identity。
    """
    if not asset_ids:
        return {}
    from pipeline.capability.job_package import file_sha256
    from pipeline.db import engine

    hashes: dict[str, str] = {}
    with Session(engine) as session:
        rows = session.exec(
            select(ReferenceAsset).where(ReferenceAsset.asset_id.in_(asset_ids))
        ).all()
        for row in rows:
            if not row.local_path:
                continue
            path = Path(row.local_path)
            if path.exists():
                hashes[row.asset_id] = file_sha256(path)
    return hashes


def check_shot_readiness(
    shot: ShotPlan, packs: dict[str, CharacterIdentityPack]
) -> ShotReadiness:
    """派工前的完備度檢查。

    刻意 fail-closed：宣告了角色卻找不到身份定義、或參考素材檔案不存在時，
    不可讓鏡頭以「已派工」的外觀通過。那會讓平台收到殘缺的 job package，
    產出的結果無法歸因，也會污染後續的模型比較。
    """
    readiness = ShotReadiness(shot_id=shot.shot_id)

    if not shot.prompt.strip():
        readiness.issues.append("缺少 prompt")

    missing_packs = [
        character_id
        for character_id in shot.character_refs
        if character_id not in packs
    ]
    if missing_packs:
        readiness.issues.append(
            f"找不到角色身份定義: {', '.join(missing_packs)}"
        )

    if readiness.issues:
        readiness.state = ShotReadinessState.BLOCKED
        return readiness

    required_refs = collect_reference_ids(shot, packs)
    if shot.first_frame_ref:
        required_refs = [shot.first_frame_ref, *required_refs]

    availability = resolve_reference_availability(required_refs)
    missing_files = [
        asset_id for asset_id, present in availability.items() if not present
    ]
    if missing_files:
        readiness.state = ShotReadinessState.INCOMPLETE
        readiness.issues.append(
            f"參考素材檔案不存在: {', '.join(sorted(missing_files))}"
        )

    return readiness


def assess_project_readiness(
    artifact: ProductionArtifact,
) -> dict[str, ShotReadiness]:
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    return {
        shot.shot_id: check_shot_readiness(shot, packs)
        for shot in artifact.shot_plans
    }


def collect_reference_ids(
    shot: ShotPlan, packs: dict[str, CharacterIdentityPack]
) -> list[str]:
    """展開鏡頭所需的全部參考素材 id。

    來源有二：鏡頭自身的 reference_asset_ids，以及所引用角色的身份素材。
    首幀不列入此清單，它於 VisualPayload.first_frame_ref 單獨表達。
    """
    ordered: list[str] = []
    seen: set[str] = set()

    for asset_id in shot.reference_asset_ids:
        if asset_id not in seen:
            seen.add(asset_id)
            ordered.append(asset_id)

    for character_id in shot.character_refs:
        pack = packs.get(character_id)
        if pack is None:
            continue
        for asset_id in pack.reference_asset_ids:
            if asset_id not in seen:
                seen.add(asset_id)
                ordered.append(asset_id)

    return ordered


def build_shot_request(
    artifact: ProductionArtifact,
    shot: ShotPlan,
    packs: dict[str, CharacterIdentityPack],
    profile: ProductionProfile,
) -> CapabilityRequest:
    duration_ms = profile.shot_duration.clamp_ms(shot.target_duration_ms)
    reference_ids = collect_reference_ids(shot, packs)

    # 首幀也是實際上傳的素材，其內容必須納入請求識別
    hashed_ids = list(reference_ids)
    if shot.first_frame_ref:
        hashed_ids = [shot.first_frame_ref, *hashed_ids]

    return CapabilityRequest(
        request_id=f"req_{artifact.project_id}_{shot.shot_id}",
        capability=shot.capability,
        project_id=artifact.project_id,
        shot_id=shot.shot_id,
        shot_plan_version=shot.version,
        profile_version=1,
        visual=VisualPayload(
            prompt=shot.prompt,
            negative_prompt=shot.negative_prompt,
            first_frame_ref=shot.first_frame_ref,
            reference_asset_ids=reference_ids,
            duration_ms=duration_ms,
            aspect_ratio=shot.aspect_ratio or profile.aspect_ratio,
            camera=shot.camera.describe(),
            character_refs=list(shot.character_refs),
            reference_hashes=resolve_reference_hashes(hashed_ids),
        ),
    )


async def dispatch_project_shots(
    artifact: ProductionArtifact,
    preferred_provider: str | None = None,
    scenario_type: str | None = None,
    shot_ids: list[str] | None = None,
    allow_incomplete: bool = False,
    only_provider: str | None = None,
    extra_parameters: dict | None = None,
) -> ShotDispatchReport:
    """為專案的每個 ShotPlan 派工。

    第一階段會得到 pending_manual，代表 job package 已產出、等待人工至平台生成。

    完備度預設 fail-closed：blocked 一律不派，incomplete 也不派，除非
    呼叫端明確傳入 allow_incomplete。未通過檢查的鏡頭會出現在報告中並
    標示原因，不會以「已派工」的外觀混入。
    """
    profile = artifact.production_profile or default_profile()
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    selected = [
        shot
        for shot in sorted(artifact.shot_plans, key=lambda s: s.order)
        if shot_ids is None or shot.shot_id in shot_ids
    ]

    report = ShotDispatchReport(project_id=artifact.project_id)

    for shot in selected:
        readiness = check_shot_readiness(shot, packs)
        blocked = readiness.state is ShotReadinessState.BLOCKED
        incomplete_blocked = (
            readiness.state is ShotReadinessState.INCOMPLETE and not allow_incomplete
        )
        if blocked or incomplete_blocked:
            report.results.append(
                ShotDispatchResult(
                    shot_id=shot.shot_id,
                    status=NOT_DISPATCHED,
                    readiness=readiness.state,
                    message="; ".join(readiness.issues),
                )
            )
            continue

        request = build_shot_request(artifact, shot, packs, profile)
        if extra_parameters:
            request = request.model_copy(
                update={"parameters": {**request.parameters, **extra_parameters}}
            )
        result = await dispatch_capability(
            request,
            profile=profile,
            scenario_type=scenario_type,
            preferred_provider=preferred_provider,
            only_provider=only_provider,
        )
        package_dir = result.outputs[0] if result.outputs else None
        message = result.error_message
        if readiness.issues:
            message = "; ".join([*readiness.issues, message or ""]).strip("; ")
        report.results.append(
            ShotDispatchResult(
                shot_id=shot.shot_id,
                status=result.status.value,
                readiness=readiness.state,
                provider=result.provider,
                model_id=result.model_id,
                job_id=result.job_id,
                package_dir=package_dir,
                message=message,
            )
        )

    artifact.decision_log.append(
        DecisionLogEntry(
            action="shots_dispatched",
            actor="local",
            note=report.summary(),
        )
    )
    artifact.touch()
    return report


def job_package_root(data_dir: Path | str, project_id: str) -> Path:
    return Path(data_dir) / "projects" / project_id / "job_packages"
