"""驗證專案 JSON 重讀保留巢狀型別；重現詳情頁曾發生的 500。"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.cue_ledger import CueLedger
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.utils.files import read_json_model, write_json

with TemporaryDirectory() as folder:
    path = Path(folder) / "project.json"
    artifact = ProductionArtifact(project_id="roundtrip_demo", title="Local check",
        asset_manifest=AssetManifest(project_id="roundtrip_demo"),
        cue_ledger=CueLedger(project_id="roundtrip_demo"))
    write_json(path, artifact)
    restored = read_json_model(path, ProductionArtifact)
    assert isinstance(restored.asset_manifest, AssetManifest)
    assert isinstance(restored.cue_ledger, CueLedger)
    assert restored.asset_manifest.assets == []
    assert restored.cue_ledger.cues == []
    write_json(path, {"project_id": "invalid", "title": "Local check", "asset_manifest": {"assets": []}})
    try:
        read_json_model(path, ProductionArtifact)
    except ValidationError:
        pass
    else:
        raise AssertionError("Missing nested project_id must be rejected")
print("Project JSON nested types and invalid payload rejection passed.")
