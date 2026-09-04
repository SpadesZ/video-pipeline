# 檔案路徑: video-pipeline/pipeline/db.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   資料庫連線與 Session 生命週期管理模組。
# 主要責任:
#   1. 設定 SQLAlchemy engine 連線池。
#   2. 提供 init_db() 與 get_session() 進行依賴注入。
#   3. 提供只讀的 schema 版本檢查，絕不自動改寫 schema。
# --------------------------------------------------------------------------

import logging
from typing import Generator

from sqlmodel import create_engine, Session, SQLModel

from pipeline.settings import get_settings

logger = logging.getLogger("pipeline.db")

settings = get_settings()

engine = create_engine(
    settings.database_url.get_secret_value(),
    connect_args={"check_same_thread": False} if settings.database_url.get_secret_value().startswith("sqlite") else {}
)

def init_db() -> None:
    # 明確 import 所有資料表模組，確保其註冊至 SQLModel.metadata。
    from pipeline.models.production_artifact import ProductionArtifact
    from pipeline.models.qc import ContinuityQC, VariantQC
    from pipeline.models.reference_asset import ReferenceAsset
    from pipeline.models.variant import AssetVariant, CapabilityJob

    SQLModel.metadata.create_all(engine)

def check_schema():
    """回傳目前 schema 版本狀態。只讀取，不建立也不修改任何結構。"""
    from pipeline.migrations import status

    return status(engine)

def log_schema_status() -> None:
    """啟動時記錄 schema 版本。落後時發出警告，但不阻斷啟動、不自動套用。"""
    try:
        state = check_schema()
    except Exception as error:  # noqa: BLE001 - schema 檢查不得影響服務啟動
        logger.warning("Schema version check skipped: %s", error)
        return

    if state.is_current:
        logger.info("Schema up to date (version=%s)", state.current)
        return

    logger.warning(
        "Schema out of date: current=%s latest=%s pending=%s. "
        "Run: python scripts/migrate.py upgrade",
        state.current,
        state.latest,
        ", ".join(state.pending),
    )

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
