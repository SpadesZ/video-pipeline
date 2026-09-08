# 檔案路徑: video-pipeline/pipeline/models/production_artifact.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   流水線主幹資料 ProductionArtifact SQLModel 資料表定義。
# 主要責任:
#   1. 定義 SQLModel table，使專案狀態能直接持久化至 PostgreSQL。
#   2. 實作 PydanticJSON 進行巢狀型別裝飾器的 JSON 序列化。
# --------------------------------------------------------------------------

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, JSON

from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.compliance import ComplianceReport
from pipeline.models.cue_ledger import CueLedger
from pipeline.models.json_column import PydanticJSON
from pipeline.models.metrics import MetricsDecision
from pipeline.models.narrative import NarrativeIR
from pipeline.models.production_profile import ProductionProfile
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.shorts_manifest import ShortsManifest
from pipeline.models.shot import CharacterIdentityPack, ShotPlan
from pipeline.models.timeline import EditDecision
from pipeline.models.transcript import TranscriptImport
from pipeline.models.visual_contract import VisualQualityContract, VisualQualityReport

# PydanticJSON 已移至 pipeline.models.json_column 以避免循環相依。
# 此處 re-export 以維持既有 import 路徑。
__all__ = ["PydanticJSON", "ProductionArtifact"]


class ProductionArtifact(SQLModel, table=True):
    __tablename__ = "production_artifacts"

    project_id: str = Field(primary_key=True)
    title: str
    language: str = "en"
    persona: str | None = None
    genre: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    approved_script_markdown: str | None = None
    voiceover_path: str | None = None
    transcript_path: str | None = None
    transcript_import: TranscriptImport | None = Field(
        default=None, sa_column=Column(PydanticJSON(TranscriptImport))
    )

    cue_ledger: CueLedger | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(CueLedger))
    )
    asset_manifest: AssetManifest | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(AssetManifest))
    )
    visual_contract: VisualQualityContract | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(VisualQualityContract))
    )
    visual_qc_report: VisualQualityReport | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(VisualQualityReport))
    )
    compliance_report: ComplianceReport | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(ComplianceReport))
    )
    metrics_decision: MetricsDecision | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(MetricsDecision))
    )
    shorts_manifest: ShortsManifest | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(ShortsManifest))
    )

    # Narrative/Shot 層。皆為一次讀寫的 project document，故以 JSON 欄位保存。
    # 大量累積且需統計的 operational data（AssetVariant / QC / CapabilityJob）
    # 另存於各自的資料表，不放在此處。
    production_profile: ProductionProfile | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(ProductionProfile))
    )
    narrative_ir: NarrativeIR | None = Field(
        default=None,
        sa_column=Column(PydanticJSON(NarrativeIR))
    )
    character_packs: list[CharacterIdentityPack] = Field(
        default_factory=list,
        sa_column=Column(PydanticJSON(CharacterIdentityPack, is_list=True))
    )
    shot_plans: list[ShotPlan] = Field(
        default_factory=list,
        sa_column=Column(PydanticJSON(ShotPlan, is_list=True))
    )
    # 剪輯決策是人工調整的成果，必須持久化而非每次由選定候選重新推導
    edit_decisions: list[EditDecision] = Field(
        default_factory=list,
        sa_column=Column(PydanticJSON(EditDecision, is_list=True))
    )

    review_status: ReviewStatus = ReviewStatus.CUES_READY

    preview_mp4: str | None = None
    upload_package_path: str | None = None
    video_packaging: dict | None = Field(default=None, sa_column=Column(JSON))
    decision_log: list[DecisionLogEntry] = Field(
        default_factory=list,
        sa_column=Column(PydanticJSON(DecisionLogEntry, is_list=True))
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
