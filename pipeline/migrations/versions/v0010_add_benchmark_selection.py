# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0010_add_benchmark_selection.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   為 asset_variants 新增 benchmark 選定旗標與原始檔名。
# 主要責任:
#   1. 新增 benchmark_selected 欄位與索引。
#   2. 新增 original_filename，供嘗試紀錄精確對應候選。
# 說明:
#   production 的 status=selected 是每顆鏡頭全域單選，用於決定成片要用
#   哪一支。benchmark 需要的是每個「比較對象 × 鏡頭」各自選一支代表作，
#   四個對象就會有四支同時被選中。兩者語義不同，共用同一個欄位會使
#   benchmark 無法比較，因此分開。
#
#   匯入時檔案會改名為 variant_id，原始檔名若不保留，嘗試紀錄的
#   output_file 就無法對應到候選，只能依匯入順序猜測。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import column_exists, table_exists

VERSION = 10
NAME = "add_benchmark_selection"

TABLE = "asset_variants"
INDEX = "ix_asset_variants_benchmark_selected"
COLUMNS = (
    ("benchmark_selected", "BOOLEAN"),
    ("original_filename", "VARCHAR"),
)


def upgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    for column, column_type in COLUMNS:
        if not column_exists(conn, TABLE, column):
            conn.execute(
                text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {column_type}")
            )
    conn.execute(
        text(f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} (benchmark_selected)")
    )


def downgrade(conn: Connection, dialect: str) -> None:
    if not table_exists(conn, TABLE):
        return
    conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
    for column, _column_type in reversed(COLUMNS):
        if column_exists(conn, TABLE, column):
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {column}"))
