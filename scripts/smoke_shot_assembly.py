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
from pipeline.adapters.video.shot_assembler import build_clip_filter
from pipeline.models.timeline import RetimeMode, build_timeline
from pipeline.models.variant import AssetVariant, VariantStatus
from pipeline.settings import get_settings
from pipeline.stages.timeline_builder import (
    AssemblyBlocked,
    build_edit_decisions,
    preflight_assembly,
    rebuild_timeline,
    render_by_profile,
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


def verify_assembly_preflight(artifact: ProductionArtifact) -> None:
    """缺少已選定候選時必須擋下組裝，不得靜默少拍幾顆鏡頭。"""
    settings = get_settings()

    incomplete = artifact.model_copy(
        update={
            "shot_plans": [
                *artifact.shot_plans,
                ShotPlan(
                    shot_id="shot_0003", beat_id="beat_0003", scene_id="scene_0001",
                    order=2, capability=Capability.VIDEO_I2V, camera=CameraSpec(),
                    prompt="尚未生成的鏡頭", target_duration_ms=5000,
                    aspect_ratio="9:16",
                ),
            ]
        }
    )

    report = preflight_assembly(incomplete)
    check(not report.ok, "缺少候選時 preflight 不應通過")
    check(
        "shot_0003" in report.missing_shots,
        f"應列出缺少候選的鏡頭: {report.missing_shots}",
    )
    check(report.planned_shots == 3, "應以分鏡數為基準")
    check(report.ready_shots == 2, f"就緒鏡頭數錯誤: {report.ready_shots}")
    check(
        any("shot_0003" in reason for reason in report.reasons),
        f"原因應指名缺少的鏡頭: {report.reasons}",
    )

    rebuild_timeline(incomplete, preserve_existing=False)
    try:
        render_by_profile(settings, incomplete)
    except AssemblyBlocked as error:
        check("shot_0003" in error.missing_shots, "例外應帶出缺少的鏡頭")
    else:
        raise AssertionError("缺少候選時組裝必須被擋下，不得輸出成片")

    # 完整的專案則應通過
    complete = preflight_assembly(artifact)
    check(complete.ok, f"完整專案不應被擋: {complete.reasons}")
    check(complete.ready_shots == complete.planned_shots, "全部鏡頭應就緒")


def verify_retime_is_rendered(artifact: ProductionArtifact) -> None:
    """hold 與變速必須實際 render，不能只存在於時間線。"""
    settings = get_settings()
    rebuild_timeline(artifact, preserve_existing=False)

    baseline = build_timeline(artifact.project_id, artifact.edit_decisions)
    baseline_ms = baseline.total_duration_ms

    update_edit_decision(artifact, SHOT_A, hold_ms=2000)
    held = build_timeline(artifact.project_id, artifact.edit_decisions)
    check(
        held.total_duration_ms == baseline_ms + 2000,
        f"時間線應反映停格: {held.total_duration_ms}",
    )

    clip = next(item for item in held.clips if item.shot_id == SHOT_A)
    check(clip.hold_ms == 2000, "TimelineClip 應帶出停格資訊供組裝使用")

    filter_chain = build_clip_filter(clip, 1080, 1920)
    check("tpad" in filter_chain, f"停格應轉為 tpad 濾鏡: {filter_chain}")

    speed_clip = clip.model_copy(
        update={"retime_mode": RetimeMode.SPEED, "retime_factor": 2.0, "hold_ms": 0}
    )
    check(speed_clip.needs_retime, "變速應被識別")
    check(
        "setpts=PTS/2" in build_clip_filter(speed_clip, 1080, 1920),
        "變速應轉為 setpts 濾鏡",
    )

    if not ffmpeg_available() or not ffprobe_available():
        return

    output = render_by_profile(settings, artifact, held)
    info = probe_media(Path(output))
    check(info.probed, f"成片無法探測: {info.error}")
    tolerance = max(500, int(held.total_duration_ms * 0.15))
    check(
        abs(info.duration_ms - held.total_duration_ms) <= tolerance,
        f"停格必須實際 render：成片 {info.duration_ms}ms 與時間線 "
        f"{held.total_duration_ms}ms 不符",
    )
    check(
        info.duration_ms > baseline_ms,
        "加入停格後成片應變長，否則濾鏡未生效",
    )


def verify_stale_selection_blocks_render(artifact: ProductionArtifact) -> None:
    """改選候選後，既有時間線不得繼續 render。"""
    settings = get_settings()

    # 為 SHOT_A 再匯入一個候選，模擬人工改選
    alternative = _TMP_DIR / "clips" / "shot_0001_alt.mp4"
    make_video(alternative, 7.0)
    with Session(engine) as session:
        session.add(
            AssetVariant(
                variant_id="var_shot_0001_alt",
                project_id=PROJECT_ID,
                shot_id=SHOT_A,
                provider="runway",
                prompt_snapshot="alt",
                actual_duration_ms=7000,
                local_path=str(alternative),
                file_hash="hash_alt",
                status=VariantStatus.IMPORTED.value,
            )
        )
        session.commit()

    timeline = rebuild_timeline(artifact, preserve_existing=False)
    original_variant = next(
        item.variant_id for item in artifact.edit_decisions if item.shot_id == SHOT_A
    )
    check(original_variant == f"var_{SHOT_A}", "初始應選定原候選")

    # 改選為另一個候選，但不重建時間線
    with Session(engine) as session:
        old = session.get(AssetVariant, f"var_{SHOT_A}")
        new = session.get(AssetVariant, "var_shot_0001_alt")
        old.status = VariantStatus.IMPORTED.value
        new.status = VariantStatus.SELECTED.value
        session.add(old)
        session.add(new)
        session.commit()

    report = preflight_assembly(artifact, timeline)
    check(not report.ok, "改選後 preflight 不應通過")
    check(
        SHOT_A in report.stale_shots,
        f"應標記過期的鏡頭: {report.stale_shots}",
    )
    check(
        any("var_shot_0001_alt" in reason for reason in report.reasons),
        f"原因應指出目前選定的候選: {report.reasons}",
    )

    try:
        render_by_profile(settings, artifact, timeline)
    except AssemblyBlocked as error:
        check(SHOT_A in error.missing_shots, "例外應帶出過期的鏡頭")
    else:
        raise AssertionError("改選後以舊時間線 render 必須被擋下")

    # 重建後即可通過，且指向新候選
    rebuilt = rebuild_timeline(artifact, preserve_existing=False)
    updated = next(
        item.variant_id for item in artifact.edit_decisions if item.shot_id == SHOT_A
    )
    check(updated == "var_shot_0001_alt", "重建後應指向新選定的候選")
    check(preflight_assembly(artifact, rebuilt).ok, "重建後 preflight 應通過")

    # 傳入與剪輯決策不符的時間線同樣要擋
    check(
        not preflight_assembly(artifact, timeline).ok,
        "舊時間線即使重建後仍不得使用",
    )

    # 還原為原候選，避免影響後續驗證
    with Session(engine) as session:
        old = session.get(AssetVariant, f"var_{SHOT_A}")
        new = session.get(AssetVariant, "var_shot_0001_alt")
        old.status = VariantStatus.SELECTED.value
        new.status = VariantStatus.REJECTED.value
        session.add(old)
        session.add(new)
        session.commit()
    rebuild_timeline(artifact, preserve_existing=False)


def verify_audio_does_not_truncate_video(artifact: ProductionArtifact) -> None:
    """音訊短於時間線時，成片長度必須仍由時間線決定。"""
    if not ffmpeg_available() or not ffprobe_available():
        return

    settings = get_settings()
    timeline = rebuild_timeline(artifact, preserve_existing=False)
    expected_ms = timeline.total_duration_ms
    check(expected_ms > 6000, "測試需要足夠長的時間線")

    project_dir = Path(settings.data_dir) / "projects" / PROJECT_ID
    project_dir.mkdir(parents=True, exist_ok=True)

    # 音訊刻意比時間線短。使用 -shortest 時成片會被截到音訊長度。
    short_audio = project_dir / "voiceover.wav"
    short_seconds = (expected_ms / 1000) - 6
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={short_seconds:.2f}",
            str(short_audio),
        ],
        capture_output=True,
        check=False,
    )
    check(short_audio.exists(), "測試音訊未產生")

    artifact.voiceover_path = str(short_audio)
    output = render_by_profile(settings, artifact, timeline)
    check(output is not None, "應產出成片")
    check(
        Path(output).suffix == ".mp4",
        f"音訊較短不應導致組裝失敗: {Path(output).name}",
    )

    info = probe_media(Path(output))
    check(info.probed, f"成片無法探測: {info.error}")
    tolerance = max(500, int(expected_ms * 0.05))
    check(
        abs(info.duration_ms - expected_ms) <= tolerance,
        f"成片 {info.duration_ms}ms 應等於時間線 {expected_ms}ms，"
        f"而非被音訊截斷至 {short_seconds:.0f}s",
    )
    check(
        info.duration_ms > short_seconds * 1000 + 1000,
        "成片明顯被音訊截斷",
    )

    # 音訊長於時間線時，成片同樣由時間線決定
    long_audio = project_dir / "voiceover_long.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", f"anullsrc=r=44100:cl=mono:d={(expected_ms / 1000) + 10:.2f}",
            str(long_audio),
        ],
        capture_output=True,
        check=False,
    )
    artifact.voiceover_path = str(long_audio)
    output_long = render_by_profile(settings, artifact, timeline)
    info_long = probe_media(Path(output_long))
    check(
        abs(info_long.duration_ms - expected_ms) <= tolerance,
        f"音訊較長時成片仍應為 {expected_ms}ms，實際 {info_long.duration_ms}ms",
    )

    artifact.voiceover_path = None


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
    verify_assembly_preflight(artifact)
    verify_stale_selection_blocks_render(artifact)
    verify_audio_does_not_truncate_video(artifact)
    verify_retime_is_rendered(artifact)
    verify_slideshow_track_intact(artifact)

    timeline = build_timeline(artifact.project_id, artifact.edit_decisions)
    print(
        f"OK shot assembly smoke clips={len(timeline.clips)} "
        f"duration={timeline.total_duration_seconds:g}s output={mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
