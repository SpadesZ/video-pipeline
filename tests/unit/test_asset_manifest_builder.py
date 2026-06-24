from pipeline.models.cue_ledger import AssetType, CueItem, CueLedger
from pipeline.models.asset_manifest import RightsStatus
from pipeline.stages.asset_manifest_builder import build_asset_manifest


def test_asset_manifest_falls_back_for_transcript_only_cues() -> None:
    cue_ledger = CueLedger(
        project_id="vid_test",
        cues=[
            CueItem(
                cue_id="cue_0001",
                start_ms=0,
                end_ms=3000,
                voice_text="Transcript-only cue still needs a visual asset.",
                asset_type=AssetType.NONE,
            )
        ],
    )

    manifest = build_asset_manifest("vid_test", cue_ledger)

    assert len(manifest.assets) == 1
    assert manifest.assets[0].asset_type == AssetType.GENERATED_IMAGE
    assert manifest.assets[0].rights_status == RightsStatus.NEEDS_REVIEW
    assert "Fallback visual asset" in (manifest.assets[0].notes or "")
