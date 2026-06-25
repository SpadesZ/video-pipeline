from typing import Generator
from sqlmodel import create_engine, Session, SQLModel

from pipeline.settings import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url.get_secret_value(),
    connect_args={"check_same_thread": False} if settings.database_url.get_secret_value().startswith("sqlite") else {}
)

def init_db() -> None:
    from pipeline.models.production_artifact import ProductionArtifact
    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
