# 檔案路徑: video-pipeline/pipeline/adapters/video/media_probe.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   影片檔規格探測。
# 主要責任:
#   1. 以 ffprobe 取得實際片長、解析度與影格率。
#   2. ffprobe 不可用或檔案無法解析時優雅降級，不中斷匯入流程。
# 說明:
#   匯入人工生成的影片時，必須以檔案本身的實際規格為準，
#   不能沿用 ShotPlan 的目標值。平台常無法精準命中要求的片長，
#   而這個落差正是評估模型的指標之一。
# --------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger("media_probe")

PROBE_TIMEOUT_SECONDS = 30


class MediaInfo(BaseModel):
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    probed: bool = False
    error: str | None = None

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None

    @property
    def aspect_ratio(self) -> str | None:
        """以最大公因數化簡為常見比例字串。"""
        if not self.width or not self.height:
            return None
        from math import gcd

        divisor = gcd(self.width, self.height)
        return f"{self.width // divisor}:{self.height // divisor}"


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    if "/" in rate:
        numerator, _, denominator = rate.partition("/")
        try:
            denom = float(denominator)
            if denom == 0:
                return None
            return round(float(numerator) / denom, 3)
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None


def probe_media(path: Path | str) -> MediaInfo:
    """讀取影片實際規格。任何失敗都回傳 probed=False 而非拋出例外。"""
    media_path = Path(path)
    if not media_path.exists():
        return MediaInfo(error=f"檔案不存在: {media_path}")
    if not ffprobe_available():
        return MediaInfo(error="ffprobe 不可用，略過規格探測")

    command = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return MediaInfo(error=f"ffprobe 執行失敗: {error}"[:200])

    if completed.returncode != 0:
        return MediaInfo(error=f"ffprobe 回傳 {completed.returncode}: {completed.stderr.strip()[:150]}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        return MediaInfo(error=f"ffprobe 輸出無法解析: {error}"[:200])

    video_stream = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )

    duration_ms: int | None = None
    duration_raw = payload.get("format", {}).get("duration")
    if duration_raw is None and video_stream is not None:
        duration_raw = video_stream.get("duration")
    if duration_raw is not None:
        try:
            duration_ms = int(round(float(duration_raw) * 1000))
        except (TypeError, ValueError):
            duration_ms = None

    if video_stream is None:
        return MediaInfo(
            duration_ms=duration_ms,
            probed=True,
            error="檔案不含視訊軌",
        )

    return MediaInfo(
        duration_ms=duration_ms,
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        fps=_parse_fps(video_stream.get("avg_frame_rate")),
        codec=video_stream.get("codec_name"),
        probed=True,
    )
