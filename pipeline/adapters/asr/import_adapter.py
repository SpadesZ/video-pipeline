import json
from pathlib import Path

from pydantic import BaseModel, Field


class ImportedTranscriptSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


def load_imported_segments(path: Path) -> list[ImportedTranscriptSegment]:
    try:
        content = path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return []

    if isinstance(payload, dict):
        payload = payload.get("segments", [])

    if not isinstance(payload, list):
        return []

    segments: list[ImportedTranscriptSegment] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            start_ms = int(float(str(item.get("start_ms", 0))))
            end_ms = int(float(str(item.get("end_ms", start_ms + 1000))))
            text = str(item.get("text", "")).strip()
            
            start_ms = max(0, start_ms)
            end_ms = max(start_ms, end_ms)
            
            if text:
                segments.append(
                    ImportedTranscriptSegment(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        text=text
                    )
                )
        except (ValueError, TypeError, KeyError):
            continue
    return segments

