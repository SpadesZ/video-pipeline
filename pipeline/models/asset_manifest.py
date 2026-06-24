from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.models.cue_ledger import AssetType


class RightsStatus(StrEnum):
    UNKNOWN = "unknown"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssetItem(BaseModel):
    asset_id: str
    cue_id: str
    asset_type: AssetType
    prompt_or_search: str | None = None
    local_path: Path | None = None
    source_url: str | None = None
    license_name: str | None = None
    rights_status: RightsStatus = RightsStatus.NEEDS_REVIEW
    notes: str | None = None


class AssetManifest(BaseModel):
    project_id: str
    assets: list[AssetItem] = Field(default_factory=list)

