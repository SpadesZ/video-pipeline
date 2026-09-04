# 檔案路徑: video-pipeline/pipeline/capability/adapters/chat.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   文字推理能力的轉接器與其 HTTP 實作。
# 主要責任:
#   1. 提供 OpenAI 相容與 Google Generative Language 的呼叫與重試。
#   2. 以 CapabilityAdapter 契約包裝上述呼叫。
#   3. 遮蔽錯誤訊息中的 API 金鑰。
# 說明:
#   HTTP 實作原先位於 pipeline/adapters/llm/lava_dispatcher.py，行為逐字
#   保留後搬移至此，使依賴方向由 capability 層向外，而非新架構反向依賴
#   舊模組。lava_dispatcher 仍 re-export 這些函式，lava_verifier 不需改動。
# --------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from pipeline.capability.base import CapabilityRequest, CapabilityResult
from pipeline.capability.provider_spec import get_provider
from pipeline.models.capability import Capability
from pipeline.models.variant import JobStatus

logger = logging.getLogger("LAVA_Dispatcher")
logger.setLevel(logging.INFO)

GOOGLE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


def safe_error(error: Exception, api_key: str | None) -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[REDACTED_API_KEY]")
    return message[:500]


def is_retryable_error(err_msg: str) -> bool:
    err_msg_lower = err_msg.lower()
    return (
        "429" in err_msg
        or "408" in err_msg
        or "502" in err_msg
        or "503" in err_msg
        or "504" in err_msg
        or "too many requests" in err_msg_lower
        or "timeout" in err_msg_lower
        or "connection" in err_msg_lower
        or "connecterror" in err_msg_lower
    )


# --------------------------------------------------------------------------
# HTTP 實作
# --------------------------------------------------------------------------

async def openai_compatible_chat(
    base_url: str,
    api_key: str,
    model_id: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> dict:
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
        data = response.json()
        if "error" in data:
            return {"ok": False, "error": f"OpenRouter API error: {data['error']}"}
        choices = data.get("choices", [])
        if not choices:
            return {"ok": False, "error": f"OpenRouter returned no choices. Response: {data}"}
        message = choices[0].get("message")
        if not message or "content" not in message:
            return {
                "ok": False,
                "error": f"OpenRouter returned empty message choices. Response: {data}",
            }
        return {
            "ok": True,
            "content": message["content"],
            "provider": "openrouter",
            "model": model_id,
        }
    except Exception as error:
        return {"ok": False, "error": safe_error(error, api_key)}


async def openai_compatible_chat_with_retry(
    base_url: str,
    api_key: str,
    model_id: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    max_retries: int = 3,
    initial_delay: float = 2.0,
) -> dict:
    delay = initial_delay
    for attempt in range(max_retries + 1):
        res = await openai_compatible_chat(
            base_url, api_key, model_id, messages, temperature, max_tokens
        )
        if res.get("ok"):
            return res

        err_msg = str(res.get("error", ""))
        if is_retryable_error(err_msg) and attempt < max_retries:
            logger.warning(
                f"OpenRouter rate limit or temporary error. "
                f"Retrying in {delay}s (attempt {attempt + 1}/{max_retries})..."
            )
            await asyncio.sleep(delay)
            delay *= 2.0
        else:
            return res
    return res


async def google_chat(
    api_key: str,
    model_id: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> dict:
    system_instruction = None
    contents = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system_instruction = {"parts": [{"text": content}]}
        else:
            g_role = "user" if role == "user" else "model"
            contents.append({"role": g_role, "parts": [{"text": content}]})

    # contents 必須以 user 起始
    if contents and contents[0]["role"] == "model":
        contents[0]["role"] = "user"

    merged_contents = []
    for msg in contents:
        if merged_contents and merged_contents[-1]["role"] == msg["role"]:
            merged_contents[-1]["parts"][0]["text"] += "\n" + msg["parts"][0]["text"]
        else:
            merged_contents.append(msg)

    payload = {
        "contents": merged_contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{GOOGLE_ENDPOINT}/{model_id}:generateContent?key={api_key}",
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return {
                "ok": False,
                "error": f"Google Gemini returned no candidates. Response: {data}",
            }
        candidate = candidates[0]
        content_obj = candidate.get("content")
        if not content_obj:
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            return {
                "ok": False,
                "error": f"Google Gemini response has no content. Finish reason: {finish_reason}",
            }
        parts = content_obj.get("parts", [])
        content = "\n".join(
            str(part.get("text", "")) for part in parts if part.get("text")
        ).strip()
        return {"ok": True, "content": content, "provider": "google", "model": model_id}
    except Exception as error:
        return {"ok": False, "error": safe_error(error, api_key)}


async def google_chat_with_retry(
    api_key: str,
    model_id: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    max_retries: int = 3,
    initial_delay: float = 2.0,
) -> dict:
    delay = initial_delay
    for attempt in range(max_retries + 1):
        res = await google_chat(api_key, model_id, messages, temperature, max_tokens)
        if res.get("ok"):
            return res

        err_msg = str(res.get("error", ""))
        if is_retryable_error(err_msg) and attempt < max_retries:
            logger.warning(
                f"Google Gemini rate limit or temporary error. "
                f"Retrying in {delay}s (attempt {attempt + 1}/{max_retries})..."
            )
            await asyncio.sleep(delay)
            delay *= 2.0
        else:
            return res
    return res


# --------------------------------------------------------------------------
# CapabilityAdapter 實作
# --------------------------------------------------------------------------

class _ChatAdapterBase:
    capability = Capability.TEXT_REASONING
    provider = ""
    default_model_env = ""
    fallback_model = ""

    def resolve_model(self, request: CapabilityRequest) -> str:
        explicit = request.parameters.get("model_id")
        if explicit:
            return str(explicit)
        return os.getenv(self.default_model_env, self.fallback_model)

    def missing_key_result(self, env_name: str) -> CapabilityResult:
        return CapabilityResult(
            ok=False,
            status=JobStatus.FAILED,
            provider=self.provider,
            error_code="missing_key",
            error_message=f"環境變數 {env_name} 未設定",
        )

    def to_result(self, raw: dict, model_id: str) -> CapabilityResult:
        if raw.get("ok"):
            return CapabilityResult(
                ok=True,
                status=JobStatus.COMPLETED,
                provider=self.provider,
                model_id=model_id,
                content=raw.get("content"),
            )
        return CapabilityResult(
            ok=False,
            status=JobStatus.FAILED,
            provider=self.provider,
            model_id=model_id,
            error_code="provider_error",
            error_message=str(raw.get("error", "unknown error")),
        )


class OpenRouterChatAdapter(_ChatAdapterBase):
    provider = "openrouter"
    default_model_env = "OPENROUTER_MODEL_ID"
    fallback_model = "openai/gpt-4o-mini"

    async def execute(self, request: CapabilityRequest) -> CapabilityResult:
        spec = get_provider(self.provider)
        env_name = (spec.api_key_env if spec else None) or "OPENROUTER_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            return self.missing_key_result(env_name)

        chat = request.chat
        if chat is None:
            return CapabilityResult.failure(
                "text_reasoning 請求缺少 chat payload", "invalid_request"
            )

        model_id = self.resolve_model(request)
        base_url = (spec.base_url if spec else None) or "https://openrouter.ai/api/v1"
        raw = await openai_compatible_chat_with_retry(
            base_url, api_key, model_id, chat.messages, chat.temperature, chat.max_tokens
        )
        return self.to_result(raw, model_id)


class GoogleChatAdapter(_ChatAdapterBase):
    provider = "google"
    default_model_env = "GOOGLE_MODEL_ID"
    fallback_model = "gemini-2.0-flash"

    async def execute(self, request: CapabilityRequest) -> CapabilityResult:
        spec = get_provider(self.provider)
        env_name = (spec.api_key_env if spec else None) or "GOOGLE_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            return self.missing_key_result(env_name)

        chat = request.chat
        if chat is None:
            return CapabilityResult.failure(
                "text_reasoning 請求缺少 chat payload", "invalid_request"
            )

        model_id = self.resolve_model(request)
        raw = await google_chat_with_retry(
            api_key, model_id, chat.messages, chat.temperature, chat.max_tokens
        )
        return self.to_result(raw, model_id)
