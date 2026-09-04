# 檔案路徑: video-pipeline/scripts/smoke_migrations.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Schema migration 系統確定性冒煙測試。
# 主要責任:
#   1. 驗證 migration 探索結果版本號連續且不重複。
#   2. 在全新 SQLite 上驗證 upgrade -> check -> downgrade 完整循環。
#   3. 驗證 status 為只讀操作，不會自行建立 schema_version 表。
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

from pipeline.db import engine
from pipeline.migrations import (
    discover_migrations,
    downgrade,
    latest_version,
    status,
    table_exists,
    upgrade,
)
from pipeline.migrations.runner import SCHEMA_VERSION_TABLE


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    migrations = discover_migrations()
    check(bool(migrations), "未探索到任何 migration")

    versions = [m.version for m in migrations]
    check(versions == sorted(versions), "migration 未依版本排序")
    check(len(versions) == len(set(versions)), "migration 版本號重複")
    check(versions[0] == 1, f"migration 應自版本 1 起算，實際為 {versions[0]}")
    for previous, nxt in zip(versions, versions[1:]):
        check(nxt == previous + 1, f"版本號不連續: {previous} -> {nxt}")

    # status 必須是只讀：不得建立 schema_version 表
    initial = status(engine)
    check(initial.current == 0, f"全新資料庫版本應為 0，實際為 {initial.current}")
    check(not initial.is_current, "全新資料庫不應被視為已是最新版本")
    with engine.connect() as conn:
        check(
            not table_exists(conn, SCHEMA_VERSION_TABLE),
            "status() 不應建立 schema_version 表，只讀操作不得改寫 schema",
        )

    applied = upgrade(engine)
    check(
        len(applied) == len(migrations),
        f"應套用 {len(migrations)} 個 migration，實際 {len(applied)}",
    )

    after_upgrade = status(engine)
    check(after_upgrade.is_current, f"套用後仍有待處理項目: {after_upgrade.pending}")
    check(
        after_upgrade.current == latest_version(),
        f"套用後版本應為 {latest_version()}，實際為 {after_upgrade.current}",
    )

    # 重複套用需為無操作
    check(not upgrade(engine), "重複 upgrade 應為無操作")

    reverted = downgrade(engine, target=0)
    check(
        len(reverted) == len(migrations),
        f"應回退 {len(migrations)} 個 migration，實際 {len(reverted)}",
    )

    after_downgrade = status(engine)
    check(
        after_downgrade.current == 0,
        f"回退後版本應為 0，實際為 {after_downgrade.current}",
    )

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT count(*) FROM {SCHEMA_VERSION_TABLE}")
        ).scalar_one()
    check(rows == 0, f"回退後 schema_version 應為空，實際有 {rows} 筆")

    # 回退後可再次套用
    check(len(upgrade(engine)) == len(migrations), "回退後應可重新套用全部 migration")

    print(
        f"OK migrations smoke dialect=sqlite count={len(migrations)} "
        f"latest={latest_version()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
