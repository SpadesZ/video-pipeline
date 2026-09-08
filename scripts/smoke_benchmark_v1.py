# 檔案路徑: video-pipeline/scripts/smoke_benchmark_v1.py
# 產生時間: 2026-09-05 +08:00
# 版本: v2.0
# 模組定位:
#   V1 Benchmark Pack 確定性冒煙測試。
# 主要責任:
#   1. fixture 完整性與三類情境涵蓋。
#   2. 四個平台皆相容且收到完全相同的內容。
#   3. BenchmarkTarget 身份貫穿 job package 與評分表。
#   4. 素材實質驗證：壞圖與錯比例必須被拒。
#   5. 素材內容 hash 納入請求識別，換圖後舊 manifest 保留。
#   6. 嘗試紀錄保留失敗列，sync 不得洗掉。
#   7. 連戲配對不得跨模型或跨鏡頭。
#   8. 表情演技依鏡頭判定，嘴型全域 N/A。
#   9. 統計公式對固定資料得到固定結果。
# 說明:
#   以 Pillow 產生真正可解碼的小圖作為素材，不使用檔頭加隨機位元組。
#   不需要任何 API 金鑰，也不進行真實影片生成。
# --------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# UI 驗證需要 import app。容器內 WORKDIR 已是 apps/api，
# 但 native 執行時得自行補上，否則 PR CI 會少驗證整個 Web 層。
API_ROOT = ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="benchmark_v1_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from PIL import Image
from sqlmodel import Session, select

from pipeline.benchmark import (
    aggregation,
    attempts,
    attribution,
    builder,
    score_import,
    score_sheet,
    target,
    v1_pack,
)
from pipeline.benchmark.asset_validation import validate_image
from pipeline.capability import (
    check_compatibility,
    get_provider,
    model_registry,
    read_job_manifest,
    routing_policy,
)
from pipeline.db import engine, init_db
from pipeline.models.capability import Capability
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import load_preset
from pipeline.models.qc import ContinuityQC, VariantQC
from pipeline.models.reference_asset import ReferenceAsset
from pipeline.models.variant import AssetVariant, CapabilityJob, VariantStatus
from pipeline.project_store import load_project
from pipeline.settings import get_settings
from pipeline.stages.shot_dispatcher import (
    ShotReadinessState,
    assess_project_readiness,
    build_shot_request,
)

VERTICAL_SIZE = (576, 1024)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_image(path: Path, size=VERTICAL_SIZE, colour=(40, 60, 90)) -> None:
    """產生真正可解碼的圖片，而非檔頭加隨機位元組。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, format="PNG")


def verify_fixture() -> None:
    shots = v1_pack.shots()
    check(len(shots) == 6, f"應有 6 顆鏡頭，實際 {len(shots)}")
    check(len(v1_pack.SCENARIOS) == 3, "應涵蓋三類情境")

    covered = {v1_pack.SHOT_SCENARIOS[shot.shot_id] for shot in shots}
    declared = {item.scenario_id for item in v1_pack.SCENARIOS}
    check(covered == declared, f"情境涵蓋不完整: {covered} vs {declared}")

    dialogue = v1_pack.shots_by_scenario("two_character_dialogue")
    check(len(dialogue) >= 3, "對話情境應包含雙人鏡頭與正反打")
    check(
        len([s for s in dialogue if "ots" in s.shot_id]) == 2,
        "應有兩顆互為反打的過肩鏡頭",
    )

    shot_ids = {shot.shot_id for shot in shots}
    for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
        check(shot_id in shot_ids, f"連戲配對指向不存在的鏡頭 {shot_id}")
        check(ref_shot_id in shot_ids, f"連戲配對指向不存在的鏡頭 {ref_shot_id}")

    for shot in shots:
        check(shot.first_frame_ref, f"{shot.shot_id} 缺少首幀")
        check(
            shot.target_duration_ms == v1_pack.SHOT_DURATION_MS,
            f"{shot.shot_id} 片長應統一",
        )
        check(
            shot.negative_prompt == v1_pack.NEGATIVE_PROMPT,
            f"{shot.shot_id} 應使用共用負面提示詞",
        )

    for pack in v1_pack.characters():
        check(
            not pack.reference_asset_ids,
            f"{pack.character_id} 不應攜帶參考素材，V1 僅以首幀傳遞身份",
        )


def verify_targets() -> None:
    """比較對象必須帶精確模型身份，且未確認者標為 provisional。"""
    registry = target.targets()
    check(len(registry.targets) >= 4, "應至少涵蓋四個比較對象")
    check(
        len(set(registry.target_ids)) == len(registry.target_ids),
        "target_id 不得重複",
    )
    for item in registry.targets:
        check(item.provider, f"{item.target_id} 缺少 provider")
        check(item.model_id, f"{item.target_id} 缺少 model_id")
        check(item.transport == "manual", f"{item.target_id} 應為人工 transport")
    check(
        registry.provisional_targets,
        "尚未經真人確認的型號必須標記 provisional",
    )
    check(
        target.resolve(registry.target_ids[0]).target_id == registry.target_ids[0],
        "resolve 應能取回 target",
    )
    try:
        target.resolve("does_not_exist")
    except ValueError:
        pass
    else:
        raise AssertionError("未登錄的 target 應被拒絕")


def verify_same_provider_distinct_models() -> None:
    """同一平台的不同版本必須是不同的比較對象，不得混淆。"""
    registry = target.load_targets(
        _write_targets_yaml(
            [
                ("kling_v1", "kling", "kling-video", "1.6"),
                ("kling_v2", "kling", "kling-video", "2.1"),
            ]
        )
    )
    check(len(registry.targets) == 2, "應載入兩個比較對象")
    check(
        len(registry.for_provider("kling")) == 2,
        "同一平台可有多個比較對象",
    )
    first, second = registry.targets
    check(first.target_id != second.target_id, "target_id 必須不同")
    check(first.display != second.display, f"顯示名稱應可區分: {first.display}")
    check(
        first.identity()["model_version"] != second.identity()["model_version"],
        "版本必須被記錄且可區分",
    )


def _write_targets_yaml(rows) -> Path:
    path = _TMP_DIR / "alt_targets.yaml"
    lines = ["targets:"]
    for target_id, provider, model_id, version in rows:
        lines.extend(
            [
                f"  - target_id: {target_id}",
                f"    provider: {provider}",
                f"    model_id: {model_id}",
                f"    model_version: '{version}'",
                "    transport: manual",
                "    provisional: false",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def verify_asset_validation() -> None:
    """壞圖、非影像與錯比例必須 fail-closed。"""
    good = _TMP_DIR / "probe" / "good.png"
    make_image(good)
    check(validate_image(good).ok, "正常直式圖應通過")

    fake = _TMP_DIR / "probe" / "fake.png"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 256)
    result = validate_image(fake)
    check(not result.ok, "檔頭加隨機位元組不應通過")
    check(result.problems, "應說明失敗原因")

    landscape = _TMP_DIR / "probe" / "landscape.png"
    make_image(landscape, size=(1024, 576))
    result = validate_image(landscape)
    check(not result.ok, "橫式圖不應通過 9:16 檢查")
    check(any("比例" in item for item in result.problems), f"{result.problems}")
    check(validate_image(landscape, require_vertical=False).ok, "不限比例時應通過")

    tiny = _TMP_DIR / "probe" / "tiny.png"
    make_image(tiny, size=(90, 160))
    result = validate_image(tiny)
    check(not result.ok, "解析度過低不應通過")
    check(any("短邊" in item for item in result.problems), f"{result.problems}")

    empty = _TMP_DIR / "probe" / "empty.png"
    empty.write_bytes(b"")
    check(not validate_image(empty).ok, "空檔不應通過")


def verify_asset_gate() -> Path:
    settings = get_settings()
    report = builder.check_assets(settings)
    check(not report.ready, "空目錄不應被視為就緒")

    directory = Path(report.assets_dir)

    # 先放一張壞圖，確認驗證會擋下
    bad = directory / v1_pack.REQUIRED_ASSETS[2].filename
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 256)
    partial = builder.check_assets(settings)
    check(not partial.ready, "壞圖不應被視為就緒")
    check(
        any(item.problems for item in partial.invalid),
        "應回報壞圖的問題",
    )

    for required in v1_pack.REQUIRED_ASSETS:
        make_image(
            directory / required.filename,
            size=VERTICAL_SIZE if required.require_vertical else (768, 768),
        )

    ready = builder.check_assets(settings)
    check(ready.ready, f"素材補齊後應就緒: {ready.instructions()}")
    check(
        all(item.file_hash for item in ready.statuses),
        "通過驗證的素材應附帶內容雜湊",
    )
    return directory


def verify_build_and_dispatch() -> None:
    settings = get_settings()
    registered = builder.register_assets(settings)
    check(
        registered == len(v1_pack.REQUIRED_ASSETS),
        f"應登錄全部素材，實際 {registered}",
    )

    with Session(engine) as session:
        assets = session.exec(select(ReferenceAsset)).all()
    check(
        all(item.file_hash for item in assets),
        "ReferenceAsset.file_hash 必須實際填入",
    )

    artifact = builder.build_artifact(settings)
    readiness = assess_project_readiness(artifact)
    not_ready = [
        shot_id
        for shot_id, item in readiness.items()
        if item.state is not ShotReadinessState.READY
    ]
    check(not not_ready, f"素材就緒後所有鏡頭應可派工: {not_ready}")

    reports = asyncio.run(builder.dispatch_all(artifact))
    registry = target.targets()
    check(
        set(reports) == set(registry.target_ids),
        f"應對全部比較對象派工: {set(reports)}",
    )
    for target_id, result in reports.items():
        check(
            result.dispatched_count == 6,
            f"{target_id} 應派工 6 顆，實際 {result.dispatched_count}",
        )


def verify_all_providers_compatible() -> None:
    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    profile = artifact.production_profile
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    registry = model_registry()

    for shot in artifact.shot_plans:
        request = build_shot_request(artifact, shot, packs, profile)
        check(
            not request.visual.reference_asset_ids,
            f"{shot.shot_id} 不應有額外參考素材",
        )
        for provider_id in v1_pack.target_providers():
            spec = get_provider(provider_id)
            models = [
                entry
                for entry in registry.models_on(provider_id)
                if entry.supports(shot.capability)
            ]
            check(models, f"{provider_id} 無支援的模型")
            problems = check_compatibility(request, models[0], spec)
            check(not problems, f"{shot.shot_id} 於 {provider_id} 不相容: {problems}")


def verify_prompt_parity_and_target_identity() -> None:
    settings = get_settings()
    index = builder.package_index(settings)

    by_shot: dict[str, dict[str, dict]] = {}
    for provider, packages in index.items():
        for package_dir in packages:
            manifest = read_job_manifest(Path(package_dir))
            by_shot.setdefault(manifest["shot_id"], {})[provider] = manifest

    check(len(by_shot) == 6, f"應涵蓋 6 顆鏡頭，實際 {len(by_shot)}")

    for shot_id, manifests in by_shot.items():
        baseline = None
        for provider, manifest in manifests.items():
            visual = manifest["request"]["visual"]
            payload = (
                visual["prompt"],
                visual["negative_prompt"],
                visual["first_frame_ref"],
                tuple(visual["reference_asset_ids"]),
                visual["duration_ms"],
                visual["aspect_ratio"],
            )
            if baseline is None:
                baseline = payload
            else:
                check(
                    payload == baseline,
                    f"{shot_id} 於 {provider} 的內容與其他平台不同",
                )

            # 比較對象身份必須寫入 manifest
            parameters = manifest["request"]["parameters"]
            check(
                parameters.get("_bm_target_id"),
                f"{shot_id}@{provider} 的 manifest 缺少 target 身份",
            )
            # 內部標記不得混入交給平台的參數
            check(
                not any(
                    key.startswith("_bm_")
                    for key in manifest["provider_parameters"]
                ),
                f"{shot_id}@{provider} 的平台參數混入了內部標記",
            )
            # 素材內容憑證
            check(
                manifest.get("reference_hashes"),
                f"{shot_id}@{provider} 缺少素材內容雜湊",
            )
            check(
                visual.get("reference_hashes"),
                f"{shot_id}@{provider} 的請求未納入素材雜湊",
            )

        hashes = {manifest["request_hash"] for manifest in manifests.values()}
        check(
            len(hashes) == len(manifests),
            f"{shot_id} 各平台的 request_hash 應互異",
        )


def verify_reference_swap_changes_identity() -> None:
    """同一 asset_id 換圖後必須產生新的 fingerprint，舊 manifest 保留。"""
    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    profile = artifact.production_profile
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    shot = artifact.shot_plans[0]

    before = build_shot_request(artifact, shot, packs, profile)
    before_hash = before.content_hash()
    check(before.visual.reference_hashes, "請求應包含素材雜湊")

    index_before = builder.package_index(settings)
    packages_before = {
        provider: set(items) for provider, items in index_before.items()
    }

    # 換掉首幀的內容，asset_id 不變
    directory = builder.assets_dir(settings)
    frame = next(
        item for item in v1_pack.REQUIRED_ASSETS
        if item.asset_id == shot.first_frame_ref
    )
    make_image(directory / frame.filename, colour=(200, 30, 30))
    builder.register_assets(settings)

    after = build_shot_request(artifact, shot, packs, profile)
    check(
        after.content_hash() != before_hash,
        "換圖後 request identity 必須改變，否則兩次生成會被視為同一份",
    )
    check(
        after.visual.reference_hashes != before.visual.reference_hashes,
        "素材雜湊應隨內容改變",
    )

    asyncio.run(builder.dispatch_all(artifact))
    index_after = builder.package_index(settings)
    for provider, items in packages_before.items():
        current = set(index_after.get(provider, []))
        check(
            items <= current,
            f"{provider} 的舊 manifest 應保留，不得被覆寫",
        )
        check(
            len(current) > len(items),
            f"{provider} 換圖後應產生新的 package 目錄",
        )


def verify_sheets_and_applicability() -> None:
    settings = get_settings()
    directory = Path(settings.data_dir) / "benchmark" / "v1" / "sheets"

    variant_path = score_sheet.write_variant_sheet(directory)
    continuity_path = score_sheet.write_continuity_sheet(directory)
    attempts_path = attempts.write_blank_ledger(directory)

    with variant_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    registry = target.targets()
    expected = 6 * len(registry.targets) * v1_pack.CANDIDATES_PER_SHOT
    check(len(rows) == expected, f"評分表列數應為 {expected}，實際 {len(rows)}")
    check(
        all(row["target_id"] and row["model_id"] for row in rows),
        "評分表每列都必須帶比較對象身份",
    )

    # 表情演技依鏡頭判定：a2 是特寫，必須可評
    a2_rows = [row for row in rows if row["shot_id"] == "bm_a2_closeup_expression"]
    check(a2_rows, "應有 a2 的列")
    check(
        all(row["facial_acting"] == "" for row in a2_rows),
        "a2 為特寫表情鏡頭，facial_acting 必須可填",
    )
    a1_rows = [row for row in rows if row["shot_id"] == "bm_a1_walk_slow_push"]
    check(
        all(row["facial_acting"] == "n/a" for row in a1_rows),
        "a1 為遠景，facial_acting 應為 n/a",
    )
    dialogue_rows = [
        row for row in rows if row["scenario"] == "two_character_dialogue"
    ]
    check(
        all(row["facial_acting"] == "" for row in dialogue_rows),
        "對話鏡頭的 facial_acting 必須可填",
    )

    with continuity_path.open(encoding="utf-8-sig") as handle:
        continuity_rows = list(csv.DictReader(handle))
    check(
        all(row["lip_sync_quality"] == "n/a" for row in continuity_rows),
        "V1 無音訊，嘴型一律 n/a",
    )
    check(
        not v1_pack.LIP_SYNC_ENABLED,
        "V1 不應啟用嘴型評分",
    )
    check(
        aggregation.BENCHMARK_WEIGHTS.lip_sync_quality == 0.0,
        "嘴型不得參與排名",
    )

    with attempts_path.open(encoding="utf-8-sig") as handle:
        attempt_rows = list(csv.DictReader(handle))
    check(len(attempt_rows) == expected, "嘗試紀錄應預留相同列數")
    check(
        all(row["target_id"] for row in attempt_rows),
        "嘗試紀錄必須帶比較對象",
    )


def _seed_variant(
    target_obj,
    shot_id: str,
    variant_id: str,
    filename: str,
    with_lineage: bool = True,
) -> None:
    """建立候選並視需要附上派工血緣。

    benchmark 歸屬只認血緣，因此測試必須真的建立 CapabilityJob，
    不能只塞一個 provider 名稱。
    """
    from pipeline.models.variant import CapabilityJob, JobStatus, TransportKind

    job_id = f"job_{variant_id}"
    with Session(engine) as session:
        if with_lineage:
            session.add(
                CapabilityJob(
                    job_id=job_id,
                    project_id=v1_pack.BENCHMARK_PROJECT_ID,
                    shot_id=shot_id,
                    capability=Capability.VIDEO_I2V.value,
                    provider=target_obj.provider,
                    model_id=target_obj.model_id,
                    transport=TransportKind.MANUAL.value,
                    status=JobStatus.COMPLETED.value,
                    request_snapshot={
                        "parameters": {"_bm_target_id": target_obj.target_id}
                    },
                )
            )
        session.add(
            AssetVariant(
                variant_id=variant_id,
                project_id=v1_pack.BENCHMARK_PROJECT_ID,
                shot_id=shot_id,
                job_id=job_id if with_lineage else None,
                provider=target_obj.provider,
                model_id=target_obj.model_id,
                prompt_snapshot="snapshot",
                local_path=str(_TMP_DIR / "clips" / filename),
                original_filename=filename,
                status=VariantStatus.IMPORTED.value,
            )
        )
        session.commit()


def verify_only_model_id_enforced() -> None:
    """target 的 exact model 必須真正限制路由，不能只靠標籤。"""
    from pipeline.benchmark.target import (
        BenchmarkTarget,
        TargetValidationError,
        validate_target,
    )

    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    profile = artifact.production_profile
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    shot = artifact.shot_plans[0]
    request = build_shot_request(artifact, shot, packs, profile)

    registry = target.targets()
    real = registry.targets[0]

    # 指定正確模型時應有候選，且只會是該模型
    decision = routing_policy().resolve(
        request,
        profile=profile,
        only_provider=real.provider,
        only_model_id=real.model_id,
    )
    check(decision.ok, f"正確的 target 應有候選: {decision.rejected}")
    check(
        all(c.model.model_id == real.model_id for c in decision.candidates),
        "路由不得挑到其他模型",
    )

    # 不存在的模型必須被拒絕，而非退回同平台其他模型
    ghost = routing_policy().resolve(
        request, profile=profile, only_provider=real.provider,
        only_model_id="model-does-not-exist",
    )
    check(not ghost.ok, "不存在的模型不應有候選")
    check(
        any("未登錄" in reason for reason in ghost.rejected),
        f"應說明模型未登錄: {ghost.rejected}",
    )

    # 模型存在但不屬於該平台
    other = next(item for item in registry.targets if item.provider != real.provider)
    mismatch = routing_policy().resolve(
        request, profile=profile, only_provider=real.provider,
        only_model_id=other.model_id,
    )
    check(not mismatch.ok, "跨平台的模型不應有候選")
    check(
        any("託管" in reason for reason in mismatch.rejected),
        f"應說明託管關係不符: {mismatch.rejected}",
    )

    # target 驗證同樣 fail-closed
    validate_target(real, shot.capability)
    for bad in (
        BenchmarkTarget(
            target_id="bad_model", provider=real.provider,
            model_id="model-does-not-exist",
        ),
        BenchmarkTarget(
            target_id="bad_provider", provider=real.provider,
            model_id=other.model_id,
        ),
    ):
        try:
            validate_target(bad, shot.capability)
        except TargetValidationError:
            pass
        else:
            raise AssertionError(f"{bad.target_id} 應被拒絕")


def verify_attribution_requires_lineage() -> None:
    """歸屬只走派工血緣，沒有血緣的候選不得被猜進 benchmark。"""
    registry = target.targets()
    primary = registry.targets[0]

    _seed_variant(primary, "bm_a1_walk_slow_push", "var_lineage_ok", "ok.mp4")
    _seed_variant(
        primary, "bm_a1_walk_slow_push", "var_no_lineage", "orphan.mp4",
        with_lineage=False,
    )

    index = attribution.build_index(v1_pack.BENCHMARK_PROJECT_ID)
    check(
        index.by_variant_id("var_lineage_ok") is not None,
        "有血緣的候選應可歸屬",
    )
    check(
        index.by_variant_id("var_lineage_ok").target_id == primary.target_id,
        "歸屬的 target 錯誤",
    )
    check(
        "var_no_lineage" in index.unattributed,
        "無血緣的候選應列為不可歸屬",
    )
    check(
        index.by_variant_id("var_no_lineage") is None,
        "無血緣的候選不得出現在索引中",
    )

    # 檔名對應：精確、找不到、歧義
    found = index.find_by_file(primary.target_id, "bm_a1_walk_slow_push", "ok.mp4")
    check(found.variant_id == "var_lineage_ok", "檔名對應錯誤")

    try:
        index.find_by_file(primary.target_id, "bm_a1_walk_slow_push", "nope.mp4")
    except LookupError:
        pass
    else:
        raise AssertionError("找不到檔名時應拒絕")

    _seed_variant(primary, "bm_a1_walk_slow_push", "var_dup_a", "dup.mp4")
    _seed_variant(primary, "bm_a1_walk_slow_push", "var_dup_b", "dup.mp4")
    dup_index = attribution.build_index(v1_pack.BENCHMARK_PROJECT_ID)
    try:
        dup_index.find_by_file(primary.target_id, "bm_a1_walk_slow_push", "dup.mp4")
    except attribution.AmbiguousAttribution:
        pass
    else:
        raise AssertionError("同名多筆時應拒絕，不得挑第一個")


def verify_attempt_sync_uses_original_filename() -> None:
    """匯入會把檔案改名為 var_<uuid>，對應必須認平台原始檔名。

    人工在 ledger 記的是平台下載時的檔名，系統內部改名對他不可見。
    若索引拿改名後的檔名去比對，每一列成功的嘗試都會 unresolved，
    而且不會有任何徵兆——統計只會少掉資料，不會報錯。
    """
    from pipeline.stages.variant_importer import import_variants

    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    registry = target.targets()
    primary = registry.targets[0]
    shot_id = "bm_b1_two_shot_dialogue"
    platform_name = "provider_result_001.mp4"

    # 取真實派工，走完整匯入路徑而非直接塞資料庫
    with Session(engine) as session:
        job = next(
            (
                item
                for item in session.exec(
                    select(CapabilityJob).where(
                        CapabilityJob.project_id == v1_pack.BENCHMARK_PROJECT_ID,
                        CapabilityJob.shot_id == shot_id,
                    )
                ).all()
                if (item.request_snapshot or {}).get("parameters", {}).get(
                    "_bm_target_id"
                )
                == primary.target_id
            ),
            None,
        )
    check(job is not None, f"{primary.target_id}/{shot_id} 應已有派工")

    clip = _TMP_DIR / "downloads" / platform_name
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2048))

    report = import_variants(
        settings,
        artifact,
        shot_id=shot_id,
        provider=primary.provider,
        files=[(platform_name, clip.read_bytes())],
        request_hash=job.request_hash,
        model_id=primary.model_id,
    )
    check(report.new_count == 1, f"應匯入 1 支候選: {report.skipped}")
    imported = report.imported[0]

    stored = Path(imported.local_path).name
    check(
        stored != platform_name,
        f"匯入後應改名為系統檔名，實際仍為 {stored}",
    )
    check(stored.startswith("var_"), f"系統檔名應為 var_ 前綴，實際 {stored}")

    index = attribution.build_index(v1_pack.BENCHMARK_PROJECT_ID)
    entry = index.by_variant_id(imported.variant_id)
    check(entry is not None, "匯入的候選應可歸屬")
    check(
        entry.file_name == platform_name,
        f"對應用的檔名應為平台原始檔名，實際 {entry.file_name}",
    )
    check(
        entry.stored_filename == stored,
        f"stored_filename 應保留系統檔名，實際 {entry.stored_filename}",
    )

    found = index.find_by_file(primary.target_id, shot_id, platform_name)
    check(
        found.variant_id == imported.variant_id,
        "以平台原始檔名應能找回候選",
    )

    # 內部檔名不得成為對應依據，否則等於承認兩套檔名都算數
    try:
        index.find_by_file(primary.target_id, shot_id, stored)
    except LookupError:
        pass
    else:
        raise AssertionError("不得以系統內部檔名對應使用者記錄的 output_file")

    # 完整走一次 ledger sync。用獨立 ledger，避免與共用評分表的列數互相牽動
    path = _TMP_DIR / "original_filename_ledger" / attempts.ATTEMPTS_SHEET
    attempts.append_attempt(
        path,
        shot_id=shot_id,
        target_id=primary.target_id,
        status="success",
        output_file=platform_name,
        generation_seconds="72",
    )
    sync = attempts.sync_variant_ids(path, v1_pack.BENCHMARK_PROJECT_ID)
    check(
        not sync.unresolved,
        f"平台檔名應可對應到候選: {sync.unresolved}",
    )

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    matched = [row for row in rows if row["output_file"] == platform_name]
    check(len(matched) == 1, f"應有一列記錄 {platform_name}")
    check(
        matched[0]["variant_id"] == imported.variant_id,
        f"variant_id 應補齊為 {imported.variant_id}，"
        f"實際 {matched[0]['variant_id']!r}",
    )


def verify_benchmark_selection_independent() -> None:
    """每個 target 在同一顆鏡頭都能各自選代表作。"""
    registry = target.targets()
    shot_id = "bm_a2_closeup_expression"

    chosen = []
    for index_no, item in enumerate(registry.targets[:3], start=1):
        variant_id = f"var_sel_{index_no}"
        _seed_variant(item, shot_id, variant_id, f"sel_{index_no}.mp4")
        attribution.select_benchmark_candidate(
            v1_pack.BENCHMARK_PROJECT_ID, variant_id
        )
        chosen.append((item.target_id, variant_id))

    index = attribution.build_index(v1_pack.BENCHMARK_PROJECT_ID)
    selected = [
        item for item in index.items
        if item.shot_id == shot_id and item.benchmark_selected
    ]
    check(
        len(selected) == 3,
        f"同一顆鏡頭應可同時有三個 target 各自選定，實際 {len(selected)}",
    )
    check(
        len({item.target_id for item in selected}) == 3,
        "三支代表作應分屬不同比較對象",
    )

    # production 的 SELECTED 不受影響
    with Session(engine) as session:
        rows = session.exec(
            select(AssetVariant).where(
                AssetVariant.project_id == v1_pack.BENCHMARK_PROJECT_ID,
                AssetVariant.shot_id == shot_id,
            )
        ).all()
    check(
        not any(row.status == VariantStatus.SELECTED for row in rows),
        "benchmark 選定不應改動 production 的 SELECTED",
    )

    # 同一 target 改選時，舊的要退掉
    first_target, first_variant = chosen[0]
    replacement = "var_sel_replace"
    _seed_variant(registry.by_id(first_target), shot_id, replacement, "replace.mp4")
    attribution.select_benchmark_candidate(
        v1_pack.BENCHMARK_PROJECT_ID, replacement
    )
    index = attribution.build_index(v1_pack.BENCHMARK_PROJECT_ID)
    same_target = [
        item for item in index.for_shot(first_target, shot_id)
        if item.benchmark_selected
    ]
    check(len(same_target) == 1, "同一 target 同一鏡頭只能有一支代表作")
    check(same_target[0].variant_id == replacement, "改選未生效")

    # 其他 target 的選擇不受影響
    still_selected = [
        item for item in index.items
        if item.shot_id == shot_id and item.benchmark_selected
    ]
    check(len(still_selected) == 3, "改選不應影響其他比較對象")

    # 無血緣的候選不得被選為代表作
    _seed_variant(
        registry.targets[0], shot_id, "var_sel_orphan", "orphan2.mp4",
        with_lineage=False,
    )
    try:
        attribution.select_benchmark_candidate(
            v1_pack.BENCHMARK_PROJECT_ID, "var_sel_orphan"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("無血緣的候選不應可被選為代表作")


def verify_sync_preserves_user_input() -> None:
    """sync 不得洗掉已填的人工欄位。"""
    settings = get_settings()
    directory = Path(settings.data_dir) / "benchmark" / "v1" / "sheets"
    registry = target.targets()
    primary = registry.targets[0]
    shot_id = "bm_b1_two_shot_dialogue"

    _seed_variant(primary, shot_id, "var_keep_1", "keep1.mp4")
    path, _ = score_sheet.sync_variant_sheet(
        directory, v1_pack.BENCHMARK_PROJECT_ID
    )

    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    row = next(item for item in rows if item["variant_id"] == "var_keep_1")
    row["identity_consistency"] = "77"
    row["temporal_stability"] = "81"
    row["facial_acting"] = "69"
    row["usable_without_repair"] = "yes"
    row["human_correction_minutes"] = "4.5"
    row["generation_seconds"] = "120"
    row["credits_used"] = "8"
    row["retries_to_usable"] = "2"
    row["notes"] = "手寫備註不可遺失"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_sheet.VARIANT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    # 新增另一支候選後重新 sync
    _seed_variant(primary, shot_id, "var_keep_2", "keep2.mp4")
    path, _ = score_sheet.sync_variant_sheet(
        directory, v1_pack.BENCHMARK_PROJECT_ID
    )

    with path.open(encoding="utf-8-sig") as handle:
        after = list(csv.DictReader(handle))
    kept = next(item for item in after if item["variant_id"] == "var_keep_1")
    for column, expected in (
        ("identity_consistency", "77"),
        ("temporal_stability", "81"),
        ("facial_acting", "69"),
        ("usable_without_repair", "yes"),
        ("human_correction_minutes", "4.5"),
        ("generation_seconds", "120"),
        ("credits_used", "8"),
        ("retries_to_usable", "2"),
        ("notes", "手寫備註不可遺失"),
    ):
        check(
            kept[column] == expected,
            f"sync 洗掉了人工填寫的 {column}: {kept[column]!r} != {expected!r}",
        )

    check(
        any(item["variant_id"] == "var_keep_2" for item in after),
        "新候選應被加入",
    )

    # 連戲表同樣要保留人工輸入
    continuity_path, _ = score_sheet.sync_continuity_sheet(
        directory, v1_pack.BENCHMARK_PROJECT_ID
    )
    with continuity_path.open(encoding="utf-8-sig") as handle:
        crows = list(csv.DictReader(handle))
    target_row = next(
        item for item in crows if item["target_id"] == primary.target_id
    )
    target_row["cross_shot_identity"] = "84"
    target_row["notes"] = "連戲備註"
    with continuity_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_sheet.CONTINUITY_COLUMNS)
        writer.writeheader()
        writer.writerows(crows)

    score_sheet.sync_continuity_sheet(directory, v1_pack.BENCHMARK_PROJECT_ID)
    with continuity_path.open(encoding="utf-8-sig") as handle:
        cafter = list(csv.DictReader(handle))
    kept_c = next(
        item
        for item in cafter
        if item["target_id"] == primary.target_id
        and item["shot_id"] == target_row["shot_id"]
        and item["ref_shot_id"] == target_row["ref_shot_id"]
    )
    check(kept_c["cross_shot_identity"] == "84", "連戲分數被 sync 洗掉")
    check(kept_c["notes"] == "連戲備註", "連戲備註被 sync 洗掉")


def verify_attempt_ledger_keeps_failures() -> None:
    """失敗的嘗試在 sync 之後仍必須存在。"""
    settings = get_settings()
    directory = Path(settings.data_dir) / "benchmark" / "v1" / "sheets"
    path = directory / attempts.ATTEMPTS_SHEET

    registry = target.targets()
    primary = registry.targets[0]
    shot_id = "bm_c1_run_tracking"

    _seed_variant(primary, shot_id, "var_attempt_ok", "c1_run_3.mp4")

    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    filled = 0
    for row in rows:
        if row["shot_id"] != shot_id or row["target_id"] != primary.target_id:
            continue
        filled += 1
        if filled == 1:
            row["status"] = "failed"
            row["failure_reason"] = "平台回報內容政策拒絕"
            row["generation_seconds"] = "40"
            row["credits_used"] = "5"
        elif filled == 2:
            row["status"] = "cancelled"
            row["generation_seconds"] = "12"
        else:
            row["status"] = "success"
            row["generation_seconds"] = "88"
            row["credits_used"] = "10"
            row["output_file"] = "c1_run_3.mp4"

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=attempts.ATTEMPT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    ledger = attempts.read_ledger(path)
    check(not ledger.errors, f"ledger 不應有錯誤: {ledger.errors}")
    check(len(ledger.attempts) == 3, f"應讀到 3 次嘗試，實際 {len(ledger.attempts)}")
    check(
        sum(1 for item in ledger.attempts if not item.succeeded) == 2,
        "應保留兩次未成功的嘗試",
    )

    report = attempts.sync_variant_ids(path, v1_pack.BENCHMARK_PROJECT_ID)
    check(
        report.updated == 1,
        f"應補齊 1 列 variant_id，實際 {report.updated}: {report.unresolved}",
    )

    after = attempts.read_ledger(path)
    check(
        len(after.attempts) == 3,
        f"sync 後嘗試紀錄不得減少，實際 {len(after.attempts)}",
    )
    check(
        sum(1 for item in after.attempts if not item.succeeded) == 2,
        "sync 不得洗掉失敗的嘗試",
    )
    matched = [
        item for item in after.attempts if item.succeeded and item.variant_id
    ]
    check(matched, "成功的嘗試應被補上 variant_id")
    check(
        matched[0].variant_id == "var_attempt_ok",
        f"應依 output_file 精確對應，實際 {matched[0].variant_id}",
    )
    check(
        all(item.variant_id is None for item in after.attempts if not item.succeeded),
        "失敗的嘗試不應被硬塞 variant_id",
    )

    # 檔名對不上時必須留空並回報，不得猜測
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["shot_id"] == shot_id and row["target_id"] == primary.target_id:
            if row["status"] == "failed":
                row["status"] = "success"
                row["output_file"] = "file_that_was_never_imported.mp4"
                row["variant_id"] = ""
                break
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=attempts.ATTEMPT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    unresolved_report = attempts.sync_variant_ids(
        path, v1_pack.BENCHMARK_PROJECT_ID
    )
    check(
        unresolved_report.unresolved,
        "對應不到的成功嘗試應被回報，而非靜默略過",
    )
    check(not unresolved_report.ok, "有未解析項目時 ok 應為 False")


def verify_continuity_pair_guard() -> None:
    """連戲配對不得跨模型或跨鏡頭。"""
    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    registry = target.targets()
    primary = registry.targets[0]
    other = next(
        item for item in registry.targets if item.provider != primary.provider
    )

    shot_id, ref_shot_id = v1_pack.CONTINUITY_PAIRS[1]

    _seed_variant(primary, ref_shot_id, "var_pair_ref", "pair_ref.mp4")
    _seed_variant(primary, shot_id, "var_pair_primary", "pair_primary.mp4")
    _seed_variant(other, ref_shot_id, "var_pair_foreign", "pair_foreign.mp4")

    for variant_id in ("var_pair_ref", "var_pair_primary", "var_pair_foreign"):
        attribution.select_benchmark_candidate(
            v1_pack.BENCHMARK_PROJECT_ID, variant_id
        )

    # 正確配對
    score_import.validate_continuity_pair(
        v1_pack.BENCHMARK_PROJECT_ID, primary.target_id, shot_id, ref_shot_id,
        "var_pair_primary", "var_pair_ref",
    )

    # 跨平台配對
    try:
        score_import.validate_continuity_pair(
            v1_pack.BENCHMARK_PROJECT_ID, primary.target_id, shot_id, ref_shot_id,
            "var_pair_primary", "var_pair_foreign",
        )
    except score_import.PairMismatch as error:
        check("平台" in str(error) or "跨" in str(error), f"{error}")
    else:
        raise AssertionError("跨平台配對應被拒絕")

    # 鏡頭錯置
    try:
        score_import.validate_continuity_pair(
            v1_pack.BENCHMARK_PROJECT_ID, primary.target_id, ref_shot_id, shot_id,
            "var_pair_primary", "var_pair_ref",
        )
    except score_import.PairMismatch as error:
        check("鏡頭" in str(error), f"{error}")
    else:
        raise AssertionError("鏡頭錯置應被拒絕")

    # 自我配對
    try:
        score_import.validate_continuity_pair(
            v1_pack.BENCHMARK_PROJECT_ID, primary.target_id, shot_id, ref_shot_id,
            "var_pair_primary", "var_pair_primary",
        )
    except score_import.PairMismatch:
        pass
    else:
        raise AssertionError("自我配對應被拒絕")

    # sync 產生的配對必須同平台
    directory = Path(settings.data_dir) / "benchmark" / "v1" / "sheets"
    path, pairs = score_sheet.sync_continuity_sheet(
        directory, v1_pack.BENCHMARK_PROJECT_ID
    )
    with path.open(encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if row["variant_id"]]
    check(pairs > 0, "應產生至少一組配對")
    for row in rows:
        if not row["ref_variant_id"]:
            continue
        score_import.validate_continuity_pair(
            v1_pack.BENCHMARK_PROJECT_ID,
            row["target_id"],
            row["shot_id"],
            row["ref_shot_id"],
            row["variant_id"],
            row["ref_variant_id"],
        )


def verify_aggregation_is_deterministic() -> None:
    """固定資料必須得到固定結果，且不產生跨情境總冠軍。"""
    ledger = attempts.AttemptLedger(path="fixture")
    registry = target.targets()
    strong, weak = registry.targets[0], registry.targets[1]

    def add(target_id: str, shot_id: str, statuses: list[str]) -> None:
        for index, status in enumerate(statuses, start=1):
            item = registry.by_id(target_id)
            ledger.attempts.append(
                attempts.Attempt(
                    scenario=v1_pack.SHOT_SCENARIOS[shot_id],
                    shot_id=shot_id,
                    target_id=target_id,
                    provider=item.provider,
                    model_id=item.model_id,
                    attempt_no=index,
                    status=status,
                    generation_seconds=60.0,
                    credits_used=10.0,
                )
            )

    shots = v1_pack.shots()
    for shot in shots:
        add(strong.target_id, shot.shot_id, ["success"])
        add(weak.target_id, shot.shot_id, ["failed", "failed", "success"])

    # 報表只納入身份與 catalog 現值相符的資料，因此 fixture 必須表明
    # 每一筆屬於哪個版本，否則會被當成舊版資料排除。
    from pipeline.benchmark import identity as identity_module

    strong_key = identity_module.from_target(strong).key
    weak_key = identity_module.from_target(weak).key

    variant_records = []
    for shot in shots:
        variant_records.append(
            {
                "target_id": strong.target_id,
                "identity_key": strong_key,
                "shot_id": shot.shot_id,
                "weighted_score": 85.0,
                "usable": True,
                "human_minutes": 2.0,
                "dimensions": {
                    "identity_consistency": 90.0,
                    "temporal_stability": 88.0,
                    "camera_control": 86.0,
                    "motion_quality": 80.0,
                    "artifact_severity": 10.0,
                    "facial_acting": 84.0,
                },
            }
        )
        variant_records.append(
            {
                "target_id": weak.target_id,
                "identity_key": weak_key,
                "shot_id": shot.shot_id,
                "weighted_score": 55.0,
                "usable": False,
                "human_minutes": 20.0,
                "dimensions": {
                    "identity_consistency": 50.0,
                    "temporal_stability": 48.0,
                    "camera_control": 45.0,
                    "motion_quality": 52.0,
                    "artifact_severity": 60.0,
                    "facial_acting": 44.0,
                },
            }
        )

    dialogue_shot = v1_pack.shots_by_scenario("two_character_dialogue")[0].shot_id
    continuity_records = [
        {
            "target_id": strong.target_id,
            "identity_key": strong_key,
            "shot_id": dialogue_shot,
            "weighted_score": 88.0,
            "cross_shot_identity": 92.0,
        },
        {
            "target_id": weak.target_id,
            "identity_key": weak_key,
            "shot_id": dialogue_shot,
            "weighted_score": 40.0,
            "cross_shot_identity": 38.0,
        },
    ]

    report = aggregation.build_report(ledger, variant_records, continuity_records)
    again = aggregation.build_report(ledger, variant_records, continuity_records)
    check(
        report.model_dump() == again.model_dump(),
        "相同輸入必須得到完全相同的報表",
    )

    strong_agg = report.by_target(strong.target_id)
    weak_agg = report.by_target(weak.target_id)
    check(strong_agg is not None and weak_agg is not None, "應涵蓋兩個比較對象")

    check(
        strong_agg.usable_shot_rate == 1.0,
        f"強者可用率應為 1.0，實際 {strong_agg.usable_shot_rate}",
    )
    check(
        weak_agg.usable_shot_rate == 0.0,
        f"弱者可用率應為 0.0，實際 {weak_agg.usable_shot_rate}",
    )
    # 失敗嘗試必須計入
    check(
        weak_agg.total_attempts == len(shots) * 3,
        f"弱者嘗試次數應含失敗，實際 {weak_agg.total_attempts}",
    )
    check(
        weak_agg.failed_attempts == len(shots) * 2,
        "失敗次數應被記錄",
    )
    check(
        strong_agg.retries_per_usable == 1.0,
        f"強者每可用鏡頭嘗試次數應為 1.0，實際 {strong_agg.retries_per_usable}",
    )
    check(
        weak_agg.retries_per_usable is None,
        "沒有可用鏡頭時不應計算每可用重試次數",
    )
    check(
        strong_agg.human_minutes_per_usable == 2.0,
        f"人工時間換算錯誤: {strong_agg.human_minutes_per_usable}",
    )
    check(
        strong_agg.overall.artifact_cleanliness == 90.0,
        f"瑕疵應取反: {strong_agg.overall.artifact_cleanliness}",
    )
    cinematic = strong_agg.scope("single_character_cinematic")
    check(cinematic is not None and cinematic.stability is not None,
          "應能計算情境穩定性")

    # 各情境獨立評選，不存在總冠軍欄位
    check(len(report.awards) == len(aggregation.ScenarioAward), "獎項數量不符")
    for award in report.awards:
        check(award.scope, f"{award.award} 應標明評分範圍")
        if award.winner:
            check(
                award.winner == strong.target_id,
                f"{award.award} 應由表現較佳者勝出，實際 {award.winner}",
            )
    check(
        not hasattr(report, "overall_winner"),
        "不得產生跨情境總冠軍",
    )

    # 情境獎項只能讀該情境的資料
    dialogue_award = report.award(
        aggregation.ScenarioAward.CHARACTER_DIALOGUE.value
    )
    check(
        dialogue_award.scope == "two_character_dialogue",
        f"對話獎應限定於對話情境: {dialogue_award.scope}",
    )
    check("僅使用該情境" in dialogue_award.note, "應標明範圍限制")

    action_award = report.award(
        aggregation.ScenarioAward.HIGH_DYNAMIC_ACTION.value
    )
    check(
        action_award.scope == "high_dynamic_action",
        f"高動態獎應限定於高動態情境: {action_award.scope}",
    )

    low_retry = report.award(
        aggregation.ScenarioAward.LOW_RETRY_PRODUCTION.value
    )
    check(
        low_retry.scope == aggregation.GLOBAL_SCOPE,
        "低重試獎為明確的跨情境指標",
    )

    # 情境聚合的鏡頭數必須小於全域
    dialogue_scope = strong_agg.scope("two_character_dialogue")
    check(
        len(dialogue_scope.shots) < len(strong_agg.overall.shots),
        "情境聚合不應包含其他情境的鏡頭",
    )
    check(
        all(
            v1_pack.SHOT_SCENARIOS[shot.shot_id] == "two_character_dialogue"
            for shot in dialogue_scope.shots
        ),
        "情境聚合混入了其他情境的鏡頭",
    )
    # 連戲資料應落在主鏡頭所屬的情境，而非全部情境
    check(
        dialogue_scope.continuity_identity == 92.0,
        f"連戲應計入對話情境: {dialogue_scope.continuity_identity}",
    )
    check(
        strong_agg.scope("high_dynamic_action").continuity_identity is None,
        "對話情境的連戲不應污染高動態情境",
    )
    check(
        report.provisional_targets,
        "報表應標示哪些比較對象的版本尚未確認",
    )
    check(
        report.aggregation_version == aggregation.AGGREGATION_VERSION,
        "報表應記錄統計公式版本",
    )


def verify_attempt_append() -> None:
    """UI 記錄嘗試：追加而非覆寫，成功必須帶檔名。"""
    settings = get_settings()
    path = Path(settings.data_dir) / "benchmark" / "v1" / "sheets" / (
        attempts.ATTEMPTS_SHEET
    )
    registry = target.targets()
    primary = registry.targets[0]
    shot_id = "bm_b2_ots_on_a"

    before = len(attempts.read_ledger(path).attempts)

    no1 = attempts.append_attempt(
        path, shot_id, primary.target_id, "failed",
        failure_reason="測試失敗紀錄", generation_seconds="30",
    )
    no2 = attempts.append_attempt(
        path, shot_id, primary.target_id, "cancelled",
    )
    check(no1 == 1 and no2 == 2, f"嘗試序號應遞增: {no1}, {no2}")

    after = attempts.read_ledger(path)
    check(
        len(after.attempts) == before + 2,
        f"應追加 2 列，實際 {len(after.attempts) - before}",
    )
    check(
        sum(1 for item in after.attempts if item.shot_id == shot_id) == 2,
        "追加的紀錄未正確寫入",
    )

    # 成功的嘗試必須帶檔名，否則之後無法對應候選
    try:
        attempts.append_attempt(path, shot_id, primary.target_id, "success")
    except ValueError as error:
        check("檔名" in str(error), f"錯誤訊息應說明缺少檔名: {error}")
    else:
        raise AssertionError("成功但未填檔名應被拒絕")

    for bad_target, bad_shot, bad_status in (
        ("no_such_target", shot_id, "failed"),
        (primary.target_id, "no_such_shot", "failed"),
        (primary.target_id, shot_id, "not_a_status"),
    ):
        try:
            attempts.append_attempt(path, bad_shot, bad_target, bad_status)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"無效輸入應被拒絕: {bad_target}/{bad_shot}/{bad_status}"
            )


def verify_ui_routes() -> None:
    """Benchmark 相關頁面必須可開啟，且不依賴 CLI 才能操作。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        for path in (
            "/",
            "/benchmark",
            "/benchmark/attempts",
            "/benchmark/results",
        ):
            response = client.get(path)
            check(
                response.status_code == 200,
                f"{path} 應回傳 200，實際 {response.status_code}",
            )

        home = client.get("/").text
        check('href="/benchmark"' in home, "導航應提供 Benchmark 入口")

        console = client.get("/benchmark").text
        for label in (
            "準備參考素材",
            "確認平台版本",
            "建立生成工單",
            "到平台生成並匯入",
        ):
            check(label in console, f"控制台缺少步驟: {label}")
        check("待確認" in console, "應標示尚未確認版本的比較對象")

        results = client.get("/benchmark/results").text
        check("統計公式版本" in results, "結果頁應標示統計公式版本")
        check("不產生總冠軍" in results, "結果頁應說明不產生跨情境總冠軍")

        form = client.get("/benchmark/attempts").text
        check("記錄生成嘗試" in form, "應提供嘗試記錄表單")
        check("cancelled" in form, "表單應可記錄取消的嘗試")


def main() -> int:
    init_db()
    verify_fixture()
    verify_targets()
    verify_same_provider_distinct_models()
    verify_asset_validation()
    verify_asset_gate()
    verify_build_and_dispatch()
    verify_all_providers_compatible()
    verify_prompt_parity_and_target_identity()
    verify_reference_swap_changes_identity()
    verify_sheets_and_applicability()
    verify_only_model_id_enforced()
    verify_attribution_requires_lineage()
    verify_attempt_sync_uses_original_filename()
    verify_benchmark_selection_independent()
    verify_sync_preserves_user_input()
    verify_attempt_ledger_keeps_failures()
    verify_attempt_append()
    verify_continuity_pair_guard()
    verify_aggregation_is_deterministic()
    verify_ui_routes()

    registry = target.targets()
    print(
        f"OK benchmark v1 smoke shots={len(v1_pack.shots())} "
        f"targets={len(registry.targets)} "
        f"scenarios={len(v1_pack.SCENARIOS)} "
        f"generations={v1_pack.total_generations()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
