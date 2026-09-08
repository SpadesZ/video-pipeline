from pathlib import Path
from datetime import datetime

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from pipeline.db import engine
from pipeline.models.asset_manifest import AssetManifest
from pipeline.models.compliance import ComplianceReport
from pipeline.models.cue_ledger import CueLedger
from pipeline.models.metrics import MetricsDecision
from pipeline.models.narrative import NarrativeIR
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.production_profile import ProductionProfile
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.shorts_manifest import ShortsManifest
from pipeline.models.shot import CharacterIdentityPack, ShotPlan
from pipeline.models.timeline import EditDecision
from pipeline.settings import Settings
from pipeline.stages.compliance_checker import check_compliance
from pipeline.models.transcript import TranscriptImport
from pipeline.models.visual_contract import VisualQualityContract, VisualQualityReport
from pipeline.utils.files import ensure_project_dir, read_json_model, safe_project_path, write_json


ARTIFACT_FILENAME = "production_artifact.json"
JSON_FIELDS = (
    "transcript_import",
    "cue_ledger",
    "asset_manifest",
    "visual_contract",
    "visual_qc_report",
    "compliance_report",
    "metrics_decision",
    "shorts_manifest",
    "production_profile",
    "narrative_ir",
    "character_packs",
    "shot_plans",
    "edit_decisions",
    "video_packaging",
    "decision_log",
)

MODEL_FIELDS = {
    "transcript_import": TranscriptImport,
    "cue_ledger": CueLedger,
    "asset_manifest": AssetManifest,
    "visual_contract": VisualQualityContract,
    "visual_qc_report": VisualQualityReport,
    "compliance_report": ComplianceReport,
    "metrics_decision": MetricsDecision,
    "shorts_manifest": ShortsManifest,
    "production_profile": ProductionProfile,
    "narrative_ir": NarrativeIR,
}

# 以列表保存的巢狀模型欄位，載入時需逐項還原
LIST_MODEL_FIELDS = {
    "character_packs": CharacterIdentityPack,
    "shot_plans": ShotPlan,
    "edit_decisions": EditDecision,
    "decision_log": DecisionLogEntry,
}


def _parse_datetime(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return value


def normalize_project_artifact(artifact: ProductionArtifact) -> ProductionArtifact:
    for field_name, model_type in MODEL_FIELDS.items():
        value = getattr(artifact, field_name, None)
        if isinstance(value, dict):
            setattr(artifact, field_name, model_type.model_validate(value))

    for field_name, model_type in LIST_MODEL_FIELDS.items():
        value = getattr(artifact, field_name, None)
        if isinstance(value, list):
            setattr(
                artifact,
                field_name,
                [
                    item if isinstance(item, model_type) else model_type.model_validate(item)
                    for item in value
                ],
            )

    if isinstance(artifact.review_status, str):
        artifact.review_status = ReviewStatus(artifact.review_status)

    artifact.created_at = _parse_datetime(artifact.created_at)
    artifact.updated_at = _parse_datetime(artifact.updated_at)

    return artifact


def project_file_path(settings: Settings, project_id: str, filename: str) -> Path:
    return safe_project_path(Path(settings.data_dir), project_id, filename)


def write_project_files(settings: Settings, artifact: ProductionArtifact) -> Path:
    project_dir = ensure_project_dir(Path(settings.data_dir), artifact.project_id)
    if artifact.transcript_import:
        write_json(project_dir / "transcript_import.json", artifact.transcript_import)
    if artifact.cue_ledger:
        write_json(project_dir / "cue_ledger.json", artifact.cue_ledger)
    write_json(project_dir / "asset_manifest.json", artifact.asset_manifest or {})
    write_json(project_dir / "visual_contract.json", artifact.visual_contract or {})
    write_json(project_dir / "visual_qc_report.json", artifact.visual_qc_report or {})
    write_json(project_dir / "compliance_report.json", artifact.compliance_report or {})
    if artifact.production_profile:
        write_json(project_dir / "production_profile.json", artifact.production_profile)
    if artifact.narrative_ir:
        write_json(project_dir / "narrative_ir.json", artifact.narrative_ir)
    if artifact.character_packs:
        write_json(
            project_dir / "character_packs.json",
            [pack.model_dump(mode="json") for pack in artifact.character_packs],
        )
    if artifact.shot_plans:
        write_json(
            project_dir / "shot_plans.json",
            [plan.model_dump(mode="json") for plan in artifact.shot_plans],
        )
    if artifact.edit_decisions:
        write_json(
            project_dir / "edit_decisions.json",
            [item.model_dump(mode="json") for item in artifact.edit_decisions],
        )
    write_json(project_dir / ARTIFACT_FILENAME, artifact)
    return project_dir


def upsert_project(session: Session, artifact: ProductionArtifact) -> ProductionArtifact:
    merged = session.merge(artifact)
    for field_name in JSON_FIELDS:
        try:
            flag_modified(merged, field_name)
        except (AttributeError, InvalidRequestError):
            continue
    session.commit()
    session.refresh(merged)
    return merged


def save_project(
    settings: Settings,
    artifact: ProductionArtifact,
    session: Session | None = None,
) -> ProductionArtifact:
    write_project_files(settings, artifact)
    if session is not None:
        return upsert_project(session, artifact)
    with Session(engine) as local_session:
        return upsert_project(local_session, artifact)


def load_project(
    settings: Settings,
    project_id: str,
    session: Session | None = None,
) -> ProductionArtifact | None:
    def _load(active_session: Session) -> ProductionArtifact | None:
        artifact = active_session.get(ProductionArtifact, project_id)
        if artifact:
            artifact = normalize_project_artifact(artifact)
            artifact.compliance_report = check_compliance(artifact)
            return save_project(settings, artifact, session=active_session)

        artifact_path = project_file_path(settings, project_id, ARTIFACT_FILENAME)
        if not artifact_path.exists():
            return None
        artifact = read_json_model(artifact_path, ProductionArtifact)
        artifact = normalize_project_artifact(artifact)
        artifact.compliance_report = check_compliance(artifact)
        return save_project(settings, artifact, session=active_session)

    if session is not None:
        return _load(session)
    with Session(engine) as local_session:
        return _load(local_session)


def list_projects(settings: Settings, session: Session | None = None) -> list[ProductionArtifact]:
    def _list(active_session: Session) -> list[ProductionArtifact]:
        artifacts: list[ProductionArtifact] = []
        seen_ids: set[str] = set()

        for artifact in active_session.exec(select(ProductionArtifact)).all():
            artifact = normalize_project_artifact(artifact)
            artifact.compliance_report = check_compliance(artifact)
            artifact = save_project(settings, artifact, session=active_session)
            artifacts.append(artifact)
            seen_ids.add(artifact.project_id)

        projects_dir = Path(settings.data_dir) / "projects"
        if projects_dir.exists():
            for artifact_path in projects_dir.glob(f"*/{ARTIFACT_FILENAME}"):
                try:
                    artifact = read_json_model(artifact_path, ProductionArtifact)
                except Exception:
                    continue
                if artifact.project_id in seen_ids:
                    continue
                artifact = normalize_project_artifact(artifact)
                artifact.compliance_report = check_compliance(artifact)
                artifacts.append(save_project(settings, artifact, session=active_session))
                seen_ids.add(artifact.project_id)

        return sorted(artifacts, key=lambda item: item.updated_at, reverse=True)

    if session is not None:
        return _list(session)
    with Session(engine) as local_session:
        return _list(local_session)
