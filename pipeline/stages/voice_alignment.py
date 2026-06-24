# 檔案路徑: video-pipeline/pipeline/stages/voice_alignment.py
# 產生時間: 2026-06-24 21:16 +08:00
# 版本: v1.0 (已棄用)
# 模組定位:
#   遺留 (Legacy) ASR 語音字幕對齊模組。
# 主要責任:
#   1. 提供舊版 ImportedTranscriptSegment 向現代 TranscriptSegment 的映射。
# 綠標提醒:
#   - 此模組為相容性保留，目前全面使用 pipeline/stages/asr_aligner.py 進行語音時間軸對齊。
#   - 呼叫時會引發 DeprecationWarning 提醒開發者遷移。
# --------------------------------------------------------------------------

import warnings
from pathlib import Path

from pipeline.models.transcript import TranscriptSegment

from pipeline.adapters.asr.import_adapter import load_imported_segments


def load_transcript_segments(transcript_path: Path | None) -> list[TranscriptSegment]:
    """
    DEPRECATED: This module is legacy and has been replaced by the active ASR aligner
    (pipeline/stages/asr_aligner.py) and transcript importer (pipeline/stages/transcript_importer.py).
    
    Loads segments and converts them to modern TranscriptSegment models.
    """
    warnings.warn(
        "load_transcript_segments is deprecated and will be removed in a future release. "
        "Use pipeline/stages/asr_aligner.py or pipeline/stages/transcript_importer.py instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if transcript_path is None:
        return []
    
    imported_segs = load_imported_segments(transcript_path)
    return [
        TranscriptSegment(
            segment_id=f"legacy_{idx:04d}",
            start_ms=seg.start_ms,
            end_ms=seg.end_ms,
            text=seg.text,
        )
        for idx, seg in enumerate(imported_segs, start=1)
    ]


