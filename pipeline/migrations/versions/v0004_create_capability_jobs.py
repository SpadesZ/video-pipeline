# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0004_create_capability_jobs.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   建立生成工作資料表 capability_jobs。
# 主要責任:
#   1. 建立 capability_jobs 表與其索引。
# 說明:
#   本表獨立於 asset_variants，因為失敗、取消與逾期的工作也必須留存，
#   否則 retry rate 與 generations_attempted 無從計算。
#   request_hash 為 job manifest 的冪等鍵，用於阻擋同一份工作重複匯入。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import table_exists, timestamp_type

VERSION = 4
NAME = "create_capability_jobs"

TABLE = "capability_jobs"
INDEXES = (
    ("ix_capability_jobs_project_id", "project_id"),
    ("ix_capability_jobs_shot_id", "shot_id"),
    ("ix_capability_jobs_capability", "capability"),
    ("ix_capability_jobs_provider", "provider"),
    ("ix_capability_jobs_status", "status"),
    ("ix_capability_jobs_request_hash", "request_hash"),
)


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        ts = timestamp_type(conn)
        conn.execute(
            text(
                f"CREATE TABLE {TABLE} ("
                "job_id VARCHAR NOT NULL, "
                "project_id VARCHAR NOT NULL, "
                "shot_id VARCHAR, "
                "capability VARCHAR NOT NULL, "
                "provider VARCHAR NOT NULL, "
                "model_id VARCHAR, "
                "model_version VARCHAR, "
                "transport VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, "
                "request_hash VARCHAR, "
                "provider_job_id VARCHAR, "
                f"submitted_at {ts}, "
                f"completed_at {ts}, "
                "error_code VARCHAR, "
                "error_message VARCHAR, "
                f"created_at {ts} NOT NULL, "
                "PRIMARY KEY (job_id)"
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
