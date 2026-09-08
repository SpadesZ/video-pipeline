# 檔案路徑: video-pipeline/scripts/smoke_narrative_models.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Narrative/Shot 核心資料模型確定性冒煙測試。
# 主要責任:
#   1. 驗證 ProductionProfile preset 為政策集合且可載入。
#   2. 驗證 NarrativeIR / ShotPlan / CharacterIdentityPack 可建構與走訪。
#   3. 驗證三段時長語義分離：目標、實際、成片佔用。
#   4. 驗證 QC 分數可為 N/A 且加權依 ProductionProfile 而異。
#   5. 驗證新欄位可經 ProductionArtifact 存檔、落盤與讀回。
#   6. 驗證 operational data 落在各自資料表而非 project JSON。
# 說明:
#   使用臨時 SQLite 與臨時 DATA_DIR，不依賴 Postgres 或任何 API 金鑰。
# --------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="narrative_smoke_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session, select

from pipeline.db import engine, init_db
from pipeline.models import (
    AssetVariant,
    Beat,
    BeatIntent,
    CameraSpec,
    Capability,
    CapabilityJob,
    CharacterIdentityPack,
    ContinuityQC,
    DialogueLine,
    EditDecision,
    GenerationCost,
    JobStatus,
    NarrativeIR,
    ProductionArtifact,
    ProviderBinding,
    ReferenceAsset,
    ReferenceAssetType,
    RenderMode,
    Scene,
    ShotFraming,
    ShotPlan,
    VariantQC,
    VariantStatus,
    build_timeline,
    load_preset,
)
from pipeline.project_store import load_project, save_project
from pipeline.settings import get_settings


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_profiles() -> None:
    comic = load_preset("comic_drama_high")
    film = load_preset("film_high")
    legacy = load_preset("slideshow_legacy")

    check(comic.aspect_ratio == "9:16", "漫劇 preset 應為直式")
    check(comic.uses_shot_assembly, "漫劇 preset 應使用鏡頭組裝")
    check(legacy.render_mode is RenderMode.SLIDESHOW, "既有 preset 應維持投影片模式")
    check(not legacy.uses_shot_assembly, "既有 preset 不應使用鏡頭組裝")

    # 權重必須因製作方式而異，否則 profile 沒有發揮作用
    check(
        comic.qc_weights.cross_shot_identity > comic.qc_weights.lip_sync_quality,
        "漫劇的角色一致性權重應高於嘴型",
    )
    check(
        film.qc_weights.camera_control > comic.qc_weights.camera_control,
        "電影的運鏡權重應高於漫劇",
    )

    clamped = comic.shot_duration.clamp_ms(30_000)
    check(clamped == 6000, f"鏡頭長度應被政策上限夾住，實際 {clamped}")


def build_narrative() -> tuple[NarrativeIR, list[CharacterIdentityPack], list[ShotPlan]]:
    beat_a = Beat(
        beat_id="beat_0001",
        scene_id="scene_0001",
        order=0,
        intent=BeatIntent.SETUP,
        summary="主角在雨中等待",
        dialogue=[DialogueLine(character_id="char_lin", text="你終於來了。")],
        character_ids=["char_lin"],
    )
    beat_b = Beat(
        beat_id="beat_0002",
        scene_id="scene_0001",
        order=1,
        intent=BeatIntent.TURN,
        summary="對方遞出信封",
        character_ids=["char_lin", "char_wei"],
    )
    scene = Scene(
        scene_id="scene_0001",
        order=0,
        summary="雨夜天橋",
        location="天橋",
        time_of_day="night",
        mood="tense",
        beats=[beat_a, beat_b],
    )
    narrative = NarrativeIR(
        project_id="smoke_narrative",
        logline="一場雨夜的交換改變兩人命運",
        scenes=[scene],
    )

    lin = CharacterIdentityPack(
        character_id="char_lin",
        role="protagonist",
        identity_description="短髮、深色風衣的女性",
        canonical_face_ref="ref_face_lin",
        wardrobe_refs=["ref_coat_lin"],
        provider_bindings={
            "kling": ProviderBinding(provider="kling", seed=1234, reference_id="k-abc")
        },
    )
    wei = CharacterIdentityPack(character_id="char_wei", role="antagonist")

    shots = [
        ShotPlan(
            shot_id="shot_0001",
            beat_id="beat_0001",
            scene_id="scene_0001",
            order=0,
            capability=Capability.VIDEO_I2V,
            camera=CameraSpec(framing=ShotFraming.WIDE),
            prompt="雨夜天橋遠景，女子獨自等待",
            character_refs=["char_lin"],
            first_frame_ref="ref_frame_0001",
            target_duration_ms=4000,
            aspect_ratio="9:16",
        ),
        ShotPlan(
            shot_id="shot_0002",
            beat_id="beat_0002",
            scene_id="scene_0001",
            order=1,
            capability=Capability.VIDEO_I2V,
            camera=CameraSpec(framing=ShotFraming.CLOSE_UP),
            prompt="特寫遞出信封的手",
            character_refs=["char_lin", "char_wei"],
            target_duration_ms=3000,
            aspect_ratio="9:16",
        ),
    ]
    return narrative, [lin, wei], shots


def verify_narrative(narrative: NarrativeIR, packs: list[CharacterIdentityPack]) -> None:
    check(narrative.beat_count == 2, f"應有 2 個 beat，實際 {narrative.beat_count}")
    walked = [beat.beat_id for _scene, beat in narrative.iter_beats()]
    check(walked == ["beat_0001", "beat_0002"], f"beat 走訪順序錯誤: {walked}")
    check(narrative.find_beat("beat_0002") is not None, "find_beat 應能找到既有 beat")
    check(narrative.find_beat("nope") is None, "find_beat 對不存在者應回傳 None")

    lin = packs[0]
    check(
        lin.reference_asset_ids == ["ref_face_lin", "ref_coat_lin"],
        f"參考素材彙整錯誤: {lin.reference_asset_ids}",
    )
    # seed 屬於平台專屬綁定，不得是角色身份的核心欄位
    check(not hasattr(lin, "seed"), "CharacterIdentityPack 不應直接持有 seed")
    binding = lin.binding_for("kling")
    check(binding is not None and binding.seed == 1234, "provider binding 應保存 seed")
    check(lin.binding_for("runway") is None, "未綁定的平台應回傳 None")


def verify_duration_layers(shots: list[ShotPlan]) -> None:
    """目標長度、生成長度與成片佔用長度必須是三個獨立概念。"""
    shot = shots[0]
    check(shot.target_duration_ms == 4000, "ShotPlan 記錄的是意圖長度")

    variant = AssetVariant(
        variant_id="var_0001",
        project_id="smoke_narrative",
        shot_id=shot.shot_id,
        provider="kling",
        requested_duration_ms=4000,
        actual_duration_ms=5200,  # 平台實際產出比要求的長
    )
    check(variant.actual_duration_ms == 5200, "AssetVariant 記錄的是生成結果長度")
    check(
        variant.duration_matches_request is False,
        "5200ms 超出 4000ms 的容許範圍，應判定不符",
    )

    # 剪輯只取用中間 3 秒，且不等於素材長度
    decision = EditDecision(
        edit_id="edit_0001",
        shot_id=shot.shot_id,
        variant_id=variant.variant_id,
        order=0,
        in_point_ms=800,
        out_point_ms=3800,
    )
    check(decision.source_duration_ms == 3000, "取用區間長度應為 3000ms")
    check(
        decision.used_duration_ms == 3000,
        "無 retime 時成片佔用等於取用區間",
    )

    timeline = build_timeline("smoke_narrative", [decision])
    check(
        timeline.total_duration_ms == 3000,
        f"時間線長度應取決於剪輯而非素材長度，實際 {timeline.total_duration_ms}",
    )
    check(
        timeline.total_duration_ms != variant.actual_duration_ms,
        "時間線長度不應等於素材檔案長度",
    )

    # 加入停格後成片變長，但素材長度不變
    held = EditDecision(
        edit_id="edit_0002",
        shot_id=shot.shot_id,
        variant_id=variant.variant_id,
        order=1,
        in_point_ms=0,
        out_point_ms=2000,
        hold_ms=500,
    )
    check(held.used_duration_ms == 2500, "停格應計入成片佔用長度")

    try:
        EditDecision(
            edit_id="bad", shot_id="s", variant_id="v", order=0,
            in_point_ms=2000, out_point_ms=1000,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("out_point 小於 in_point 時應拒絕建立")


def verify_qc_scoring() -> None:
    comic = load_preset("comic_drama_high")
    film = load_preset("film_high")

    # 無對白鏡頭的嘴型分數為 N/A，不得以 0 分混淆
    continuity = ContinuityQC(
        qc_id="cqc_0001",
        project_id="smoke_narrative",
        scope="pair",
        shot_id="shot_0002",
        ref_shot_id="shot_0001",
        cross_shot_identity=90,
        wardrobe_continuity=80,
        location_continuity=70,
        lip_sync_quality=None,
    )
    comic_score = continuity.weighted_score(comic.qc_weights)
    check(comic_score is not None, "有分數時加權結果不應為 None")
    # 若 None 被當成 0 分，加權會顯著低於此值
    check(comic_score > 80, f"N/A 不應被當作 0 分，實際加權 {comic_score}")

    film_score = continuity.weighted_score(film.qc_weights)
    check(
        film_score != comic_score,
        "不同 ProductionProfile 的權重應產生不同加權結果",
    )

    empty = ContinuityQC(
        qc_id="cqc_0002", project_id="p", scope="scene", shot_id="s1"
    )
    check(empty.weighted_score(comic.qc_weights) is None, "全 N/A 應回傳 None")

    variant_qc = VariantQC(
        qc_id="vqc_0001",
        variant_id="var_0001",
        project_id="smoke_narrative",
        shot_id="shot_0001",
        prompt_adherence=80,
        temporal_stability=90,
        artifact_severity=100,  # 瑕疵極嚴重
        usable_without_repair=False,
    )
    severe = variant_qc.weighted_score(comic.qc_weights)
    variant_qc.artifact_severity = 0  # 無瑕疵
    clean = variant_qc.weighted_score(comic.qc_weights)
    check(
        clean > severe,
        f"artifact_severity 越高應使加權越低，clean={clean} severe={severe}",
    )


def verify_persistence(
    narrative: NarrativeIR,
    packs: list[CharacterIdentityPack],
    shots: list[ShotPlan],
) -> None:
    settings = get_settings()
    init_db()

    artifact = ProductionArtifact(
        project_id="smoke_narrative",
        title="雨夜交換",
        production_profile=load_preset("comic_drama_high"),
        narrative_ir=narrative,
        character_packs=packs,
        shot_plans=shots,
    )
    save_project(settings, artifact)

    loaded = load_project(settings, "smoke_narrative")
    check(loaded is not None, "應能讀回專案")
    check(loaded.production_profile is not None, "production_profile 應被保存")
    check(
        loaded.production_profile.preset_id == "comic_drama_high",
        "preset_id 應被保存",
    )
    check(loaded.narrative_ir is not None, "narrative_ir 應被保存")
    check(
        loaded.narrative_ir.beat_count == 2,
        f"讀回的 beat 數量錯誤: {loaded.narrative_ir.beat_count}",
    )
    check(len(loaded.character_packs) == 2, "character_packs 應被保存")
    check(len(loaded.shot_plans) == 2, "shot_plans 應被保存")
    check(
        isinstance(loaded.shot_plans[0], ShotPlan),
        f"shot_plans 應還原為模型，實際型別 {type(loaded.shot_plans[0])}",
    )
    check(
        loaded.character_packs[0].binding_for("kling").seed == 1234,
        "provider binding 應能往返序列化",
    )

    project_dir = Path(settings.data_dir) / "projects" / "smoke_narrative"
    for filename in (
        "production_profile.json",
        "narrative_ir.json",
        "character_packs.json",
        "shot_plans.json",
    ):
        check((project_dir / filename).exists(), f"缺少落盤檔案 {filename}")


def verify_operational_tables() -> None:
    """AssetVariant 等 operational data 必須落在各自資料表。"""
    with Session(engine) as session:
        session.add(
            ReferenceAsset(
                asset_id="ref_face_lin",
                project_id="smoke_narrative",
                asset_type=ReferenceAssetType.FACE.value,
                file_hash="deadbeef",
            )
        )
        session.add(
            CapabilityJob(
                job_id="job_0001",
                project_id="smoke_narrative",
                shot_id="shot_0001",
                capability=Capability.VIDEO_I2V.value,
                provider="kling",
                status=JobStatus.PENDING_MANUAL.value,
                request_hash="hash_0001",
            )
        )
        session.add(
            AssetVariant(
                variant_id="var_0001",
                project_id="smoke_narrative",
                shot_id="shot_0001",
                job_id="job_0001",
                provider="kling",
                model_id="kling-v2",
                prompt_snapshot="雨夜天橋遠景",
                actual_duration_ms=5200,
                status=VariantStatus.IMPORTED.value,
                cost=GenerationCost(generations_attempted=3, usable_variants=1),
            )
        )
        # 重生成血緣
        session.add(
            AssetVariant(
                variant_id="var_0002",
                project_id="smoke_narrative",
                shot_id="shot_0001",
                parent_variant_id="var_0001",
                provider="kling",
                prompt_snapshot="雨夜天橋遠景，加強雨勢",
                status=VariantStatus.SELECTED.value,
            )
        )
        session.commit()

    with Session(engine) as session:
        variants = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == "smoke_narrative")
        ).all()
        check(len(variants) == 2, f"應有 2 個候選，實際 {len(variants)}")

        child = session.get(AssetVariant, "var_0002")
        check(child.parent_variant_id == "var_0001", "重生成血緣應被保存")
        check(child.is_selected, "選定狀態應可判定")

        parent = session.get(AssetVariant, "var_0001")
        check(parent.cost is not None, "成本應被保存")
        check(
            parent.cost.attempts_per_usable == 3.0,
            f"每可用鏡頭嘗試次數計算錯誤: {parent.cost.attempts_per_usable}",
        )

        job = session.get(CapabilityJob, "job_0001")
        check(not job.is_terminal, "pending_manual 不是終態")
        job.status = JobStatus.EXPIRED.value
        check(job.is_terminal and job.is_failure, "expired 應為終態且計入失敗")

        # 依 provider 統計，這是 Benchmark 的基礎查詢形態
        by_provider = session.exec(
            select(AssetVariant).where(AssetVariant.provider == "kling")
        ).all()
        check(len(by_provider) == 2, "應能依 provider 查詢候選")

    # operational data 不得混入 project JSON
    settings = get_settings()
    artifact_json = (
        Path(settings.data_dir) / "projects" / "smoke_narrative" / "production_artifact.json"
    ).read_text(encoding="utf-8")
    for leaked in ("var_0001", "job_0001", "asset_variants"):
        check(
            leaked not in artifact_json,
            f"operational data '{leaked}' 不應出現在 project JSON",
        )


def main() -> int:
    verify_profiles()
    narrative, packs, shots = build_narrative()
    verify_narrative(narrative, packs)
    verify_duration_layers(shots)
    verify_qc_scoring()
    verify_persistence(narrative, packs, shots)
    verify_operational_tables()

    print(
        "OK narrative models smoke "
        f"beats={narrative.beat_count} shots={len(shots)} packs={len(packs)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
