# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0001_baseline.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Migration 基準點，標記 schema 處於 SQLModel create_all() 的初始狀態。
# 主要責任:
#   1. 為既有資料庫建立版本基準，使後續增量 migration 有明確起點。
# 說明:
#   本版本刻意為 no-op。此時 production_artifacts 由 create_all() 建立，
#   全新資料庫與既有資料庫套用後結果一致，因此不執行任何 DDL。
#   自 v0002 起的 migration 才會實際變更 schema。
# --------------------------------------------------------------------------

from sqlalchemy import Connection

VERSION = 1
NAME = "baseline"


def upgrade(conn: Connection, dialect: str) -> None:
    """基準點不變更 schema，僅由 runner 記錄版本。"""
    return None


def downgrade(conn: Connection, dialect: str) -> None:
    """基準點無可回退的變更。"""
    return None
