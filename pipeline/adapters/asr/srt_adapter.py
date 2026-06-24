import re

from pipeline.models.transcript import TranscriptSegment

TIME_RE = re.compile(
    r"(?:(?P<h>\d{1,2}):)?(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)


def parse_srt(content: str) -> tuple[list[TranscriptSegment], list[str]]:
    warnings: list[str] = []
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n").replace("\r", "\n").strip())

    for block_index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            warnings.append(f"Skipped SRT block {block_index}: missing time range")
            continue
        parts = lines[0].split("-->", 1)
        left = parts[0].strip()
        right = parts[1].strip() if len(parts) > 1 else ""

        start_ms = parse_timestamp(left)
        right_split = right.split()
        end_ms = parse_timestamp(right_split[0]) if right_split else None
        text = " ".join(lines[1:]).strip()
        if start_ms is None or end_ms is None or not text:
            warnings.append(f"Skipped SRT block {block_index}: invalid timestamp or empty text")
            continue
        segments.append(
            TranscriptSegment(
                segment_id=f"seg_{len(segments) + 1:04d}",
                start_ms=start_ms,
                end_ms=max(end_ms, start_ms + 500),
                text=text,
            )
        )
    return segments, warnings


def parse_timestamp(value: str) -> int | None:
    match = TIME_RE.search(value)
    if not match:
        return None
    hours = int(match.group("h")) if match.group("h") else 0
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms").ljust(3, "0")[:3])
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis

