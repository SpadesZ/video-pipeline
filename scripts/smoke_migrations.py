# 檔案路徑: video-pipeline/scripts/smoke_migrations.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.1
# 模組定位:
#   Schema migration 系統確定性冒煙測試。
# 主要責任:
#   1. 驗證 migration 探索結果版本號連續且不重複。
#   2. 驗證 status 為只讀操作，不會自行建立 schema_version 表。
#   3. 情境 A - 未初始化的資料庫可安全跑完全部 migration 與回退。
#   4. 情境 B - create_all() 之後套用 migration（實際部署流程）。
#   5. 比對 create_all() 與 migration 建出的資料表欄位是否一致，
#      藉此攔截模型與 migration 不同步。
# 說明:
#   使用臨時 SQLite 檔，不依賴 Postgres 或任何 API 金鑰，可於 native Python 執行。
# --------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="migrate_smoke_")
_DB_PATH = Path(_TMP_DIR) / "smoke.db"

os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from sqlalchemy import text
from sqlmodel import SQLModel, create_engine

import pipeline.models  # noqa: F401 - 註冊全部資料表至 metadata
from pipeline.db import engine
from pipeline.migrations import (
    column_exists,
    discover_migrations,
    downgrade,
    latest_version,
    status,
    table_exists,
    upgrade,
)
from pipeline.migrations.runner import SCHEMA_VERSION_TABLE

MIGRATION_TABLES = ("reference_assets", "capability_jobs", "asset_variants",
                    "variant_qc", "continuity_qc")
NARRATIVE_COLUMNS = ("production_profile", "narrative_ir", "character_packs",
                     "shot_plans")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sqlite_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return {row[1] for row in rows}


def new_engine(name: str):
    path = Path(_TMP_DIR) / name
    return create_engine(f"sqlite:///{path.as_posix()}")


def verify_registry() -> list:
    migrations = discover_migrations()
    check(bool(migrations), "未探索到任何 migration")

    versions = [m.version for m in migrations]
    check(versions == sorted(versions), "migration 未依版本排序")
    check(len(versions) == len(set(versions)), "migration 版本號重複")
    check(versions[0] == 1, f"migration 應自版本 1 起算，實際為 {versions[0]}")
    for previous, nxt in zip(versions, versions[1:]):
        check(nxt == previous + 1, f"版本號不連續: {previous} -> {nxt}")
    return migrations


def verify_status_is_read_only() -> None:
    initial = status(engine)
    check(initial.current == 0, f"全新資料庫版本應為 0，實際為 {initial.current}")
    check(not initial.is_current, "全新資料庫不應被視為已是最新版本")
    with engine.connect() as conn:
        check(
            not table_exists(conn, SCHEMA_VERSION_TABLE),
            "status() 不應建立 schema_version 表，只讀操作不得改寫 schema",
        )


def verify_bare_database(migrations: list) -> None:
    """情境 A：資料庫未經 create_all() 初始化。"""
    applied = upgrade(engine)
    check(
        len(applied) == len(migrations),
        f"應套用 {len(migrations)} 個 migration，實際 {len(applied)}",
    )
    check(status(engine).is_current, "套用後仍有待處理項目")
    check(not upgrade(engine), "重複 upgrade 應為無操作")

    with engine.connect() as conn:
        for table in MIGRATION_TABLES:
            check(table_exists(conn, table), f"migration 未建立資料表 {table}")

    reverted = downgrade(engine, target=0)
    check(
        len(reverted) == len(migrations),
        f"應回退 {len(migrations)} 個 migration，實際 {len(reverted)}",
    )
    check(status(engine).current == 0, "回退後版本應為 0")

    with engine.connect() as conn:
        for table in MIGRATION_TABLES:
            check(not table_exists(conn, table), f"回退後資料表 {table} 應已移除")
        rows = conn.execute(
            text(f"SELECT count(*) FROM {SCHEMA_VERSION_TABLE}")
        ).scalar_one()
    check(rows == 0, f"回退後 schema_version 應為空，實際有 {rows} 筆")

    check(len(upgrade(engine)) == len(migrations), "回退後應可重新套用全部 migration")


def verify_with_base_schema(migrations: list) -> None:
    """情境 B：先 create_all() 再套用 migration，即實際部署流程。"""
    base_engine = new_engine("with_base.db")
    SQLModel.metadata.create_all(base_engine)

    applied = upgrade(base_engine)
    check(
        len(applied) == len(migrations),
        "create_all() 之後仍應記錄全部 migration 版本",
    )
    check(status(base_engine).is_current, "情境 B 套用後應為最新版本")
    check(not upgrade(base_engine), "情境 B 重複 upgrade 應為無操作")

    with base_engine.connect() as conn:
        for column in NARRATIVE_COLUMNS:
            check(
                column_exists(conn, "production_artifacts", column),
                f"production_artifacts 缺少欄位 {column}",
            )
        for table in MIGRATION_TABLES:
            check(table_exists(conn, table), f"情境 B 缺少資料表 {table}")


def verify_schema_parity() -> None:
    """create_all() 與 migration 建出的資料表欄位必須一致。"""
    model_engine = new_engine("parity_model.db")
    SQLModel.metadata.create_all(model_engine)

    migration_engine = new_engine("parity_migration.db")
    upgrade(migration_engine)

    with model_engine.connect() as model_conn, migration_engine.connect() as mig_conn:
        for table in MIGRATION_TABLES:
            model_cols = sqlite_columns(model_conn, table)
            migration_cols = sqlite_columns(mig_conn, table)
            missing = model_cols - migration_cols
            extra = migration_cols - model_cols
            check(
                not missing,
                f"{table}: migration 缺少模型欄位 {sorted(missing)}",
            )
            check(
                not extra,
                f"{table}: migration 多出模型沒有的欄位 {sorted(extra)}",
            )


def main() -> int:
    migrations = verify_registry()
    verify_status_is_read_only()
    verify_bare_database(migrations)
    verify_with_base_schema(migrations)
    verify_schema_parity()

    print(
        f"OK migrations smoke dialect=sqlite count={len(migrations)} "
        f"latest={latest_version()} tables={len(MIGRATION_TABLES)} parity=ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
