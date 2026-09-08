import logging
from pathlib import Path

from pipeline.adapters.asr.faster_whisper_adapter import transcribe_with_faster_whisper
from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.cue_ledger import AssetType, CueItem, CueLedger
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.transcript import TranscriptFormat, TranscriptImport
from pipeline.settings import Settings
from pipeline.stages.asset_manifest_builder import build_asset_manifest
from pipeline.stages.run_mvp import to_srt
from pipeline.utils.files import ensure_project_dir, write_json


logger = logging.getLogger("ASRTranscriber")
logger.setLevel(logging.INFO)

ASR_AUDIO_CANDIDATES = ("voiceover.wav", "voiceover.mp3", "audio.wav", "audio.mp3")


def find_asr_audio_path(settings: Settings, artifact: ProductionArtifact) -> Path | None:
    project_dir = ensure_project_dir(Path(settings.data_dir), artifact.project_id)
    candidate_paths: list[Path] = []

    if artifact.voiceover_path:
        voiceover_path = Path(artifact.voiceover_path)
        candidate_paths.append(voiceover_path)
        if not voiceover_path.is_absolute():
            candidate_paths.append(Path(settings.data_dir) / voiceover_path)
        candidate_paths.append(project_dir / voiceover_path.name)

    candidate_paths.extend(project_dir / candidate for candidate in ASR_AUDIO_CANDIDATES)

    for candidate_path in candidate_paths:
        if candidate_path.exists():
            return candidate_path
    return None


def run_local_asr(
    settings: Settings,
    artifact: ProductionArtifact,
    audio_path: Path,
    actor: str = "local_asr",
) -> ProductionArtifact:
    segments = transcribe_with_faster_whisper(audio_path)
    if not segments:
        message = f"ASR produced no transcript segments for {audio_path.name}; existing timeline was left unchanged."
        logger.warning(message)
        raise ValueError(message)

    duration_ms = max((segment.end_ms for segment in segments), default=0)
    transcript_import = TranscriptImport(
        format=TranscriptFormat.AUTO,
        source_name="faster_whisper_cpu",
        segments=segments,
        duration_ms=duration_ms,
        warnings=[],
    )

    old_cues = artifact.cue_ledger.cues if artifact.cue_ledger else []
    cues: list[CueItem] = []
    for index, segment in enumerate(transcript_import.segments):
        old = old_cues[index] if index < len(old_cues) else None
        cues.append(
            CueItem(
                cue_id=f"cue_{index + 1:04d}",
                start_ms=segment.start_ms,
                end_ms=max(segment.end_ms, segment.start_ms + 500),
                voice_text=segment.text,
                subtitle_text=segment.text,
                visual_prompt=old.visual_prompt if old else None,
                asset_type=old.asset_type if old else AssetType.NONE,
                shorts_id=old.shorts_id if old else f"short_{(index // 6) + 1:02d}",
                risk_note=old.risk_note if old else None,
            )
        )

    cue_ledger = CueLedger(project_id=artifact.project_id, cues=cues)
    project_dir = ensure_project_dir(Path(settings.data_dir), artifact.project_id)
    subtitles_path = project_dir / "subtitles.srt"
    subtitles_path.write_text(to_srt(cue_ledger), encoding="utf-8")
    cue_ledger.subtitles_path = str(subtitles_path)

    artifact.transcript_import = transcript_import
    artifact.cue_ledger = cue_ledger
    artifact.asset_manifest = build_asset_manifest(artifact.project_id, cue_ledger)
    artifact.review_status = ReviewStatus.CUES_READY
    artifact.decision_log.append(
        DecisionLogEntry(
            action="local_asr_completed",
            actor=actor,
            note=f"CPU ASR generated {len(segments)} transcript segments from {audio_path.name}.",
            to_status=artifact.review_status,
        )
    )
    artifact.touch()

    write_json(project_dir / "transcript_import.json", artifact.transcript_import)
    write_json(project_dir / "cue_ledger.json", artifact.cue_ledger)
    write_json(
        project_dir / "asset_manifest.json",
        artifact.asset_manifest or AssetManifest(project_id=artifact.project_id),
    )
    return artifact
