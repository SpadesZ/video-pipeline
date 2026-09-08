import os

from pipeline.adapters.llm.lava_dispatcher import google_chat, openai_compatible_chat
from pipeline.adapters.llm.lava_settings import connection_by_id
from pipeline.models.llm_control import LLMCapability


def sanitize_provider_error(message: object, api_key: str | None = None) -> str:
    text = str(message)
    if api_key:
        text = text.replace(api_key, "[REDACTED_API_KEY]")
    for env_key in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY"):
        value = os.getenv(env_key)
        if value:
            text = text.replace(value, "[REDACTED_API_KEY]")
    return text[:500]


async def verify_connection(connection_id: str, capability: str = "chat") -> dict:
    normalized_capability = str(capability or "").strip().lower()
    if normalized_capability != LLMCapability.CHAT:
        return {
            "ok": False,
            "connection_id": connection_id,
            "capability": normalized_capability,
            "error": "Only chat verification is supported for video LAVA tasks.",
        }

    connection = connection_by_id(connection_id)
    if not connection:
        return {"ok": False, "connection_id": connection_id, "error": "Unknown LAVA connection id."}

    api_key = os.getenv(connection.api_key_env)
    if not api_key:
        return {
            "ok": False,
            "connection_id": connection.connection_id,
            "provider": connection.provider,
            "model": connection.model_id,
            "configured": False,
            "error": f"Missing runtime env key {connection.api_key_env}.",
        }

    messages = [{"role": "user", "content": "Reply OK in 5 words or less."}]
    if connection.provider == "openrouter":
        result = await openai_compatible_chat(
            connection.base_url or "",
            api_key,
            connection.model_id,
            messages,
            temperature=0,
            max_tokens=20,
        )
    elif connection.provider == "google":
        result = await google_chat(
            api_key,
            connection.model_id,
            messages,
            temperature=0,
            max_tokens=20,
        )
    else:
        result = {"ok": False, "error": f"Unsupported provider: {connection.provider}"}

    if result.get("ok"):
        return {
            "ok": True,
            "connection_id": connection.connection_id,
            "provider": connection.provider,
            "model": result.get("model", connection.model_id),
            "configured": True,
            "reply": str(result.get("content", ""))[:120],
        }

    return {
        "ok": False,
        "connection_id": connection.connection_id,
        "provider": connection.provider,
        "model": connection.model_id,
        "configured": True,
        "error": sanitize_provider_error(result.get("error", "Unknown verification error"), api_key),
    }
