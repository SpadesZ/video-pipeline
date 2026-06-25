# 檔案路徑: video-pipeline/apps/api/app/deps.py
# 產生時間: 2026-06-25 14:20 +08:00
# 版本: v1.0
# 模組定位:
#   FastAPI 依賴注入管理模組。
# 主要責任:
#   1. 提供全域 Settings 依賴設定。
#   2. 提供資料庫 Session 依賴注入。
# --------------------------------------------------------------------------

from functools import lru_cache
from typing import Generator
from sqlmodel import Session
from pipeline.settings import Settings, get_settings
from pipeline.db import get_session as db_get_session


@lru_cache()
def settings_dep() -> Settings:
    return get_settings()

def get_session() -> Generator[Session, None, None]:
    yield from db_get_session()
