from pathlib import Path

from pipeline.adapters.video.ffmpeg_renderer import render_placeholder_preview
from pipeline.models.cue_ledger import CueLedger


def render_preview(project_dir: Path, cue_ledger: CueLedger, title: str) -> Path | None:
    output_path = project_dir / "preview.mp4"
    return render_placeholder_preview(output_path=output_path, cue_ledger=cue_ledger, title=title)

