import json

from pipeline.models.transcript import TranscriptSegment


def parse_json_transcript(content: str) -> tuple[list[TranscriptSegment], list[str]]:
    warnings: list[str] = []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON format: {e}")

    if isinstance(payload, dict):
        items = payload.get("segments", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("JSON transcript must be a list or an object with segments")

    if not isinstance(items, list):
        raise ValueError("JSON segments must be a list")

    segments: list[TranscriptSegment] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            warnings.append(f"Skipped JSON item {index}: not an object")
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            warnings.append(f"Skipped JSON item {index}: empty text")
            continue

        try:
            if "start_ms" in item and item["start_ms"] is not None:
                start_ms = int(float(str(item["start_ms"])))
            else:
                start_ms = coerce_seconds_to_ms(item.get("start", 0))
        except (ValueError, TypeError):
            warnings.append(f"Skipped JSON item {index}: invalid start timestamp")
            continue

        try:
            if "end_ms" in item and item["end_ms"] is not None:
                end_ms = int(float(str(item["end_ms"])))
            else:
                val = item.get("end")
                if val is not None:
                    end_ms = coerce_seconds_to_ms(val)
                else:
                    end_ms = start_ms + 1000
        except (ValueError, TypeError):
            warnings.append(f"Skipped JSON item {index}: invalid end timestamp")
            continue

        segments.append(
            TranscriptSegment(
                segment_id=f"seg_{len(segments) + 1:04d}",
                start_ms=max(0, start_ms),
                end_ms=max(end_ms, start_ms + 500),
                text=text,
            )
        )
    return segments, warnings


def coerce_seconds_to_ms(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        value = value.strip()
    try:
        number = float(value or 0)
        return int(number * 1000)
    except (ValueError, TypeError):
        raise ValueError(f"Could not convert {value!r} to seconds")
