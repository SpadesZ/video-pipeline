# 檔案路徑: video-pipeline/pipeline/stages/timeline_builder.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   剪輯決策與時間線建構階段。
# 主要責任:
#   1. 由選定候選產生預設 EditDecision，並保留人工既有的調整。
#   2. 將 EditDecision 展開為 Timeline，再由 Timeline 衍生 CueLedger。
# 說明:
#   CueLedger 在此降為衍生產物。時間線長度取決於剪輯決策的取用區間與
#   retime，不等於素材檔案的長度總和。對白戲常由音訊決定節奏，
#   若直接串接檔案長度，成片節奏會失控。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.models.cue_ledger import AssetType, CueItem, CueLedger
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.review import DecisionLogEntry
from pipeline.models.shot import ShotPlan
from pipeline.models.timeline import EditDecision, Timeline, build_timeline
from pipeline.models.variant import AssetVariant, VariantStatus

logger = logging.getLogger("timeline_builder")

DEFAULT_CLIP_MS = 4000


def selected_variants(project_id: str) -> dict[str, AssetVariant]:
    """取得每顆鏡頭已選定的候選。"""
    from pipeline.db import engine
    from sqlmodel import Session, select

    with Session(engine) as session:
        rows = session.exec(
            select(AssetVariant).where(
                AssetVariant.project_id == project_id,
                AssetVariant.status == VariantStatus.SELECTED.value,
            )
        ).all()
    return {row.shot_id: row for row in rows}


def _usable_length(variant: AssetVariant, shot: ShotPlan | None) -> int:
    """候選可取用的長度。優先採用實際片長，缺值時退回鏡頭目標長度。"""
    if variant.actual_duration_ms and variant.actual_duration_ms > 0:
        return variant.actual_duration_ms
    if shot is not None:
        return shot.target_duration_ms
    return DEFAULT_CLIP_MS


def build_edit_decisions(
    artifact: ProductionArtifact, preserve_existing: bool = True
) -> list[EditDecision]:
    """為每顆有選定候選的鏡頭建立剪輯決策。

    preserve_existing 為真時，保留人工已調整的 in/out point 與轉場，
    僅補上尚未有決策的鏡頭。人工的取捨不應被自動重建覆寫。
    """
    chosen = selected_variants(artifact.project_id)
    shots = {shot.shot_id: shot for shot in artifact.shot_plans}
    existing = {item.shot_id: item for item in artifact.edit_decisions}

    decisions: list[EditDecision] = []
    for order, shot in enumerate(
        sorted(artifact.shot_plans, key=lambda item: item.order)
    ):
        variant = chosen.get(shot.shot_id)
        if variant is None:
            continue

        previous = existing.get(shot.shot_id)
        if preserve_existing and previous is not None:
            # 沿用人工調整，只更新排序與所指向的候選
            decisions.append(
                previous.model_copy(
                    update={"order": order, "variant_id": variant.variant_id}
                )
            )
            continue

        decisions.append(
            EditDecision(
                edit_id=f"edit_{shot.shot_id}",
                shot_id=shot.shot_id,
                variant_id=variant.variant_id,
                order=order,
                in_point_ms=0,
                out_point_ms=_usable_length(variant, shots.get(shot.shot_id)),
            )
        )
    return decisions


def timeline_to_cue_ledger(
    artifact: ProductionArtifact, timeline: Timeline
) -> CueLedger:
    """由時間線衍生 CueLedger。

    Cue 的起訖時間來自剪輯結果，而非固定秒數切分。旁白文字取自鏡頭
    所屬節拍，使字幕與畫面對齊。
    """
    shots = {shot.shot_id: shot for shot in artifact.shot_plans}
    narrative = artifact.narrative_ir

    beat_text: dict[str, str] = {}
    if narrative is not None:
        for _scene, beat in narrative.iter_beats():
            if beat.narration:
                beat_text[beat.beat_id] = beat.narration
            elif beat.dialogue:
                beat_text[beat.beat_id] = " ".join(
                    line.text for line in beat.dialogue
                )

    cues: list[CueItem] = []
    for index, clip in enumerate(timeline.clips, start=1):
        shot = shots.get(clip.shot_id)
        text = ""
        if shot is not None:
            text = beat_text.get(shot.beat_id, "") or shot.prompt
        cues.append(
            CueItem(
                cue_id=f"cue_{index:04d}",
                start_ms=clip.timeline_start_ms,
                end_ms=clip.timeline_end_ms,
                voice_text=text,
                subtitle_text=text,
                visual_prompt=shot.prompt if shot else None,
                asset_type=AssetType.GENERATED_IMAGE,
            )
        )

    return CueLedger(project_id=artifact.project_id, cues=cues)


def rebuild_timeline(
    artifact: ProductionArtifact,
    preserve_existing: bool = True,
    actor: str = "local",
) -> Timeline:
    """重建剪輯決策、時間線與 CueLedger，並寫入 artifact。"""
    decisions = build_edit_decisions(artifact, preserve_existing=preserve_existing)
    artifact.edit_decisions = decisions

    timeline = build_timeline(artifact.project_id, decisions)
    artifact.cue_ledger = timeline_to_cue_ledger(artifact, timeline)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="timeline_rebuilt",
            actor=actor,
            note=(
                f"{len(timeline.clips)} clips, "
                f"{timeline.total_duration_seconds:g}s"
            ),
        )
    )
    artifact.touch()
    return timeline


class AssemblyBlocked(RuntimeError):
    """成片組裝的前置條件未滿足。"""

    def __init__(self, missing_shots: list[str], reasons: list[str]) -> None:
        self.missing_shots = missing_shots
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class AssemblyPreflight(BaseModel):
    project_id: str
    planned_shots: int = 0
    ready_shots: int = 0
    missing_shots: list[str] = Field(default_factory=list)
    stale_shots: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_shots and not self.stale_shots and not self.reasons


def check_selection_consistency(
    artifact: ProductionArtifact, timeline: Timeline | None = None
) -> tuple[list[str], list[str]]:
    """驗證 ShotPlan → selected variant → EditDecision → TimelineClip 一致。

    改選候選後，既有的剪輯決策仍指向舊候選。若不檢查就直接 render，
    成片用的會是已經被換掉的素材，而畫面上看不出任何異常。
    回傳 (stale_shots, reasons)。
    """
    chosen = selected_variants(artifact.project_id)
    decisions = {item.shot_id: item for item in artifact.edit_decisions}

    stale: list[str] = []
    reasons: list[str] = []

    for shot in sorted(artifact.shot_plans, key=lambda item: item.order):
        variant = chosen.get(shot.shot_id)
        if variant is None:
            continue
        decision = decisions.get(shot.shot_id)
        if decision is None:
            stale.append(shot.shot_id)
            reasons.append(f"{shot.shot_id}: 已選定候選但尚無剪輯決策")
        elif decision.variant_id != variant.variant_id:
            stale.append(shot.shot_id)
            reasons.append(
                f"{shot.shot_id}: 剪輯決策指向 {decision.variant_id}，"
                f"但目前選定的是 {variant.variant_id}"
            )

    # 剪輯決策若引用了不屬於本專案分鏡的鏡頭，同樣視為過期
    planned_ids = {shot.shot_id for shot in artifact.shot_plans}
    for shot_id in decisions:
        if shot_id not in planned_ids:
            stale.append(shot_id)
            reasons.append(f"{shot_id}: 剪輯決策引用了不存在的鏡頭")

    if timeline is not None:
        expected = build_timeline(artifact.project_id, artifact.edit_decisions)
        actual_ids = [clip.variant_id for clip in timeline.clips]
        expected_ids = [clip.variant_id for clip in expected.clips]
        if actual_ids != expected_ids:
            reasons.append("傳入的時間線與目前的剪輯決策不符，需重建")
        elif timeline.total_duration_ms != expected.total_duration_ms:
            reasons.append(
                f"傳入的時間線長度 {timeline.total_duration_ms}ms 與目前剪輯決策"
                f"{expected.total_duration_ms}ms 不符，需重建"
            )

    return stale, reasons


def preflight_assembly(
    artifact: ProductionArtifact, timeline: Timeline | None = None
) -> AssemblyPreflight:
    """檢查成片組裝的前置條件。

    分鏡表上的每一顆鏡頭都必須有已選定的候選。若缺任何一顆仍照常輸出，
    成片會少掉幾個鏡頭卻看不出來，而長度與時間線也對不上。
    寧可明確擋下並列出缺哪些鏡頭。
    """
    report = AssemblyPreflight(
        project_id=artifact.project_id, planned_shots=len(artifact.shot_plans)
    )
    if not artifact.shot_plans:
        report.reasons.append("專案沒有任何 ShotPlan")
        return report

    chosen = selected_variants(artifact.project_id)
    paths = variant_path_map(artifact.project_id)

    for shot in sorted(artifact.shot_plans, key=lambda item: item.order):
        variant = chosen.get(shot.shot_id)
        if variant is None:
            report.missing_shots.append(shot.shot_id)
            continue
        local_path = paths.get(variant.variant_id)
        if not local_path or not Path(local_path).exists():
            report.missing_shots.append(shot.shot_id)
            report.reasons.append(
                f"{shot.shot_id}: 選定候選 {variant.variant_id} 的檔案不存在"
            )
            continue
        report.ready_shots += 1

    without_selection = [
        shot_id
        for shot_id in report.missing_shots
        if not any(shot_id in reason for reason in report.reasons)
    ]
    if without_selection:
        report.reasons.insert(
            0, f"以下鏡頭尚未選定候選: {', '.join(without_selection)}"
        )

    stale, stale_reasons = check_selection_consistency(artifact, timeline)
    report.stale_shots = stale
    report.reasons.extend(stale_reasons)

    return report


def variant_path_map(project_id: str) -> dict[str, str]:
    from pipeline.db import engine
    from sqlmodel import Session, select

    with Session(engine) as session:
        rows = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == project_id)
        ).all()
    return {row.variant_id: row.local_path for row in rows if row.local_path}


def render_by_profile(
    settings, artifact: ProductionArtifact, timeline: Timeline | None = None
):
    """依 ProductionProfile 的 render_mode 選擇渲染路徑。

    這是雙軌設計的分流點：既有投影片路徑保留為一組 preset，
    新的鏡頭組裝走 shot_assembly。分流依政策欄位，不依 preset 名稱。

    鏡頭組裝前會執行完備度檢查，缺少已選定候選時拋出 AssemblyBlocked，
    不會靜默略過該鏡頭後照樣輸出。
    """
    from pipeline.models.production_profile import default_profile

    profile = artifact.production_profile or default_profile()
    project_dir = Path(settings.data_dir) / "projects" / artifact.project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    if not profile.uses_shot_assembly:
        from pipeline.stages.preview_renderer import render_preview

        if artifact.cue_ledger is None:
            return None
        return render_preview(project_dir, artifact.cue_ledger, artifact.title)

    active_timeline = timeline or build_timeline(
        artifact.project_id, artifact.edit_decisions
    )

    preflight = preflight_assembly(artifact, active_timeline)
    if not preflight.ok:
        raise AssemblyBlocked(
            [*preflight.missing_shots, *preflight.stale_shots], preflight.reasons
        )

    from pipeline.adapters.video.shot_assembler import assemble_timeline

    voiceover = None
    if artifact.voiceover_path and Path(artifact.voiceover_path).exists():
        voiceover = Path(artifact.voiceover_path)

    return assemble_timeline(
        project_dir=project_dir,
        timeline=active_timeline,
        variant_paths=variant_path_map(artifact.project_id),
        aspect_ratio=profile.aspect_ratio,
        audio_path=voiceover,
    )


def update_edit_decision(
    artifact: ProductionArtifact,
    shot_id: str,
    in_point_ms: int | None = None,
    out_point_ms: int | None = None,
    hold_ms: int | None = None,
    actor: str = "local",
) -> Timeline:
    """調整單一鏡頭的取用區間，並重算時間線。"""
    updated: list[EditDecision] = []
    found = False
    for decision in artifact.edit_decisions:
        if decision.shot_id != shot_id:
            updated.append(decision)
            continue
        found = True
        changes: dict = {}
        if in_point_ms is not None:
            changes["in_point_ms"] = in_point_ms
        if out_point_ms is not None:
            changes["out_point_ms"] = out_point_ms
        if hold_ms is not None:
            changes["hold_ms"] = hold_ms
        # model_copy 不觸發驗證，改以重新建構確保 out > in
        updated.append(
            EditDecision.model_validate({**decision.model_dump(), **changes})
        )

    if not found:
        raise ValueError(f"鏡頭 {shot_id} 尚無剪輯決策")

    artifact.edit_decisions = updated
    timeline = build_timeline(artifact.project_id, updated)
    artifact.cue_ledger = timeline_to_cue_ledger(artifact, timeline)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="edit_decision_updated",
            actor=actor,
            note=f"{shot_id}: in={in_point_ms} out={out_point_ms} hold={hold_ms}",
        )
    )
    artifact.touch()
    return timeline
