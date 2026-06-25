from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Type

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, JSON
from sqlalchemy.types import TypeDecorator
from pydantic import BaseModel

from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.compliance import ComplianceReport
from pipeline.models.cue_ledger import CueLedger
from pipeline.models.metrics import MetricsDecision
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.transcript import TranscriptImport
from pipeline.models.visual_contract import VisualQualityContract, VisualQualityReport


class PydanticJSON(TypeDecorator):
    """
    SQLAlchemy TypeDecorator that transparently serializes/deserializes Pydantic models
    to/from JSON columns. Supports both single models and lists of models.
    """
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_model: Type[BaseModel], is_list: bool = False):
        super().__init__()
        self.pydantic_model = pydantic_model
        self.is_list = is_list

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if self.is_list:
            if isinstance(value, list):
                return [item.model_dump(mode='json') if isinstance(item, BaseModel) else item for item in value]
        else:
            if isinstance(value, BaseModel):
                return value.model_dump(mode='json')
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if self.is_list:
            if isinstance(value, list):
                return [self.pydantic_model.model_validate(item) for item in value]
        else:
            if isinstance(value, dict):
                return self.pydantic_model.model_validate(value)
        return value


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
