# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0007_add_job_request_specs.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   為 capability_jobs 新增派工當下的請求規格欄位。
# 主要責任:
#   1. 新增 requested_duration_ms 與 requested_aspect_ratio。
# 說明:
#   ShotPlan.target_duration_ms 是導演意圖，派工前會經 ProductionProfile
#   的鏡頭長度政策夾住，實際送出去的值可能不同。匯入時若回頭以
#   ShotPlan 為基準計算落差，得到的偏差會是錯的。
#   因此把真正 dispatch 出去的規格記錄在工作上，作為落差比對的唯一基準。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, table_exists

VERSION = 7
NAME = "add_job_request_specs"

TABLE = "capability_jobs"
COLUMNS = (
    ("requested_duration_ms", "INTEGER"),
    ("requested_aspect_ratio", "VARCHAR"),
)


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column, column_type in COLUMNS:
        if column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {column_type}"))


def downgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column, _column_type in reversed(COLUMNS):
        if not column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {column}"))
