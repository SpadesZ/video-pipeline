# 檔案路徑: video-pipeline/pipeline/capability/routing.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   路由政策 RoutingPolicy，決定一項請求交由哪個模型與平台執行。
# 主要責任:
#   1. 依能力、製作政策與情境挑選模型與平台。
#   2. 過濾不相容的組合，並保留候選順序作為 fallback。
# 重要約束:
#   規則只能比對 ProductionProfile 的政策欄位（quality_tier、motion_policy、
#   aspect_ratio 等），不得比對 preset_id。preset 只是政策的預設值組合，
#   一旦用於分支，漫劇、短劇與電影就會各自長出一條路由路徑。
# --------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pipeline.capability.base import CapabilityRequest
from pipeline.capability.model_registry import (
    ModelEntry,
    ModelRegistry,
    check_compatibility,
    model_registry,
)
from pipeline.capability.provider_spec import ProviderSpec, provider_specs
from pipeline.models.capability import Capability
from pipeline.models.production_profile import ProductionProfile

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "routing.yaml"


class RoutingRule(BaseModel):
    rule_id: str
    capability: Capability
    priority: int = 0

    # 匹配條件，皆為政策欄位。None 表示不限制。
    quality_tier: str | None = None
    motion_policy: str | None = None
    aspect_ratio: str | None = None
    scenario_type: str | None = None

    # 依序偏好的模型
    prefer_models: list[str] = Field(default_factory=list)

    def matches(
        self,
        capability: Capability,
        profile: ProductionProfile | None,
        scenario_type: str | None,
    ) -> bool:
        if self.capability is not capability:
            return False
        if self.scenario_type is not None and self.scenario_type != scenario_type:
            return False
        if profile is None:
            # 未指定 profile 時，只有無政策條件的規則可套用
            return (
                self.quality_tier is None
                and self.motion_policy is None
                and self.aspect_ratio is None
            )
        if self.quality_tier is not None and self.quality_tier != profile.quality_tier:
            return False
        if (
            self.motion_policy is not None
            and self.motion_policy != profile.motion_policy
        ):
            return False
        if self.aspect_ratio is not None and self.aspect_ratio != profile.aspect_ratio:
            return False
        return True


@dataclass(frozen=True)
class RoutingCandidate:
    model: ModelEntry
    provider: ProviderSpec
    rule_id: str | None = None


@dataclass
class RoutingDecision:
    candidates: list[RoutingCandidate] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> RoutingCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def ok(self) -> bool:
        return bool(self.candidates)


class RoutingPolicy:
    def __init__(
        self,
        rules: list[RoutingRule],
        registry: ModelRegistry | None = None,
        providers: dict[str, ProviderSpec] | None = None,
    ) -> None:
        self.rules = sorted(rules, key=lambda r: -r.priority)
        self._registry = registry or model_registry()
        self._providers = providers or provider_specs()

    def resolve(
        self,
        request: CapabilityRequest,
        profile: ProductionProfile | None = None,
        scenario_type: str | None = None,
        preferred_provider: str | None = None,
        only_provider: str | None = None,
    ) -> RoutingDecision:
        """解析可用的模型與平台候選。

        preferred_provider 只調整順序，不相容時仍會退到其他平台。
        only_provider 則完全排除其他平台：benchmark 需要確保某顆鏡頭
        確實由指定平台生成，一旦退而求其次，比較資料就失去意義。
        """
        decision = RoutingDecision()
        seen: set[tuple[str, str]] = set()

        for model, rule_id in self._ordered_models(
            request.capability, profile, scenario_type
        ):
            for provider_id in model.hosted_by:
                if only_provider and provider_id != only_provider:
                    continue
                provider = self._providers.get(provider_id)
                if provider is None:
                    decision.rejected.append(
                        f"{model.model_id}@{provider_id}: 平台未登錄"
                    )
                    continue
                key = (model.model_id, provider_id)
                if key in seen:
                    continue
                seen.add(key)

                problems = check_compatibility(request, model, provider)
                if problems:
                    decision.rejected.append(
                        f"{model.model_id}@{provider_id}: {'; '.join(problems)}"
                    )
                    continue
                decision.candidates.append(
                    RoutingCandidate(model=model, provider=provider, rule_id=rule_id)
                )

        if preferred_provider:
            decision.candidates.sort(
                key=lambda c: 0 if c.provider.provider_id == preferred_provider else 1
            )
        return decision

    def _ordered_models(
        self,
        capability: Capability,
        profile: ProductionProfile | None,
        scenario_type: str | None,
    ) -> list[tuple[ModelEntry, str | None]]:
        ordered: list[tuple[ModelEntry, str | None]] = []
        chosen: set[str] = set()

        for rule in self.rules:
            if not rule.matches(capability, profile, scenario_type):
                continue
            for model_id in rule.prefer_models:
                if model_id in chosen:
                    continue
                entry = self._registry.get(model_id)
                if entry is None or not entry.supports(capability):
                    continue
                ordered.append((entry, rule.rule_id))
                chosen.add(model_id)

        # 規則未涵蓋的模型仍可作為後備，順序依 catalog
        for entry in self._registry.models_for(capability):
            if entry.model_id not in chosen:
                ordered.append((entry, None))
                chosen.add(entry.model_id)

        return ordered


def load_routing_rules(catalog_path: Path | None = None) -> list[RoutingRule]:
    path = catalog_path or CATALOG_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [RoutingRule.model_validate(item) for item in data.get("rules", [])]


@lru_cache(maxsize=1)
def routing_policy() -> RoutingPolicy:
    return RoutingPolicy(load_routing_rules())
