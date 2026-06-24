import os

from pipeline.models.llm_control import LLMBrainStatus, LLMConnection, LLMTaskBinding
from pipeline.secrets import load_runtime_secrets
from pipeline.adapters.llm.task_registry import VIDEO_LLM_TASKS


DEFAULT_BINDINGS = [
    LLMTaskBinding(task_id="topic_research", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="script_outline", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="visual_bible", connection_id="google_primary"),
    LLMTaskBinding(task_id="storyboard", connection_id="google_primary"),
    LLMTaskBinding(task_id="packaging", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="quality_review", connection_id="openrouter_primary"),
]


def get_llm_brain_status() -> LLMBrainStatus:
    load_runtime_secrets()
    connections = default_connections()
    configured = [
        connection.api_key_env
        for connection in connections
        if bool(os.getenv(connection.api_key_env))
    ]
    return LLMBrainStatus(
        connections=connections,
        tasks=VIDEO_LLM_TASKS,
        bindings=DEFAULT_BINDINGS,
        configured_env_keys=configured,
    )


def binding_for_task(task_id: str) -> LLMTaskBinding | None:
    return next((binding for binding in DEFAULT_BINDINGS if binding.task_id == task_id), None)


def connection_by_id(connection_id: str) -> LLMConnection | None:
    return next((connection for connection in default_connections() if connection.connection_id == connection_id), None)


def default_connections() -> list[LLMConnection]:
    load_runtime_secrets()
    return [
        LLMConnection(
            connection_id="openrouter_primary",
            provider="openrouter",
            label="OpenRouter Primary",
            model_id=os.getenv("OPENROUTER_MODEL_ID", "openai/gpt-4o-mini"),
            api_key_env="OPENROUTER_API_KEY",
            base_url="https://openrouter.ai/api/v1",
        ),
        LLMConnection(
            connection_id="google_primary",
            provider="google",
            label="Google Gemini Primary",
            model_id=os.getenv("GOOGLE_MODEL_ID", "gemini-2.0-flash"),
            api_key_env="GOOGLE_API_KEY",
        ),
    ]
