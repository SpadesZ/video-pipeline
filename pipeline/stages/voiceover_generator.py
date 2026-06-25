# 檔案路徑: video-pipeline/pipeline/stages/voiceover_generator.py
# 產生時間: 2026-06-25 17:00 +08:00
# 版本: v1.1
# 模組定位:
#   配音生成器模組 (TTS Voiceover Generator)。
# 主要責任:
#   1. 對接 ElevenLabs API 生成高品質人聲配音 (MP3/WAV)。
#   2. 當無 API Key 或 API 呼叫失敗時，自動降級生成無聲 WAV 音檔，防止流水線因外部 API 異常而卡住。
#   3. 使用 FFmpeg 完成 MP3 到 WAV 的格式轉換以相容後續 ASR 流程。
# --------------------------------------------------------------------------

import os
import wave
import httpx
import logging
import shutil
import subprocess
from pathlib import Path
from pipeline.secrets import load_runtime_secrets

logger = logging.getLogger("Voiceover_Generator")
logger.setLevel(logging.INFO)


def generate_silent_wav(output_path: Path, duration_sec: float = 2.0) -> None:
    """Generate a silent mono WAV file as a fallback to avoid pipeline blockers."""
    sample_rate = 16000
    num_samples = int(sample_rate * duration_sec)
    
    with wave.open(str(output_path), 'wb') as wav_file:
        # Mono, 2 bytes per sample, 16000 Hz
        wav_file.setparams((1, 2, sample_rate, num_samples, 'NONE', 'not compressed'))
        # Write silence (zeros)
        wav_file.writeframes(b'\x00' * (num_samples * 2))
    logger.info(f"Fallback: Generated silent WAV at {output_path} ({duration_sec}s)")


async def generate_voiceover(text: str, output_path: Path, voice_id: str = "21m00Tcm4TlvDq8ikWAM") -> Path:
    """
    Generate voiceover MP3/WAV using ElevenLabs API.
    Falls back to generating a silent WAV file if ELEVENLABS_API_KEY is not configured or fails.
    """
    load_runtime_secrets()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY not found in environment. Generating fallback silence.")
        # Estimate duration: approx 130 words per minute (approx 2 words per second)
        word_count = len(text.split())
        estimated_duration = max(2.0, word_count / 2.2)
        generate_silent_wav(output_path, duration_sec=estimated_duration)
        return output_path

    logger.info(f"Generating voiceover via ElevenLabs for text ({len(text)} chars)...")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "accept": "audio/mpeg"
    }
    payload = {
        "text": text,
        "model_id": "eleven_monolingual_v1",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code != 200:
                logger.error(f"ElevenLabs API returned error {response.status_code}: {response.text}")
                raise RuntimeError(f"ElevenLabs API error: {response.text}")
            
            # If output path expects WAV but response is MP3, convert using FFmpeg
            if output_path.suffix.lower() == ".wav":
                temp_mp3 = output_path.with_suffix(".mp3")
                temp_mp3.write_bytes(response.content)
                
                ffmpeg = shutil.which("ffmpeg")
                if ffmpeg:
                    try:
                        cmd = [ffmpeg, "-y", "-i", str(temp_mp3), str(output_path)]
                        subprocess.run(cmd, check=True, capture_output=True)
                        logger.info(f"Successfully converted ElevenLabs MP3 to WAV at {output_path}")
                    except Exception as convert_err:
                        logger.error(f"Failed to convert ElevenLabs MP3 to WAV via FFmpeg: {convert_err}. Keeping MP3 content.")
                        output_path.write_bytes(response.content)
                    finally:
                        if temp_mp3.exists():
                            temp_mp3.unlink()
                else:
                    logger.warning("ffmpeg not found. Cannot convert ElevenLabs MP3 to WAV. Saving MP3 data directly.")
                    output_path.write_bytes(response.content)
            else:
                output_path.write_bytes(response.content)
            logger.info(f"Voiceover successfully generated at {output_path}")
            return output_path
    except Exception as e:
        logger.error(f"ElevenLabs invocation failed: {e}. Generating fallback silence.")
        word_count = len(text.split())
        estimated_duration = max(2.0, word_count / 2.2)
        generate_silent_wav(output_path, duration_sec=estimated_duration)
        return output_path
