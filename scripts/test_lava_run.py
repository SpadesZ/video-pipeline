import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from worker import run_lava_workflow_async
except ImportError:
    try:
        from apps.worker.worker import run_lava_workflow_async
    except ImportError:
        # Fallback if executing from a different path
        sys.path.append(str(ROOT / "apps" / "worker"))
        from worker import run_lava_workflow_async


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python test_lava_run.py <project_id>")
        sys.exit(1)
        
    project_id = sys.argv[1]
    print(f"Triggering LAVA Brain workflow for project: {project_id}")
    
    # Run the workflow. Since we might not have active ElevenLabs credit or voice setup, 
    # run_tts=True will try ElevenLabs and fallback to silent WAV if it's missing.
    result = await run_lava_workflow_async(project_id, run_tts=True)
    print("Workflow executed successfully!")
    print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
