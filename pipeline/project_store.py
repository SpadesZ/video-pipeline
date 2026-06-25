from pathlib import Path

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from pipeline.db import engine
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.settings import Settings
from pipeline.stages.compliance_checker import check_compliance
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
    "video_packaging",
    "decision_log",
)


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
            artifact.compliance_report = check_compliance(artifact)
            return save_project(settings, artifact, session=active_session)

        artifact_path = project_file_path(settings, project_id, ARTIFACT_FILENAME)
        if not artifact_path.exists():
            return None
        artifact = read_json_model(artifact_path, ProductionArtifact)
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
                artifact.compliance_report = check_compliance(artifact)
                artifacts.append(save_project(settings, artifact, session=active_session))
                seen_ids.add(artifact.project_id)

        return sorted(artifacts, key=lambda item: item.updated_at, reverse=True)

    if session is not None:
        return _list(session)
    with Session(engine) as local_session:
        return _list(local_session)
