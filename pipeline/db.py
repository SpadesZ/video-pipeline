# 檔案路徑: video-pipeline/pipeline/db.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   資料庫連線與 Session 生命週期管理模組。
# 主要責任:
#   1. 設定 SQLAlchemy engine 連線池。
#   2. 提供 init_db() 與 get_session() 進行依賴注入。
# --------------------------------------------------------------------------

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
