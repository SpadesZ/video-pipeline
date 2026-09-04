# 檔案路徑: video-pipeline/scripts/smoke_variant_qc.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   候選匯入與品質評分確定性冒煙測試。
# 主要責任:
#   1. 驗證匯入建立 AssetVariant、以 file_hash 冪等、並推進 CapabilityJob。
#   2. 驗證實際規格以檔案為準，與 ShotPlan 目標值的落差被記錄。
#   3. 驗證 QC 評分可為 N/A、範圍受檢、加權依 ProductionProfile 而異。
#   4. 驗證選片為互斥操作並留下稽核軌跡。
#   5. 驗證專案彙整以 usable-shot rate 與人工修正時間為核心指標。
# 說明:
#   以 ffmpeg 產生測試影片；ffmpeg 不可用時改用位元組佔位檔，
#   規格探測會優雅降級，測試仍需通過。
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

_TMP_DIR = Path(tempfile.mkdtemp(prefix="variant_qc_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session, select

from pipeline.adapters.video.media_probe import ffprobe_available, probe_media
from pipeline.db import engine, init_db
from pipeline.models.capability import Capability
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import load_preset
from pipeline.models.qc import ContinuityQC, ContinuityScope, VariantQC
from pipeline.models.shot import CameraSpec, ShotPlan
from pipeline.models.variant import (
    AssetVariant,
    CapabilityJob,
    JobStatus,
    TransportKind,
    VariantStatus,
)
from pipeline.settings import get_settings
from pipeline.stages.shot_qc import (
    QCValidationError,
    ShotOutcome,
    record_continuity_qc,
    record_variant_qc,
    summarize_project_qc,
)
from pipeline.stages.variant_importer import (
    JobLinkError,
    import_variants,
    list_variants,
    select_variant,
)

PROJECT_ID = "smoke_qc"
SHOT_A = "shot_0001"
SHOT_B = "shot_0002"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_video(path: Path, seconds: float, width: int = 540, height: int = 960) -> bytes:
    """以 ffmpeg 產生測試影片。不可用時退回位元組佔位檔。"""
    if shutil.which("ffmpeg"):
        completed = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}:d={seconds}:r=24",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0 and path.exists():
            return path.read_bytes()
    return b"\x00\x00\x00\x18ftypmp42" + os.urandom(256)


def build_artifact() -> ProductionArtifact:
    profile = load_preset("comic_drama_high")
    shots = [
        ShotPlan(
            shot_id=SHOT_A,
            beat_id="beat_0001",
            scene_id="scene_0001",
            order=0,
            capability=Capability.VIDEO_I2V,
            camera=CameraSpec(),
            prompt="雨夜天橋遠景",
            target_duration_ms=5000,
            aspect_ratio="9:16",
        ),
        ShotPlan(
            shot_id=SHOT_B,
            beat_id="beat_0002",
            scene_id="scene_0001",
            order=1,
            capability=Capability.VIDEO_I2V,
            camera=CameraSpec(),
            prompt="特寫遞出信封",
            target_duration_ms=5000,
            aspect_ratio="9:16",
        ),
    ]
    return ProductionArtifact(
        project_id=PROJECT_ID,
        title="qc smoke",
        production_profile=profile,
        shot_plans=shots,
    )


def seed_job(
    shot_id: str,
    request_hash: str,
    project_id: str = PROJECT_ID,
    provider: str = "kling",
    requested_duration_ms: int | None = 6000,
) -> str:
    """建立派工記錄。

    requested_duration_ms 刻意設為 6000 而非 ShotPlan 的 5000，
    模擬 ProductionProfile 夾住後的實際送出值，以驗證落差比對用的是
    真正 dispatch 的規格而非原始意圖。
    """
    job_id = f"job_{provider}_{shot_id}_{request_hash[:6]}"
    with Session(engine) as session:
        session.add(
            CapabilityJob(
                job_id=job_id,
                project_id=project_id,
                shot_id=shot_id,
                capability=Capability.VIDEO_I2V.value,
                provider=provider,
                model_id="kling-video",
                transport=TransportKind.MANUAL.value,
                status=JobStatus.PENDING_MANUAL.value,
                request_hash=request_hash,
                requested_duration_ms=requested_duration_ms,
                requested_aspect_ratio="9:16",
            )
        )
        session.commit()
    return job_id


def verify_import(artifact: ProductionArtifact) -> None:
    settings = get_settings()
    job_id = seed_job(SHOT_A, "hash_shot_a")

    # good 命中請求的 6 秒；long 明顯超出，用於驗證落差警告
    good = make_video(_TMP_DIR / "good.mp4", 6.0)
    long_clip = make_video(_TMP_DIR / "long.mp4", 9.0)

    report = import_variants(
        settings=settings,
        artifact=artifact,
        shot_id=SHOT_A,
        provider="kling",
        files=[("good.mp4", good), ("long.mp4", long_clip)],
        request_hash="hash_shot_a",
        model_id="kling-video",
    )

    check(report.new_count == 2, f"應匯入 2 個候選，實際 {report.new_count}")
    check(report.job_id == job_id, "未以 request_hash 關聯既有工作")
    check(not report.skipped, f"不應有跳過的檔案: {report.skipped}")

    # 工作狀態應被推進為完成
    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        check(job.status == JobStatus.COMPLETED, f"工作狀態應更新，實際 {job.status}")
        check(job.completed_at is not None, "完成時間未記錄")
        check(job.is_terminal, "completed 應為終態")

    variants = list_variants(PROJECT_ID, SHOT_A)
    check(len(variants) == 2, f"資料表應有 2 個候選，實際 {len(variants)}")
    for variant in variants:
        check(variant.file_hash, "未記錄 file_hash")
        check(Path(variant.local_path).exists(), "候選檔案未落盤")
        check(variant.status == VariantStatus.IMPORTED, "匯入後狀態應為 imported")
        check(variant.job_id == job_id, "候選未關聯工作")
        # 基準必須是派工當下送出的 6000，而非 ShotPlan 的 5000
        check(
            variant.requested_duration_ms == 6000,
            f"基準應取自派工記錄而非 ShotPlan，實際 {variant.requested_duration_ms}",
        )
        check(variant.prompt_snapshot == "雨夜天橋遠景", "未保存提示詞快照")

    if ffprobe_available():
        # 實際規格必須以檔案為準，落差則以派工規格為基準
        long_variant = next(
            item for item in report.imported if item.local_path.endswith(".mp4")
            and item.actual_duration_ms and item.actual_duration_ms > 7000
        )
        check(
            long_variant.warnings,
            "片長明顯偏離請求時應產生警告",
        )
        check(
            any("請求 6000ms" in w for w in long_variant.warnings),
            f"落差應以派工規格 6000ms 為基準: {long_variant.warnings}",
        )

        # 命中請求長度者不應產生片長警告
        good_variant = next(
            item for item in report.imported
            if item.actual_duration_ms and 5500 <= item.actual_duration_ms <= 6500
        )
        check(
            not any("片長" in w for w in good_variant.warnings),
            f"容差內不應警告: {good_variant.warnings}",
        )

    # 相同內容重複匯入不應建立第二筆
    again = import_variants(
        settings=settings,
        artifact=artifact,
        shot_id=SHOT_A,
        provider="kling",
        files=[("good.mp4", good)],
        request_hash="hash_shot_a",
    )
    check(again.new_count == 0, "相同檔案不應重複建立候選")
    check(again.imported[0].duplicate, "應標示為重複")
    check(
        len(list_variants(PROJECT_ID, SHOT_A)) == 2,
        "重複匯入後候選數量不應增加",
    )

    # 非影片檔案應被跳過而非中斷整批匯入
    mixed = import_variants(
        settings=settings,
        artifact=artifact,
        shot_id=SHOT_A,
        provider="kling",
        files=[("notes.txt", b"hello"), ("empty.mp4", b"")],
    )
    check(len(mixed.skipped) == 2, f"應跳過 2 個檔案: {mixed.skipped}")
    check(not mixed.imported, "不應匯入無效檔案")

    actions = [entry.action for entry in artifact.decision_log]
    check("variants_imported" in actions, "未記錄匯入事件")


def verify_qc_scoring(artifact: ProductionArtifact) -> None:
    variants = list_variants(PROJECT_ID, SHOT_A)
    first, second = variants[0], variants[1]

    record_variant_qc(
        artifact,
        first.variant_id,
        scores={
            "prompt_adherence": 85,
            "temporal_stability": 90,
            "motion_quality": 70,
            "camera_control": None,  # 靜態鏡頭不評運鏡
            "artifact_severity": 10,
        },
        usable_without_repair=True,
        human_correction_minutes=4.5,
        retries_to_usable=1,
    )
    record_variant_qc(
        artifact,
        second.variant_id,
        scores={
            "prompt_adherence": 40,
            "temporal_stability": 35,
            "artifact_severity": 80,
        },
        usable_without_repair=False,
        human_correction_minutes=30.0,
        retries_to_usable=3,
    )

    profile = artifact.production_profile
    with Session(engine) as session:
        rows = {
            row.variant_id: row
            for row in session.exec(
                select(VariantQC).where(VariantQC.project_id == PROJECT_ID)
            ).all()
        }
    check(len(rows) == 2, f"應有 2 筆單鏡頭評分，實際 {len(rows)}")

    good_qc = rows[first.variant_id]
    check(good_qc.camera_control is None, "留空的維度應保持 N/A")
    good_score = good_qc.weighted_score(profile.qc_weights)
    bad_score = rows[second.variant_id].weighted_score(profile.qc_weights)
    check(good_score > bad_score, f"品質較佳者加權應較高: {good_score} vs {bad_score}")

    # 重複評分應覆寫而非新增
    record_variant_qc(
        artifact, first.variant_id, scores={"prompt_adherence": 95},
        usable_without_repair=True,
    )
    with Session(engine) as session:
        count = len(
            session.exec(
                select(VariantQC).where(VariantQC.variant_id == first.variant_id)
            ).all()
        )
    check(count == 1, f"同一候選應只有一筆評分，實際 {count}")

    # 超出範圍的分數必須被拒絕
    for bad in ({"prompt_adherence": 120}, {"temporal_stability": -5}):
        try:
            record_variant_qc(artifact, first.variant_id, scores=bad)
        except QCValidationError:
            pass
        else:
            raise AssertionError(f"超出範圍的分數應被拒絕: {bad}")


def verify_continuity_qc(artifact: ProductionArtifact) -> None:
    record_continuity_qc(
        artifact,
        shot_id=SHOT_B,
        ref_shot_id=SHOT_A,
        scope=ContinuityScope.PAIR.value,
        scores={
            "cross_shot_identity": 88,
            "wardrobe_continuity": 92,
            "location_continuity": 75,
            "lip_sync_quality": None,  # 無對白鏡頭
        },
    )

    with Session(engine) as session:
        rows = session.exec(
            select(ContinuityQC).where(ContinuityQC.project_id == PROJECT_ID)
        ).all()
    check(len(rows) == 1, f"應有 1 筆連戲評分，實際 {len(rows)}")
    record = rows[0]
    check(record.ref_shot_id == SHOT_A, "未記錄比較對象")
    check(record.lip_sync_quality is None, "無對白鏡頭的嘴型應為 N/A")

    comic = load_preset("comic_drama_high")
    film = load_preset("film_high")
    comic_score = record.weighted_score(comic.qc_weights)
    film_score = record.weighted_score(film.qc_weights)
    check(comic_score is not None, "有分數時不應回傳 None")
    check(
        comic_score != film_score,
        "不同製作方式的權重應產生不同加權結果",
    )
    # N/A 若被當成 0 分，加權會掉到 60 以下
    check(comic_score > 80, f"N/A 不應被當作 0 分: {comic_score}")

    # scope=pair 缺少比較對象必須被拒絕
    try:
        record_continuity_qc(
            artifact, shot_id=SHOT_B, scores={"cross_shot_identity": 50}
        )
    except QCValidationError:
        pass
    else:
        raise AssertionError("scope=pair 缺少 ref_shot_id 應被拒絕")


def verify_selection(artifact: ProductionArtifact) -> None:
    variants = list_variants(PROJECT_ID, SHOT_A)
    first, second = variants[0], variants[1]

    select_variant(artifact, first.variant_id, reason="角色一致性最佳")
    with Session(engine) as session:
        chosen = session.get(AssetVariant, first.variant_id)
        other = session.get(AssetVariant, second.variant_id)
    check(chosen.is_selected, "選定的候選狀態應為 selected")
    check(chosen.selected_reason == "角色一致性最佳", "未記錄選片理由")
    check(not other.is_selected, "其他候選不應同時為 selected")

    # 改選另一個時，原本選定者必須退回
    select_variant(artifact, second.variant_id, reason="改用重生成版本")
    with Session(engine) as session:
        chosen = session.get(AssetVariant, second.variant_id)
        previous = session.get(AssetVariant, first.variant_id)
    check(chosen.is_selected, "新選定者狀態錯誤")
    check(not previous.is_selected, "同一鏡頭不應有兩個 selected")
    check(previous.selected_reason is None, "退選後理由應清除")

    actions = [entry.action for entry in artifact.decision_log]
    check("variant_selected" in actions, "未記錄選片事件")


def verify_strict_hash_matching(artifact: ProductionArtifact) -> None:
    """提供 request_hash 時必須完全相符，不得退回猜測其他工作。"""
    settings = get_settings()
    video = make_video(_TMP_DIR / "strict.mp4", 5.0)

    # 屬於別的專案的工作
    seed_job(SHOT_A, "hash_other_project", project_id="other_project")
    # 屬於別的鏡頭的工作
    seed_job(SHOT_B, "hash_other_shot")
    # 屬於別的平台的工作
    seed_job(SHOT_A, "hash_other_provider", provider="runway")

    cases = [
        ("hash_does_not_exist", "找不到"),
        ("hash_other_project", "project"),
        ("hash_other_shot", "shot"),
        ("hash_other_provider", "provider"),
    ]
    for bad_hash, expected in cases:
        try:
            import_variants(
                settings=settings, artifact=artifact, shot_id=SHOT_A,
                provider="kling", files=[("x.mp4", video)], request_hash=bad_hash,
            )
        except JobLinkError as error:
            check(
                expected in str(error),
                f"錯誤訊息應說明不符原因 ({expected}): {error}",
            )
        else:
            raise AssertionError(f"不符的 request_hash 應被拒絕: {bad_hash}")

    # 完全未提供 hash 時允許匯入，但必須明確標示為未關聯
    unlinked = import_variants(
        settings=settings, artifact=artifact, shot_id=SHOT_A,
        provider="veo", files=[("unlinked.mp4", video)],
    )
    check(unlinked.new_count == 1, "未提供 hash 時應可匯入")
    check(not unlinked.linked, "未提供 hash 應標示為 unlinked")
    check("unlinked" in unlinked.summary(), "摘要應標示未關聯")
    imported = unlinked.imported[0]
    check(
        any("未關聯" in w for w in imported.warnings),
        f"未關聯時應警告無可信基準: {imported.warnings}",
    )

    with Session(engine) as session:
        variant = session.get(AssetVariant, imported.variant_id)
    check(
        variant.requested_duration_ms is None,
        "未關聯派工時不得回頭以 ShotPlan 目標值充當基準",
    )

    # 不屬於本專案的鏡頭必須被拒絕
    try:
        import_variants(
            settings=settings, artifact=artifact, shot_id="shot_not_here",
            provider="kling", files=[("x.mp4", video)],
        )
    except ValueError as error:
        check("不屬於專案" in str(error), f"應說明鏡頭不屬於本專案: {error}")
    else:
        raise AssertionError("外部鏡頭應被拒絕")


def verify_cross_project_guard(artifact: ProductionArtifact) -> None:
    """帶著別的專案的 variant_id 不得改動資料。"""
    from pipeline.stages.variant_importer import load_owned_variant

    with Session(engine) as session:
        session.add(
            AssetVariant(
                variant_id="var_foreign",
                project_id="other_project",
                shot_id="shot_x",
                provider="kling",
                prompt_snapshot="foreign",
            )
        )
        session.commit()

    with Session(engine) as session:
        try:
            load_owned_variant(session, PROJECT_ID, "var_foreign")
        except ValueError as error:
            check("屬於專案" in str(error), f"應說明歸屬不符: {error}")
        else:
            raise AssertionError("跨專案讀取應被拒絕")

    for operation in (
        lambda: select_variant(artifact, "var_foreign", reason="x"),
        lambda: record_variant_qc(
            artifact, "var_foreign", scores={"prompt_adherence": 50}
        ),
    ):
        try:
            operation()
        except ValueError:
            pass
        else:
            raise AssertionError("跨專案操作應被拒絕")

    # 連戲評分引用外部候選同樣不可
    try:
        record_continuity_qc(
            artifact, shot_id=SHOT_A, ref_shot_id=SHOT_B,
            scores={"cross_shot_identity": 50}, variant_id="var_foreign",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("連戲評分引用外部候選應被拒絕")


def verify_summary(artifact: ProductionArtifact) -> None:
    summary = summarize_project_qc(artifact)

    # 分母來自分鏡表，兩顆鏡頭都要算進去，即使 SHOT_B 從未產出候選
    check(
        summary.planned_count == 2,
        f"分母應涵蓋全部分鏡，實際 {summary.planned_count}",
    )
    check(summary.usable_count == 1, f"可用鏡頭數錯誤: {summary.usable_count}")
    check(
        summary.usable_shot_rate_of_planned == 0.5,
        f"以分鏡為分母的可用率應為 0.5，實際 {summary.usable_shot_rate_of_planned}",
    )

    outcomes = summary.outcome_counts()
    check(
        outcomes[ShotOutcome.USABLE.value] == 1,
        f"應有 1 顆 usable: {outcomes}",
    )
    check(
        outcomes[ShotOutcome.PLANNED.value] + outcomes[ShotOutcome.DISPATCHED.value]
        + outcomes[ShotOutcome.FAILED.value] == 1,
        f"SHOT_B 應被歸入未完成類別: {outcomes}",
    )
    check(
        sum(outcomes.values()) == summary.planned_count,
        "各狀態計數總和應等於分鏡數，狀態必須互斥",
    )

    by_shot = {item.shot_id: item for item in summary.shots}
    shot_a = by_shot[SHOT_A]
    check(shot_a.outcome is ShotOutcome.USABLE, "SHOT_A 應為 usable")
    check(shot_a.scored_count == 2, f"已評分數量錯誤: {shot_a.scored_count}")
    check(shot_a.usable_count == 1, f"可用候選數錯誤: {shot_a.usable_count}")
    check(shot_a.best_variant_id is not None, "未選出最佳候選")
    check(
        shot_a.usable_variant_rate
        == round(shot_a.usable_count / shot_a.variant_count, 3),
        "候選可用率應等於可用數除以候選數",
    )
    # 候選可用率與 usable-shot rate 是不同指標，不得混用
    check(
        shot_a.usable_variant_rate != summary.usable_shot_rate_of_planned,
        "候選可用率與鏡頭可用率在此情境下應不同",
    )

    check(summary.total_correction_minutes > 0, "應累計人工修正時間")
    check(
        summary.correction_minutes_per_usable_shot is not None,
        "應能計算每可用鏡頭的人工時間",
    )
    check(summary.continuity_count >= 1, "連戲評分數量錯誤")


def main() -> int:
    init_db()
    artifact = build_artifact()

    verify_import(artifact)
    verify_strict_hash_matching(artifact)
    verify_qc_scoring(artifact)
    verify_continuity_qc(artifact)
    verify_selection(artifact)
    verify_cross_project_guard(artifact)
    verify_summary(artifact)

    probe_note = "ffprobe" if ffprobe_available() else "no-ffprobe"
    print(
        f"OK variant qc smoke variants={len(list_variants(PROJECT_ID))} "
        f"probe={probe_note}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
