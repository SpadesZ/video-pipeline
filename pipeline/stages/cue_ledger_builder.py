from pipeline.models.cue_ledger import CueItem, CueLedger
from pipeline.stages.script_parser import ScriptParseResult


def build_cue_ledger(project_id: str, parsed: ScriptParseResult, cue_seconds: int = 8) -> CueLedger:
    if cue_seconds <= 0:
        raise ValueError("cue_seconds must be a positive integer greater than 0.")
    cues: list[CueItem] = []
    cursor_ms = 0
    cue_ms = cue_seconds * 1000
    shorts_index = 1
    active_shorts_id: str | None = None

    for index, segment in enumerate(parsed.segments, start=1):
        if segment.shorts_boundary or active_shorts_id is None:
            active_shorts_id = f"short_{shorts_index:02d}"
            shorts_index += 1

        cues.append(
            CueItem(
                cue_id=f"cue_{index:04d}",
                start_ms=cursor_ms,
                end_ms=cursor_ms + cue_ms,
                voice_text=segment.text,
                subtitle_text=segment.text,
                visual_prompt=segment.visual_prompt,
                asset_type=segment.asset_type,
                shorts_id=active_shorts_id,
                risk_note=segment.risk_note,
            )
        )
        cursor_ms += cue_ms

    return CueLedger(project_id=project_id, cues=cues)

