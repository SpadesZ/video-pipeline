# 檔案路徑: video-pipeline/pipeline/benchmark/workflow.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark 六步流程的狀態推導。
# 主要責任:
#   1. 以單一函式回答「做到哪、卡在哪、還缺什麼」。
#   2. 描述每個步驟上的操作，包含停用原因。
# 說明:
#   控制台與 AI 助手都需要知道流程狀態。若各自推導，兩者遲早會不一致：
#   畫面顯示按鈕已停用，助手卻說可以按。因此狀態只在這裡算一次，
#   頁面負責畫，助手負責解釋，兩者讀同一份資料。
#
#   ControlState 由後端明確宣告，而非讓助手去猜 DOM 的語義。
#   停用原因寫在資料裡，使用者問「為什麼不能按」時才有確定的答案。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pipeline.benchmark import attempts, attribution, builder, target, v1_pack
from pipeline.settings import Settings

logger = logging.getLogger("benchmark.workflow")

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID

# 步驟狀態。blocked 代表有明確阻擋原因，todo 代表尚未輪到。
STEP_DONE = "done"
STEP_ACTIVE = "active"
STEP_TODO = "todo"
STEP_BLOCKED = "blocked"


class ControlState(BaseModel):
    """一個使用者可操作的控制項。

    danger_level 表達按下去會不會蓋掉既有資料，讓助手能回答
    「按了會不會覆蓋」而不必猜。
    """

    control_id: str
    label: str
    action: str
    enabled: bool = True
    disabled_reason: str | None = None
    danger_level: str = "safe"  # safe | caution | destructive
    help_key: str | None = None
    href: str | None = None


class StepState(BaseModel):
    number: int
    key: str
    title: str
    state: str
    summary: str
    blockers: list[str] = Field(default_factory=list)
    controls: list[ControlState] = Field(default_factory=list)


class TargetState(BaseModel):
    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    ui_label: str = ""
    provisional: bool = True
    job_count: int = 0
    variant_count: int = 0


class WorkflowState(BaseModel):
    """整個 V1 Benchmark 的狀態快照。"""

    project_id: str = PROJECT_ID
    current_step: int = 1
    steps: list[StepState] = Field(default_factory=list)

    assets_total: int = 0
    assets_ready: int = 0
    asset_problems: list[str] = Field(default_factory=list)

    targets: list[TargetState] = Field(default_factory=list)
    provisional_target_ids: list[str] = Field(default_factory=list)

    build_ok: bool = False
    build_blockers: list[builder.BuildBlocker] = Field(default_factory=list)

    project_exists: bool = False
    shots_total: int = 0
    jobs_total: int = 0
    jobs_expected: int = 0

    attempts_recorded: int = 0
    attempts_failed: int = 0
    variants_imported: int = 0
    variants_unattributed: int = 0

    variants_scored: int = 0
    benchmark_selected: int = 0
    continuity_scored: int = 0
    continuity_expected: int = 0

    degraded: list[str] = Field(default_factory=list)

    def step(self, key: str) -> StepState | None:
        return next((item for item in self.steps if item.key == key), None)

    def control(self, control_id: str) -> ControlState | None:
        for step in self.steps:
            for item in step.controls:
                if item.control_id == control_id:
                    return item
        return None

    def all_controls(self) -> list[ControlState]:
        return [item for step in self.steps for item in step.controls]

    def blocked_reasons(self) -> list[str]:
        return [
            reason for step in self.steps for reason in step.blockers
        ]

    def next_action(self) -> str:
        """一句話說明下一步。使用者問「我現在要做什麼」時的答案。"""
        for step in self.steps:
            if step.state in (STEP_BLOCKED, STEP_ACTIVE):
                if step.blockers:
                    return f"步驟 {step.number}「{step.title}」：{step.blockers[0]}"
                return f"步驟 {step.number}「{step.title}」：{step.summary}"
        return "六個步驟都已完成，可以查看情境結果。"


def _asset_step(state: WorkflowState) -> StepState:
    controls = [
        ControlState(
            control_id="benchmark.assets.open",
            label="上傳參考素材",
            action="開啟素材上傳頁，逐格上傳角色設定圖與六張首幀",
            href="/benchmark/assets",
            help_key="benchmark.assets",
        )
    ]
    if state.assets_ready >= state.assets_total and state.assets_total:
        return StepState(
            number=1, key="assets", title="準備參考素材", state=STEP_DONE,
            summary=f"{state.assets_ready}/{state.assets_total} 已就緒並通過驗證",
            controls=controls,
        )
    return StepState(
        number=1, key="assets", title="準備參考素材", state=STEP_BLOCKED,
        summary=f"{state.assets_ready}/{state.assets_total} 已就緒",
        blockers=state.asset_problems[:8],
        controls=controls,
    )


def _target_step(state: WorkflowState) -> StepState:
    controls = [
        ControlState(
            control_id="benchmark.targets.open",
            label="確認平台版本",
            action="開啟比較對象確認頁，填入平台上實際顯示的模型名稱與版本",
            href="/benchmark/targets",
            help_key="benchmark.targets",
        )
    ]
    pending = state.provisional_target_ids
    if not pending:
        return StepState(
            number=2, key="targets", title="確認平台版本", state=STEP_DONE,
            summary=f"{len(state.targets)} 個比較對象都已確認版本",
            controls=controls,
        )
    return StepState(
        number=2, key="targets", title="確認平台版本", state=STEP_ACTIVE,
        summary=(
            f"{len(pending)}/{len(state.targets)} 個比較對象的平台版本尚未確認"
        ),
        blockers=[f"{item} 的平台實際版本尚未確認" for item in pending],
        controls=controls,
    )


def _dispatch_step(state: WorkflowState) -> StepState:
    disabled_reason = None
    if not state.build_ok:
        disabled_reason = "；".join(
            item.message for item in state.build_blockers[:3]
        )

    controls = [
        ControlState(
            control_id="benchmark.build",
            label="建立正式 Job Packages",
            action=(
                "以目前確認的比較對象對六顆鏡頭各建立一份派工，"
                "並產生空白的評分表與嘗試紀錄"
            ),
            enabled=state.build_ok,
            disabled_reason=disabled_reason,
            danger_level="caution",
            help_key="benchmark.build",
        ),
        ControlState(
            control_id="benchmark.jobs.open",
            label="查看 Job Workbench",
            action="逐一開啟每份派工，複製提示詞、下載參考圖、記錄與匯入結果",
            href="/benchmark/jobs",
            enabled=state.jobs_total > 0,
            disabled_reason=None if state.jobs_total else "尚未建立任何派工",
            help_key="benchmark.workbench",
        ),
    ]

    if not state.build_ok:
        return StepState(
            number=3, key="dispatch", title="建立派工", state=STEP_BLOCKED,
            summary="尚未滿足正式派工條件",
            blockers=[item.message for item in state.build_blockers[:8]],
            controls=controls,
        )
    if state.jobs_total == 0:
        return StepState(
            number=3, key="dispatch", title="建立派工", state=STEP_ACTIVE,
            summary="條件已滿足，可以建立正式 job packages",
            controls=controls,
        )
    return StepState(
        number=3, key="dispatch", title="建立派工", state=STEP_DONE,
        summary=f"{state.jobs_total} 份 job package 已建立",
        controls=controls,
    )


def _generation_step(state: WorkflowState) -> StepState:
    controls = [
        ControlState(
            control_id="benchmark.jobs.open.generate",
            label="開啟 Job Workbench",
            action="從派工頁複製提示詞到平台生成，回來記錄嘗試並匯入影片",
            href="/benchmark/jobs",
            enabled=state.jobs_total > 0,
            disabled_reason=None if state.jobs_total else "尚未建立任何派工",
            help_key="benchmark.workbench",
        ),
    ]
    blockers: list[str] = []
    if state.variants_unattributed:
        blockers.append(
            f"{state.variants_unattributed} 支候選沒有派工來源，不會計入統計"
        )

    summary = (
        f"已記錄 {state.attempts_recorded} 次嘗試"
        f"（未成功 {state.attempts_failed} 次）、"
        f"已匯入 {state.variants_imported} 支候選"
    )
    if state.jobs_total == 0:
        return StepState(
            number=4, key="generation", title="生成與匯入", state=STEP_TODO,
            summary="尚未建立派工", controls=controls,
        )
    if state.variants_imported == 0:
        return StepState(
            number=4, key="generation", title="生成與匯入", state=STEP_ACTIVE,
            summary=summary, blockers=blockers, controls=controls,
        )
    step_state = STEP_DONE if not blockers else STEP_ACTIVE
    return StepState(
        number=4, key="generation", title="生成與匯入", state=step_state,
        summary=summary, blockers=blockers, controls=controls,
    )


def _scoring_step(state: WorkflowState) -> StepState:
    controls = [
        ControlState(
            control_id="benchmark.qc.open",
            label="評分與選片",
            action="為每支候選打分，並為每個比較對象在每顆鏡頭選一支代表作",
            href=f"/projects/{PROJECT_ID}/view#variants",
            enabled=state.variants_imported > 0,
            disabled_reason=(
                None if state.variants_imported else "尚無候選可評分"
            ),
            help_key="benchmark.qc",
        ),
        ControlState(
            control_id="benchmark.continuity.open",
            label="連戲評分",
            action="並排比較兩顆鏡頭的代表作，評身份、服裝與場景的一致性",
            href="/benchmark/continuity",
            enabled=state.benchmark_selected > 0,
            disabled_reason=(
                None if state.benchmark_selected else "尚未選定任何代表作"
            ),
            help_key="benchmark.continuity",
        ),
        ControlState(
            control_id="benchmark.sync",
            label="同步評分表",
            action=(
                "自動補齊代表作與 CSV 中的 variant_id。"
                "只補空欄位，不會覆蓋已填寫的分數"
            ),
            danger_level="caution",
            help_key="benchmark.sync",
        ),
    ]
    if not state.variants_imported:
        return StepState(
            number=5, key="scoring", title="評分與選定代表作", state=STEP_TODO,
            summary="尚無候選可評分", controls=controls,
        )
    summary = (
        f"已評分 {state.variants_scored}/{state.variants_imported} 支候選、"
        f"已選定 {state.benchmark_selected} 組代表作、"
        f"已完成 {state.continuity_scored}/{state.continuity_expected} 組連戲評分"
    )
    blockers: list[str] = []
    if state.variants_scored < state.variants_imported:
        blockers.append(
            f"還有 {state.variants_imported - state.variants_scored} 支候選未評分"
        )
    if state.continuity_scored < state.continuity_expected:
        blockers.append(
            f"還有 {state.continuity_expected - state.continuity_scored} "
            "組連戲評分未完成"
        )
    done = not blockers and state.benchmark_selected > 0
    return StepState(
        number=5, key="scoring", title="評分與選定代表作",
        state=STEP_DONE if done else STEP_ACTIVE,
        summary=summary, blockers=blockers, controls=controls,
    )


def _result_step(state: WorkflowState) -> StepState:
    controls = [
        ControlState(
            control_id="benchmark.results.open",
            label="查看結果",
            action="依情境分別呈現各比較對象的可用率、重試與人工成本",
            href="/benchmark/results",
            enabled=state.variants_scored > 0,
            disabled_reason=None if state.variants_scored else "尚無評分資料",
            help_key="benchmark.results",
        )
    ]
    if not state.variants_scored:
        return StepState(
            number=6, key="results", title="查看情境結果", state=STEP_TODO,
            summary="尚無評分資料", controls=controls,
        )
    return StepState(
        number=6, key="results", title="查看情境結果", state=STEP_ACTIVE,
        summary="依情境分別評選，不產生跨情境總冠軍", controls=controls,
    )


def collect(settings: Settings) -> WorkflowState:
    """推導目前的流程狀態。

    任何一段資料讀取失敗都記入 degraded 而不拋出：控制台是使用者判斷
    現況的唯一入口，整頁開不起來比少一塊資訊嚴重得多。
    """
    from sqlmodel import Session, select

    from pipeline.db import engine
    from pipeline.models.qc import ContinuityQC, VariantQC
    from pipeline.models.variant import CapabilityJob
    from pipeline.project_store import load_project

    state = WorkflowState()
    state.shots_total = len(v1_pack.shots())
    state.continuity_expected = len(v1_pack.CONTINUITY_PAIRS) * len(
        target.targets().targets
    )

    try:
        report = builder.check_assets(settings)
        state.assets_total = len(report.statuses)
        state.assets_ready = state.assets_total - len(report.missing) - len(
            report.invalid
        )
        state.asset_problems = [
            f"{builder.asset_label(item.description)}："
            + ("; ".join(item.problems) if item.problems else "尚未上傳")
            for item in [*report.missing, *report.invalid]
        ]
    except Exception as error:  # noqa: BLE001
        logger.warning("asset check failed: %s", error)
        state.degraded.append(f"素材檢查失敗: {str(error)[:120]}")
        report = None

    try:
        gate = builder.evaluate_build_gate(settings, report)
        state.build_ok = gate.ok
        state.build_blockers = list(gate.blockers)
        state.provisional_target_ids = list(gate.provisional_target_ids)
    except Exception as error:  # noqa: BLE001
        logger.warning("build gate failed: %s", error)
        state.degraded.append(f"派工條件檢查失敗: {str(error)[:120]}")
        state.build_ok = False

    job_counts: dict[str, int] = {}
    variant_counts: dict[str, int] = {}
    try:
        artifact = load_project(settings, PROJECT_ID)
        state.project_exists = artifact is not None
        state.jobs_expected = len(target.targets().targets) * state.shots_total

        with Session(engine) as session:
            jobs = session.exec(
                select(CapabilityJob).where(CapabilityJob.project_id == PROJECT_ID)
            ).all()
            for job in jobs:
                parameters = (job.request_snapshot or {}).get("parameters") or {}
                key = parameters.get(attribution.TARGET_PARAMETER_KEY)
                if key:
                    job_counts[str(key)] = job_counts.get(str(key), 0) + 1
            state.jobs_total = sum(job_counts.values())

            state.variants_scored = len(
                session.exec(
                    select(VariantQC).where(VariantQC.project_id == PROJECT_ID)
                ).all()
            )
            state.continuity_scored = len(
                session.exec(
                    select(ContinuityQC).where(
                        ContinuityQC.project_id == PROJECT_ID
                    )
                ).all()
            )

        index = attribution.build_index(PROJECT_ID)
        state.variants_imported = len(index.items)
        state.variants_unattributed = len(index.unattributed)
        state.benchmark_selected = sum(
            1 for item in index.items if item.benchmark_selected
        )
        for item in index.items:
            variant_counts[item.target_id] = variant_counts.get(item.target_id, 0) + 1
    except Exception as error:  # noqa: BLE001
        logger.warning("project state failed: %s", error)
        state.degraded.append(f"專案狀態讀取失敗: {str(error)[:120]}")

    try:
        ledger = attempts.read_ledger(
            attempts_path(settings)
        )
        state.attempts_recorded = len(ledger.attempts)
        state.attempts_failed = sum(
            1 for item in ledger.attempts if not item.succeeded
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("ledger read failed: %s", error)
        state.degraded.append(f"嘗試紀錄讀取失敗: {str(error)[:120]}")

    state.targets = [
        TargetState(
            target_id=item.target_id,
            provider=item.provider,
            model_id=item.model_id,
            model_version=item.model_version,
            ui_label=item.ui_label,
            provisional=item.provisional,
            job_count=job_counts.get(item.target_id, 0),
            variant_count=variant_counts.get(item.target_id, 0),
        )
        for item in target.targets().targets
    ]

    state.steps = [
        _asset_step(state),
        _target_step(state),
        _dispatch_step(state),
        _generation_step(state),
        _scoring_step(state),
        _result_step(state),
    ]
    state.current_step = next(
        (
            step.number
            for step in state.steps
            if step.state in (STEP_BLOCKED, STEP_ACTIVE)
        ),
        len(state.steps),
    )
    return state


class ContinuityPair(BaseModel):
    """一組待評連戲的鏡頭配對。

    兩支影片都必須是同一個比較對象在各自鏡頭上的代表作。
    缺任何一邊就無法評，這時列出缺的是哪一邊，而不是讓使用者
    在畫面上看到一個空的播放器卻不知道為什麼。
    """

    target_id: str
    shot_id: str
    ref_shot_id: str
    scenario: str = ""
    variant_id: str | None = None
    ref_variant_id: str | None = None
    scored: bool = False
    qc_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.target_id}|{self.shot_id}|{self.ref_shot_id}"

    @property
    def ready(self) -> bool:
        return bool(self.variant_id and self.ref_variant_id)

    @property
    def missing(self) -> list[str]:
        gaps = []
        if not self.variant_id:
            gaps.append(f"{self.shot_id} 尚未選定代表作")
        if not self.ref_variant_id:
            gaps.append(f"{self.ref_shot_id} 尚未選定代表作")
        return gaps


def continuity_pairs(project_id: str = PROJECT_ID) -> list[ContinuityPair]:
    """列出所有連戲配對及其代表作。

    配對只由 benchmark_selected 的代表作組成。若允許任意兩支候選配對，
    分數會落在使用者當下隨手挑的組合上，換一次選片結論就變了。
    """
    from sqlmodel import Session, select

    from pipeline.db import engine
    from pipeline.models.qc import ContinuityQC

    index = attribution.build_index(project_id)
    selected: dict[tuple[str, str], str] = {}
    for item in index.items:
        if item.benchmark_selected:
            selected[(item.target_id, item.shot_id)] = item.variant_id

    with Session(engine) as session:
        rows = session.exec(
            select(ContinuityQC).where(ContinuityQC.project_id == project_id)
        ).all()
    scored = {
        (row.shot_id, row.ref_shot_id or "", row.variant_id or ""): row
        for row in rows
    }

    pairs: list[ContinuityPair] = []
    for item in target.targets().targets:
        for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
            variant_id = selected.get((item.target_id, shot_id))
            ref_variant_id = selected.get((item.target_id, ref_shot_id))
            existing = scored.get((shot_id, ref_shot_id, variant_id or ""))
            pairs.append(
                ContinuityPair(
                    target_id=item.target_id,
                    shot_id=shot_id,
                    ref_shot_id=ref_shot_id,
                    scenario=v1_pack.SHOT_SCENARIOS.get(shot_id, ""),
                    variant_id=variant_id,
                    ref_variant_id=ref_variant_id,
                    scored=existing is not None,
                    qc_id=existing.qc_id if existing else None,
                )
            )
    return pairs


def find_continuity_pair(
    target_id: str, shot_id: str, project_id: str = PROJECT_ID
) -> ContinuityPair:
    found = next(
        (
            item
            for item in continuity_pairs(project_id)
            if item.target_id == target_id and item.shot_id == shot_id
        ),
        None,
    )
    if found is None:
        raise LookupError(f"{target_id}/{shot_id} 不是已登錄的連戲配對")
    return found


def sheets_dir(settings: Settings):
    from pathlib import Path

    return Path(settings.data_dir) / "benchmark" / "v1" / "sheets"


def attempts_path(settings: Settings):
    return sheets_dir(settings) / attempts.ATTEMPTS_SHEET
