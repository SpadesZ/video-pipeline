from pipeline.adapters.asr.json_transcript_adapter import parse_json_transcript
from pipeline.adapters.asr.srt_adapter import parse_srt
from pipeline.adapters.asr.vtt_adapter import parse_vtt


def test_parse_srt_segments() -> None:
    content = """1
00:00:00,000 --> 00:00:04,200
Opening hook.

2
00:00:04,200 --> 00:00:08,700
Second line.
"""
    segments, warnings = parse_srt(content)

    assert warnings == []
    assert len(segments) == 2
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 4200
    assert segments[1].start_ms == 4200
    assert segments[1].end_ms == 8700


def test_parse_json_transcript_start_ms_is_milliseconds() -> None:
    content = """[
      {"start_ms": 0, "end_ms": 4200, "text": "Opening hook."},
      {"start_ms": 4200, "end_ms": 8700, "text": "Second line."}
    ]"""
    segments, warnings = parse_json_transcript(content)

    assert warnings == []
    assert len(segments) == 2
    assert segments[1].start_ms == 4200
    assert segments[1].end_ms == 8700


def test_parse_vtt_hourless_and_headers() -> None:
    content = """WEBVTT
Kind: captions
Language: en

00:11.000 --> 00:13.250
Hello from WebVTT!

00:13.250 -->
Empty end block.
"""
    segments, warnings = parse_vtt(content)
    assert len(segments) == 1
    assert segments[0].start_ms == 11000
    assert segments[0].end_ms == 13250
    assert segments[0].text == "Hello from WebVTT!"


def test_parse_json_invalid_and_fallback() -> None:
    content = """[
      {"start_ms": 1000, "end_ms": 3000, "text": "Valid item"},
      {"start": 3.0, "end": 4.5, "text": "Valid float seconds"},
      {"start_ms": "invalid", "text": "Invalid start"},
      {"start_ms": 4500, "end_ms": "invalid", "text": "Invalid end"},
      {"start_ms": 5000, "end_ms": 4000, "text": "Valid start/end with inverted order"}
    ]"""
    segments, warnings = parse_json_transcript(content)
    assert len(segments) == 3
    assert segments[0].start_ms == 1000
    assert segments[0].end_ms == 3000
    assert segments[1].start_ms == 3000
    assert segments[1].end_ms == 4500
    assert segments[2].start_ms == 5000
    assert segments[2].end_ms == 5500

