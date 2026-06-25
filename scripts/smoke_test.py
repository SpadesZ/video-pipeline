import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.settings import get_settings
from pipeline.db import get_session
from pipeline.stages.run_mvp import run_mvp_pipeline


async def run_smoke() -> None:
    settings = get_settings()
    session = next(get_session())
    script = """
<VISUAL_BREAK: simple production flow diagram>
The MVP starts with a reviewed script and turns it into a cue ledger.
<BROLL: editing timeline and analytics dashboard>
Then it creates an asset manifest, subtitles, a preview, and an upload package.
<RISK_DISCLOSURE: no guaranteed revenue>
Human review remains the final gate before publishing.
"""
    artifact = await run_mvp_pipeline(
        session=session,
        settings=settings,
        title="Smoke Test Video",
        script_markdown=script,
        language="en",
    )
    project_dir = Path(settings.data_dir) / "projects" / artifact.project_id
    required = [
        "cue_ledger.json",
        "asset_manifest.json",
        "subtitles.srt",
        "upload_package.md",
    ]
    missing = [name for name in required if not (project_dir / name).exists()]
    if missing:
        raise SystemExit(f"Missing outputs: {missing}")
    print(f"OK project_id={artifact.project_id} dir={project_dir}")


def main() -> None:
    asyncio.run(run_smoke())


if __name__ == "__main__":
    main()
