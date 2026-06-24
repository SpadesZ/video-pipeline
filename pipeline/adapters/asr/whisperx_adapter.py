from pathlib import Path


def transcribe_with_whisperx_cpu(audio_path: Path) -> None:
    raise NotImplementedError(
        "WhisperX is intentionally not installed in the default CPU image. "
        "Use ASR_PROVIDER=import for MVP, or add a dedicated asr-cpu image later."
    )

