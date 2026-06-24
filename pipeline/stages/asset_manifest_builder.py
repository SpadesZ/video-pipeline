from pipeline.models.asset_manifest import AssetItem, AssetManifest
from pipeline.models.cue_ledger import AssetType, CueLedger


def build_asset_manifest(project_id: str, cue_ledger: CueLedger) -> AssetManifest:
    assets: list[AssetItem] = []

    for cue in cue_ledger.cues:
        asset_type = cue.asset_type
        notes = "Needs human rights review before upload."
        if asset_type == AssetType.NONE:
            asset_type = AssetType.GENERATED_IMAGE
            notes = "Fallback visual asset generated from transcript cue; needs prompt, source, and rights review."
        assets.append(
            AssetItem(
                asset_id=f"asset_{cue.cue_id}",
                cue_id=cue.cue_id,
                asset_type=asset_type,
                prompt_or_search=cue.visual_prompt or cue.voice_text[:120],
                notes=notes,
            )
        )

    return AssetManifest(project_id=project_id, assets=assets)
