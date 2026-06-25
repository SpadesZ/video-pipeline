# 檔案路徑: video-pipeline/pipeline/stages/run_mvp.py
# 產生時間: 2026-06-24 21:15 +08:00
# 版本: v1.0
# 模組定位:
#   MVP 影音合成流水線排程協調器 (Orchestrator)。
# 主要責任:
#   1. 解析腳本，生成時間軸 Cue ledger 及 subtitles.srt。
#   2. 生成 asset_manifest，並觸發 Consensus Optimization 審查 visual contract。
#   3. 調用 FFmpeg 渲染引擎生成 preview 預覽影片與 upload_package。
# 綠標提醒:
#   - 本地測試或非同步 Celery worker 執行此協調器時，皆會追加 pipeline_initialized 以供流程追蹤。
#   - SRT 生成字串時務必呼叫 .strip() 去除結尾多餘換行與空白，以確保字幕格式合規。
# --------------------------------------------------------------------------

from pathlib import Path
from uuid import uuid4
import asyncio
from sqlmodel import Session

from pipeline.models.production_artifact import ProductionArtifact
from pipeline.settings import Settings
from pipeline.stages.asset_manifest_builder import build_asset_manifest
from pipeline.stages.compliance_checker import check_compliance
from pipeline.stages.cue_ledger_builder import build_cue_ledger
from pipeline.stages.packaging_generator import write_upload_package
from pipeline.stages.preview_renderer import render_preview
from pipeline.stages.script_parser import parse_script
from pipeline.stages.visual_contract_builder import build_visual_contract
from pipeline.utils.files import ensure_project_dir, write_json
from pipeline.models.review import DecisionLogEntry



def create_project_id(prefix: str = "vid") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


async def run_mvp_pipeline(
    settings: Settings,
    title: str,
    script_markdown: str,
    session: Session | None = None,
    project_id: str | None = None,
    language: str = "en",
    persona: str | None = None,
) -> ProductionArtifact:
    resolved_project_id = project_id or create_project_id()
    project_dir = ensure_project_dir(settings.data_dir, resolved_project_id)

    parsed = parse_script(script_markdown)
    cue_ledger = build_cue_ledger(
        project_id=resolved_project_id,
        parsed=parsed,
        cue_seconds=settings.default_cue_seconds,
    )
    subtitles_path = project_dir / "subtitles.srt"
    subtitles_path.write_text(to_srt(cue_ledger), encoding="utf-8")
    cue_ledger.subtitles_path = str(subtitles_path)

    asset_manifest = build_asset_manifest(resolved_project_id, cue_ledger)

    artifact = ProductionArtifact(
        project_id=resolved_project_id,
        title=title,
        language=language,
        persona=persona,
        approved_script_markdown=script_markdown,
        cue_ledger=cue_ledger,
        asset_manifest=asset_manifest,
    )
    artifact.visual_contract, artifact.visual_qc_report = await build_visual_contract(artifact)
    # 寫入初始化決策日誌，確保程式執行或 Web 端觸發皆有完整的追蹤軌跡
    artifact.decision_log.append(
        DecisionLogEntry(
            action="pipeline_initialized",
            actor="orchestrator",
            note="Orchestrated MVP pipeline stages completed.",
            to_status=artifact.review_status,
        )
    )
    artifact.compliance_report = check_compliance(artifact)
    loop = asyncio.get_running_loop()
    artifact.preview_mp4 = str(await loop.run_in_executor(None, render_preview, project_dir, cue_ledger, title))
    artifact.upload_package_path = str(write_upload_package(project_dir, artifact))
    artifact.touch()

    write_json(project_dir / "cue_ledger.json", cue_ledger)
    write_json(project_dir / "asset_manifest.json", asset_manifest)
    write_json(project_dir / "visual_contract.json", artifact.visual_contract)
    write_json(project_dir / "visual_qc_report.json", artifact.visual_qc_report)
    write_json(project_dir / "compliance_report.json", artifact.compliance_report)

    if session:
        session.add(artifact)
        session.commit()
        session.refresh(artifact)

    return artifact


def to_srt(cue_ledger) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cue_ledger.cues, start=1):
        # 去除多餘的前後換行與空格，防止字幕渲染時重疊或錯亂
        clean_subtitle = (cue.subtitle_text or cue.voice_text).strip()
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_time(cue.start_ms)} --> {format_srt_time(cue.end_ms)}",
                    clean_subtitle,
                    "",
                ]
            )
        )
    return "\n".join(blocks)


def format_srt_time(ms: int) -> str:
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
