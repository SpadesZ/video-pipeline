from pipeline.adapters.asr.srt_adapter import parse_srt
from pipeline.models.transcript import TranscriptSegment


def parse_vtt(content: str) -> tuple[list[TranscriptSegment], list[str]]:
    cleaned_lines = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    
    in_header = True
    for line in lines:
        stripped = line.strip()
        if in_header:
            if not stripped:
                in_header = False
                cleaned_lines.append("")
                continue
            if stripped.upper() == "WEBVTT" or (":" in stripped and "-->" not in stripped):
                continue
            if "-->" in stripped:
                in_header = False
        
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            cleaned_lines.append("")
            continue
        cleaned_lines.append(stripped)
    return parse_srt("\n".join(cleaned_lines))

