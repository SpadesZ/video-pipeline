from pathlib import Path
import asyncio

from pipeline.adapters.asr.json_transcript_adapter import parse_json_transcript
from pipeline.adapters.asr.plain_text_adapter import parse_plain_text
from pipeline.adapters.asr.srt_adapter import parse_srt
from pipeline.adapters.asr.vtt_adapter import parse_vtt
from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.cue_ledger import AssetType, CueItem, CueLedger
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.transcript import TranscriptFormat, TranscriptImport, TranscriptSegment
from pipeline.settings import Settings
from pipeline.stages.asset_manifest_builder import build_asset_manifest
from pipeline.stages.compliance_checker import check_compliance
from pipeline.stages.preview_renderer import render_preview
from pipeline.stages.run_mvp import to_srt
from pipeline.stages.visual_contract_builder import build_visual_contract
from pipeline.utils.files import write_json


async def import_transcript(
    settings: Settings,
    artifact: ProductionArtifact,
    transcript_format: TranscriptFormat,
    content: str,
    source_name: str = "paste",
    actor: str = "local",
    note: str | None = None,
) -> ProductionArtifact:
    resolved_format = detect_format(content) if transcript_format == TranscriptFormat.AUTO else transcript_format
    segments, warnings = parse_content(resolved_format, content, settings.default_cue_seconds)
    segments = normalize_segments(segments, warnings)
    if not segments:
        raise ValueError("Transcript import produced no usable segments")

    transcript_import = TranscriptImport(
        format=resolved_format,
        source_name=source_name or "paste",
        segments=segments,
        duration_ms=max(segment.end_ms for segment in segments),
        warnings=warnings,
    )

    cue_ledger = rebuild_cue_ledger_from_segments(artifact, transcript_import)
    project_dir = Path(settings.data_dir) / "projects" / artifact.project_id
    subtitles_path = project_dir / "subtitles.srt"
    subtitles_path.write_text(to_srt(cue_ledger), encoding="utf-8")
    cue_ledger.subtitles_path = subtitles_path

    artifact.transcript_import = transcript_import
    artifact.cue_ledger = cue_ledger
    artifact.asset_manifest = build_asset_manifest(artifact.project_id, cue_ledger)
    artifact.visual_contract, artifact.visual_qc_report = await build_visual_contract(artifact)
    loop = asyncio.get_running_loop()
    artifact.preview_mp4 = await loop.run_in_executor(None, render_preview, project_dir, cue_ledger, artifact.title)
    artifact.review_status = ReviewStatus.CUES_READY
    artifact.decision_log.append(
        DecisionLogEntry(
            action="transcript_imported",
            actor=actor or "local",
            note=note or f"{resolved_format} import with {len(segments)} segments",
            to_status=artifact.review_status,
        )
    )
    artifact.compliance_report = check_compliance(artifact)
    artifact.touch()

    write_transcript_outputs(project_dir, artifact)
    return artifact


def detect_format(content: str) -> TranscriptFormat:
    head = content.lstrip()[:200].lower()
    if head.startswith("webvtt"):
        return TranscriptFormat.VTT
    if "-->" in head and "," in head:
        return TranscriptFormat.SRT
    if "-->" in head and "." in head:
        return TranscriptFormat.VTT
    if head.startswith("[") or head.startswith("{"):
        return TranscriptFormat.JSON
    return TranscriptFormat.TEXT


def parse_content(
    transcript_format: TranscriptFormat,
    content: str,
    cue_seconds: int,
) -> tuple[list[TranscriptSegment], list[str]]:
    if transcript_format == TranscriptFormat.SRT:
        return parse_srt(content)
    if transcript_format == TranscriptFormat.VTT:
        return parse_vtt(content)
    if transcript_format == TranscriptFormat.JSON:
        return parse_json_transcript(content)
    if transcript_format == TranscriptFormat.TEXT:
        return parse_plain_text(content, cue_seconds=cue_seconds)
    raise ValueError(f"Unsupported transcript format: {transcript_format}")


def normalize_segments(segments: list[TranscriptSegment], warnings: list[str]) -> list[TranscriptSegment]:
    normalized = sorted(segments, key=lambda segment: (segment.start_ms, segment.end_ms))
    output: list[TranscriptSegment] = []
    last_end = 0
    for segment in normalized:
        start_ms = max(segment.start_ms, last_end)
        end_ms = max(segment.end_ms, start_ms + 500)
        if start_ms != segment.start_ms:
            warnings.append(f"Adjusted overlapping segment {segment.segment_id}")
        output.append(
            TranscriptSegment(
                segment_id=f"seg_{len(output) + 1:04d}",
                start_ms=start_ms,
                end_ms=end_ms,
                text=segment.text.strip(),
            )
        )
        last_end = end_ms
    return output


def rebuild_cue_ledger_from_segments(
    artifact: ProductionArtifact,
    transcript_import: TranscriptImport,
) -> CueLedger:
    old_cues = artifact.cue_ledger.cues if artifact.cue_ledger else []
    cues: list[CueItem] = []
    for index, segment in enumerate(transcript_import.segments):
        old = old_cues[index] if index < len(old_cues) else None
        cues.append(
            CueItem(
                cue_id=f"cue_{index + 1:04d}",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                voice_text=segment.text,
                subtitle_text=segment.text,
                visual_prompt=old.visual_prompt if old else None,
                asset_type=old.asset_type if old else AssetType.NONE,
                shorts_id=old.shorts_id if old else f"short_{(index // 6) + 1:02d}",
                risk_note=old.risk_note if old else None,
            )
        )
    return CueLedger(project_id=artifact.project_id, cues=cues)


def write_transcript_outputs(project_dir: Path, artifact: ProductionArtifact) -> None:
    write_json(project_dir / "transcript_import.json", artifact.transcript_import or {})
    write_json(project_dir / "cue_ledger.json", artifact.cue_ledger or {})
    write_json(project_dir / "asset_manifest.json", artifact.asset_manifest or AssetManifest(project_id=artifact.project_id))
    write_json(project_dir / "visual_contract.json", artifact.visual_contract or {})
    write_json(project_dir / "visual_qc_report.json", artifact.visual_qc_report or {})
    write_json(project_dir / "compliance_report.json", artifact.compliance_report or {})
    write_json(project_dir / "production_artifact.json", artifact)
