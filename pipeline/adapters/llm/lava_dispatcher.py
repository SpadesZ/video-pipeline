# 檔案路徑: video-pipeline/pipeline/adapters/llm/lava_dispatcher.py
# 產生時間: 2026-09-04 +08:00
# 版本: v2.0
# 模組定位:
#   LAVA 文字任務派送的相容層。
# 主要責任:
#   1. 將 task_id 與其 binding 轉換為 CapabilityRequest，交由 Capability Router。
#   2. 將 CapabilityResult 轉回既有回傳格式，使呼叫端不需修改。
#   3. re-export chat 低階函式，供 lava_verifier 沿用。
# 說明:
#   派送邏輯已移至 pipeline/capability/。原先以 provider 名稱硬編碼的
#   if-else 分支改為 (capability, provider) 查表，新增能力不必再擴充分支。
#   本模組維持既有介面：dispatch_llm_task 的參數與回傳鍵值不變，
#   pipeline/stages/llm_executors.py 的六個任務執行器一行未改。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from pipeline.adapters.llm.lava_settings import binding_for_task, default_connections
from pipeline.adapters.llm.task_registry import TASK_IDS
from pipeline.capability.adapters.chat import (  # noqa: F401 - 供 lava_verifier 沿用
    google_chat,
    google_chat_with_retry,
    is_retryable_error,
    openai_compatible_chat,
    openai_compatible_chat_with_retry,
    safe_error,
)
from pipeline.capability.base import CapabilityRequest, ChatPayload
from pipeline.capability.router import dispatch_capability
from pipeline.models.capability import Capability
from pipeline.secrets import load_runtime_secrets

logger = logging.getLogger("LAVA_Dispatcher")
logger.setLevel(logging.INFO)

# 轉接器回報的狀態碼對應至既有 fallback_order 的狀態字串
_STATUS_ALIASES = {"missing_key": "missing_key", "no_adapter": "inactive"}


def _connection_lookup() -> dict[str, str]:
    """provider -> connection_id。既有 UI 以 connection 為單位呈現。"""
    return {conn.provider: conn.connection_id for conn in default_connections()}


def _legacy_fallback_order(result, providers: dict[str, str]) -> list[dict]:
    order: list[dict] = []
    for attempt in result.attempts:
        connection_id = providers.get(attempt.provider, attempt.provider)
        status = "attempted"
        if attempt.error and "未設定" in attempt.error:
            status = "missing_key"
        status = _STATUS_ALIASES.get(attempt.status, status)
        entry = {"connection_id": connection_id, "status": status}
        if attempt.model_id:
            entry["model"] = attempt.model_id
        order.append(entry)
    return order


async def dispatch_llm_task(
    task_id: str,
    messages: list[dict],
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> dict:
    load_runtime_secrets()
    if task_id not in TASK_IDS:
        return {"ok": False, "error": f"Unknown video LLM task: {task_id}"}

    binding = binding_for_task(task_id)
    connections = default_connections()
    providers = {conn.provider: conn.connection_id for conn in connections}

    primary_connection = None
    if binding:
        primary_connection = next(
            (c for c in connections if c.connection_id == binding.connection_id), None
        )
    preferred_provider = primary_connection.provider if primary_connection else None

    parameters: dict = {}
    if binding and binding.model_id:
        parameters["model_id"] = binding.model_id

    request = CapabilityRequest(
        request_id=f"llm_{task_id}",
        capability=Capability.TEXT_REASONING,
        chat=ChatPayload(
            messages=messages, temperature=temperature, max_tokens=max_tokens
        ),
        parameters=parameters,
    )

    result = await dispatch_capability(
        request, preferred_provider=preferred_provider
    )
    fallback_order = _legacy_fallback_order(result, providers)

    if result.ok:
        connection_id = providers.get(result.provider, result.provider)
        if preferred_provider and result.provider != preferred_provider:
            logger.info(
                f"Fallback active: Task '{task_id}' fell back from "
                f"{binding.connection_id if binding else None} to {connection_id}"
            )
        return {
            "ok": True,
            "content": result.content,
            "provider": result.provider,
            "model": result.model_id,
            "connection_id": connection_id,
            "fallback_order": fallback_order,
        }

    last_error = result.error_message or "Unknown error"
    for attempt in reversed(result.attempts):
        if attempt.error:
            last_error = attempt.error
            break

    return {
        "ok": False,
        "error": (
            f"All LLM connections failed for task '{task_id}'. "
            f"Last error: {last_error}"
        ),
        "fallback_order": fallback_order,
    }
