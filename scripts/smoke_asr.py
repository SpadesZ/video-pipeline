import os
import sys
import wave
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(ROOT / 'test.db').as_posix()}")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session

from pipeline.db import engine, init_db
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.transcript import TranscriptSegment
from pipeline.project_store import load_project, project_file_path, save_project
from pipeline.settings import get_settings
import pipeline.stages.asr_transcriber as asr_transcriber


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def create_dummy_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00" * (16000 * 2 // 2))


def mock_transcribe(path: Path) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(segment_id="seg_0001", start_ms=0, end_ms=1200, text="hello from local cpu asr"),
        TranscriptSegment(segment_id="seg_0002", start_ms=1200, end_ms=2600, text="timeline smoke completed"),
    ]


def main() -> None:
    init_db()
    settings = get_settings()
    project_id = f"smoke_asr_{uuid4().hex[:10]}"

    artifact = ProductionArtifact(
        project_id=project_id,
        title="ASR Smoke Test",
        approved_script_markdown="This project verifies the ASR completion stage.",
        language="en",
    )
    audio_path = project_file_path(settings, project_id, "voiceover.wav")
    create_dummy_wav(audio_path)
    artifact.voiceover_path = str(audio_path)
    save_project(settings, artifact)

    if os.getenv("ASR_SMOKE_REAL", "0") not in {"1", "true", "TRUE"}:
        asr_transcriber.transcribe_with_faster_whisper = mock_transcribe

    with Session(engine) as session:
        artifact = session.get(ProductionArtifact, project_id)
        assert_ok(artifact is not None, "Smoke project was not saved to the database")
        artifact = asr_transcriber.run_local_asr(settings, artifact, audio_path, actor="smoke_asr")
        save_project(settings, artifact, session=session)

    loaded = load_project(settings, project_id)
    assert_ok(loaded is not None, "ASR smoke project could not be reloaded")
    assert_ok(loaded.transcript_import is not None, "transcript_import was not created")
    assert_ok(len(loaded.transcript_import.segments) >= 1, "transcript_import has no segments")
    assert_ok(loaded.cue_ledger is not None, "cue_ledger was not created")
    assert_ok(
        len(loaded.cue_ledger.cues) == len(loaded.transcript_import.segments),
        "cue count does not match transcript segment count",
    )

    transcript_path = project_file_path(settings, project_id, "transcript_import.json")
    cue_path = project_file_path(settings, project_id, "cue_ledger.json")
    subtitles_path = project_file_path(settings, project_id, "subtitles.srt")
    artifact_path = project_file_path(settings, project_id, "production_artifact.json")
    for output_path in (transcript_path, cue_path, subtitles_path, artifact_path):
        assert_ok(output_path.exists(), f"Missing ASR output file: {output_path}")

    print(
        "OK ASR smoke "
        f"project_id={project_id} "
        f"segments={len(loaded.transcript_import.segments)} "
        f"subtitles={subtitles_path}"
    )


if __name__ == "__main__":
    main()
