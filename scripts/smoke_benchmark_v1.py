# 檔案路徑: video-pipeline/scripts/smoke_benchmark_v1.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark Pack 確定性冒煙測試。
# 主要責任:
#   1. 驗證 fixture 涵蓋三類情境且鏡頭成對可比。
#   2. 驗證四個平台皆相容，且收到完全相同的提示詞與參考素材。
#   3. 驗證素材未就緒時 fail-closed，就緒後可產出 job packages。
#   4. 驗證 only_provider 不會退到其他平台。
#   5. 驗證評分表往返：產生、回填 variant_id、匯入 QC。
# 說明:
#   使用臨時 SQLite 與臨時 DATA_DIR，以位元組佔位檔充當參考素材，
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

_TMP_DIR = Path(tempfile.mkdtemp(prefix="benchmark_v1_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session, select

from pipeline.benchmark import builder, score_import, score_sheet, v1_pack
from pipeline.capability import (
    CapabilityRequest,
    check_compatibility,
    get_provider,
    model_registry,
    read_job_manifest,
    routing_policy,
)
from pipeline.db import engine, init_db
from pipeline.models.qc import ContinuityQC, VariantQC
from pipeline.models.variant import AssetVariant, CapabilityJob, VariantStatus
from pipeline.project_store import load_project
from pipeline.settings import get_settings
from pipeline.stages.shot_dispatcher import (
    ShotReadinessState,
    assess_project_readiness,
    build_shot_request,
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def verify_fixture() -> None:
    shots = v1_pack.shots()
    check(len(shots) == 6, f"應有 6 顆鏡頭，實際 {len(shots)}")
    check(len(v1_pack.SCENARIOS) == 3, "應涵蓋三類情境")

    covered = {v1_pack.SHOT_SCENARIOS[shot.shot_id] for shot in shots}
    declared = {item.scenario_id for item in v1_pack.SCENARIOS}
    check(covered == declared, f"情境涵蓋不完整: {covered} vs {declared}")

    for scenario in v1_pack.SCENARIOS:
        subset = v1_pack.shots_by_scenario(scenario.scenario_id)
        check(subset, f"情境 {scenario.scenario_id} 沒有對應鏡頭")

    # 對話情境需要反打配對，才能評估身份是否混淆
    dialogue = v1_pack.shots_by_scenario("two_character_dialogue")
    check(len(dialogue) >= 3, "對話情境應包含雙人鏡頭與正反打")
    ots = [s for s in dialogue if "ots" in s.shot_id]
    check(len(ots) == 2, "應有兩顆互為反打的過肩鏡頭")
    for shot in dialogue:
        check(
            len(shot.character_refs) == 2,
            f"{shot.shot_id} 對話鏡頭應含兩名角色",
        )

    # 跨鏡頭連戲配對必須指向存在的鏡頭
    shot_ids = {shot.shot_id for shot in shots}
    for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
        check(shot_id in shot_ids, f"連戲配對指向不存在的鏡頭 {shot_id}")
        check(ref_shot_id in shot_ids, f"連戲配對指向不存在的鏡頭 {ref_shot_id}")

    characters = {pack.character_id for pack in v1_pack.characters()}
    for shot in shots:
        for character_id in shot.character_refs:
            check(
                character_id in characters,
                f"{shot.shot_id} 引用未定義的角色 {character_id}",
            )
        check(shot.first_frame_ref, f"{shot.shot_id} 缺少首幀")
        check(
            shot.target_duration_ms == v1_pack.SHOT_DURATION_MS,
            f"{shot.shot_id} 片長應統一",
        )
        check(
            shot.aspect_ratio == v1_pack.ASPECT_RATIO,
            f"{shot.shot_id} 比例應統一",
        )
        check(
            shot.negative_prompt == v1_pack.NEGATIVE_PROMPT,
            f"{shot.shot_id} 應使用共用負面提示詞",
        )

    # 角色不得攜帶額外參考素材，否則會超出部分平台的上限
    for pack in v1_pack.characters():
        check(
            not pack.reference_asset_ids,
            f"{pack.character_id} 不應攜帶參考素材，V1 僅以首幀傳遞身份",
        )
        check(pack.identity_description, f"{pack.character_id} 缺少身份描述")


def verify_all_providers_compatible() -> None:
    """每顆鏡頭都必須能在四個平台生成，否則無法公平比較。"""
    from pipeline.models.production_profile import load_preset
    from pipeline.models.production_artifact import ProductionArtifact

    artifact = ProductionArtifact(
        project_id=v1_pack.BENCHMARK_PROJECT_ID,
        title="fixture",
        production_profile=load_preset(builder.BENCHMARK_PRESET),
        character_packs=v1_pack.characters(),
        shot_plans=v1_pack.shots(),
    )
    profile = artifact.production_profile
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    registry = model_registry()

    for shot in v1_pack.shots():
        request = build_shot_request(artifact, shot, packs, profile)
        check(
            len(request.visual.reference_asset_ids) == 0,
            f"{shot.shot_id} 不應有額外參考素材: "
            f"{request.visual.reference_asset_ids}",
        )
        for provider_id in v1_pack.TARGET_PROVIDERS:
            spec = get_provider(provider_id)
            check(spec is not None, f"平台 {provider_id} 未登錄")
            models = [
                entry
                for entry in registry.models_on(provider_id)
                if entry.supports(shot.capability)
            ]
            check(models, f"{provider_id} 無支援 {shot.capability.value} 的模型")
            problems = check_compatibility(request, models[0], spec)
            check(
                not problems,
                f"{shot.shot_id} 於 {provider_id} 不相容: {problems}",
            )


def verify_asset_gate() -> tuple[Path, list[Path]]:
    """素材未就緒時必須 fail-closed。"""
    settings = get_settings()
    report = builder.check_assets(settings)
    check(
        len(report.statuses) == len(v1_pack.REQUIRED_ASSETS),
        "素材清單數量不符",
    )
    check(not report.ready, "空目錄不應被視為就緒")
    check(report.instructions(), "應列出待準備清單")

    directory = Path(report.assets_dir)
    created: list[Path] = []
    for required in v1_pack.REQUIRED_ASSETS:
        path = directory / required.filename
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
        created.append(path)

    ready_report = builder.check_assets(settings)
    check(ready_report.ready, f"素材補齊後應就緒: {ready_report.instructions()}")
    return directory, created


def verify_build_and_dispatch() -> None:
    settings = get_settings()
    registered = builder.register_assets(settings)
    check(
        registered == len(v1_pack.REQUIRED_ASSETS),
        f"應登錄全部素材，實際 {registered}",
    )

    artifact = builder.build_artifact(settings)
    check(len(artifact.shot_plans) == 6, "專案應含 6 顆鏡頭")

    readiness = assess_project_readiness(artifact)
    not_ready = [
        shot_id
        for shot_id, item in readiness.items()
        if item.state is not ShotReadinessState.READY
    ]
    check(not not_ready, f"素材就緒後所有鏡頭應可派工: {not_ready}")

    reports = asyncio.run(builder.dispatch_all(artifact))
    check(
        set(reports) == set(v1_pack.TARGET_PROVIDERS),
        f"應對四個平台派工: {set(reports)}",
    )
    for provider, result in reports.items():
        check(
            result.dispatched_count == 6,
            f"{provider} 應派工 6 顆，實際 {result.dispatched_count}: "
            f"{[r.message for r in result.results if not r.dispatched]}",
        )
        for item in result.results:
            check(
                item.provider == provider,
                f"{item.shot_id} 應由 {provider} 承接，實際 {item.provider}",
            )

    counts = builder.jobs_summary()
    for provider in v1_pack.TARGET_PROVIDERS:
        check(
            counts.get(provider) == 6,
            f"{provider} 應有 6 筆派工，實際 {counts.get(provider)}",
        )

    index = builder.package_index(settings)
    check(
        set(index) == set(v1_pack.TARGET_PROVIDERS),
        f"job packages 應涵蓋四個平台: {set(index)}",
    )
    for provider, packages in index.items():
        check(len(packages) == 6, f"{provider} 應有 6 份 job package")


def verify_prompt_parity() -> None:
    """同一顆鏡頭送往不同平台的內容必須完全相同。"""
    settings = get_settings()
    index = builder.package_index(settings)

    by_shot: dict[str, dict[str, dict]] = {}
    for provider, packages in index.items():
        for package_dir in packages:
            manifest = read_job_manifest(Path(package_dir))
            by_shot.setdefault(manifest["shot_id"], {})[provider] = manifest

    check(len(by_shot) == 6, f"應涵蓋 6 顆鏡頭，實際 {len(by_shot)}")

    for shot_id, manifests in by_shot.items():
        check(
            set(manifests) == set(v1_pack.TARGET_PROVIDERS),
            f"{shot_id} 未涵蓋全部平台",
        )
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
                continue
            check(
                payload == baseline,
                f"{shot_id} 於 {provider} 的內容與其他平台不同，比較會失真",
            )

        # request_hash 跨平台必然不同，因為它涵蓋路由選定的 model_id。
        # 這是必要的：若四個平台共用同一個 hash，同一顆鏡頭送出四次會被
        # 冪等機制視為重複，只會留下一筆派工。
        hashes = {manifest["request_hash"] for manifest in manifests.values()}
        check(
            len(hashes) == len(manifests),
            f"{shot_id} 各平台的 request_hash 應互異，否則派工會被去重",
        )

        # 平台欄位名稱本就不同，這是允許的轉換
        param_keys = {
            provider: set(manifest["provider_parameters"])
            for provider, manifest in manifests.items()
        }
        check(
            len({frozenset(keys) for keys in param_keys.values()}) > 1,
            f"{shot_id} 各平台的參數欄位名稱應依 ProviderSpec 轉換",
        )


def verify_only_provider_does_not_fallback() -> None:
    """指定平台後不得退到其他平台。"""
    settings = get_settings()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    profile = artifact.production_profile
    packs = {pack.character_id: pack for pack in artifact.character_packs}
    shot = artifact.shot_plans[0]

    request = build_shot_request(artifact, shot, packs, profile)
    # 片長刻意超出所有平台上限，使該平台不相容
    impossible = request.model_copy(
        update={"visual": request.visual.model_copy(update={"duration_ms": 600_000})}
    )
    decision = routing_policy().resolve(impossible, profile=profile, only_provider="veo")
    check(not decision.ok, "不相容時不應有候選")
    check(
        all("veo" in reason for reason in decision.rejected),
        f"只應評估指定平台: {decision.rejected}",
    )

    ok_decision = routing_policy().resolve(request, profile=profile, only_provider="veo")
    check(ok_decision.ok, "相容時應有候選")
    check(
        all(c.provider.provider_id == "veo" for c in ok_decision.candidates),
        "候選不得包含其他平台",
    )


def verify_score_sheet_roundtrip() -> None:
    settings = get_settings()
    directory = Path(settings.data_dir) / "benchmark" / "v1" / "sheets"

    variant_path = score_sheet.write_variant_sheet(directory)
    continuity_path = score_sheet.write_continuity_sheet(directory)

    with variant_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    expected = 6 * len(v1_pack.TARGET_PROVIDERS) * v1_pack.CANDIDATES_PER_SHOT
    check(len(rows) == expected, f"評分表列數應為 {expected}，實際 {len(rows)}")
    check(
        all(row["facial_acting"] == "n/a"
            for row in rows
            if row["scenario"] != "two_character_dialogue"),
        "非對話情境的表情欄位應預設為 n/a",
    )

    with continuity_path.open(encoding="utf-8-sig") as handle:
        continuity_rows = list(csv.DictReader(handle))
    check(
        len(continuity_rows)
        == len(v1_pack.CONTINUITY_PAIRS) * len(v1_pack.TARGET_PROVIDERS),
        "連戲表列數不符",
    )

    # 模擬匯入兩個候選後回填
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    shot_a = artifact.shot_plans[0].shot_id
    shot_ref = v1_pack.CONTINUITY_PAIRS[0][1]
    with Session(engine) as session:
        for index, (shot_id, provider) in enumerate(
            [(shot_a, "kling"), (shot_ref, "kling")], start=1
        ):
            session.add(
                AssetVariant(
                    variant_id=f"var_bm_{index}",
                    project_id=v1_pack.BENCHMARK_PROJECT_ID,
                    shot_id=shot_id,
                    provider=provider,
                    prompt_snapshot="snapshot",
                    status=VariantStatus.IMPORTED.value,
                )
            )
        session.commit()

    synced_path, synced_rows = score_sheet.sync_variant_sheet(
        directory, v1_pack.BENCHMARK_PROJECT_ID
    )
    check(synced_rows == 2, f"應回填 2 列，實際 {synced_rows}")
    with synced_path.open(encoding="utf-8-sig") as handle:
        synced = list(csv.DictReader(handle))
    check(all(row["variant_id"] for row in synced), "回填後應有 variant_id")

    # 填分數後匯入
    for row in synced:
        row["identity_consistency"] = "82"
        row["temporal_stability"] = "78"
        row["prompt_adherence"] = "85"
        row["motion_quality"] = "70"
        row["camera_control"] = "75"
        row["artifact_severity"] = "12"
        row["usable_without_repair"] = "yes"
        row["retries_to_usable"] = "2"
        row["human_correction_minutes"] = "3.5"
        row["generation_seconds"] = "95"
    with synced_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_sheet.VARIANT_COLUMNS)
        writer.writeheader()
        writer.writerows(synced)

    result = score_import.import_variant_scores(artifact, synced_path)
    check(result.ok, f"匯入不應有錯誤: {result.errors}")
    check(result.applied == 2, f"應匯入 2 列，實際 {result.applied}")

    with Session(engine) as session:
        records = session.exec(
            select(VariantQC).where(
                VariantQC.project_id == v1_pack.BENCHMARK_PROJECT_ID
            )
        ).all()
    check(len(records) == 2, f"應建立 2 筆評分，實際 {len(records)}")
    record = records[0]
    check(record.identity_consistency == 82, "身份一致性未寫入")
    check(record.generation_seconds == 95, "生成耗時未寫入")
    check(record.usable_without_repair is True, "可用旗標未寫入")
    # 非對話鏡頭的表情欄位為 n/a，必須保持 None
    check(record.facial_acting is None, "n/a 不得被轉為 0 分")

    # 連戲評分匯入
    for row in continuity_rows:
        if row["shot_id"] == v1_pack.CONTINUITY_PAIRS[0][0] and row["provider"] == "kling":
            row["cross_shot_identity"] = "88"
            row["wardrobe_continuity"] = "90"
            row["location_continuity"] = "80"
    with continuity_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=score_sheet.CONTINUITY_COLUMNS)
        writer.writeheader()
        writer.writerows(continuity_rows)

    continuity_result = score_import.import_continuity_scores(artifact, continuity_path)
    check(continuity_result.ok, f"連戲匯入錯誤: {continuity_result.errors}")
    check(continuity_result.applied == 1, "應匯入 1 列連戲評分")

    with Session(engine) as session:
        continuity_records = session.exec(
            select(ContinuityQC).where(
                ContinuityQC.project_id == v1_pack.BENCHMARK_PROJECT_ID
            )
        ).all()
    check(len(continuity_records) == 1, "應建立 1 筆連戲評分")
    check(continuity_records[0].lip_sync_quality is None, "n/a 不得被轉為 0 分")


def main() -> int:
    init_db()
    verify_fixture()
    verify_all_providers_compatible()
    verify_asset_gate()
    verify_build_and_dispatch()
    verify_prompt_parity()
    verify_only_provider_does_not_fallback()
    verify_score_sheet_roundtrip()

    print(
        f"OK benchmark v1 smoke shots={len(v1_pack.shots())} "
        f"providers={len(v1_pack.TARGET_PROVIDERS)} "
        f"scenarios={len(v1_pack.SCENARIOS)} "
        f"generations={v1_pack.total_generations()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
