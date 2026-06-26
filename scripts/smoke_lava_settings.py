import asyncio
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_DATA_DIR = ROOT / "data" / "temp" / "lava_settings_smoke"
shutil.rmtree(SMOKE_DATA_DIR, ignore_errors=True)
SMOKE_DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["DATA_DIR"] = str(SMOKE_DATA_DIR)
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["SECRETS_FILE"] = str(SMOKE_DATA_DIR / "missing.env")

from pipeline.adapters.llm.lava_dispatcher import dispatch_llm_task
from pipeline.adapters.llm.lava_settings import (
    get_llm_brain_status,
    load_lava_settings,
    update_task_binding,
)
from pipeline.adapters.llm.lava_verifier import sanitize_provider_error, verify_connection
from pipeline.adapters.llm.task_registry import TASK_IDS, VIDEO_LLM_TASKS
from pipeline.settings import get_settings


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


async def main() -> None:
    settings = get_settings()
    status = get_llm_brain_status(settings)

    assert_ok(len(status.connections) >= 2, "Expected default LAVA connections")
    assert_ok({task.task_id for task in status.tasks} == TASK_IDS, "Task registry/status mismatch")
    assert_ok(status.configured_env_keys == [], "No-key smoke should not see configured env keys")

    binding_task_ids = {binding.task_id for binding in status.bindings}
    missing_bindings = [
        task.task_id
        for task in VIDEO_LLM_TASKS
        if task.required and task.task_id not in binding_task_ids and not task.fallback_behavior
    ]
    assert_ok(not missing_bindings, f"Required tasks lack binding or fallback: {missing_bindings}")

    updated = update_task_binding(
        "topic_research",
        "google_primary",
        settings=settings,
        model_id="gemini-test-model",
    )
    assert_ok(updated.connection_id == "google_primary", "Binding update did not return updated connection")
    saved = load_lava_settings(settings)
    assert_ok(saved.bindings[0].task_id == "topic_research", "Binding update was not persisted")

    try:
        update_task_binding("unknown_task", "google_primary", settings=settings)
    except ValueError as exc:
        assert_ok("Unknown LAVA task id" in str(exc), "Unknown task error was not explicit")
    else:
        raise SystemExit("Unknown task binding update should fail")

    try:
        update_task_binding("topic_research", "unknown_connection", settings=settings)
    except ValueError as exc:
        assert_ok("Unknown LAVA connection id" in str(exc), "Unknown connection error was not explicit")
    else:
        raise SystemExit("Unknown connection binding update should fail")

    secret = "demo-provider-key-value"
    sanitized = sanitize_provider_error(f"provider leaked {secret}", secret)
    assert_ok(secret not in sanitized and "[REDACTED_API_KEY]" in sanitized, "Provider error was not sanitized")

    verification = await verify_connection("openrouter_primary")
    assert_ok(verification["ok"] is False, "No-key verification should not pass")
    assert_ok(verification.get("configured") is False, "No-key verification should report configured=false")
    assert_ok("OPENROUTER_API_KEY" in verification.get("error", ""), "No-key verification should name missing env key")

    unknown = await dispatch_llm_task("unknown_task", [{"role": "user", "content": "test"}])
    assert_ok(unknown["ok"] is False and "Unknown video LLM task" in unknown["error"], "Unknown task dispatch should fail clearly")

    no_key = await dispatch_llm_task("topic_research", [{"role": "user", "content": "test"}])
    assert_ok(no_key["ok"] is False, "No-key dispatch should fail without live providers")
    fallback_order = no_key.get("fallback_order", [])
    assert_ok(fallback_order and all(item["status"] == "missing_key" for item in fallback_order), "No-key fallback order should show missing keys")

    print(f"OK lava_settings smoke tasks={len(status.tasks)} bindings={len(status.bindings)} config={status.config_path}")


if __name__ == "__main__":
    asyncio.run(main())
