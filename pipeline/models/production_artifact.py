from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.compliance import ComplianceReport
from pipeline.models.cue_ledger import CueLedger
from pipeline.models.metrics import MetricsDecision
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.transcript import TranscriptImport
from pipeline.models.visual_contract import VisualQualityContract, VisualQualityReport


class ProductionArtifact(BaseModel):
    project_id: str
    title: str
    language: str = "en"
    persona: str | None = None
    genre: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    approved_script_markdown: str | None = None
    voiceover_path: Path | None = None
    transcript_path: Path | None = None
    transcript_import: TranscriptImport | None = None

    cue_ledger: CueLedger | None = None
    asset_manifest: AssetManifest | None = None
    visual_contract: VisualQualityContract | None = None
    visual_qc_report: VisualQualityReport | None = None
    compliance_report: ComplianceReport | None = None
    metrics_decision: MetricsDecision | None = None
    review_status: ReviewStatus = ReviewStatus.CUES_READY

    preview_mp4: Path | None = None
    upload_package_path: Path | None = None
    video_packaging: dict | None = None
    decision_log: list[DecisionLogEntry] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
