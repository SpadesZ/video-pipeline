# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0008_add_job_lineage.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   為 capability_jobs 新增不可變的派工快照欄位。
# 主要責任:
#   1. 新增 request_snapshot、provider_parameters、reference_asset_ids、
#      manifest_path。
# 說明:
#   匯入候選時若從當前 ShotPlan 回推提示詞與參考素材，一旦分鏡在派工後
#   被修改，血緣就會指向從未真正送出去的內容。生成的來源必須凍結在派工
#   當下，因此把完整請求快照存在工作上，匯入時只讀這份快照。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, json_type, table_exists

VERSION = 8
NAME = "add_job_lineage"

TABLE = "capability_jobs"


def _columns(conn: Connection) -> tuple[tuple[str, str], ...]:
    jtype = json_type(conn)
    return (
        ("request_snapshot", jtype),
        ("provider_parameters", jtype),
        ("reference_asset_ids", jtype),
        ("manifest_path", "VARCHAR"),
    )


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column, column_type in _columns(conn):
        if column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {column_type}"))


def downgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column, _column_type in reversed(_columns(conn)):
        if not column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {column}"))
