from pipeline.models.transcript import TranscriptSegment


def parse_plain_text(content: str, cue_seconds: int = 8) -> tuple[list[TranscriptSegment], list[str]]:
    warnings = ["Plain text has no timestamps; generated rough timing."]
    lines = [line.strip() for line in content.replace("\r\n", "\n").replace("\r", "\n").splitlines() if line.strip()]
    if not lines:
        lines = [content.strip()] if content.strip() else []

    cue_ms = max(3, cue_seconds) * 1000
    segments: list[TranscriptSegment] = []
    for index, line in enumerate(lines):
        segments.append(
            TranscriptSegment(
                segment_id=f"seg_{index + 1:04d}",
                start_ms=index * cue_ms,
                end_ms=(index + 1) * cue_ms,
                text=line,
            )
        )
    return segments, warnings

