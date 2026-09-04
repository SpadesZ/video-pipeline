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

from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.capability.base import CapabilityRequest, VisualPayload
from pipeline.capability.router import dispatch_capability
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import ProductionProfile, default_profile
from pipeline.models.review import DecisionLogEntry
from pipeline.models.shot import CharacterIdentityPack, ShotPlan
from pipeline.models.variant import JobStatus


class ShotDispatchResult(BaseModel):
    shot_id: str
    status: str
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
    def failed_count(self) -> int:
        return len(self.results) - self.dispatched_count

    def summary(self) -> str:
        return (
            f"{self.dispatched_count} dispatched, {self.failed_count} failed "
            f"of {len(self.results)} shots"
        )


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
            reference_asset_ids=collect_reference_ids(shot, packs),
            duration_ms=duration_ms,
            aspect_ratio=shot.aspect_ratio or profile.aspect_ratio,
            camera=shot.camera.describe(),
            character_refs=list(shot.character_refs),
        ),
    )


async def dispatch_project_shots(
    artifact: ProductionArtifact,
    preferred_provider: str | None = None,
    scenario_type: str | None = None,
    shot_ids: list[str] | None = None,
) -> ShotDispatchReport:
    """為專案的每個 ShotPlan 派工。

    第一階段會得到 pending_manual，代表 job package 已產出、等待人工至平台生成。
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
        request = build_shot_request(artifact, shot, packs, profile)
        result = await dispatch_capability(
            request,
            profile=profile,
            scenario_type=scenario_type,
            preferred_provider=preferred_provider,
        )
        package_dir = result.outputs[0] if result.outputs else None
        report.results.append(
            ShotDispatchResult(
                shot_id=shot.shot_id,
                status=result.status.value,
                provider=result.provider,
                model_id=result.model_id,
                job_id=result.job_id,
                package_dir=package_dir,
                message=result.error_message,
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
