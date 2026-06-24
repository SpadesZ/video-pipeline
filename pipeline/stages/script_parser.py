import re
from pydantic import BaseModel, Field

from pipeline.models.cue_ledger import AssetType

MARKER_RE = re.compile(r"<(?P<name>SHORT_BREAK|VISUAL_BREAK|BROLL|SCREENCAST|RISK_DISCLOSURE)(?::(?P<body>[^>]+))?>")


class ScriptSegment(BaseModel):
    text: str
    asset_type: AssetType = AssetType.NONE
    visual_prompt: str | None = None
    risk_note: str | None = None
    shorts_boundary: bool = False


class ScriptParseResult(BaseModel):
    segments: list[ScriptSegment] = Field(default_factory=list)


def parse_script(script_markdown: str) -> ScriptParseResult:
    segments: list[ScriptSegment] = []
    current_asset_type = AssetType.NONE
    current_visual: str | None = None
    current_risk: str | None = None
    shorts_boundary = False

    for raw_line in script_markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        markers = list(MARKER_RE.finditer(line))
        if markers:
            for marker in markers:
                name = marker.group("name")
                body = (marker.group("body") or "").strip() or None
                if name == "SHORT_BREAK":
                    shorts_boundary = True
                elif name == "VISUAL_BREAK":
                    current_asset_type = AssetType.GENERATED_IMAGE
                    current_visual = body
                elif name == "BROLL":
                    current_asset_type = AssetType.BROLL
                    current_visual = body
                elif name == "SCREENCAST":
                    current_asset_type = AssetType.SCREENCAST
                    current_visual = body
                elif name == "RISK_DISCLOSURE":
                    current_risk = body or "Risk disclosure required."

            line = MARKER_RE.sub("", line).strip()
            if not line:
                continue

        segments.append(
            ScriptSegment(
                text=line,
                asset_type=current_asset_type,
                visual_prompt=current_visual,
                risk_note=current_risk,
                shorts_boundary=shorts_boundary,
            )
        )
        current_asset_type = AssetType.NONE
        current_visual = None
        current_risk = None
        shorts_boundary = False

    return ScriptParseResult(segments=segments)

