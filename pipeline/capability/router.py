# 檔案路徑: video-pipeline/pipeline/capability/router.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Capability Router 的轉接器登錄與派送。
# 主要責任:
#   1. 以 (capability, provider) 查表取得轉接器，取代硬編碼的 if-else 分支。
#   2. 依 RoutingPolicy 的候選順序嘗試，並保留 fallback 軌跡。
# 說明:
#   pending_manual 與成功同樣視為終止條件。manual transport 產出 job
#   package 後即回傳 pending_manual 等待人工完成，此時不應繼續嘗試其他
#   平台，否則同一顆鏡頭會被重複派工。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from pipeline.capability.base import (
    AttemptRecord,
    CapabilityAdapter,
    CapabilityRequest,
    CapabilityResult,
)
from pipeline.capability.routing import RoutingPolicy, routing_policy
from pipeline.models.capability import Capability
from pipeline.models.production_profile import ProductionProfile
from pipeline.models.variant import JobStatus

logger = logging.getLogger("capability.router")

_ADAPTERS: dict[tuple[Capability, str], CapabilityAdapter] = {}


def register_adapter(adapter: CapabilityAdapter) -> None:
    _ADAPTERS[(adapter.capability, adapter.provider)] = adapter


def get_adapter(capability: Capability, provider: str) -> CapabilityAdapter | None:
    return _ADAPTERS.get((capability, provider))


def registered_adapters() -> list[tuple[Capability, str]]:
    return sorted(_ADAPTERS, key=lambda key: (key[0].value, key[1]))


def clear_adapters() -> None:
    """僅供測試使用，重設登錄表。"""
    _ADAPTERS.clear()


def _install_default_adapters() -> None:
    from pipeline.capability.adapters.chat import (
        GoogleChatAdapter,
        OpenRouterChatAdapter,
    )

    register_adapter(OpenRouterChatAdapter())
    register_adapter(GoogleChatAdapter())


async def dispatch_capability(
    request: CapabilityRequest,
    profile: ProductionProfile | None = None,
    scenario_type: str | None = None,
    preferred_provider: str | None = None,
    policy: RoutingPolicy | None = None,
) -> CapabilityResult:
    active_policy = policy or routing_policy()
    decision = active_policy.resolve(
        request,
        profile=profile,
        scenario_type=scenario_type,
        preferred_provider=preferred_provider,
    )

    attempts: list[AttemptRecord] = []

    if not decision.ok:
        reason = "; ".join(decision.rejected) or "無可用模型"
        return CapabilityResult(
            ok=False,
            status=JobStatus.FAILED,
            error_code="no_route",
            error_message=f"{request.capability.value} 無可用路由: {reason}",
            attempts=attempts,
        )

    for candidate in decision.candidates:
        provider_id = candidate.provider.provider_id
        adapter = get_adapter(request.capability, provider_id)
        if adapter is None:
            attempts.append(
                AttemptRecord(
                    provider=provider_id,
                    model_id=candidate.model.model_id,
                    status="no_adapter",
                    error=f"未登錄 {request.capability.value}@{provider_id} 轉接器",
                )
            )
            continue

        # 以 setdefault 傳遞路由選定的模型：呼叫端明確指定的 model_id
        # 優先於路由決策，例如 LAVA 設定頁的每任務模型覆寫。
        scoped_parameters = {**request.parameters}
        scoped_parameters.setdefault("model_id", candidate.model.model_id)
        scoped = request.model_copy(update={"parameters": scoped_parameters})

        try:
            result = await adapter.execute(scoped)
        except Exception as error:  # noqa: BLE001 - 單一轉接器失敗不應中斷整體派送
            attempts.append(
                AttemptRecord(
                    provider=provider_id,
                    model_id=candidate.model.model_id,
                    status="exception",
                    error=str(error)[:300],
                )
            )
            continue

        attempts.append(
            AttemptRecord(
                provider=provider_id,
                model_id=candidate.model.model_id,
                status=result.status.value,
                error=result.error_message,
            )
        )

        # 成功或等待人工皆為終止條件
        if result.ok or result.awaiting_human:
            if len(attempts) > 1:
                logger.info(
                    "Fallback active: %s resolved by %s after %d attempts",
                    request.capability.value,
                    provider_id,
                    len(attempts),
                )
            result.attempts = attempts
            result.provider = result.provider or provider_id
            result.model_id = result.model_id or candidate.model.model_id
            return result

    last_error = next(
        (a.error for a in reversed(attempts) if a.error), "所有候選皆失敗"
    )
    return CapabilityResult(
        ok=False,
        status=JobStatus.FAILED,
        error_code="all_candidates_failed",
        error_message=f"{request.capability.value} 全部候選失敗: {last_error}",
        attempts=attempts,
    )


_install_default_adapters()
