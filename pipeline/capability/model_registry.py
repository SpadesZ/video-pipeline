# 檔案路徑: video-pipeline/pipeline/capability/model_registry.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   模型登錄表 ModelRegistry。
# 主要責任:
#   1. 以 model 為單位描述能力、輸入型態、限制與計價。
#   2. 以 hosted_by 表達模型與平台的多對多關係。
#   3. 提供請求對模型限制的相容性檢查。
# 說明:
#   刻意將 model 與 provider 解耦。一個平台會託管多個模型，同一個模型也
#   可能被不同平台託管，若把兩者綁在一起，日後換平台就得改動路由邏輯。
#   benchmark_stats 由 E6 依情境回填，供 RoutingPolicy 參考。
# --------------------------------------------------------------------------

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pipeline.capability.base import CapabilityRequest
from pipeline.capability.provider_spec import ProviderSpec
from pipeline.models.capability import Capability

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "models.yaml"


class ModelEntry(BaseModel):
    model_id: str
    label: str = ""
    model_version: str | None = None

    capabilities: list[Capability] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)
    hosted_by: list[str] = Field(default_factory=list)

    max_reference_assets: int | None = None
    min_duration_ms: int | None = None
    max_duration_ms: int | None = None
    aspect_ratios: list[str] = Field(default_factory=list)

    credits_per_generation: float | None = None
    # 由 E6 Benchmark 依 scenario 回填，形如 {scenario: {metric: value}}
    benchmark_stats: dict = Field(default_factory=dict)
    notes: str | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def hosted_on(self, provider_id: str) -> bool:
        return provider_id in self.hosted_by


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry]) -> None:
        self._entries: dict[str, ModelEntry] = {}
        for entry in entries:
            if entry.model_id in self._entries:
                raise ValueError(f"model_id 重複: {entry.model_id}")
            self._entries[entry.model_id] = entry

    def __len__(self) -> int:
        return len(self._entries)

    def all(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def get(self, model_id: str) -> ModelEntry | None:
        return self._entries.get(model_id)

    def models_for(self, capability: Capability) -> list[ModelEntry]:
        return [entry for entry in self._entries.values() if entry.supports(capability)]

    def models_on(self, provider_id: str) -> list[ModelEntry]:
        return [
            entry for entry in self._entries.values() if entry.hosted_on(provider_id)
        ]

    def providers_for(self, model_id: str) -> list[str]:
        entry = self.get(model_id)
        return list(entry.hosted_by) if entry else []


def load_model_registry(catalog_path: Path | None = None) -> ModelRegistry:
    path = catalog_path or CATALOG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = [ModelEntry.model_validate(item) for item in data.get("models", [])]
    return ModelRegistry(entries)


@lru_cache(maxsize=1)
def model_registry() -> ModelRegistry:
    return load_model_registry()


def check_compatibility(
    request: CapabilityRequest,
    model: ModelEntry,
    provider: ProviderSpec,
) -> list[str]:
    """回傳請求違反模型或平台限制的原因清單。空清單代表相容。"""
    problems: list[str] = []

    if not model.supports(request.capability):
        problems.append(f"模型 {model.model_id} 不支援 {request.capability.value}")
    if not provider.supports(request.capability):
        problems.append(
            f"平台 {provider.provider_id} 不支援 {request.capability.value}"
        )
    if not model.hosted_on(provider.provider_id):
        problems.append(
            f"模型 {model.model_id} 未由平台 {provider.provider_id} 託管"
        )

    visual = request.visual
    if visual is not None:
        duration = visual.duration_ms
        violation = provider.duration_limits.violation(duration)
        if violation:
            problems.append(f"{provider.provider_id}: {violation}")
        if duration is not None:
            if model.min_duration_ms is not None and duration < model.min_duration_ms:
                problems.append(
                    f"{model.model_id}: duration {duration}ms 低於下限 {model.min_duration_ms}ms"
                )
            if model.max_duration_ms is not None and duration > model.max_duration_ms:
                problems.append(
                    f"{model.model_id}: duration {duration}ms 超過上限 {model.max_duration_ms}ms"
                )

        ratio = visual.aspect_ratio
        if ratio:
            if model.aspect_ratios and ratio not in model.aspect_ratios:
                problems.append(f"{model.model_id}: 不支援比例 {ratio}")
            if (
                provider.supported_aspect_ratios
                and ratio not in provider.supported_aspect_ratios
            ):
                problems.append(f"{provider.provider_id}: 不支援比例 {ratio}")

        ref_count = len(visual.reference_asset_ids)
        for limit, owner in (
            (model.max_reference_assets, model.model_id),
            (provider.max_reference_assets, provider.provider_id),
        ):
            if limit is not None and ref_count > limit:
                problems.append(f"{owner}: 參考素材 {ref_count} 個超過上限 {limit}")

    return problems
