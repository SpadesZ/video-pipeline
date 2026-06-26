import os
from pathlib import Path

from pipeline.models.llm_control import LLMBrainStatus, LLMConnection, LLMSettingsStore, LLMTaskBinding
from pipeline.secrets import load_runtime_secrets
from pipeline.adapters.llm.task_registry import VIDEO_LLM_TASKS
from pipeline.settings import Settings
from pipeline.utils.files import read_json_model, write_json


DEFAULT_BINDINGS = [
    LLMTaskBinding(task_id="topic_research", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="script_outline", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="visual_bible", connection_id="google_primary"),
    LLMTaskBinding(task_id="storyboard", connection_id="google_primary"),
    LLMTaskBinding(task_id="packaging", connection_id="openrouter_primary"),
    LLMTaskBinding(task_id="quality_review", connection_id="openrouter_primary"),
]


def lava_settings_path(settings: Settings | None = None) -> Path:
    data_dir = Path(settings.data_dir) if settings else Path(os.getenv("DATA_DIR", "/data"))
    return data_dir / "lava_settings.json"


def _default_binding_map() -> dict[str, LLMTaskBinding]:
    return {binding.task_id: binding for binding in DEFAULT_BINDINGS}


def _valid_task_ids() -> set[str]:
    return {task.task_id for task in VIDEO_LLM_TASKS}


def _valid_connection_ids() -> set[str]:
    return {connection.connection_id for connection in default_connections()}


def load_lava_settings(settings: Settings | None = None) -> LLMSettingsStore:
    path = lava_settings_path(settings)
    if not path.exists():
        return LLMSettingsStore()
    try:
        store = read_json_model(path, LLMSettingsStore)
    except Exception:
        return LLMSettingsStore()
    valid_tasks = _valid_task_ids()
    valid_connections = _valid_connection_ids()
    store.bindings = [
        binding
        for binding in store.bindings
        if binding.task_id in valid_tasks and binding.connection_id in valid_connections
    ]
    return store


def save_lava_settings(store: LLMSettingsStore, settings: Settings | None = None) -> None:
    write_json(lava_settings_path(settings), store)


def task_bindings(settings: Settings | None = None) -> list[LLMTaskBinding]:
    merged = _default_binding_map()
    for binding in load_lava_settings(settings).bindings:
        merged[binding.task_id] = binding
    return [merged[task.task_id] for task in VIDEO_LLM_TASKS if task.task_id in merged]


def update_task_binding(
    task_id: str,
    connection_id: str,
    settings: Settings | None = None,
    model_id: str | None = None,
) -> LLMTaskBinding:
    if task_id not in _valid_task_ids():
        raise ValueError(f"Unknown LAVA task id: {task_id}")
    if connection_id not in _valid_connection_ids():
        raise ValueError(f"Unknown LAVA connection id: {connection_id}")

    clean_model_id = (model_id or "").strip() or None
    binding = LLMTaskBinding(task_id=task_id, connection_id=connection_id, model_id=clean_model_id)
    store = load_lava_settings(settings)
    existing = {item.task_id: item for item in store.bindings}
    existing[task_id] = binding
    store.bindings = [existing[key] for key in sorted(existing)]
    save_lava_settings(store, settings)
    return binding


def get_llm_brain_status(settings: Settings | None = None) -> LLMBrainStatus:
    load_runtime_secrets()
    connections = default_connections()
    configured = [
        connection.api_key_env
        for connection in connections
        if bool(os.getenv(connection.api_key_env))
    ]
    store = load_lava_settings(settings)
    return LLMBrainStatus(
        connections=connections,
        tasks=VIDEO_LLM_TASKS,
        bindings=task_bindings(settings),
        configured_env_keys=configured,
        binding_source="runtime" if store.bindings else "default",
        config_path=str(lava_settings_path(settings)),
    )


def binding_for_task(task_id: str, settings: Settings | None = None) -> LLMTaskBinding | None:
    return next((binding for binding in task_bindings(settings) if binding.task_id == task_id), None)


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
