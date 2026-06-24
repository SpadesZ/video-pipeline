import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def safe_project_path(data_dir: Path, project_id: str, filename: str | None = None) -> Path:
    base_dir = (data_dir / "projects").resolve()
    if filename:
        full_path = (base_dir / project_id / filename).resolve()
    else:
        full_path = (base_dir / project_id).resolve()
    try:
        full_path.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Directory traversal detected or invalid project ID: '{project_id}'")
    return full_path


def ensure_project_dir(data_dir: Path, project_id: str) -> Path:
    project_dir = safe_project_path(data_dir, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def write_json(path: Path, model: BaseModel | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(model, BaseModel):
        payload = model.model_dump(mode="json")
    else:
        payload = model
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json_model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))

