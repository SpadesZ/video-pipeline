# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0009_add_qc_dimensions.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   為 variant_qc 新增 V1 Benchmark 所需的評分維度。
# 主要責任:
#   1. 新增 identity_consistency、facial_acting、generation_seconds。
# 說明:
#   identity_consistency 衡量單一鏡頭內角色身份是否穩定，與
#   continuity_qc.cross_shot_identity 的鏡頭之間比對是不同語義：
#   一顆鏡頭內臉部漂移屬於前者，兩顆鏡頭像不像同一人屬於後者。
#   facial_acting 衡量表情演技，是對話類鏡頭的主要判準之一。
#   generation_seconds 記錄平台生成耗時，用於比較各平台的產出效率。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, table_exists

VERSION = 9
NAME = "add_qc_dimensions"

TABLE = "variant_qc"
COLUMNS = (
    ("identity_consistency", "INTEGER"),
    ("facial_acting", "INTEGER"),
    ("generation_seconds", "FLOAT"),
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
