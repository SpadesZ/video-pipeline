import os
import asyncio
import httpx
import logging

from pipeline.adapters.llm.lava_settings import binding_for_task, default_connections
from pipeline.adapters.llm.task_registry import TASK_IDS
from pipeline.secrets import load_runtime_secrets

logger = logging.getLogger("LAVA_Dispatcher")
logger.setLevel(logging.INFO)


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
    primary_conn_id = binding.connection_id if binding else None
    model_override = binding.model_id if binding else None
    
    connections = default_connections()
    
    # Arrange connections to try the primary one first, then fallback to others
    trial_conns = []
    if primary_conn_id:
        primary_conn = next((c for c in connections if c.connection_id == primary_conn_id), None)
        if primary_conn:
            trial_conns.append(primary_conn)
            
    for c in connections:
        if c not in trial_conns:
            trial_conns.append(c)
            
    last_error = None
    attempted_connections = []
    for connection in trial_conns:
        if not connection.is_active:
            attempted_connections.append({"connection_id": connection.connection_id, "status": "inactive"})
            continue
        api_key = os.getenv(connection.api_key_env)
        if not api_key:
            attempted_connections.append({"connection_id": connection.connection_id, "status": "missing_key"})
            continue
            
        res = {"ok": False}
        model_id = model_override or connection.model_id
        attempted_connections.append({"connection_id": connection.connection_id, "status": "attempted", "model": model_id})
        try:
            if connection.provider == "openrouter":
                res = await openai_compatible_chat_with_retry(
                    connection.base_url or "", 
                    api_key, 
                    model_id,
                    messages, 
                    temperature, 
                    max_tokens
                )
            elif connection.provider == "google":
                res = await google_chat_with_retry(
                    api_key, 
                    model_id,
                    messages, 
                    temperature, 
                    max_tokens
                )
        except Exception as e:
            res = {"ok": False, "error": str(e)}
            
        if res.get("ok"):
            if connection.connection_id != primary_conn_id:
                logger.info(f"Fallback active: Task '{task_id}' fell back from {primary_conn_id} to {connection.connection_id}")
            res["connection_id"] = connection.connection_id
            res["fallback_order"] = attempted_connections
            return res
        else:
            last_error = res.get("error", "Unknown error")
            logger.warning(f"Connection {connection.connection_id} failed for task '{task_id}': {last_error}")
            
    return {
        "ok": False,
        "error": f"All LLM connections failed for task '{task_id}'. Last error: {last_error}",
        "fallback_order": attempted_connections,
    }


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
        res = await openai_compatible_chat(base_url, api_key, model_id, messages, temperature, max_tokens)
        if res.get("ok"):
            return res
            
        err_msg = str(res.get("error", ""))
        if is_retryable_error(err_msg) and attempt < max_retries:
            logger.warning(f"OpenRouter rate limit or temporary error. Retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
            await asyncio.sleep(delay)
            delay *= 2.0
        else:
            return res
    return res


async def openai_compatible_chat(
    base_url: str,
    api_key: str,
    model_id: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> dict:
    payload = {"model": model_id, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        if "error" in data:
            return {"ok": False, "error": f"OpenRouter API error: {data['error']}"}
        choices = data.get("choices", [])
        if not choices:
            return {"ok": False, "error": f"OpenRouter returned no choices. Response: {data}"}
        message = choices[0].get("message")
        if not message or "content" not in message:
            return {"ok": False, "error": f"OpenRouter returned empty message choices. Response: {data}"}
        content = message["content"]
        return {"ok": True, "content": content, "provider": "openrouter", "model": model_id}
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
            logger.warning(f"Google Gemini rate limit or temporary error. Retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
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
            
    # Ensure alternating roles for contents (must start with user)
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
            "maxOutputTokens": max_tokens
        }
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}",
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return {"ok": False, "error": f"Google Gemini returned no candidates. Response: {data}"}
        candidate = candidates[0]
        content_obj = candidate.get("content")
        if not content_obj:
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            return {"ok": False, "error": f"Google Gemini response has no content. Finish reason: {finish_reason}"}
        parts = content_obj.get("parts", [])
        content = "\n".join(str(part.get("text", "")) for part in parts if part.get("text")).strip()
        return {"ok": True, "content": content, "provider": "google", "model": model_id}
    except Exception as error:
        return {"ok": False, "error": safe_error(error, api_key)}


def safe_error(error: Exception, api_key: str | None) -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[REDACTED_API_KEY]")
    return message[:500]
