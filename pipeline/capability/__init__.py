# 檔案路徑: video-pipeline/pipeline/capability/__init__.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Capability Router 套件入口。
# 主要責任:
#   1. 對外導出請求契約、路由與轉接器登錄的公開介面。
# 架構分層:
#   CapabilityRequest -> ModelRegistry -> RoutingPolicy -> ProviderAdapter
#   model 與 provider 解耦，transport（manual / api）僅是最末層的差異。
# --------------------------------------------------------------------------

from pipeline.capability.base import (
    AttemptRecord,
    AudioPayload,
    CapabilityAdapter,
    CapabilityRequest,
    CapabilityResult,
    ChatPayload,
    VisualPayload,
    REQUEST_SCHEMA_VERSION,
)
from pipeline.capability.job_package import (
    JOB_SCHEMA_VERSION,
    JobPackage,
    build_job_package,
    read_job_manifest,
)
from pipeline.capability.model_registry import (
    ModelEntry,
    ModelRegistry,
    check_compatibility,
    load_model_registry,
    model_registry,
)
from pipeline.capability.provider_spec import (
    ProviderSpec,
    get_provider,
    load_provider_specs,
    provider_specs,
    providers_supporting,
)
from pipeline.capability.router import (
    clear_adapters,
    dispatch_capability,
    get_adapter,
    install_default_adapters,
    register_adapter,
    registered_adapters,
)
from pipeline.capability.routing import (
    RoutingCandidate,
    RoutingDecision,
    RoutingPolicy,
    RoutingRule,
    load_routing_rules,
    routing_policy,
)

__all__ = [
    "AttemptRecord",
    "AudioPayload",
    "CapabilityAdapter",
    "CapabilityRequest",
    "CapabilityResult",
    "ChatPayload",
    "JOB_SCHEMA_VERSION",
    "JobPackage",
    "ModelEntry",
    "ModelRegistry",
    "ProviderSpec",
    "REQUEST_SCHEMA_VERSION",
    "RoutingCandidate",
    "RoutingDecision",
    "RoutingPolicy",
    "RoutingRule",
    "VisualPayload",
    "build_job_package",
    "check_compatibility",
    "clear_adapters",
    "dispatch_capability",
    "get_adapter",
    "get_provider",
    "install_default_adapters",
    "load_model_registry",
    "load_provider_specs",
    "load_routing_rules",
    "model_registry",
    "provider_specs",
    "providers_supporting",
    "read_job_manifest",
    "register_adapter",
    "registered_adapters",
    "routing_policy",
]
