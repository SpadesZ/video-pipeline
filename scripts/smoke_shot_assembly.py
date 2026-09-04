# 檔案路徑: video-pipeline/scripts/smoke_shot_assembly.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   剪輯決策、時間線與影片組裝確定性冒煙測試。
# 主要責任:
#   1. 驗證時間線長度取決於剪輯決策，而非素材檔案長度總和。
#   2. 驗證人工調整的取用區間不被自動重建覆寫。
#   3. 驗證 CueLedger 由時間線衍生。
#   4. 驗證 ProductionProfile 的 render_mode 分流，且既有投影片路徑未被破壞。
#   5. 驗證實際組裝產出可播放的 MP4，且成片長度符合時間線。
# 說明:
#   以 ffmpeg 產生測試影片；ffmpeg 不可用時組裝會輸出清單檔，測試仍需通過。
# --------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="assembly_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session

from pipeline.adapters.video.media_probe import ffprobe_available, probe_media
from pipeline.adapters.video.shot_assembler import (
    assemble_timeline,
    ffmpeg_available,
    target_resolution,
)
from pipeline.db import engine, init_db
from pipeline.models.capability import Capability
from pipeline.models.narrative import Beat, BeatIntent, NarrativeIR, Scene
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import load_preset
from pipeline.models.shot import CameraSpec, ShotPlan
from pipeline.models.timeline import build_timeline
from pipeline.models.variant import AssetVariant, VariantStatus
from pipeline.settings import get_settings
from pipeline.stages.timeline_builder import (
    build_edit_decisions,
    render_by_profile,
    rebuild_timeline,
    update_edit_decision,
)

PROJECT_ID = "smoke_assembly"
SHOT_A = "shot_0001"
SHOT_B = "shot_0002"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_video(path: Path, seconds: float, width: int = 540, height: int = 960) -> bool:
    if not shutil.which("ffmpeg"):
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(256))
        return False
    completed = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", f"color=c=blue:s={width}x{height}:d={seconds}:r=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def build_artifact() -> ProductionArtifact:
    beats = [
        Beat(
            beat_id="beat_0001", scene_id="scene_0001", order=0,
            intent=BeatIntent.SETUP, narration="她在雨中等待。",
        ),
        Beat(
            beat_id="beat_0002", scene_id="scene_0001", order=1,
            intent=BeatIntent.TURN, narration="信封遞了過來。",
        ),
    ]
    narrative = NarrativeIR(
        project_id=PROJECT_ID,
        logline="雨夜交換",
        scenes=[Scene(scene_id="scene_0001", order=0, beats=beats)],
    )
    shots = [
        ShotPlan(
            shot_id=SHOT_A, beat_id="beat_0001", scene_id="scene_0001", order=0,
            capability=Capability.VIDEO_I2V, camera=CameraSpec(),
            prompt="雨夜天橋遠景", target_duration_ms=5000, aspect_ratio="9:16",
        ),
        ShotPlan(
            shot_id=SHOT_B, beat_id="beat_0002", scene_id="scene_0001", order=1,
            capability=Capability.VIDEO_I2V, camera=CameraSpec(),
            prompt="特寫遞出信封", target_duration_ms=5000, aspect_ratio="9:16",
        ),
    ]
    return ProductionArtifact(
        project_id=PROJECT_ID,
        title="assembly smoke",
        production_profile=load_preset("comic_drama_high"),
        narrative_ir=narrative,
        shot_plans=shots,
    )


def seed_variants(clips_dir: Path) -> dict[str, int]:
    """建立兩個已選定的候選，實際長度刻意不同於鏡頭目標長度。"""
    clips_dir.mkdir(parents=True, exist_ok=True)
    durations = {SHOT_A: 8.0, SHOT_B: 6.0}
    actual: dict[str, int] = {}

    with Session(engine) as session:
        for index, (shot_id, seconds) in enumerate(durations.items(), start=1):
            path = clips_dir / f"{shot_id}.mp4"
            real = make_video(path, seconds)
            measured = probe_media(path).duration_ms if real else int(seconds * 1000)
            actual[shot_id] = measured or int(seconds * 1000)
            session.add(
                AssetVariant(
                    variant_id=f"var_{shot_id}",
                    project_id=PROJECT_ID,
                    shot_id=shot_id,
                    provider="kling",
                    model_id="kling-video",
                    prompt_snapshot="p",
                    requested_duration_ms=5000,
                    actual_duration_ms=actual[shot_id],
                    local_path=str(path),
                    file_hash=f"hash_{index}",
                    status=VariantStatus.SELECTED.value,
                )
            )
        session.commit()
    return actual


def verify_edit_decisions(artifact: ProductionArtifact, actual: dict[str, int]) -> None:
    decisions = build_edit_decisions(artifact)
    check(len(decisions) == 2, f"應為兩顆鏡頭建立決策，實際 {len(decisions)}")

    by_shot = {item.shot_id: item for item in decisions}
    check(by_shot[SHOT_A].in_point_ms == 0, "預設取用應自 0 開始")
    check(
        by_shot[SHOT_A].out_point_ms == actual[SHOT_A],
        f"預設取用長度應為素材實際長度: {by_shot[SHOT_A].out_point_ms}",
    )
    check(
        by_shot[SHOT_A].variant_id == f"var_{SHOT_A}",
        "決策應指向已選定的候選",
    )

    # 未選定候選的鏡頭不應產生決策
    orphan = artifact.model_copy(
        update={
            "shot_plans": artifact.shot_plans
            + [
                ShotPlan(
                    shot_id="shot_9999", beat_id="b", scene_id="s", order=9,
                    capability=Capability.VIDEO_I2V, prompt="未選定",
                    target_duration_ms=5000,
                )
            ]
        }
    )
    check(
        len(build_edit_decisions(orphan)) == 2,
        "沒有選定候選的鏡頭不應出現在剪輯決策中",
    )


def verify_timeline_is_not_file_length(
    artifact: ProductionArtifact, actual: dict[str, int]
) -> None:
    timeline = rebuild_timeline(artifact)
    source_total = sum(actual.values())
    check(
        timeline.total_duration_ms == source_total,
        "未調整時，時間線等於取用區間總和",
    )

    # 調整取用區間後，時間線必須改變而素材長度不變
    update_edit_decision(artifact, SHOT_A, in_point_ms=1000, out_point_ms=4000)
    trimmed = build_timeline(artifact.project_id, artifact.edit_decisions)
    expected = 3000 + actual[SHOT_B]
    check(
        trimmed.total_duration_ms == expected,
        f"時間線應反映取用區間 {expected}ms，實際 {trimmed.total_duration_ms}ms",
    )
    check(
        trimmed.total_duration_ms != source_total,
        "時間線長度不應等於素材檔案長度總和",
    )

    # 素材本身未被改動
    with Session(engine) as session:
        variant = session.get(AssetVariant, f"var_{SHOT_A}")
    check(
        variant.actual_duration_ms == actual[SHOT_A],
        "剪輯不應改變素材的實際長度紀錄",
    )

    # 停格會延長成片但不改變素材
    update_edit_decision(artifact, SHOT_B, hold_ms=500)
    held = build_timeline(artifact.project_id, artifact.edit_decisions)
    check(
        held.total_duration_ms == expected + 500,
        f"停格應計入成片長度: {held.total_duration_ms}",
    )

    # 非法區間必須被拒絕
    try:
        update_edit_decision(artifact, SHOT_A, in_point_ms=4000, out_point_ms=1000)
    except ValueError:
        pass
    else:
        raise AssertionError("out_point 小於 in_point 應被拒絕")

    # 人工調整不應被自動重建覆寫
    rebuild_timeline(artifact, preserve_existing=True)
    preserved = {item.shot_id: item for item in artifact.edit_decisions}
    check(
        preserved[SHOT_A].in_point_ms == 1000,
        "重建時應保留人工調整的取用區間",
    )
    check(preserved[SHOT_B].hold_ms == 500, "重建時應保留停格設定")

    # 明確要求重設時才可覆寫
    rebuild_timeline(artifact, preserve_existing=False)
    reset = {item.shot_id: item for item in artifact.edit_decisions}
    check(reset[SHOT_A].in_point_ms == 0, "明確重設時應回到預設取用區間")


def verify_cue_ledger_is_derived(artifact: ProductionArtifact) -> None:
    timeline = rebuild_timeline(artifact, preserve_existing=False)
    ledger = artifact.cue_ledger
    check(ledger is not None, "應產生 CueLedger")
    check(
        len(ledger.cues) == len(timeline.clips),
        f"cue 數量應等於時間線片段數: {len(ledger.cues)} vs {len(timeline.clips)}",
    )

    first, second = ledger.cues[0], ledger.cues[1]
    check(first.start_ms == 0, "首個 cue 應自 0 開始")
    check(
        second.start_ms == first.end_ms,
        "cue 應連續銜接，不得重疊或留空",
    )
    check(
        first.end_ms == timeline.clips[0].timeline_end_ms,
        "cue 時間應取自時間線而非固定秒數",
    )
    # 旁白取自敘事層，字幕才會與畫面對齊
    check(first.voice_text == "她在雨中等待。", f"cue 文字應取自節拍: {first.voice_text}")
    check(second.voice_text == "信封遞了過來。", "第二個 cue 文字錯誤")


def verify_assembly(artifact: ProductionArtifact) -> str:
    settings = get_settings()
    timeline = rebuild_timeline(artifact, preserve_existing=False)
    output = render_by_profile(settings, artifact, timeline)
    check(output is not None, "組裝應回傳產物路徑")

    result_path = Path(output)
    check(result_path.exists(), f"產物不存在: {result_path}")

    if ffmpeg_available():
        check(result_path.suffix == ".mp4", f"應輸出 MP4，實際 {result_path.name}")
        check(result_path.stat().st_size > 1000, "輸出檔案過小，可能未成功編碼")

        if ffprobe_available():
            info = probe_media(result_path)
            check(info.probed, f"成片無法探測: {info.error}")
            check(info.duration_ms is not None, "成片缺少片長資訊")
            expected = timeline.total_duration_ms
            tolerance = max(500, int(expected * 0.15))
            check(
                abs(info.duration_ms - expected) <= tolerance,
                f"成片長度 {info.duration_ms}ms 與時間線 {expected}ms 不符",
            )
            width, height = target_resolution("9:16")
            check(
                (info.width, info.height) == (width, height),
                f"成片解析度應統一為 {width}x{height}，實際 {info.resolution}",
            )
        return "mp4"

    check(
        result_path.name.endswith(".assembly.txt"),
        f"ffmpeg 不可用時應輸出組裝清單，實際 {result_path.name}",
    )
    return "manifest"


def verify_slideshow_track_intact(artifact: ProductionArtifact) -> None:
    """既有投影片路徑必須仍可運作，不能被鏡頭組裝取代。"""
    settings = get_settings()
    legacy = artifact.model_copy(
        update={"production_profile": load_preset("slideshow_legacy")}
    )
    check(
        not legacy.production_profile.uses_shot_assembly,
        "既有 preset 不應走鏡頭組裝",
    )
    output = render_by_profile(settings, legacy)

    if not ffmpeg_available():
        # 既有 renderer 在缺少 ffmpeg 時寫出 .ffmpeg.txt 並回傳 None，
        # 這是它原本的契約，不應在此改寫。
        check(output is None, "缺少 ffmpeg 時投影片路徑應回傳 None")
        return

    check(output is not None, "投影片路徑應仍能產出結果")
    check(Path(output).exists(), f"投影片產物不存在: {output}")


def main() -> int:
    init_db()
    artifact = build_artifact()
    actual = seed_variants(_TMP_DIR / "clips")

    verify_edit_decisions(artifact, actual)
    verify_timeline_is_not_file_length(artifact, actual)
    verify_cue_ledger_is_derived(artifact)
    mode = verify_assembly(artifact)
    verify_slideshow_track_intact(artifact)

    timeline = build_timeline(artifact.project_id, artifact.edit_decisions)
    print(
        f"OK shot assembly smoke clips={len(timeline.clips)} "
        f"duration={timeline.total_duration_seconds:g}s output={mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
