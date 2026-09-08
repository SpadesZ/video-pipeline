# 檔案路徑: video-pipeline/pipeline/migrations/versions/v0005_create_variant_tables.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   建立生成候選與品質評分資料表。
# 主要責任:
#   1. 建立 asset_variants、variant_qc、continuity_qc 三張表與索引。
# 說明:
#   asset_variants 為 operational data，一支片可能有 20 顆鏡頭乘以每顆
#   5-10 個候選，故獨立成表而非存於 ProductionArtifact 的 JSON 欄位。
#   variant_qc 與 continuity_qc 分離：前者是單一 clip 自身品質，後者是
#   鏡頭之間的關係，帶 ref_shot_id 與 scope 以明確比較對象。
#   評分欄位皆可為 NULL 表示該維度不適用，不以 0 分混淆。
# --------------------------------------------------------------------------

from sqlalchemy import Connection, text

from pipeline.migrations.runner import json_type, table_exists, timestamp_type

VERSION = 5
NAME = "create_variant_tables"

VARIANTS = "asset_variants"
VARIANT_QC = "variant_qc"
CONTINUITY_QC = "continuity_qc"

INDEXES = (
    ("ix_asset_variants_project_id", VARIANTS, "project_id"),
    ("ix_asset_variants_shot_id", VARIANTS, "shot_id"),
    ("ix_asset_variants_job_id", VARIANTS, "job_id"),
    ("ix_asset_variants_parent_variant_id", VARIANTS, "parent_variant_id"),
    ("ix_asset_variants_provider", VARIANTS, "provider"),
    ("ix_asset_variants_model_id", VARIANTS, "model_id"),
    ("ix_asset_variants_file_hash", VARIANTS, "file_hash"),
    ("ix_asset_variants_status", VARIANTS, "status"),
    ("ix_variant_qc_project_id", VARIANT_QC, "project_id"),
    ("ix_variant_qc_shot_id", VARIANT_QC, "shot_id"),
    ("ix_variant_qc_usable_without_repair", VARIANT_QC, "usable_without_repair"),
    ("ix_continuity_qc_project_id", CONTINUITY_QC, "project_id"),
    ("ix_continuity_qc_scope", CONTINUITY_QC, "scope"),
    ("ix_continuity_qc_shot_id", CONTINUITY_QC, "shot_id"),
    ("ix_continuity_qc_ref_shot_id", CONTINUITY_QC, "ref_shot_id"),
    ("ix_continuity_qc_scene_id", CONTINUITY_QC, "scene_id"),
    ("ix_continuity_qc_variant_id", CONTINUITY_QC, "variant_id"),
)

UNIQUE_INDEXES = (("ix_variant_qc_variant_id", VARIANT_QC, "variant_id"),)


def upgrade(conn: Connection, dialect: str) -> None:
    ts = timestamp_type(conn)
    jtype = json_type(conn)

    if not table_exists(conn, VARIANTS):
        conn.execute(
            text(
                f"CREATE TABLE {VARIANTS} ("
                "variant_id VARCHAR NOT NULL, "
                "project_id VARCHAR NOT NULL, "
                "shot_id VARCHAR NOT NULL, "
                "job_id VARCHAR, "
                "parent_variant_id VARCHAR, "
                "provider VARCHAR NOT NULL, "
                "model_id VARCHAR, "
                "model_version VARCHAR, "
                "generation_mode VARCHAR, "
                "prompt_snapshot VARCHAR NOT NULL, "
                "negative_prompt VARCHAR NOT NULL, "
                f"reference_asset_ids {jtype}, "
                f"provider_parameters {jtype}, "
                "requested_duration_ms INTEGER, "
                "actual_duration_ms INTEGER, "
                "resolution VARCHAR, "
                "fps FLOAT, "
                "file_hash VARCHAR, "
                "local_path VARCHAR, "
                "status VARCHAR NOT NULL, "
                "selected_reason VARCHAR, "
                f"cost {jtype}, "
                f"generation_timestamp {ts}, "
                f"created_at {ts} NOT NULL, "
                "PRIMARY KEY (variant_id)"
                ")"
            )
        )

    if not table_exists(conn, VARIANT_QC):
        conn.execute(
            text(
                f"CREATE TABLE {VARIANT_QC} ("
                "qc_id VARCHAR NOT NULL, "
                "variant_id VARCHAR NOT NULL, "
                "project_id VARCHAR NOT NULL, "
                "shot_id VARCHAR NOT NULL, "
                "prompt_adherence INTEGER, "
                "temporal_stability INTEGER, "
                "motion_quality INTEGER, "
                "camera_control INTEGER, "
                "artifact_severity INTEGER, "
                "usable_without_repair BOOLEAN, "
                "human_correction_minutes FLOAT, "
                "retries_to_usable INTEGER, "
                "reviewer VARCHAR NOT NULL, "
                "notes VARCHAR, "
                f"created_at {ts} NOT NULL, "
                "PRIMARY KEY (qc_id)"
                ")"
            )
        )

    if not table_exists(conn, CONTINUITY_QC):
        conn.execute(
            text(
                f"CREATE TABLE {CONTINUITY_QC} ("
                "qc_id VARCHAR NOT NULL, "
                "project_id VARCHAR NOT NULL, "
                "scope VARCHAR NOT NULL, "
                "shot_id VARCHAR NOT NULL, "
                "ref_shot_id VARCHAR, "
                "scene_id VARCHAR, "
                "variant_id VARCHAR, "
                "ref_variant_id VARCHAR, "
                "cross_shot_identity INTEGER, "
                "wardrobe_continuity INTEGER, "
                "location_continuity INTEGER, "
                "lip_sync_quality INTEGER, "
                "reviewer VARCHAR NOT NULL, "
                "notes VARCHAR, "
                f"created_at {ts} NOT NULL, "
                "PRIMARY KEY (qc_id)"
                ")"
            )
        )

    for index_name, table, column in INDEXES:
        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")
        )
    for index_name, table, column in UNIQUE_INDEXES:
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"
            )
        )


def downgrade(conn: Connection, dialect: str) -> None:
    for index_name, _table, _column in (*INDEXES, *UNIQUE_INDEXES):
        conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    for table in (CONTINUITY_QC, VARIANT_QC, VARIANTS):
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
