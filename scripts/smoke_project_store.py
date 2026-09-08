import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(ROOT / 'test.db').as_posix()}")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlmodel import Session

from pipeline.db import engine, init_db
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.project_store import load_project, project_file_path, save_project
from pipeline.settings import get_settings

try:
    from apps.worker.worker import run_lava_workflow_async
except ImportError:
    from worker import run_lava_workflow_async


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def make_project_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


async def run_smoke() -> None:
    init_db()
    settings = get_settings()

    store_project_id = make_project_id("smoke_store")
    artifact = ProductionArtifact(
        project_id=store_project_id,
        title="Project Store Smoke",
        approved_script_markdown=(
            "This smoke test verifies that project state survives across DB and JSON storage.\n\n"
            "It avoids external generation services and checks local persistence only."
        ),
    )
    save_project(settings, artifact)

    artifact_path = project_file_path(settings, store_project_id, "production_artifact.json")
    assert_ok(artifact_path.exists(), f"Missing artifact JSON: {artifact_path}")

    with Session(engine) as session:
        db_artifact = session.get(ProductionArtifact, store_project_id)
        assert_ok(db_artifact is not None, "Project was not saved to the database")
        session.delete(db_artifact)
        session.commit()

    loaded = load_project(settings, store_project_id)
    assert_ok(loaded is not None, "JSON fallback did not load the project")
    with Session(engine) as session:
        db_artifact = session.get(ProductionArtifact, store_project_id)
        assert_ok(db_artifact is not None, "JSON fallback did not sync the project back to the database")

    worker_project_id = make_project_id("smoke_worker")
    worker_artifact = ProductionArtifact(
        project_id=worker_project_id,
        title="Worker run_tts False Smoke",
        approved_script_markdown=(
            "The worker should tolerate run_tts being disabled.\n\n"
            "It should skip ASR alignment when there is no existing voiceover file."
        ),
    )
    save_project(settings, worker_artifact)
    result = await run_lava_workflow_async(worker_project_id, run_tts=False)
    assert_ok(result["project_id"] == worker_project_id, "Worker returned the wrong project id")

    loaded_worker = load_project(settings, worker_project_id)
    assert_ok(loaded_worker is not None, "Worker project could not be reloaded")
    actions = [entry.action for entry in loaded_worker.decision_log]
    assert_ok("tts_generation_skipped" in actions, "Worker did not record TTS skip")
    assert_ok("asr_alignment_skipped" in actions, "Worker did not record ASR skip")

    print(
        "OK project_store smoke "
        f"json_fallback={store_project_id} run_tts_false={worker_project_id}"
    )


def main() -> None:
    asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
