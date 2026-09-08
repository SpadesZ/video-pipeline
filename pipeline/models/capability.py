# 檔案路徑: video-pipeline/pipeline/models/capability.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   AI 製片能力（Capability）列舉定義。
# 主要責任:
#   1. 定義 Capability Router 可調度的所有能力種類。
#   2. 提供能力分組，供 ProductionProfile 與 ModelRegistry 判斷。
# 說明:
#   本列舉刻意獨立於 llm_control.py。LLMCapability 描述的是純文字/視覺
#   推理的 LLM 世界；Capability 描述的是整個影音生產能力空間，兩者
#   語義不同，不應混用同一個列舉。llm_control.LLMCapability 僅作為
#   既有 LLM 任務註冊表的相容層保留。
# --------------------------------------------------------------------------

from enum import StrEnum


class Capability(StrEnum):
    """Capability Router 可調度的能力。值為穩定字串，供設定檔與 job manifest 使用。"""

    TEXT_REASONING = "text_reasoning"
    VISION_ANALYSIS = "vision_analysis"
    IMAGE_GENERATION = "image_generation"
    VIDEO_T2V = "video_t2v"
    VIDEO_I2V = "video_i2v"
    VIDEO_REFERENCE = "video_reference"
    TTS = "tts"
    LIP_SYNC = "lip_sync"
    MUSIC = "music"
    SFX = "sfx"
    VIDEO_QC = "video_qc"


VIDEO_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.VIDEO_T2V,
        Capability.VIDEO_I2V,
        Capability.VIDEO_REFERENCE,
    }
)

AUDIO_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.TTS,
        Capability.MUSIC,
        Capability.SFX,
        Capability.LIP_SYNC,
    }
)

REASONING_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.TEXT_REASONING,
        Capability.VISION_ANALYSIS,
    }
)


def is_video(capability: Capability) -> bool:
    return capability in VIDEO_CAPABILITIES


def is_audio(capability: Capability) -> bool:
    return capability in AUDIO_CAPABILITIES


def produces_clip(capability: Capability) -> bool:
    """該能力是否產出可進入時間線的影片片段。"""
    return capability in VIDEO_CAPABILITIES or capability is Capability.LIP_SYNC
