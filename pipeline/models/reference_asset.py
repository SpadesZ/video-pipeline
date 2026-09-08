# 檔案路徑: video-pipeline/pipeline/models/reference_asset.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   一級參考素材 ReferenceAsset 資料表。
# 主要責任:
#   1. 以獨立資料表管理角色臉、全身、服裝、場景、首幀與聲音等參考素材。
#   2. 保留 file_hash 與 rights，支撐素材血緣與版權追蹤。
# 說明:
#   與既有 asset_manifest.AssetItem 是不同概念。AssetItem 是 cue 綁定的
#   成品素材清單；ReferenceAsset 是餵給生成模型的輸入參考，被
#   CharacterIdentityPack、ShotPlan 與 job manifest 以 asset_id 引用。
#   狀態類欄位一律以 VARCHAR 儲存並由 Python 列舉驗證，不建立資料庫原生
#   enum 型別，以免日後新增值需要無法回退的 ALTER TYPE。
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, Column, String
from sqlmodel import Field, SQLModel


class ReferenceAssetType(StrEnum):
    FACE = "face"
    FULLBODY = "fullbody"
    WARDROBE = "wardrobe"
    LOCATION = "location"
    FIRST_FRAME = "first_frame"
    VOICE = "voice"
    STYLE = "style"


class ReferenceRights(StrEnum):
    UNKNOWN = "unknown"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReferenceAsset(SQLModel, table=True):
    __tablename__ = "reference_assets"

    asset_id: str = Field(primary_key=True)
    project_id: str = Field(index=True)

    # 值域為 ReferenceAssetType，以字串儲存
    asset_type: str = Field(sa_column=Column(String, nullable=False, index=True))

    label: str | None = None
    local_path: str | None = None
    file_hash: str | None = Field(default=None, index=True)
    source: str | None = None

    # 值域為 ReferenceRights
    rights: str = Field(
        default=ReferenceRights.NEEDS_REVIEW.value,
        sa_column=Column(String, nullable=False),
    )

    # 避開 SQLModel/SQLAlchemy 保留名稱 metadata
    asset_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_approved(self) -> bool:
        return self.rights == ReferenceRights.APPROVED
