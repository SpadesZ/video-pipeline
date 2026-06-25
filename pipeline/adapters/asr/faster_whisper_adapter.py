import logging
import os
from pathlib import Path

from pipeline.models.transcript import TranscriptSegment


logger = logging.getLogger("FasterWhisperAdapter")
logger.setLevel(logging.INFO)


def transcribe_with_faster_whisper(audio_path: Path) -> list[TranscriptSegment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Start the Docker ASR profile "
            "or use external transcript import."
        ) from exc

    if not audio_path.exists():
        raise FileNotFoundError(f"ASR input file not found: {audio_path}")

    model_size = os.getenv("ASR_MODEL_SIZE", "tiny")
    compute_type = os.getenv("ASR_COMPUTE_TYPE", "int8")
    language = os.getenv("ASR_LANGUAGE", "").strip() or None

    logger.info("Loading faster-whisper model %s on CPU for %s", model_size, audio_path)
    model = WhisperModel(model_size, device="cpu", compute_type=compute_type)

    kwargs = {"beam_size": 5}
    if language:
        kwargs["language"] = language
    segments_generator, info = model.transcribe(str(audio_path), **kwargs)

    transcript_segments: list[TranscriptSegment] = []
    logger.info(
        "Detected language '%s' with probability %s",
        getattr(info, "language", "unknown"),
        getattr(info, "language_probability", "unknown"),
    )

    for index, segment in enumerate(segments_generator, start=1):
        text = segment.text.strip()
        if not text:
            continue
        transcript_segments.append(
            TranscriptSegment(
                segment_id=f"seg_{index:04d}",
                start_ms=max(0, int(segment.start * 1000)),
                end_ms=max(0, int(segment.end * 1000)),
                text=text,
            )
        )

    logger.info("Transcription complete, generated %s segments.", len(transcript_segments))
    return transcript_segments
