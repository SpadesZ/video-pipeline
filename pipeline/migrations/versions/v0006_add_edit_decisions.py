# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0006_add_edit_decisions.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   為 production_artifacts 新增剪輯決策欄位。
# 主要責任:
#   1. 新增 edit_decisions JSON 欄位。
# 說明:
#   剪輯決策是人工調整的成果（in/out point、retime、轉場），必須持久化，
#   不能每次由選定候選重新推導，否則人工的取捨會在重載後遺失。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, json_type, table_exists

VERSION = 6
NAME = "add_edit_decisions"

TABLE = "production_artifacts"
COLUMNS = ("edit_decisions",)


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    column_type = json_type(conn)
    for column in COLUMNS:
        if column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {column_type}"))


def downgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column in reversed(COLUMNS):
        if not column_exists(conn, TABLE, column):
            continue
        conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {column}"))
