# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0003_create_reference_assets.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   建立一級參考素材資料表 reference_assets。
# 主要責任:
#   1. 建立 reference_assets 表與其索引。
# 說明:
#   asset_type 與 rights 以 VARCHAR 儲存並由 Python 列舉驗證，不建立資料庫
#   原生 enum 型別，避免日後新增值需要無法在交易內執行且無法回退的
#   ALTER TYPE ADD VALUE。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import json_type, table_exists, timestamp_type

VERSION = 3
NAME = "create_reference_assets"

TABLE = "reference_assets"
INDEXES = (
    ("ix_reference_assets_project_id", "project_id"),
    ("ix_reference_assets_asset_type", "asset_type"),
    ("ix_reference_assets_file_hash", "file_hash"),
)


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        conn.execute(
            text(
                f"CREATE TABLE {TABLE} ("
                "asset_id VARCHAR NOT NULL, "
                "project_id VARCHAR NOT NULL, "
                "asset_type VARCHAR NOT NULL, "
                "label VARCHAR, "
                "local_path VARCHAR, "
                "file_hash VARCHAR, "
                "source VARCHAR, "
                "rights VARCHAR NOT NULL, "
                f"asset_metadata {json_type(conn)}, "
                f"created_at {timestamp_type(conn)} NOT NULL, "
                "PRIMARY KEY (asset_id)"
                ")"
            )
        )
    for index_name, column in INDEXES:
        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {TABLE} ({column})")
        )


def downgrade(conn: Connection, dialect: str) -> None:
    for index_name, _column in INDEXES:
        conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
