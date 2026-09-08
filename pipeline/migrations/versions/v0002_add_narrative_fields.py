# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0002_add_narrative_fields.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   為 production_artifacts 新增 Narrative/Shot 層 JSON 欄位。
# 主要責任:
#   1. 新增 production_profile、narrative_ir、character_packs、shot_plans 欄位。
# 說明:
#   四者皆為一次讀寫的 project document，適合以 JSON 欄位保存。
#   大量累積且需統計的 operational data 另建資料表，見 v0004 與 v0005。
#   以 column_exists 保持冪等：全新資料庫由 create_all() 建立時已含這些欄位。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, json_type, table_exists

VERSION = 2
NAME = "add_narrative_fields"

TABLE = "production_artifacts"
COLUMNS = ("production_profile", "narrative_ir", "character_packs", "shot_plans")


def upgrade(conn: Connection, dialect: str) -> None:
    # 基礎資料表由 create_all() 建立，本 migration 只負責增量欄位。
    # 表尚未初始化時無事可做：屆時 create_all() 會直接建出含這些欄位的表。
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
