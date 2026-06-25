# 檔案路徑: video-pipeline/apps/worker/worker.py
# 產生時間: 2026-06-24 21:24 +08:00
# 版本: v1.0
# 模組定位:
#   Celery Worker 非同步任務執行器。
# 主要責任:
#   1. 註冊與執行排程工作，如 video_pipeline.preview_project 及 video_pipeline.run_lava_workflow。
#   2. 管理非同步管線之語音生成、語音字幕對齊、Consensus 視覺契約、圖片下載與預覽合成。
# 綠標提醒:
#   - 呼叫 celery broker/backend 時務必使用 .get_secret_value() 自 Pydantic SecretStr 解開連線 URL。
# --------------------------------------------------------------------------

import asyncio
from pathlib import Path
import re
from celery import Celery


from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.transcript import TranscriptSegment
from pipeline.project_store import save_project
from pipeline.settings import get_settings
from pipeline.stages.preview_renderer import render_preview
from pipeline.stages.visual_contract_builder import build_visual_contract
from pipeline.stages.voiceover_generator import generate_voiceover
from pipeline.stages.asr_aligner import align_voiceover_cues
from pipeline.adapters.platforms.image_generator import generate_ai_image
from pipeline.utils.files import write_json
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.stages.compliance_checker import check_compliance
from pipeline.db import engine, init_db
from sqlmodel import Session

settings = get_settings()

celery_app = Celery(
    "video_pipeline",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)


async def run_lava_workflow_async(project_id: str, run_tts: bool = True) -> dict:
    project_dir = Path(settings.data_dir) / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    with Session(engine) as session:
        artifact = session.get(ProductionArtifact, project_id)
        if not artifact:
            raise ValueError("Project not found")

        clean_text = re.sub(r"<[^>]+>", "", artifact.approved_script_markdown or "").strip()
        voiceover_path = Path(artifact.voiceover_path) if artifact.voiceover_path else project_dir / "voiceover.wav"

        # Step 1: Voiceover TTS Generation (if requested and we have script)
        if run_tts and clean_text:
            voiceover_path = project_dir / "voiceover.wav"
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="tts_generation_started",
                    actor="lava_brain",
                    note="Requesting ElevenLabs TTS voiceover..."
                )
            )

            await generate_voiceover(clean_text, voiceover_path)
            artifact.voiceover_path = str(voiceover_path)
        elif run_tts:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="tts_generation_skipped",
                    actor="lava_brain",
                    note="Skipped TTS because the approved script is empty."
                )
            )
        else:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="tts_generation_skipped",
                    actor="lava_brain",
                    note="Skipped TTS because run_tts=False. Existing voiceover will be reused if present."
                )
            )

        # Step 2: ASR Alignment
        # Convert script into segments for alignment
        # We split by <SHORT_BREAK> or simply create 5-10 word segments as placeholders
        paragraphs = [p.strip() for p in clean_text.split("\n\n") if p.strip()]
        if clean_text and not paragraphs:
            paragraphs = [clean_text]
        segments = []
        for index, text in enumerate(paragraphs):
            segments.append(
                TranscriptSegment(
                    segment_id=f"seg_{index+1:04d}",
                    start_ms=0,
                    end_ms=0,
                    text=text
                )
            )

        if segments and voiceover_path.exists():
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="asr_alignment_started",
                    actor="lava_brain",
                    note="Aligning voiceover track to cues..."
                )
            )
            cue_ledger = await align_voiceover_cues(project_id, segments, voiceover_path)
            artifact.cue_ledger = cue_ledger
            write_json(project_dir / "cue_ledger.json", cue_ledger)
        elif segments:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="asr_alignment_skipped",
                    actor="lava_brain",
                    note="Skipped ASR alignment because no voiceover audio is available."
                )
            )
        else:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="asr_alignment_skipped",
                    actor="lava_brain",
                    note="Skipped ASR alignment because the approved script has no alignable text."
                )
            )

        # Step 3: Run Visual Bible & Storyboard (Consensus Optimization)
        if artifact.cue_ledger:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="storyboard_generation_started",
                    actor="lava_brain",
                    note="Running LAVA Storyboard with Consensus Optimization..."
                )
            )

            # This calls LAVA LLM tasks to build visual style guide, characters, and select best storyboard prompt per cue
            visual_contract, visual_qc_report = await build_visual_contract(artifact)
            artifact.visual_contract = visual_contract
            artifact.visual_qc_report = visual_qc_report
            write_json(project_dir / "visual_contract.json", visual_contract)
            write_json(project_dir / "visual_qc_report.json", visual_qc_report)

            # Step 4: Call Image Generation API to download images
            assets_dir = project_dir / "assets"
            assets_dir.mkdir(exist_ok=True)

            artifact.decision_log.append(
                DecisionLogEntry(
                    action="image_generation_started",
                    actor="lava_brain",
                    note=f"Generating {len(artifact.cue_ledger.cues)} scene images..."
                )
            )

            # Download images for each cue
            for cue in artifact.cue_ledger.cues:
                img_path = assets_dir / f"asset_{cue.cue_id}.png"
                prompt = cue.visual_prompt or cue.voice_text
                neg_prompt = visual_contract.style_guide.negative_prompts if visual_contract.style_guide else []
                await generate_ai_image(prompt, img_path, negative_prompt=", ".join(neg_prompt))

            # Step 5: Render final preview
            if artifact.cue_ledger:
                loop = asyncio.get_running_loop()
                artifact.preview_mp4 = str(await loop.run_in_executor(None, render_preview, project_dir, artifact.cue_ledger, artifact.title))

            artifact.review_status = ReviewStatus.CUES_READY

            artifact.decision_log.append(
                DecisionLogEntry(
                    action="lava_workflow_completed",
                    actor="lava_brain",
                    note=f"LAVA Brain completed. Score: {artifact.visual_qc_report.score if artifact.visual_qc_report else 100}",
                    to_status=artifact.review_status
                )
            )

        artifact.compliance_report = check_compliance(artifact)
        artifact.touch()
        artifact = save_project(settings, artifact, session=session)
        return {
            "project_id": project_id,
            "preview_mp4": str(artifact.preview_mp4) if artifact.preview_mp4 else None,
            "score": artifact.visual_qc_report.score if artifact.visual_qc_report else 100
        }


# 設定軟超時為 540 秒，硬超時為 600 秒，避免遇到 API 懸掛或 FFmpeg 死鎖時無限阻塞 Worker 進程。
@celery_app.task(
    name="video_pipeline.preview_project",
    time_limit=600,
    soft_time_limit=540,
)
def preview_project(project_id: str) -> dict:
    project_dir = Path(settings.data_dir) / "projects" / project_id
    with Session(engine) as session:
        artifact = session.get(ProductionArtifact, project_id)
        if not artifact:
            raise ValueError("Project not found")
        if artifact.cue_ledger is None:
            raise ValueError("Project has no cue ledger")

        artifact.preview_mp4 = str(render_preview(project_dir, artifact.cue_ledger, artifact.title))
        artifact.touch()

        artifact = save_project(settings, artifact, session=session)
        return {
            "project_id": project_id,
            "preview_mp4": str(artifact.preview_mp4) if artifact.preview_mp4 else None,
        }


# 執行整個 LAVA 工作流（腳本分割、TTS 語音、ASR 對齊、畫作生成、影片渲染），設置超時防範死鎖。
@celery_app.task(
    name="video_pipeline.run_lava_workflow",
    time_limit=600,
    soft_time_limit=540,
)
def run_lava_workflow(project_id: str, run_tts: bool = True) -> dict:
    """Celery wrapper for executing the asynchronous LAVA pipeline workflow."""
    return asyncio.run(run_lava_workflow_async(project_id, run_tts=run_tts))


@celery_app.task(
    name="video_pipeline.run_asr_job",
    time_limit=1800,
    soft_time_limit=1740,
)
def run_asr_job(project_id: str) -> dict:
    from pipeline.stages.asr_transcriber import find_asr_audio_path, run_local_asr

    init_db()

    with Session(engine) as session:
        artifact = session.get(ProductionArtifact, project_id)
        if not artifact:
            raise ValueError(f"Project not found: {project_id}")

        audio_path = find_asr_audio_path(settings, artifact)
        if not audio_path:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="local_asr_missing_audio",
                    actor="asr_worker",
                    note="No ASR audio file found. Add voiceover.wav, voiceover.mp3, audio.wav, or audio.mp3 to the project directory.",
                )
            )
            artifact.touch()
            save_project(settings, artifact, session=session)
            raise ValueError(f"No ASR audio file found for project {project_id}.")

        artifact.decision_log.append(
            DecisionLogEntry(
                action="local_asr_started",
                actor="asr_worker",
                note=f"Starting local CPU ASR for {audio_path.name}.",
            )
        )
        artifact.touch()
        artifact = save_project(settings, artifact, session=session)

        try:
            artifact = run_local_asr(settings, artifact, audio_path, actor="asr_worker")
            artifact = save_project(settings, artifact, session=session)
        except Exception as exc:
            artifact.decision_log.append(
                DecisionLogEntry(
                    action="local_asr_failed",
                    actor="asr_worker",
                    note=str(exc),
                )
            )
            artifact.touch()
            save_project(settings, artifact, session=session)
            raise

        return {
            "project_id": project_id,
            "status": "success",
            "segments": len(artifact.transcript_import.segments) if artifact.transcript_import else 0,
            "subtitles_path": artifact.cue_ledger.subtitles_path if artifact.cue_ledger else None,
        }

