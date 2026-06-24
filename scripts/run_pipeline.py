import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.settings import get_settings
from pipeline.stages.run_mvp import run_mvp_pipeline

SAMPLE_SCRIPT = """
<VISUAL_BREAK: abstract map of AI video workflow>
This is the opening hook: most automated video systems fail because they automate before they validate.
<BROLL: creator desktop workflow, analytics dashboard>
The safer version is a human-reviewed pipeline that turns a script and voiceover into cues, assets, subtitles, and a rough cut.
<SHORT_BREAK>
<SCREENCAST: show cue ledger JSON and upload package>
Only after the first three videos prove retention and conversion should the system scale production.
<RISK_DISCLOSURE: no income claim is guaranteed>
This is not a guaranteed income machine. It is a repeatable production and measurement workflow.
"""


async def run_pipeline() -> None:
    artifact = await run_mvp_pipeline(
        settings=get_settings(),
        title="CPU First Video Pipeline MVP",
        script_markdown=SAMPLE_SCRIPT,
        language="en",
        persona="operator",
    )
    print(artifact.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
