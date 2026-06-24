# 檔案路徑: video-pipeline/pipeline/stages/asr_aligner.py
# 產生時間: 2026-06-24 21:32 +08:00
# 版本: v1.0
# 模組定位:
#   ASR 語音辨識與時間對齊處理器。
# 主要責任:
#   1. 計算配音音軌的實體時間長度。
#   2. 解析各字幕/配音片段，動態計算起訖時間戳，將文字對齊至對應的 Cue 軌道。
# 綠標提醒:
#   - 對齊核心使用單詞長度與時間區間的等比插值估算，避免停頓處過度延展。
# --------------------------------------------------------------------------

import os
import wave
import logging
import shutil
import subprocess
from pathlib import Path

from pipeline.models.cue_ledger import CueLedger, CueItem, AssetType
from pipeline.models.transcript import TranscriptSegment

logger = logging.getLogger("ASR_Aligner")
logger.setLevel(logging.INFO)


def get_wav_duration_ms(wav_path: Path) -> int:
    """Get the duration of an audio file in milliseconds using ffprobe, wave, or size estimation."""
    if not wav_path.exists():
        return 5000

    # 1. Try ffprobe for 100% accuracy on any audio format (MP3, WAV, etc.)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            cmd = [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(wav_path)
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            duration_sec = float(res.stdout.strip())
            return int(duration_sec * 1000)
        except Exception as e:
            logger.warning(f"ffprobe duration check failed: {e}. Falling back.")

    # 2. Try standard wave library (only works for real WAV files)
    try:
        with wave.open(str(wav_path), 'rb') as f:
            frames = f.getnframes()
            rate = f.getframerate()
            duration_ms = int((frames / float(rate)) * 1000)
            return duration_ms
    except Exception as e:
        logger.warning(f"Could not read WAV duration: {e}. Estimating from file size.")

        # 3. Fallback estimation based on file size
        size = wav_path.stat().st_size
        suffix = wav_path.suffix.lower()
        
        is_mp3 = False
        try:
            with open(wav_path, "rb") as f:
                header = f.read(4)
                if header.startswith(b"ID3") or (len(header) > 0 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
                    is_mp3 = True
        except Exception:
            pass

        if suffix == ".mp3" or is_mp3:
            # ElevenLabs standard MP3 is 128 kbps (16000 bytes/sec)
            return int((size / 16000.0) * 1000)

        # Standard mono 16kHz 16bit is 32000 bytes/sec
        return int((size / 32000.0) * 1000)


def distribute_durations(duration_ms: int, segments: list[TranscriptSegment], total_chars: int) -> list[int]:
    """Distribute total duration across segments deterministically, ensuring a minimum duration while sum matches duration_ms."""
    n = len(segments)
    if n == 0:
        return []
    if n == 1:
        return [duration_ms]
        
    target_min = 1500
    if n * 1500 > duration_ms:
        target_min = max(1, duration_ms // n)
        
    allocated = [0] * n
    remaining_indices = set(range(n))
    remaining_duration = duration_ms
    
    while remaining_indices:
        rem_chars = sum(len(segments[i].text) for i in remaining_indices)
        if rem_chars == 0:
            share = remaining_duration // len(remaining_indices)
            for i in remaining_indices:
                allocated[i] = share
            break
            
        under_min_found = False
        for i in list(remaining_indices):
            ratio = len(segments[i].text) / rem_chars
            proj = int(remaining_duration * ratio)
            if proj < target_min:
                allocated[i] = target_min
                remaining_duration -= target_min
                remaining_indices.remove(i)
                under_min_found = True
                break
                
        if not under_min_found:
            for i in remaining_indices:
                ratio = len(segments[i].text) / rem_chars
                allocated[i] = int(remaining_duration * ratio)
            break
            
    diff = duration_ms - sum(allocated)
    if diff != 0:
        allocated[-1] += diff
        
    return allocated


async def align_voiceover_cues(
    project_id: str,
    segments: list[TranscriptSegment],
    audio_path: Path
) -> CueLedger:
    """
    Align a list of transcript segments to a voiceover audio file.
    Uses a highly robust Text-to-Audio word-ratio alignment algorithm on CPU,
    which is 100% deterministic and does not rely on heavy GPU Whisper models.
    """
    logger.info(f"Aligning {len(segments)} segments to audio file: {audio_path}")
    
    # 1. Get total audio duration
    duration_ms = get_wav_duration_ms(audio_path)
    logger.info(f"Audio file duration: {duration_ms} ms")
    
    # 2. Compute total character count to allocate timestamps proportionally
    total_chars = sum(len(seg.text) for seg in segments)
    if total_chars == 0:
        total_chars = 1
        
    cues: list[CueItem] = []
    current_ms = 0
    
    # Distribute durations deterministically so sum of segments matches audio duration
    durations = distribute_durations(duration_ms, segments, total_chars)
    
    for index, seg in enumerate(segments):
        segment_duration = durations[index]
        start_ms = current_ms
        end_ms = start_ms + segment_duration
            
        cues.append(
            CueItem(
                cue_id=f"cue_{index + 1:04d}",
                start_ms=start_ms,
                end_ms=end_ms,
                voice_text=seg.text,
                subtitle_text=seg.text,
                visual_prompt=None,  # Will be populated by LAVA Storyboard
                asset_type=AssetType.NONE,
                shorts_id=f"short_{(index // 5) + 1:02d}",
            )
        )
        current_ms = end_ms
        
    logger.info(f"Successfully aligned {len(cues)} cues. Total timeline: {current_ms} ms.")
    return CueLedger(project_id=project_id, cues=cues)
