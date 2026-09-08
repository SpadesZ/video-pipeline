# 檔案路徑: video-pipeline/scripts/smoke_manual_provider.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   人工派工 transport 與 job manifest 確定性冒煙測試。
# 主要責任:
#   1. 驗證 job package 結構與 job.json 必要欄位齊全。
#   2. 驗證平台欄位差異全數由 ProviderSpec.parameter_mapping 表達。
#   3. 驗證 request_hash 冪等，重複派工不產生第二筆工作記錄。
#   4. 驗證參考素材被複製，缺漏時明確警告而非靜默通過。
#   5. 驗證派送回傳 pending_manual 且終止 fallback。
# 說明:
#   使用臨時 SQLite 與臨時目錄，不需要任何 API 金鑰，不呼叫外部服務。
# --------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="manual_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["SECRETS_FILE"] = str(_TMP_DIR / "missing.env")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session, select

from pipeline.capability import (
    CapabilityRequest,
    VisualPayload,
    build_job_package,
    dispatch_capability,
    get_provider,
    model_registry,
    read_job_manifest,
    registered_adapters,
    routing_policy,
)
from pipeline.capability.job_package import JOB_SCHEMA_VERSION
from pipeline.capability.transports.manual import install_manual_adapters
from pipeline.db import engine, init_db
from pipeline.models.capability import Capability
from pipeline.models.production_profile import load_preset
from pipeline.models.reference_asset import ReferenceAsset, ReferenceAssetType
from pipeline.models.variant import CapabilityJob, JobStatus, TransportKind

PACKAGE_ROOT = _TMP_DIR / "packages"

REQUIRED_JOB_FIELDS = (
    "schema_version",
    "request_id",
    "request_hash",
    "created_at",
    "capability",
    "provider",
    "transport",
    "model_id",
    "shot_id",
    "shot_plan_version",
    "profile_version",
    "provider_parameters",
    "request",
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def seed_reference_assets() -> Path:
    """建立一個實體參考素材檔並登錄至 reference_assets。"""
    assets_dir = _TMP_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    face = assets_dir / "face_lin.png"
    face.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    coat = assets_dir / "coat_lin.png"
    coat.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)

    with Session(engine) as session:
        session.add(
            ReferenceAsset(
                asset_id="ref_face_lin",
                project_id="smoke_manual",
                asset_type=ReferenceAssetType.FACE.value,
                local_path=str(face),
                file_hash="deadbeef",
            )
        )
        session.add(
            ReferenceAsset(
                asset_id="ref_coat_lin",
                project_id="smoke_manual",
                asset_type=ReferenceAssetType.WARDROBE.value,
                local_path=str(coat),
                file_hash="cafebabe",
            )
        )
        # 刻意登錄一筆沒有實體檔案的素材，用於驗證缺漏警告與 fail-closed
        session.add(
            ReferenceAsset(
                asset_id="ref_missing",
                project_id="smoke_manual",
                asset_type=ReferenceAssetType.WARDROBE.value,
                local_path=None,
            )
        )
        session.commit()
    return face


def make_request(
    shot_id: str = "shot_0001",
    refs: list[str] | None = None,
    prompt: str = "雨夜天橋遠景，女子獨自等待",
    duration_ms: int = 6000,
) -> CapabilityRequest:
    return CapabilityRequest(
        request_id=f"req_{shot_id}",
        capability=Capability.VIDEO_I2V,
        project_id="smoke_manual",
        shot_id=shot_id,
        shot_plan_version=1,
        profile_version=1,
        visual=VisualPayload(
            prompt=prompt,
            negative_prompt="模糊, 變形",
            first_frame_ref="ref_face_lin",
            reference_asset_ids=refs or [],
            duration_ms=duration_ms,
            aspect_ratio="9:16",
            camera="wide, static",
        ),
        parameters={"model_id": "kling-video"},
    )


def verify_package_structure() -> None:
    request = make_request()
    spec = get_provider("kling")
    model = model_registry().get("kling-video")
    package = build_job_package(
        request, spec, model, PACKAGE_ROOT,
        reference_paths={"ref_face_lin": Path(seeded_face)},
    )

    package_dir = Path(package.package_dir)
    check(package_dir.is_dir(), "job package 目錄未建立")
    # 目錄結構為 <shot_id>/<provider>/<request_hash 前綴>
    check(
        package.request_hash.startswith(package_dir.name),
        f"目錄應以 request_hash 前綴命名，實際 {package_dir.name}",
    )
    check(package_dir.parent.name == "kling", "上層目錄應以平台命名")
    check(package_dir.parent.parent.name == "shot_0001", "最上層應以 shot_id 分組")

    for filename in ("job.json", "prompt.txt", "README.md"):
        check((package_dir / filename).exists(), f"缺少 {filename}")
    check((package_dir / "refs").is_dir(), "缺少 refs 目錄")

    manifest = read_job_manifest(package_dir)
    for field in REQUIRED_JOB_FIELDS:
        check(field in manifest, f"job.json 缺少必要欄位 {field}")
    check(
        manifest["schema_version"] == JOB_SCHEMA_VERSION,
        "job.json schema_version 不符",
    )
    check(manifest["transport"] == TransportKind.MANUAL.value, "transport 應為 manual")
    check(manifest["shot_plan_version"] == 1, "shot_plan_version 未寫入")

    # request 欄位是完整的 CapabilityRequest，API transport 未來直接讀取
    restored = CapabilityRequest.model_validate(manifest["request"])
    check(
        restored.content_hash() == manifest["request_hash"],
        "job.json 的 request 與 request_hash 不一致",
    )
    check(restored.visual.prompt == request.visual.prompt, "prompt 未完整保存")

    prompt_text = (package_dir / "prompt.txt").read_text(encoding="utf-8")
    check(prompt_text.strip() == request.visual.prompt, "prompt.txt 內容不符")

    readme = (package_dir / "README.md").read_text(encoding="utf-8")
    check("操作步驟" in readme, "README 缺少人工操作步驟")
    check(spec.human_instructions.strip()[:10] in readme, "README 未帶入平台說明")
    check(manifest["request_hash"] in readme, "README 應標示 request_hash 供匯入比對")

    # 參考素材應被複製進 refs/
    copied = list((package_dir / "refs").iterdir())
    check(len(copied) == 1, f"應複製 1 個參考素材，實際 {len(copied)}")
    check(copied[0].name.startswith("ref_face_lin"), "參考素材檔名應保留 asset_id")
    check(not package.missing_references, "不應有缺漏素材")


def verify_parameter_mapping_is_data_driven() -> None:
    """相同請求送往不同平台，欄位名應依各自 spec 轉換。"""
    request = make_request(shot_id="shot_0002")
    registry = model_registry()

    kling_manifest = read_job_manifest(
        Path(
            build_job_package(
                request, get_provider("kling"), registry.get("kling-video"),
                PACKAGE_ROOT,
            ).package_dir
        )
    )
    runway_request = request.model_copy(
        update={"parameters": {"model_id": "runway-gen3"}}
    )
    runway_manifest = read_job_manifest(
        Path(
            build_job_package(
                runway_request, get_provider("runway"), registry.get("runway-gen3"),
                PACKAGE_ROOT,
            ).package_dir
        )
    )

    kling_params = kling_manifest["provider_parameters"]
    runway_params = runway_manifest["provider_parameters"]

    # Kling 的首幀欄位是 start_frame，Runway 是 input_image
    check("start_frame" in kling_params, f"Kling 首幀欄位未轉換: {kling_params}")
    check("input_image" in runway_params, f"Runway 首幀欄位未轉換: {runway_params}")
    check(
        "aspect_ratio" in kling_params and "ratio" in runway_params,
        "比例欄位未依各平台轉換",
    )
    check(
        kling_params != runway_params,
        "不同平台的參數應不同，否則 parameter_mapping 未生效",
    )
    # model_id 屬於路由決策，不應洩漏進平台參數
    check("model_id" not in kling_params, "model_id 不應出現在平台參數中")

    # 片長必須換算為平台介面的單位。內部恆為毫秒，直接輸出會讓人工填錯。
    check(
        kling_params["duration"] == 6,
        f"Kling 片長應換算為 6 秒，實際 {kling_params['duration']}",
    )
    check(
        runway_params["duration"] == 6,
        f"Runway 片長應換算為 6 秒，實際 {runway_params['duration']}",
    )

    veo_request = request.model_copy(update={"parameters": {"model_id": "veo-video"}})
    veo_params = read_job_manifest(
        Path(
            build_job_package(
                veo_request, get_provider("veo"), registry.get("veo-video"),
                PACKAGE_ROOT,
            ).package_dir
        )
    )["provider_parameters"]
    # 欄位名宣告為 duration_seconds，值就必須真的是秒
    check(
        veo_params["duration_seconds"] == 6,
        f"Veo 的 duration_seconds 必須是秒，實際 {veo_params['duration_seconds']}",
    )


def verify_missing_reference_warning() -> None:
    request = make_request(shot_id="shot_0003", refs=["ref_missing"])
    package = build_job_package(
        request, get_provider("kling"), model_registry().get("kling-video"),
        PACKAGE_ROOT,
        reference_paths={"ref_face_lin": Path(seeded_face)},
    )
    check("ref_missing" in package.missing_references, "未回報缺漏的參考素材")
    check(package.warnings, "缺漏素材時應產生警告")

    manifest = read_job_manifest(Path(package.package_dir))
    check(manifest["missing_references"] == ["ref_missing"], "job.json 未記錄缺漏素材")


def verify_hash_idempotency() -> None:
    a = make_request(shot_id="shot_0004")
    b = make_request(shot_id="shot_0004")
    c = make_request(shot_id="shot_0004", prompt="換了提示詞")
    check(a.content_hash() == b.content_hash(), "相同內容應得到相同 hash")
    check(a.content_hash() != c.content_hash(), "提示詞不同時 hash 必須改變")

    d = make_request(shot_id="shot_0004", duration_ms=9000)
    check(a.content_hash() != d.content_hash(), "片長不同時 hash 必須改變")


def verify_dispatch_and_job_record() -> None:
    installed = install_manual_adapters(output_root=PACKAGE_ROOT, record_job=True)
    check(installed, "未註冊任何人工 transport")
    providers = {provider for _cap, provider in installed}
    check(
        {"kling", "runway", "veo", "seedance"} <= providers,
        f"人工平台註冊不完整: {providers}",
    )

    request = make_request(shot_id="shot_0005")
    profile = load_preset("comic_drama_high")

    result = asyncio.run(dispatch_capability(request, profile=profile))
    check(result.awaiting_human, f"應回傳 pending_manual，實際 {result.status}")
    check(not result.ok, "pending_manual 不算成功")
    check(not result.is_terminal, "pending_manual 不是終態")
    check(result.job_id is not None, "應建立工作記錄")
    check(result.outputs, "應回傳 job package 路徑")
    check(Path(result.outputs[0]).is_dir(), "job package 路徑應存在")

    # 等待人工時不應繼續嘗試其他平台
    check(
        len(result.attempts) == 1,
        f"pending_manual 應終止 fallback，實際嘗試 {len(result.attempts)} 次",
    )

    with Session(engine) as session:
        jobs = session.exec(
            select(CapabilityJob).where(CapabilityJob.shot_id == "shot_0005")
        ).all()
    check(len(jobs) == 1, f"應有 1 筆工作記錄，實際 {len(jobs)}")
    job = jobs[0]
    check(job.status == JobStatus.PENDING_MANUAL, "工作狀態應為 pending_manual")
    check(job.transport == TransportKind.MANUAL, "transport 應為 manual")
    check(job.request_hash == request.content_hash(), "工作記錄未保存 request_hash")
    check(not job.is_terminal, "pending_manual 不是終態")

    # 重複派送同一份工作不應產生第二筆記錄
    again = asyncio.run(dispatch_capability(request, profile=profile))
    check(again.awaiting_human, "重複派送仍應回傳 pending_manual")
    check(again.job_id == job.job_id, "重複派送應沿用既有工作記錄")

    with Session(engine) as session:
        jobs_after = session.exec(
            select(CapabilityJob).where(CapabilityJob.shot_id == "shot_0005")
        ).all()
    check(
        len(jobs_after) == 1,
        f"重複派送不應新增記錄，實際 {len(jobs_after)} 筆",
    )


def verify_routing_selects_manual_provider() -> None:
    """人工 transport 應被路由納入候選，且順序來自政策。"""
    decision = routing_policy().resolve(
        make_request(shot_id="shot_0006"), profile=load_preset("comic_drama_high")
    )
    check(decision.ok, "漫劇情境應有可用候選")
    check(
        all(c.provider.transport is TransportKind.MANUAL for c in decision.candidates),
        "第一階段影片候選應全為人工 transport",
    )

    registered = dict.fromkeys(p for _c, p in registered_adapters())
    check("kling" in registered, "kling 轉接器未註冊")


def verify_shot_plan_dispatch() -> None:
    """ShotPlan 派工：角色身份素材必須被展開，否則 refs/ 會是空的。"""
    from pipeline.models.production_artifact import ProductionArtifact
    from pipeline.models.shot import (
        CameraSpec,
        CharacterIdentityPack,
        ShotFraming,
        ShotPlan,
    )
    from pipeline.stages.shot_dispatcher import (
        build_shot_request,
        collect_reference_ids,
        dispatch_project_shots,
    )

    pack = CharacterIdentityPack(
        character_id="char_lin",
        role="protagonist",
        canonical_face_ref="ref_face_lin",
        wardrobe_refs=["ref_coat_lin"],
    )
    shot = ShotPlan(
        shot_id="shot_0100",
        beat_id="beat_0001",
        scene_id="scene_0001",
        order=0,
        capability=Capability.VIDEO_I2V,
        camera=CameraSpec(framing=ShotFraming.CLOSE_UP),
        prompt="特寫，女子抬頭",
        character_refs=["char_lin"],
        # 6 秒且 2 個參考素材：kling 與 seedance 可接受，runway 與 veo 因
        # 參考素材上限為 1 會被相容性檢查排除。這是預期行為。
        target_duration_ms=6000,
        aspect_ratio="9:16",
    )

    # 角色身份素材必須併入請求，否則平台拿不到參考圖
    collected = collect_reference_ids(shot, {"char_lin": pack})
    check(
        collected == ["ref_face_lin", "ref_coat_lin"],
        f"角色參考素材未展開: {collected}",
    )
    check(
        collect_reference_ids(shot, {}) == [],
        "找不到角色定義時不應憑空產生素材 id",
    )

    profile = load_preset("comic_drama_high")
    artifact = ProductionArtifact(
        project_id="smoke_manual",
        title="dispatch test",
        production_profile=profile,
        character_packs=[pack],
        shot_plans=[shot],
    )

    request = build_shot_request(artifact, shot, {"char_lin": pack}, profile)
    check(request.shot_id == "shot_0100", "shot_id 未帶入請求")
    check(request.shot_plan_version == shot.version, "shot_plan_version 未帶入")
    check(
        request.visual.reference_asset_ids == ["ref_face_lin", "ref_coat_lin"],
        "請求未包含角色參考素材",
    )
    check("close up" in request.visual.camera, f"運鏡未描述: {request.visual.camera}")

    # 目標長度必須被 profile 的鏡頭長度政策夾住
    long_shot = shot.model_copy(update={"target_duration_ms": 60_000})
    clamped = build_shot_request(artifact, long_shot, {"char_lin": pack}, profile)
    check(
        clamped.visual.duration_ms == 6000,
        f"鏡頭長度未套用政策上限: {clamped.visual.duration_ms}",
    )

    # 參考素材數量超過平台上限時，該平台必須被排除而非硬送
    from pipeline.capability import routing_policy

    decision = routing_policy().resolve(request, profile=profile)
    check(decision.ok, "應仍有可用平台")
    selected = {c.provider.provider_id for c in decision.candidates}
    check(
        "runway" not in selected and "veo" not in selected,
        f"參考素材上限為 1 的平台不應入選: {selected}",
    )

    report = asyncio.run(dispatch_project_shots(artifact))
    check(len(report.results) == 1, f"應派工 1 顆鏡頭，實際 {len(report.results)}")
    result = report.results[0]
    check(
        result.status == JobStatus.PENDING_MANUAL.value,
        f"人工 transport 應回傳 pending_manual，實際 {result.status} "
        f"provider={result.provider} message={result.message}",
    )
    check(result.dispatched, "pending_manual 應計入已派工")
    check(report.failed_count == 0, f"不應有失敗: {report.summary()}")
    check(result.package_dir is not None, "應回傳 job package 路徑")

    # 派工必須留下稽核軌跡
    actions = [entry.action for entry in artifact.decision_log]
    check("shots_dispatched" in actions, f"未記錄派工事件: {actions}")

    manifest = read_job_manifest(Path(result.package_dir))
    check(
        manifest["request"]["visual"]["reference_asset_ids"]
        == ["ref_face_lin", "ref_coat_lin"],
        "job.json 未保存角色參考素材",
    )
    # 已登錄且有實體檔案的素材應被複製
    refs = list((Path(result.package_dir) / "refs").iterdir())
    check(
        any(item.name.startswith("ref_face_lin") for item in refs),
        f"角色臉部參考未複製進 refs/: {[i.name for i in refs]}",
    )

    # 派工當下的規格必須記錄於工作上，供匯入時比對落差
    with Session(engine) as session:
        job = session.get(CapabilityJob, result.job_id)
    check(job is not None, "應建立工作記錄")
    check(
        job.requested_duration_ms == 6000,
        f"工作應記錄實際送出的片長，實際 {job.requested_duration_ms}",
    )
    check(job.requested_aspect_ratio == "9:16", "工作應記錄實際送出的比例")


def verify_manifest_not_overwritten() -> None:
    """同一鏡頭同一平台的第二次派工不得覆寫前一份 manifest。"""
    spec = get_provider("kling")
    model = model_registry().get("kling-video")

    first = make_request(shot_id="shot_rev", prompt="第一版提示詞")
    second = make_request(shot_id="shot_rev", prompt="第二版提示詞")
    check(
        first.content_hash() != second.content_hash(),
        "內容不同時 request_hash 必須不同",
    )

    package_a = build_job_package(first, spec, model, PACKAGE_ROOT)
    package_b = build_job_package(second, spec, model, PACKAGE_ROOT)

    check(
        package_a.package_dir != package_b.package_dir,
        "不同 request 應落在不同目錄，否則舊 manifest 會被覆寫",
    )
    check(Path(package_a.job_path).exists(), "第一份 manifest 應仍存在")
    check(Path(package_b.job_path).exists(), "第二份 manifest 應存在")

    manifest_a = read_job_manifest(Path(package_a.package_dir))
    manifest_b = read_job_manifest(Path(package_b.package_dir))
    check(
        manifest_a["request"]["visual"]["prompt"] == "第一版提示詞",
        "第一份 manifest 的內容被覆寫了",
    )
    check(
        manifest_b["request"]["visual"]["prompt"] == "第二版提示詞",
        "第二份 manifest 內容錯誤",
    )
    check(
        manifest_a["request_hash"] != manifest_b["request_hash"],
        "兩份 manifest 的 request_hash 應不同",
    )

    # 相同內容重複產生則應落在同一目錄，維持冪等
    package_again = build_job_package(first, spec, model, PACKAGE_ROOT)
    check(
        package_again.package_dir == package_a.package_dir,
        "相同內容應維持冪等，落在同一目錄",
    )


def verify_job_snapshot_is_frozen() -> None:
    """派工後修改 ShotPlan，工作上的快照不得跟著變。"""
    from pipeline.models.production_artifact import ProductionArtifact
    from pipeline.models.shot import CameraSpec, CharacterIdentityPack, ShotPlan
    from pipeline.stages.shot_dispatcher import dispatch_project_shots

    pack = CharacterIdentityPack(
        character_id="char_lin", canonical_face_ref="ref_face_lin"
    )
    original = ShotPlan(
        shot_id="shot_frozen", beat_id="b", scene_id="s", order=0,
        capability=Capability.VIDEO_I2V, camera=CameraSpec(),
        prompt="派工當下的提示詞", negative_prompt="原始負面詞",
        character_refs=["char_lin"], target_duration_ms=6000,
        aspect_ratio="9:16",
    )
    artifact = ProductionArtifact(
        project_id="smoke_manual", title="frozen",
        production_profile=load_preset("comic_drama_high"),
        character_packs=[pack], shot_plans=[original],
    )

    report = asyncio.run(dispatch_project_shots(artifact))
    result = report.results[0]
    check(result.dispatched, f"應成功派工: {result.message}")

    with Session(engine) as session:
        job = session.get(CapabilityJob, result.job_id)
    check(job is not None, "應建立工作記錄")
    check(job.request_snapshot, "工作應保存請求快照")
    check(
        job.request_snapshot["visual"]["prompt"] == "派工當下的提示詞",
        "快照未保存派工當下的提示詞",
    )
    check(
        job.reference_asset_ids == ["ref_face_lin"],
        f"快照未保存參考素材: {job.reference_asset_ids}",
    )
    check(job.provider_parameters, "快照應保存平台參數")
    check(job.manifest_path and Path(job.manifest_path).exists(), "應記錄 manifest 路徑")

    # 分鏡在派工後被改寫
    artifact.shot_plans = [
        original.model_copy(
            update={
                "prompt": "改寫後的提示詞",
                "negative_prompt": "改寫後的負面詞",
                "character_refs": [],
            }
        )
    ]

    with Session(engine) as session:
        job_after = session.get(CapabilityJob, result.job_id)
    check(
        job_after.request_snapshot["visual"]["prompt"] == "派工當下的提示詞",
        "分鏡改寫後，工作快照不得跟著變動",
    )
    check(
        job_after.reference_asset_ids == ["ref_face_lin"],
        "分鏡改寫後，快照的參考素材不得變動",
    )


def verify_readiness_fail_closed() -> None:
    """完備度不足的鏡頭不得以「已派工」的外觀通過。"""
    from pipeline.models.production_artifact import ProductionArtifact
    from pipeline.models.shot import CharacterIdentityPack, ShotPlan
    from pipeline.stages.shot_dispatcher import (
        NOT_DISPATCHED,
        ShotReadinessState,
        assess_project_readiness,
        check_shot_readiness,
        dispatch_project_shots,
    )

    pack = CharacterIdentityPack(
        character_id="char_lin",
        canonical_face_ref="ref_face_lin",
        wardrobe_refs=["ref_missing"],  # 已登錄但沒有實體檔案
    )
    base = dict(
        beat_id="beat_0001", scene_id="scene_0001", order=0,
        capability=Capability.VIDEO_I2V, target_duration_ms=6000,
        aspect_ratio="9:16",
    )

    incomplete_shot = ShotPlan(
        shot_id="shot_incomplete", prompt="有效提示詞",
        character_refs=["char_lin"], **base
    )
    unknown_character = ShotPlan(
        shot_id="shot_unknown", prompt="有效提示詞",
        character_refs=["char_ghost"], **base
    )
    empty_prompt = ShotPlan(shot_id="shot_empty", prompt="   ", **base)

    packs = {"char_lin": pack}
    check(
        check_shot_readiness(incomplete_shot, packs).state
        is ShotReadinessState.INCOMPLETE,
        "參考素材檔案缺失應判定為 incomplete",
    )
    blocked = check_shot_readiness(unknown_character, packs)
    check(
        blocked.state is ShotReadinessState.BLOCKED,
        "找不到角色身份定義應判定為 blocked",
    )
    check(
        any("角色身份定義" in issue for issue in blocked.issues),
        f"blocked 原因應說明缺少角色定義: {blocked.issues}",
    )
    check(
        check_shot_readiness(empty_prompt, packs).state is ShotReadinessState.BLOCKED,
        "缺少 prompt 應判定為 blocked",
    )

    artifact = ProductionArtifact(
        project_id="smoke_manual",
        title="readiness",
        production_profile=load_preset("comic_drama_high"),
        character_packs=[pack],
        shot_plans=[incomplete_shot, unknown_character, empty_prompt],
    )

    states = assess_project_readiness(artifact)
    check(len(states) == 3, "應評估全部鏡頭的完備度")

    # 預設 fail-closed：三顆都不應被派工
    report = asyncio.run(dispatch_project_shots(artifact))
    check(len(report.results) == 3, "報告應涵蓋全部鏡頭")
    check(report.dispatched_count == 0, "完備度不足時不應派工")
    check(report.blocked_count == 3, f"三顆都應被擋下: {report.summary()}")
    for item in report.results:
        check(
            item.status == NOT_DISPATCHED,
            f"{item.shot_id} 不應顯示為已派工狀態: {item.status}",
        )
        check(item.message, f"{item.shot_id} 應說明未派工原因")

    # 明確允許時，incomplete 可派但 blocked 仍不可
    forced = asyncio.run(dispatch_project_shots(artifact, allow_incomplete=True))
    by_shot = {item.shot_id: item for item in forced.results}
    check(
        by_shot["shot_incomplete"].dispatched,
        "明確允許後 incomplete 應可派工",
    )
    check(
        by_shot["shot_incomplete"].readiness is ShotReadinessState.INCOMPLETE,
        "派工後仍應保留 incomplete 標記",
    )
    check(
        not by_shot["shot_unknown"].dispatched,
        "blocked 即使明確允許也不得派工",
    )
    check(not by_shot["shot_empty"].dispatched, "缺 prompt 者不得派工")


def main() -> int:
    init_db()
    global seeded_face
    seeded_face = seed_reference_assets()

    verify_package_structure()
    verify_parameter_mapping_is_data_driven()
    verify_missing_reference_warning()
    verify_hash_idempotency()
    verify_routing_selects_manual_provider()
    verify_dispatch_and_job_record()
    verify_shot_plan_dispatch()
    verify_manifest_not_overwritten()
    verify_job_snapshot_is_frozen()
    verify_readiness_fail_closed()

    # 結構為 <shot_id>/<provider>/<request_hash>/job.json
    packages = list(PACKAGE_ROOT.glob("*/*/*/job.json"))
    print(
        f"OK manual provider smoke packages={len(packages)} "
        f"adapters={len(registered_adapters())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
