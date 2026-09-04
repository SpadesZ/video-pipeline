# 檔案路徑: video-pipeline/pipeline/capability/job_package.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   人工派工用的 job package 產生器。
# 主要責任:
#   1. 依 CapabilityRequest 與 ProviderSpec 產出 machine-readable 的 job.json。
#   2. 產出人工操作所需的 prompt.txt、refs/ 與 README.md。
#   3. 以 request_hash 提供冪等鍵，阻擋同一份工作被重複匯入。
# 說明:
#   job.json 是唯一事實來源，其 request 欄位即 CapabilityRequest 的序列化。
#   未來 API transport 直接吃同一份 request model，因此人工與 API 兩條路徑
#   共用契約，替換時上層不需改寫。
#   平台欄位差異一律由 ProviderSpec.parameter_mapping 表達，本模組不得
#   出現任何平台名稱的分支。
# --------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.capability.base import CapabilityRequest
from pipeline.capability.model_registry import ModelEntry
from pipeline.capability.provider_spec import ProviderSpec

JOB_SCHEMA_VERSION = "1.0"
JOB_FILENAME = "job.json"
PROMPT_FILENAME = "prompt.txt"
README_FILENAME = "README.md"
REFS_DIRNAME = "refs"


class JobPackage(BaseModel):
    """已產出的 job package 位置與識別資訊。"""

    request_id: str
    request_hash: str
    capability: str
    provider: str
    model_id: str
    shot_id: str | None = None

    package_dir: str
    job_path: str
    prompt_path: str
    readme_path: str
    reference_paths: list[str] = Field(default_factory=list)

    missing_references: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_reference_ids(request: CapabilityRequest) -> list[str]:
    ids: list[str] = []
    visual = request.visual
    if visual is not None:
        if visual.first_frame_ref:
            ids.append(visual.first_frame_ref)
        ids.extend(visual.reference_asset_ids)
    audio = request.audio
    if audio is not None and audio.voice_ref:
        ids.append(audio.voice_ref)

    seen: set[str] = set()
    ordered: list[str] = []
    for asset_id in ids:
        if asset_id in seen:
            continue
        seen.add(asset_id)
        ordered.append(asset_id)
    return ordered


def _provider_parameters(
    request: CapabilityRequest, provider: ProviderSpec
) -> dict:
    """將請求內容轉換為平台欄位名。轉換規則來自 ProviderSpec，非程式分支。"""
    values: dict = {}
    visual = request.visual
    if visual is not None:
        if visual.negative_prompt:
            values["negative_prompt"] = visual.negative_prompt
        if visual.first_frame_ref:
            values["first_frame_ref"] = visual.first_frame_ref
        if visual.duration_ms is not None:
            # 內部恆為毫秒，輸出時依平台介面單位換算
            values["duration_ms"] = provider.duration_unit.from_ms(visual.duration_ms)
        if visual.aspect_ratio:
            values["aspect_ratio"] = visual.aspect_ratio
        if visual.camera:
            values["camera"] = visual.camera
    audio = request.audio
    if audio is not None:
        if audio.duration_ms is not None:
            values["duration_ms"] = provider.duration_unit.from_ms(audio.duration_ms)
        if audio.style:
            values["style"] = audio.style

    values.update(request.parameters)
    values.pop("model_id", None)
    return provider.map_parameters(values)


def _missing_required_fields(
    request: CapabilityRequest, provider: ProviderSpec
) -> list[str]:
    visual = request.visual
    audio = request.audio
    present: dict[str, object] = {}
    if visual is not None:
        present = {
            "prompt": visual.prompt,
            "negative_prompt": visual.negative_prompt,
            "aspect_ratio": visual.aspect_ratio,
            "duration_ms": visual.duration_ms,
            "first_frame_ref": visual.first_frame_ref,
        }
    elif audio is not None:
        present = {"text": audio.text, "duration_ms": audio.duration_ms}
    elif request.chat is not None:
        present = {"messages": request.chat.messages}

    return [field for field in provider.required_fields if not present.get(field)]


def _prompt_text(request: CapabilityRequest) -> str:
    visual = request.visual
    if visual is not None:
        return visual.prompt
    audio = request.audio
    if audio is not None:
        return audio.text
    chat = request.chat
    if chat is not None:
        return "\n\n".join(
            f"[{message.get('role', 'user')}]\n{message.get('content', '')}"
            for message in chat.messages
        )
    return ""


def _readme_text(
    request: CapabilityRequest,
    provider: ProviderSpec,
    model: ModelEntry,
    parameters: dict,
    reference_names: list[str],
    missing_required: list[str],
) -> str:
    lines = [
        f"# {provider.label} - {request.capability.value}",
        "",
        f"- shot_id: {request.shot_id or '(未指定)'}",
        f"- model: {model.model_id}",
        f"- request_id: {request.request_id}",
        f"- request_hash: {request.content_hash()}",
        "",
        "## 平台參數",
        "",
        f"> 本平台片長以「{provider.duration_unit.value}」為單位，下列數值已換算。",
        "",
    ]
    if parameters:
        lines.extend(f"- {key}: {value}" for key, value in sorted(parameters.items()))
    else:
        lines.append("- (無)")

    lines.extend(["", "## 參考素材", ""])
    if reference_names:
        lines.extend(f"- {REFS_DIRNAME}/{name}" for name in reference_names)
    else:
        lines.append("- (無)")

    if missing_required:
        lines.extend(
            [
                "",
                "## 注意",
                "",
                f"- 缺少本平台必填欄位: {', '.join(missing_required)}",
            ]
        )

    instructions = provider.human_instructions.strip()
    if instructions:
        lines.extend(["", "## 操作步驟", "", instructions])

    lines.extend(
        [
            "",
            "## 匯回",
            "",
            "- 下載產出的影片檔，連同平台回報的實際片長一併匯回系統。",
            "- 同一鏡頭若生成多次，每次都需匯入。重試次數是評估模型的指標之一。",
            "- 匯入時請提供本目錄的 request_hash，用於比對工作來源。",
            "",
        ]
    )
    return "\n".join(lines)


def build_job_package(
    request: CapabilityRequest,
    provider: ProviderSpec,
    model: ModelEntry,
    output_root: Path,
    reference_paths: dict[str, Path] | None = None,
) -> JobPackage:
    """產出單一 shot 於單一平台的 job package。

    output_root 下的目錄結構為 <shot_id>/<provider_id>/。
    重複產生同一份工作時會覆寫，但 request_hash 不變，可用於偵測重複匯入。
    """
    resolved_refs = reference_paths or {}
    shot_segment = request.shot_id or request.request_id
    package_dir = output_root / shot_segment / provider.provider_id
    refs_dir = package_dir / REFS_DIRNAME
    package_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    missing_references: list[str] = []
    copied_names: list[str] = []
    copied_paths: list[str] = []

    for asset_id in _collect_reference_ids(request):
        source = resolved_refs.get(asset_id)
        if source is None or not Path(source).exists():
            missing_references.append(asset_id)
            continue
        source_path = Path(source)
        target = refs_dir / f"{asset_id}{source_path.suffix}"
        shutil.copyfile(source_path, target)
        copied_names.append(target.name)
        copied_paths.append(str(target))

    if missing_references:
        warnings.append(
            f"缺少參考素材檔案: {', '.join(missing_references)}"
        )

    missing_required = _missing_required_fields(request, provider)
    if missing_required:
        warnings.append(f"缺少平台必填欄位: {', '.join(missing_required)}")

    parameters = _provider_parameters(request, provider)
    request_hash = request.content_hash()

    manifest = {
        "schema_version": JOB_SCHEMA_VERSION,
        "request_id": request.request_id,
        "request_hash": request_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "capability": request.capability.value,
        "provider": provider.provider_id,
        "transport": provider.transport.value,
        "model_id": model.model_id,
        "model_version": model.model_version,
        "project_id": request.project_id,
        "shot_id": request.shot_id,
        "shot_plan_version": request.shot_plan_version,
        "profile_version": request.profile_version,
        "provider_parameters": parameters,
        "reference_files": copied_names,
        "missing_references": missing_references,
        "warnings": warnings,
        # 完整請求，API transport 未來直接讀取此欄位
        "request": request.model_dump(mode="json"),
    }

    job_path = package_dir / JOB_FILENAME
    job_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    prompt_path = package_dir / PROMPT_FILENAME
    prompt_path.write_text(_prompt_text(request), encoding="utf-8")

    readme_path = package_dir / README_FILENAME
    readme_path.write_text(
        _readme_text(
            request, provider, model, parameters, copied_names, missing_required
        ),
        encoding="utf-8",
    )

    return JobPackage(
        request_id=request.request_id,
        request_hash=request_hash,
        capability=request.capability.value,
        provider=provider.provider_id,
        model_id=model.model_id,
        shot_id=request.shot_id,
        package_dir=str(package_dir),
        job_path=str(job_path),
        prompt_path=str(prompt_path),
        readme_path=str(readme_path),
        reference_paths=copied_paths,
        missing_references=missing_references,
        warnings=warnings,
    )


def read_job_manifest(package_dir: Path) -> dict:
    return json.loads((Path(package_dir) / JOB_FILENAME).read_text(encoding="utf-8"))
