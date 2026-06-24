# 檔案路徑: video-pipeline/pipeline/stages/script_splitter.py
# 產生時間: 2026-06-24 21:30 +08:00
# 版本: v1.0
# 模組定位:
#   主影片腳本切片與短影片子專案生成處理器。
# 主要責任:
#   1. 解析 <SHORT_BREAK> 標記，將主腳本切片成最多 10 個短影片子專案。
#   2. 生成子專案的 cue ledger、asset manifest，並在執行緒池中併發渲染短影音預覽，提升吞吐率。
# --------------------------------------------------------------------------

import re
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from pipeline.models.cue_ledger import AssetType, CueItem, CueLedger
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.settings import Settings
from pipeline.utils.files import ensure_project_dir, write_json
from pipeline.models.review import ReviewStatus, DecisionLogEntry
from pipeline.stages.visual_contract_builder import build_visual_contract
from pipeline.stages.asset_manifest_builder import build_asset_manifest
from pipeline.stages.preview_renderer import render_preview
from pipeline.stages.run_mvp import to_srt

logger = logging.getLogger("Script_Splitter")
logger.setLevel(logging.INFO)


def split_script_into_shorts(parent_artifact: ProductionArtifact, settings: Settings) -> list[ProductionArtifact]:
    """
    Split the parent script by '<SHORT_BREAK>' into up to 10 Shorts subprojects.
    Each subproject will have its own ProductionArtifact and metadata.
    """
    script = parent_artifact.approved_script_markdown or ""
    if not script:
        logger.warning("No script found to split.")
        return []

    # 1. Split script by <SHORT_BREAK>
    parts = [p.strip() for p in re.split(r"<SHORT_BREAK>", script) if p.strip()]
    shorts_artifacts: list[ProductionArtifact] = []
    render_tasks = []

    for index, part in enumerate(parts[:10]):
        short_id = f"{parent_artifact.project_id}_short_{index + 1:02d}"
        project_dir = ensure_project_dir(settings.data_dir, short_id)

        # Build rough cue timing (approx 10 words per cue, 5 seconds duration)
        # We strip other visual tags to make subtitle_text clean
        clean_part = re.sub(r"<[^>]+>", "", part).strip()
        sentences = [s.strip() for s in re.split(r"[.!?\n]", clean_part) if s.strip()]
        
        cues: list[CueItem] = []
        current_ms = 0
        for cue_idx, sent in enumerate(sentences):
            duration = max(2000, len(sent.split()) * 400)
            cues.append(
                CueItem(
                    cue_id=f"cue_{cue_idx + 1:04d}",
                    start_ms=current_ms,
                    end_ms=current_ms + duration,
                    voice_text=sent,
                    subtitle_text=sent,
                    visual_prompt=f"Short clip illustration for sentence: {sent[:60]}",
                    asset_type=AssetType.GENERATED_IMAGE,
                )
            )
            current_ms += duration

        cue_ledger = CueLedger(project_id=short_id, cues=cues)
        subtitles_path = project_dir / "subtitles.srt"
        subtitles_path.write_text(to_srt(cue_ledger), encoding="utf-8")
        cue_ledger.subtitles_path = subtitles_path

        asset_manifest = build_asset_manifest(short_id, cue_ledger)

        # Form a new sub-artifact for the Shorts
        short_artifact = ProductionArtifact(
            project_id=short_id,
            title=f"{parent_artifact.title} - Short {index + 1:02d}",
            language=parent_artifact.language,
            persona=parent_artifact.persona,
            genre=parent_artifact.genre,
            approved_script_markdown=part,
            cue_ledger=cue_ledger,
            asset_manifest=asset_manifest,
            review_status=ReviewStatus.CUES_READY,
        )
        
        # Add basic visual contract
        # Since build_visual_contract is async, we do an offline fallback here to avoid blocking,
        # or it can be upgraded via LLM later.
        from pipeline.stages.visual_contract_builder import build_fallback_contract
        vc, vqr = build_fallback_contract(short_artifact)
        short_artifact.visual_contract = vc
        short_artifact.visual_qc_report = vqr

        short_artifact.decision_log.append(
            DecisionLogEntry(
                action="shorts_split_created",
                actor="system",
                note=f"Split from parent project {parent_artifact.project_id} (Part {index + 1})"
            )
        )
        short_artifact.touch()

        # Save files
        write_json(project_dir / "cue_ledger.json", cue_ledger)
        write_json(project_dir / "asset_manifest.json", asset_manifest)
        write_json(project_dir / "visual_contract.json", vc)
        write_json(project_dir / "visual_qc_report.json", vqr)

        shorts_artifacts.append(short_artifact)
        render_tasks.append((project_dir, cue_ledger, short_artifact.title, short_artifact))

    # Parallelize preview rendering
    logger.info(f"Starting parallel preview rendering for {len(render_tasks)} Shorts subprojects")
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_art = {
            executor.submit(render_preview, p_dir, ledger, title): (p_dir, art)
            for p_dir, ledger, title, art in render_tasks
        }
        for future in as_completed(future_to_art):
            p_dir, art = future_to_art[future]
            try:
                preview_path = future.result()
                art.preview_mp4 = preview_path
                write_json(p_dir / "production_artifact.json", art)
                logger.info(f"Successfully generated Short subproject: {art.project_id}")
            except Exception as e:
                logger.error(f"Failed to render preview for {art.project_id}: {e}")

    return shorts_artifacts
