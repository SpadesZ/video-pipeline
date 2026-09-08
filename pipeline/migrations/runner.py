# 檔案路徑: video-pipeline/pipeline/migrations/runner.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   顯式版本化 schema migration 執行核心。
# 主要責任:
#   1. 維護 schema_version 表並記錄已套用版本。
#   2. 探索 versions/ 下的 migration 模組並依序 upgrade/downgrade。
#   3. 提供方言感知（SQLite / PostgreSQL）的 DDL 輔助函式。
#   4. 提供只讀的 schema 版本檢查，供應用啟動時驗證，絕不自動改寫 schema。
# --------------------------------------------------------------------------

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import Connection, inspect, text
from sqlalchemy.engine import Engine

SCHEMA_VERSION_TABLE = "schema_version"


class MigrationError(RuntimeError):
    """Migration 套用或回退失敗。"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection, str], None]
    downgrade: Callable[[Connection, str], None]

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


# --------------------------------------------------------------------------
# 方言感知輔助函式：供各 migration 明確呼叫，不做任何自動比對
# --------------------------------------------------------------------------

def dialect_of(conn: Connection) -> str:
    return conn.engine.dialect.name


def table_exists(conn: Connection, table: str) -> bool:
    # 使用公開的 inspect API。dialect.has_table 是內部介面，缺少 info_cache
    # 參數時在部分方言下會回報錯誤結果。
    return inspect(conn).has_table(table)


def column_exists(conn: Connection, table: str, column: str) -> bool:
    if not table_exists(conn, table):
        return False
    columns = {col["name"] for col in inspect(conn).get_columns(table)}
    return column in columns


def json_type(conn: Connection) -> str:
    """兩種方言皆支援的 JSON 欄位型別。"""
    return "JSON" if dialect_of(conn) == "postgresql" else "TEXT"


def timestamp_type(conn: Connection) -> str:
    return "TIMESTAMP" if dialect_of(conn) == "postgresql" else "DATETIME"


# --------------------------------------------------------------------------
# schema_version 表
# --------------------------------------------------------------------------

def ensure_version_table(conn: Connection) -> None:
    """建立 schema_version 表本身。這是 migration 系統的元資料，不屬於任何版本。"""
    if table_exists(conn, SCHEMA_VERSION_TABLE):
        return
    ts = timestamp_type(conn)
    conn.execute(
        text(
            f"CREATE TABLE {SCHEMA_VERSION_TABLE} ("
            "version INTEGER PRIMARY KEY, "
            "name VARCHAR(255) NOT NULL, "
            f"applied_at {ts} NOT NULL"
            ")"
        )
    )


def applied_versions(conn: Connection) -> list[int]:
    if not table_exists(conn, SCHEMA_VERSION_TABLE):
        return []
    rows = conn.execute(
        text(f"SELECT version FROM {SCHEMA_VERSION_TABLE} ORDER BY version")
    ).fetchall()
    return [int(row[0]) for row in rows]


def current_version(conn: Connection) -> int:
    versions = applied_versions(conn)
    return versions[-1] if versions else 0


def _record_applied(conn: Connection, migration: Migration) -> None:
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, name, applied_at) "
            "VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": migration.version,
            "name": migration.name,
            "applied_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )


def _record_reverted(conn: Connection, migration: Migration) -> None:
    conn.execute(
        text(f"DELETE FROM {SCHEMA_VERSION_TABLE} WHERE version = :version"),
        {"version": migration.version},
    )


# --------------------------------------------------------------------------
# 探索
# --------------------------------------------------------------------------

def discover_migrations() -> list[Migration]:
    """掃描 versions/ 套件，回傳依版本排序的 migration 清單。"""
    from pipeline.migrations import versions as versions_pkg

    found: list[Migration] = []
    for module_info in pkgutil.iter_modules(versions_pkg.__path__):
        if not module_info.name.startswith("v"):
            continue
        module = importlib.import_module(
            f"{versions_pkg.__name__}.{module_info.name}"
        )
        for attr in ("VERSION", "NAME", "upgrade", "downgrade"):
            if not hasattr(module, attr):
                raise MigrationError(
                    f"Migration {module_info.name} 缺少必要屬性: {attr}"
                )
        found.append(
            Migration(
                version=int(module.VERSION),
                name=str(module.NAME),
                upgrade=module.upgrade,
                downgrade=module.downgrade,
            )
        )

    found.sort(key=lambda m: m.version)
    seen: set[int] = set()
    for migration in found:
        if migration.version in seen:
            raise MigrationError(f"版本號重複: {migration.version}")
        seen.add(migration.version)
    return found


def latest_version(migrations: Iterable[Migration] | None = None) -> int:
    items = list(migrations) if migrations is not None else discover_migrations()
    return items[-1].version if items else 0


# --------------------------------------------------------------------------
# 套用與回退
# --------------------------------------------------------------------------

def upgrade(engine: Engine, target: int | None = None) -> list[Migration]:
    """套用所有未套用且版本 <= target 的 migration。回傳實際套用的清單。"""
    migrations = discover_migrations()
    applied: list[Migration] = []

    with engine.begin() as conn:
        ensure_version_table(conn)
        done = set(applied_versions(conn))

    for migration in migrations:
        if migration.version in done:
            continue
        if target is not None and migration.version > target:
            break
        with engine.begin() as conn:
            try:
                migration.upgrade(conn, dialect_of(conn))
                _record_applied(conn, migration)
            except Exception as error:  # noqa: BLE001 - 需附上版本資訊再拋出
                raise MigrationError(
                    f"套用 {migration.label} 失敗: {error}"
                ) from error
        applied.append(migration)

    return applied


def downgrade(engine: Engine, target: int) -> list[Migration]:
    """回退至指定版本（含）。回傳實際回退的清單，由高版本往低版本執行。"""
    migrations = discover_migrations()

    with engine.begin() as conn:
        ensure_version_table(conn)
        done = set(applied_versions(conn))

    reverted: list[Migration] = []
    for migration in sorted(migrations, key=lambda m: m.version, reverse=True):
        if migration.version not in done or migration.version <= target:
            continue
        with engine.begin() as conn:
            try:
                migration.downgrade(conn, dialect_of(conn))
                _record_reverted(conn, migration)
            except Exception as error:  # noqa: BLE001
                raise MigrationError(
                    f"回退 {migration.label} 失敗: {error}"
                ) from error
        reverted.append(migration)

    return reverted


# --------------------------------------------------------------------------
# 只讀檢查
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SchemaStatus:
    current: int
    latest: int
    pending: list[str]

    @property
    def is_current(self) -> bool:
        return not self.pending


def status(engine: Engine) -> SchemaStatus:
    """只讀取版本狀態，不建立任何表、不修改 schema。"""
    migrations = discover_migrations()
    with engine.connect() as conn:
        done = set(applied_versions(conn))
    pending = [m.label for m in migrations if m.version not in done]
    return SchemaStatus(
        current=max(done) if done else 0,
        latest=latest_version(migrations),
        pending=pending,
    )
