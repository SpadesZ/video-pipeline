from enum import StrEnum

from pydantic import BaseModel, Field


class ComplianceSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ComplianceFinding(BaseModel):
    code: str
    severity: ComplianceSeverity
    message: str
    cue_id: str | None = None
    evidence_ref: str | None = None


class ComplianceReport(BaseModel):
    project_id: str
    upload_ready: bool
    findings: list[ComplianceFinding] = Field(default_factory=list)

