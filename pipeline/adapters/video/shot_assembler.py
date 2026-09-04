# 檔案路徑: video-pipeline/pipeline/adapters/video/shot_assembler.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   以真實影片片段組裝成片。
# 主要責任:
#   1. 依 TimelineClip 的取用區間裁切各候選影片並統一編碼規格。
#   2. 以 ffmpeg concat 合併，加上淡入淡出。
#   3. ffmpeg 不可用時輸出組裝清單而非中斷。
# 說明:
#   與既有 ffmpeg_renderer 的差別在於來源：後者把單張圖片鋪成投影片，
#   本模組串接實際生成的影片片段。兩者並存，由 ProductionProfile 的
#   render_mode 決定走哪一條。
#   各平台產出的解析度與影格率不一致，直接 concat 會失敗或畫面跳動，
#   因此裁切階段一律重新編碼為統一規格。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from pipeline.models.timeline import Timeline, TimelineClip

logger = logging.getLogger("shot_assembler")

FFMPEG_TIMEOUT_SECONDS = 600
DEFAULT_FPS = 24
FADE_IN_SECONDS = 0.5
FADE_OUT_SECONDS = 1.0

# 依畫面比例決定輸出尺寸。來源片段規格不一，統一後才能安全串接。
RESOLUTION_BY_RATIO = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}
FALLBACK_RESOLUTION = (1080, 1920)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def target_resolution(aspect_ratio: str | None) -> tuple[int, int]:
    return RESOLUTION_BY_RATIO.get(aspect_ratio or "", FALLBACK_RESOLUTION)


def _run(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)[:300]
    if completed.returncode != 0:
        return False, completed.stderr.strip()[:300]
    return True, ""


def _write_manifest(
    output_path: Path, timeline: Timeline, variant_paths: dict[str, str], reason: str
) -> Path:
    """無法實際編碼時，輸出可供人工檢視的組裝清單。"""
    manifest = output_path.with_suffix(".assembly.txt")
    lines = [
        f"# assembly manifest ({reason})",
        f"# project: {timeline.project_id}",
        f"# total: {timeline.total_duration_seconds:g}s across {len(timeline.clips)} clips",
        "",
    ]
    for clip in timeline.clips:
        source = variant_paths.get(clip.variant_id, "<missing>")
        lines.append(
            f"{clip.order:03d} {clip.shot_id} "
            f"[{clip.source_in_ms}-{clip.source_out_ms}ms] "
            f"-> {clip.timeline_start_ms}ms +{clip.used_duration_ms}ms  {source}"
        )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def build_clip_filter(clip: TimelineClip, width: int, height: int) -> str:
    """組出單一片段的濾鏡鏈。

    retime 與 hold 必須在此實際套用，否則時間線計算出的長度與成片會分歧。
    變速以 setpts 調整時間戳，停格以 tpad 複製最後一幀延長。
    """
    stages = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        f"fps={DEFAULT_FPS}",
    ]
    if clip.needs_retime:
        stages.append(f"setpts=PTS/{clip.retime_factor:g}")
    if clip.hold_ms > 0:
        stages.append(
            f"tpad=stop_mode=clone:stop_duration={clip.hold_ms / 1000:.3f}"
        )
    stages.append("format=yuv420p")
    return ",".join(stages)


def _trim_clip(
    clip: TimelineClip,
    source: Path,
    destination: Path,
    width: int,
    height: int,
) -> tuple[bool, str]:
    start_seconds = clip.source_in_ms / 1000
    duration_seconds = (clip.source_out_ms - clip.source_in_ms) / 1000
    # -ss 與 -t 都置於 -i 之前，屬於輸入端限制：只讀取指定區間。
    # 若 -t 放在輸出端，tpad 停格與 setpts 變速產生的額外長度會被截掉，
    # 成片就會與時間線對不上。
    return _run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start_seconds:.3f}",
            "-t", f"{duration_seconds:.3f}",
            "-i", str(source),
            "-vf", build_clip_filter(clip, width, height),
            "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            str(destination),
        ]
    )


def assemble_timeline(
    project_dir: Path,
    timeline: Timeline,
    variant_paths: dict[str, str],
    aspect_ratio: str | None = None,
    output_name: str = "preview.mp4",
    audio_path: Path | None = None,
) -> Path | None:
    """依時間線串接實際影片片段。

    回傳輸出檔路徑。無法編碼時回傳組裝清單路徑，兩者皆非 None，
    以便呼叫端記錄產物；完全失敗才回傳 None。
    """
    output_path = project_dir / output_name

    if not timeline.clips:
        logger.warning("Timeline has no clips; nothing to assemble")
        return None

    missing = [
        clip.variant_id
        for clip in timeline.clips
        if not variant_paths.get(clip.variant_id)
        or not Path(variant_paths[clip.variant_id]).exists()
    ]
    if missing:
        return _write_manifest(
            output_path, timeline, variant_paths, f"missing sources: {len(missing)}"
        )

    if not ffmpeg_available():
        return _write_manifest(output_path, timeline, variant_paths, "ffmpeg not found")

    width, height = target_resolution(aspect_ratio)
    work_dir = project_dir / "temp_clips"
    work_dir.mkdir(parents=True, exist_ok=True)

    trimmed: list[Path] = []
    try:
        for clip in timeline.clips:
            source = Path(variant_paths[clip.variant_id])
            destination = work_dir / f"clip_{clip.order:04d}.mp4"
            ok, error = _trim_clip(clip, source, destination, width, height)
            if not ok:
                logger.warning("Trim failed for %s: %s", clip.shot_id, error)
                return _write_manifest(
                    output_path, timeline, variant_paths, f"trim failed: {error[:80]}"
                )
            trimmed.append(destination)

        concat_file = work_dir / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in trimmed) + "\n",
            encoding="utf-8",
        )

        total_seconds = timeline.total_duration_seconds
        fade_out_start = max(0.0, total_seconds - FADE_OUT_SECONDS)
        video_filter = (
            f"fade=t=in:st=0:d={FADE_IN_SECONDS},"
            f"fade=t=out:st={fade_out_start:.3f}:d={FADE_OUT_SECONDS},"
            "format=yuv420p"
        )

        command = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
        ]
        if audio_path and Path(audio_path).exists():
            command += ["-i", str(audio_path)]
        command += ["-vf", video_filter, "-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        if audio_path and Path(audio_path).exists():
            command += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        command.append(str(output_path))

        ok, error = _run(command)
        if not ok:
            logger.warning("Concat failed: %s", error)
            return _write_manifest(
                output_path, timeline, variant_paths, f"concat failed: {error[:80]}"
            )
        return output_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
