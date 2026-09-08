# 檔案路徑: video-pipeline/pipeline/benchmark/builder.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark 專案建立與 job package 產出。
# 主要責任:
#   1. 檢查人工需準備的素材是否就緒，未就緒時列出清單而非靜默產出。
#   2. 以固定 fixture 建立 benchmark 專案。
#   3. 對每個目標平台各自產出 job packages。
# 說明:
#   每個平台以 only_provider 嚴格指定，不允許退到其他平台。
#   若某平台因規格限制無法生成某顆鏡頭，必須明確顯示為未派工，
#   而不是悄悄由別的平台頂替，否則比較資料會失真。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.benchmark import v1_pack
from pipeline.benchmark.asset_validation import validate_image
from pipeline.benchmark.target import BenchmarkTarget, targets, validate_target
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import load_preset
from pipeline.models.reference_asset import ReferenceAsset, ReferenceRights
from pipeline.settings import Settings
from pipeline.stages.shot_dispatcher import ShotDispatchReport, dispatch_project_shots

logger = logging.getLogger("benchmark.builder")

ASSETS_SUBDIR = Path("benchmark") / "v1" / "assets"
BENCHMARK_PRESET = "comic_drama_high"


class AssetStatus(BaseModel):
    asset_id: str
    filename: str
    description: str
    present: bool
    valid: bool = False
    problems: list[str] = Field(default_factory=list)
    file_hash: str | None = None
    dimensions: str | None = None
    ratio: str | None = None
    require_vertical: bool = True


class AssetReport(BaseModel):
    assets_dir: str
    statuses: list[AssetStatus] = Field(default_factory=list)

    @property
    def missing(self) -> list[AssetStatus]:
        return [item for item in self.statuses if not item.present]

    @property
    def invalid(self) -> list[AssetStatus]:
        return [item for item in self.statuses if item.present and not item.valid]

    @property
    def ready(self) -> bool:
        return not self.missing and not self.invalid

    def instructions(self) -> list[str]:
        lines = [f"{item.filename}  <-  {item.description}" for item in self.missing]
        lines.extend(
            f"{item.filename}  !!  {'; '.join(item.problems)}"
            for item in self.invalid
        )
        return lines


def assets_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / ASSETS_SUBDIR


def check_assets(settings: Settings) -> AssetReport:
    """檢查素材目錄。實際解碼驗證，只回報狀態，不建立任何資料。

    僅檢查檔案大小不足以擋下壞圖：PNG 檔頭加隨機位元組會通過大小檢查，
    卻在上傳平台時失敗；比例錯誤的首幀會讓四個平台各自裁切，結果無法比較。
    """
    from pipeline.capability.job_package import file_sha256

    directory = assets_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    report = AssetReport(assets_dir=str(directory))

    for required in v1_pack.REQUIRED_ASSETS:
        path = directory / required.filename
        present = path.exists() and path.stat().st_size > 0
        status = AssetStatus(
            asset_id=required.asset_id,
            filename=required.filename,
            description=required.description,
            present=present,
            require_vertical=required.require_vertical,
        )
        if present:
            check = validate_image(path, require_vertical=required.require_vertical)
            status.valid = check.ok
            status.problems = list(check.problems)
            status.dimensions = check.dimensions
            status.ratio = check.ratio_text
            if check.ok:
                status.file_hash = file_sha256(path)
        report.statuses.append(status)

    return report


def asset_label(description: str) -> str:
    """素材說明的短標題。

    直接截斷長說明會切在句子中間，變成「角色 A 的視覺設定圖。短髮、
    深色長風衣的女性，：尚未上傳」這種讀不通的句子。改成取第一個
    句號或冒號之前的部分，那正好是這批說明的自然標題。
    """
    text = (description or "").strip()
    # 兩種寫法都有：「角色 A 的視覺設定圖。短髮…」以句號分隔，
    # 「A1 首幀：角色 A 站在…」以冒號分隔。取最短的那個開頭，
    # 才不會在冒號式的說明上切出整句話。
    heads = [
        head
        for separator in ("。", "：")
        if (head := text.split(separator, 1)[0]) != text and head
    ]
    return (min(heads, key=len) if heads else text)[:30]


def required_asset(asset_id: str):
    """依 asset_id 取得素材規格。未登錄的 id 一律拒絕。"""
    found = next(
        (item for item in v1_pack.REQUIRED_ASSETS if item.asset_id == asset_id), None
    )
    if found is None:
        raise ValueError(f"未登錄的素材欄位: {asset_id}")
    return found


class AssetUploadResult(BaseModel):
    ok: bool = False
    asset_id: str
    filename: str = ""
    problems: list[str] = Field(default_factory=list)
    file_hash: str | None = None
    dimensions: str | None = None
    ratio: str | None = None


def save_asset_upload(
    settings: Settings, asset_id: str, upload_name: str, payload: bytes
) -> AssetUploadResult:
    """驗證通過才寫入素材目錄。

    先落到暫存檔驗證再搬移，不直接覆寫目標路徑：若使用者不小心傳了
    橫式圖或壞檔，原本已經通過驗證的那張不該因此消失。上傳失敗後
    畫面仍應顯示舊圖，而不是變成空欄位。
    """
    from pipeline.capability.job_package import file_sha256

    required = required_asset(asset_id)
    result = AssetUploadResult(asset_id=asset_id, filename=required.filename)

    if not payload:
        result.problems.append("檔案為空")
        return result

    directory = assets_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".staging_{asset_id}{Path(upload_name).suffix.lower()}"
    try:
        staging.write_bytes(payload)
        check = validate_image(staging, require_vertical=required.require_vertical)
        result.dimensions = check.dimensions
        result.ratio = check.ratio_text
        if not check.ok:
            result.problems = list(check.problems)
            return result

        destination = directory / required.filename
        staging.replace(destination)
        result.ok = True
        result.file_hash = file_sha256(destination)
    finally:
        if staging.exists():
            staging.unlink()
    return result


def asset_path(settings: Settings, asset_id: str) -> Path:
    """素材的實際位置。僅供後端讀取，不對使用者顯示。"""
    return assets_dir(settings) / required_asset(asset_id).filename


class BuildBlocker(BaseModel):
    """一個阻擋正式派工的原因。code 供 UI 與 AI 助手辨識，不做字串比對。"""

    code: str
    message: str
    target_id: str | None = None
    fix_link: str | None = None


class BuildGate(BaseModel):
    """正式 Build 的前置條件。任一項不成立即不得產生 job packages。"""

    blockers: list[BuildBlocker] = Field(default_factory=list)
    assets_ready: bool = False
    confirmed_target_ids: list[str] = Field(default_factory=list)
    provisional_target_ids: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def codes(self) -> list[str]:
        seen: list[str] = []
        for item in self.blockers:
            if item.code not in seen:
                seen.append(item.code)
        return seen

    def summary(self) -> str:
        return "; ".join(item.message for item in self.blockers[:5])


def evaluate_build_gate(
    settings: Settings, report: AssetReport | None = None
) -> BuildGate:
    """判斷是否可以建立正式 job packages。

    三個條件必須同時成立：素材就緒、所有比較對象都已由真人確認版本、
    且每個對象都通過 catalog 驗證。缺任何一項就產生派工，等於用不確定的
    模型身份跑完整輪比較——事後無從得知當時實際用的是哪一版，
    整輪資料只能作廢重跑。因此這裡 fail closed，只警告是不夠的。
    """
    gate = BuildGate()
    report = report if report is not None else check_assets(settings)
    gate.assets_ready = report.ready

    if not report.ready:
        for item in [*report.missing, *report.invalid]:
            gate.blockers.append(
                BuildBlocker(
                    code="asset_not_ready",
                    message=(
                        f"{asset_label(item.description)}："
                        + ("; ".join(item.problems) if item.problems else "尚未上傳")
                    ),
                    fix_link="/benchmark/assets",
                )
            )

    registry = targets()
    capabilities = {shot.capability for shot in v1_pack.shots()}
    for item in registry.targets:
        if item.provisional:
            gate.provisional_target_ids.append(item.target_id)
            gate.blockers.append(
                BuildBlocker(
                    code="target_provisional",
                    target_id=item.target_id,
                    message=f"{item.target_id} 的平台實際版本尚未確認",
                    fix_link=f"/benchmark/targets#{item.target_id}",
                )
            )
        else:
            gate.confirmed_target_ids.append(item.target_id)

        for capability in sorted(capabilities, key=lambda value: value.value):
            try:
                validate_target(item, capability)
            except Exception as error:  # noqa: BLE001 - 驗證失敗種類不一
                gate.blockers.append(
                    BuildBlocker(
                        code="target_invalid",
                        target_id=item.target_id,
                        message=str(error)[:200],
                        fix_link=f"/benchmark/targets#{item.target_id}",
                    )
                )
                break

    return gate


def register_assets(settings: Settings) -> int:
    """將已驗證的素材登錄至 reference_assets。回傳登錄筆數。

    file_hash 每次都以檔案內容重算並覆寫：素材可能在登錄後被替換，
    沿用舊值會讓換圖後的請求得到與換圖前相同的識別。
    """
    from pipeline.capability.job_package import file_sha256
    from pipeline.db import engine

    directory = assets_dir(settings)
    registered = 0
    with Session(engine) as session:
        for required in v1_pack.REQUIRED_ASSETS:
            path = directory / required.filename
            if not path.exists():
                continue
            check = validate_image(path, require_vertical=required.require_vertical)
            if not check.ok:
                logger.warning(
                    "跳過未通過驗證的素材 %s: %s",
                    required.filename,
                    "; ".join(check.problems),
                )
                continue

            digest = file_sha256(path)
            existing = session.get(ReferenceAsset, required.asset_id)
            if existing is None:
                session.add(
                    ReferenceAsset(
                        asset_id=required.asset_id,
                        project_id=v1_pack.BENCHMARK_PROJECT_ID,
                        asset_type=required.asset_type,
                        label=required.description[:80],
                        local_path=str(path),
                        file_hash=digest,
                        rights=ReferenceRights.APPROVED.value,
                        source="benchmark_v1_manual",
                    )
                )
            else:
                existing.local_path = str(path)
                existing.file_hash = digest
                session.add(existing)
            registered += 1
        session.commit()
    return registered


def build_artifact(settings: Settings) -> ProductionArtifact:
    """建立或更新 benchmark 專案。fixture 為唯一來源，每次重建都相同。"""
    from pipeline.project_store import load_project, save_project

    existing = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    artifact = existing or ProductionArtifact(
        project_id=v1_pack.BENCHMARK_PROJECT_ID,
        title=f"Benchmark Pack {v1_pack.BENCHMARK_PACK_VERSION}",
        language="en",
    )
    artifact.production_profile = load_preset(BENCHMARK_PRESET)
    artifact.narrative_ir = v1_pack.narrative()
    artifact.character_packs = v1_pack.characters()
    artifact.shot_plans = v1_pack.shots()
    artifact.touch()
    save_project(settings, artifact)
    return artifact


async def dispatch_target(
    artifact: ProductionArtifact, target: BenchmarkTarget
) -> ShotDispatchReport:
    """對單一比較對象產出全部鏡頭的 job packages。

    target 身份以底線前綴寫入 request.parameters，因此會進入
    job manifest 與工作快照，但不會混入交給平台的參數欄位。
    """
    # 派工前確認 target 對應到 catalog 中真實可用的模型。
    # 只貼標籤而不驗證，標籤與實際生成的模型可能不同。
    for shot in artifact.shot_plans:
        validate_target(target, shot.capability)

    return await dispatch_project_shots(
        artifact,
        only_provider=target.provider,
        only_model_id=target.model_id,
        preferred_provider=target.provider,
        extra_parameters={
            "_bm_target_id": target.target_id,
            "_bm_model_version": target.model_version or "",
            "_bm_ui_label": target.ui_label,
            "_bm_provisional": target.provisional,
        },
    )


async def dispatch_all(
    artifact: ProductionArtifact, target_ids: tuple[str, ...] | None = None
) -> dict[str, ShotDispatchReport]:
    registry = targets()
    selected = (
        [registry.by_id(item) for item in target_ids]
        if target_ids
        else list(registry.targets)
    )
    missing = [
        target_id
        for target_id, target in zip(target_ids or (), selected)
        if target is None
    ]
    if missing:
        raise ValueError(f"未登錄的 benchmark target: {', '.join(missing)}")

    reports: dict[str, ShotDispatchReport] = {}
    for target in selected:
        reports[target.target_id] = await dispatch_target(artifact, target)
    return reports


def package_index(settings: Settings) -> dict[str, list[str]]:
    """列出已產出的 job package，依平台分組。"""
    root = (
        Path(settings.data_dir)
        / "projects"
        / v1_pack.BENCHMARK_PROJECT_ID
        / "job_packages"
    )
    index: dict[str, list[str]] = {}
    if not root.is_dir():
        return index
    for job_file in sorted(root.glob("*/*/*/job.json")):
        provider = job_file.parent.parent.name
        index.setdefault(provider, []).append(str(job_file.parent))
    return index


def jobs_summary(project_id: str = v1_pack.BENCHMARK_PROJECT_ID) -> dict[str, int]:
    """依平台統計已建立的派工數。"""
    from pipeline.db import engine
    from pipeline.models.variant import CapabilityJob

    counts: dict[str, int] = {}
    with Session(engine) as session:
        for job in session.exec(
            select(CapabilityJob).where(CapabilityJob.project_id == project_id)
        ).all():
            counts[job.provider] = counts.get(job.provider, 0) + 1
    return counts
