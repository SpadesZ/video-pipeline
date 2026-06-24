from pipeline.models.cue_ledger import AssetType
from pipeline.stages.script_parser import parse_script


def test_parse_visual_and_risk_markers() -> None:
    parsed = parse_script(
        """
<VISUAL_BREAK: dashboard shot>
Explain the dashboard.
<RISK_DISCLOSURE: no guaranteed returns>
Avoid misleading claims.
"""
    )

    assert len(parsed.segments) == 2
    assert parsed.segments[0].asset_type == AssetType.GENERATED_IMAGE
    assert parsed.segments[0].visual_prompt == "dashboard shot"
    assert parsed.segments[1].risk_note == "no guaranteed returns"

