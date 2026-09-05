# 檔案路徑: video-pipeline/pipeline/assistant/context.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   AI 助手的分層脈絡組裝。
# 主要責任:
#   1. 依目前 route、entity 與流程狀態組出助手需要的脈絡。
#   2. 明確界定哪些資料會送給模型，哪些不送。
# 說明:
#   把整個 repo 或整張資料表塞進 prompt 有三個問題：貴、慢，
#   而且模型會被無關內容帶偏。這裡改成分層——常駐的只有系統摘要與
#   目前頁面，實體細節依 route 取需要的欄位，深入的概念問題才檢索知識庫。
#
#   送出去的欄位是白名單。不是「過濾掉密鑰」，而是「只放進列舉過的欄位」：
#   前者會隨著模型新增欄位而漏，後者不會。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pipeline.assistant import knowledge
from pipeline.settings import Settings

logger = logging.getLogger("assistant.context")

# 絕對不進入脈絡的欄位名片段。白名單之外還多一層，
# 是為了擋住之後有人在白名單裡加了帶密鑰的欄位。
FORBIDDEN_FRAGMENTS = (
    "api_key", "apikey", "secret", "token", "password", "credential",
    "authorization", "private_key", "access_key",
)


def scrub(value):
    """遞迴移除疑似密鑰的欄位。白名單之外的第二道防線。"""
    if isinstance(value, dict):
        return {
            key: scrub(item)
            for key, item in value.items()
            if not any(bad in str(key).lower() for bad in FORBIDDEN_FRAGMENTS)
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


class ControlContext(BaseModel):
    control_id: str
    label: str
    action: str
    enabled: bool
    disabled_reason: str | None = None
    danger_level: str = "safe"


class ErrorContext(BaseModel):
    code: str = ""
    message: str = ""
    explanation: str = ""
    fix: str = ""
    next_route: str = ""


class WorkflowContext(BaseModel):
    current_step: int
    next_action: str
    steps: list[dict] = Field(default_factory=list)
    assets: str = ""
    targets: str = ""
    jobs: str = ""
    candidates: str = ""
    scoring: str = ""
    provisional_target_ids: list[str] = Field(default_factory=list)


class AssistantContext(BaseModel):
    """一次提問所附帶的全部脈絡。可直接序列化供測試檢查。"""

    route: str
    page_title: str = ""
    page_purpose: str = ""
    page_detail: str = ""
    entity_type: str = ""
    entity_id: str = ""
    entity: dict = Field(default_factory=dict)
    workflow: WorkflowContext | None = None
    controls: list[ControlContext] = Field(default_factory=list)
    errors: list[ErrorContext] = Field(default_factory=list)
    retrieved: list[knowledge.KnowledgeCard] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def as_prompt(self) -> str:
        """組成給模型的脈絡文字。"""
        blocks = [knowledge.system_summary()]

        page = [f"使用者目前在 {self.route}"]
        if self.page_title:
            page.append(f"頁面：{self.page_title}——{self.page_purpose}")
        if self.page_detail:
            page.append(self.page_detail.strip())
        blocks.append("\n".join(page))

        if self.workflow is not None:
            state = [
                f"目前在第 {self.workflow.current_step} 步",
                f"下一步：{self.workflow.next_action}",
                f"素材：{self.workflow.assets}",
                f"比較對象：{self.workflow.targets}",
                f"派工：{self.workflow.jobs}",
                f"候選：{self.workflow.candidates}",
                f"評分：{self.workflow.scoring}",
            ]
            for step in self.workflow.steps:
                line = f"  {step['number']}. {step['title']}: {step['state']}"
                if step.get("blockers"):
                    line += "（卡住：" + "；".join(step["blockers"][:3]) + "）"
                state.append(line)
            blocks.append("目前流程狀態：\n" + "\n".join(state))

        if self.entity:
            fields = "\n".join(
                f"  {key}: {value}" for key, value in self.entity.items()
            )
            blocks.append(f"目前檢視的 {self.entity_type}：\n{fields}")

        if self.controls:
            lines = []
            for item in self.controls:
                status = (
                    "可按" if item.enabled else f"停用（{item.disabled_reason}）"
                )
                lines.append(
                    f"  「{item.label}」[{item.control_id}] {status}"
                    f" — {item.action}"
                )
            blocks.append("這一頁的操作：\n" + "\n".join(lines))

        if self.errors:
            lines = []
            for item in self.errors:
                lines.append(
                    f"  {item.code}: {item.message}\n"
                    f"    為什麼：{item.explanation}\n"
                    f"    怎麼修：{item.fix}（{item.next_route}）"
                )
            blocks.append("目前的錯誤或警告：\n" + "\n".join(lines))

        if self.retrieved:
            blocks.append(
                "相關系統知識：\n"
                + "\n\n".join(item.text for item in self.retrieved)
            )

        return "\n\n".join(blocks)


# 各 entity_type 允許送出的欄位。白名單，不是黑名單。
ENTITY_FIELDS = {
    "capability_job": (
        "job_id", "shot_id", "scenario", "target_id", "provider", "model_id",
        "model_version", "ui_label", "status", "capability", "transport",
        "aspect_ratio", "duration_ms", "camera", "variants_imported",
        "provisional_at_dispatch",
    ),
    "continuity_pair": (
        "target_id", "shot_id", "ref_shot_id", "scenario", "variant_id",
        "ref_variant_id", "scored", "ready",
    ),
    "asset_variant": (
        "variant_id", "shot_id", "target_id", "provider", "model_id",
        "model_version", "file_name", "benchmark_selected", "version_drift",
    ),
}


def _job_entity(entity_id: str, project_id: str) -> dict:
    from pipeline.benchmark import job_view

    view = job_view.load_job(project_id, entity_id)
    data = view.model_dump()
    # 提示詞可能很長，脈絡只需要知道有沒有、大概是什麼
    data["prompt_preview"] = (view.prompt or "")[:160]
    return {
        key: data[key] for key in ENTITY_FIELDS["capability_job"] if key in data
    } | {"prompt_preview": data["prompt_preview"]}


def _continuity_entity(entity_id: str, project_id: str) -> dict:
    from pipeline.benchmark import workflow

    parts = entity_id.split("|")
    if len(parts) < 2:
        return {}
    pair = workflow.find_continuity_pair(parts[0], parts[1], project_id)
    data = pair.model_dump() | {"ready": pair.ready}
    return {
        key: data[key] for key in ENTITY_FIELDS["continuity_pair"] if key in data
    }


def build(
    settings: Settings,
    route: str,
    question: str = "",
    entity_type: str = "",
    entity_id: str = "",
    error_code: str = "",
    error_message: str = "",
) -> AssistantContext:
    """組出脈絡。任何一層失敗都降級為缺少那一層，不讓助手整個壞掉。"""
    from pipeline.benchmark import workflow as bm_workflow

    context = AssistantContext(
        route=route or "/", entity_type=entity_type, entity_id=entity_id
    )

    page = knowledge.page_for_route(context.route)
    if page is not None:
        context.page_title = page.title
        context.page_purpose = page.purpose
        context.page_detail = page.detail

    # benchmark 之外的頁面不需要 benchmark 流程狀態，也就不去讀資料庫
    if context.route.startswith("/benchmark"):
        try:
            state = bm_workflow.collect(settings)
            context.workflow = WorkflowContext(
                current_step=state.current_step,
                next_action=state.next_action(),
                steps=[
                    {
                        "number": step.number,
                        "title": step.title,
                        "state": step.state,
                        "blockers": list(step.blockers),
                    }
                    for step in state.steps
                ],
                assets=f"{state.assets_ready}/{state.assets_total} 已通過驗證",
                targets=(
                    f"{len(state.targets)} 個，其中 "
                    f"{len(state.provisional_target_ids)} 個版本待確認"
                ),
                jobs=f"{state.jobs_total}/{state.jobs_expected} 份派工已建立",
                candidates=(
                    f"已匯入 {state.variants_imported} 支，"
                    f"{state.variants_unattributed} 支無血緣；"
                    f"已記錄 {state.attempts_recorded} 次嘗試"
                ),
                scoring=(
                    f"已評分 {state.variants_scored}/{state.variants_imported}，"
                    f"代表作 {state.benchmark_selected} 組，"
                    f"連戲 {state.continuity_scored}/{state.continuity_expected} 組"
                ),
                provisional_target_ids=list(state.provisional_target_ids),
            )
            context.controls = [
                ControlContext(
                    control_id=item.control_id,
                    label=item.label,
                    action=item.action,
                    enabled=item.enabled,
                    disabled_reason=item.disabled_reason,
                    danger_level=item.danger_level,
                )
                for item in state.all_controls()
            ]
            for blocker in state.build_blockers[:6]:
                doc = knowledge.error_for_code(blocker.code)
                context.errors.append(
                    ErrorContext(
                        code=blocker.code,
                        message=blocker.message,
                        explanation=doc.why.strip() if doc else "",
                        fix=doc.fix if doc else "",
                        next_route=doc.next if doc else (blocker.fix_link or ""),
                    )
                )
        except Exception as error:  # noqa: BLE001 - 助手不得因狀態讀取失敗而中斷
            logger.warning("assistant workflow context failed: %s", error)
            context.notes.append("目前無法讀取流程狀態")

    if entity_type and entity_id:
        project_id = bm_workflow.PROJECT_ID
        try:
            if entity_type == "capability_job":
                context.entity = _job_entity(entity_id, project_id)
            elif entity_type == "continuity_pair":
                context.entity = _continuity_entity(entity_id, project_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("assistant entity context failed: %s", error)
            context.notes.append(f"無法讀取 {entity_type} {entity_id}")

    # 使用者從錯誤橫幅按 Ask AI 進來，帶的是那一則錯誤
    if error_code or error_message:
        doc = knowledge.error_for_code(error_code)
        context.errors.insert(
            0,
            ErrorContext(
                code=error_code,
                message=error_message,
                explanation=doc.why.strip() if doc else "",
                fix=doc.fix if doc else "",
                next_route=doc.next if doc else "",
            ),
        )

    # 只有問到系統概念時才檢索，一般的操作問題由上面的狀態回答
    if question:
        context.retrieved = knowledge.retrieve(question)

    context.entity = scrub(context.entity)
    return context
