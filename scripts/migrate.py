# 檔案路徑: video-pipeline/scripts/migrate.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Schema migration 命令列工具。
# 主要責任:
#   1. status - 顯示目前版本、最新版本與待套用清單。
#   2. check  - 供 CI 使用，schema 落後時以非零狀態碼結束。
#   3. upgrade / downgrade - 明確套用或回退指定版本。
# 使用範例:
#   docker compose run --rm api python scripts/migrate.py status
#   docker compose run --rm api python scripts/migrate.py check
#   docker compose run --rm api python scripts/migrate.py upgrade
#   docker compose run --rm api python scripts/migrate.py downgrade --target 1
# --------------------------------------------------------------------------

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.db import engine
from pipeline.migrations import (
    MigrationError,
    discover_migrations,
    downgrade,
    latest_version,
    status,
    upgrade,
)


def cmd_status() -> int:
    current = status(engine)
    print(f"dialect        : {engine.dialect.name}")
    print(f"current version: {current.current}")
    print(f"latest version : {current.latest}")
    if current.pending:
        print(f"pending        : {', '.join(current.pending)}")
    else:
        print("pending        : (none)")
    return 0


def cmd_check() -> int:
    current = status(engine)
    if current.is_current:
        print(f"OK schema up to date (version={current.current})")
        return 0
    print(
        f"SCHEMA OUT OF DATE: current={current.current} latest={current.latest}",
        file=sys.stderr,
    )
    print(f"pending: {', '.join(current.pending)}", file=sys.stderr)
    print("run: python scripts/migrate.py upgrade", file=sys.stderr)
    return 1


def cmd_upgrade(target: int | None) -> int:
    applied = upgrade(engine, target=target)
    if not applied:
        print("OK nothing to apply")
        return 0
    for migration in applied:
        print(f"applied {migration.label}")
    print(f"OK schema at version {status(engine).current}")
    return 0


def cmd_downgrade(target: int) -> int:
    reverted = downgrade(engine, target=target)
    if not reverted:
        print("OK nothing to revert")
        return 0
    for migration in reverted:
        print(f"reverted {migration.label}")
    print(f"OK schema at version {status(engine).current}")
    return 0


def cmd_list() -> int:
    for migration in discover_migrations():
        print(migration.label)
    print(f"latest={latest_version()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Schema migration 工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="顯示版本狀態")
    sub.add_parser("check", help="schema 落後時以非零狀態碼結束")
    sub.add_parser("list", help="列出所有已知 migration")

    up = sub.add_parser("upgrade", help="套用未套用的 migration")
    up.add_argument("--target", type=int, default=None, help="套用至此版本為止")

    down = sub.add_parser("downgrade", help="回退至指定版本")
    down.add_argument("--target", type=int, required=True, help="回退後的目標版本")

    args = parser.parse_args()
    command = args.command or "status"

    try:
        if command == "status":
            return cmd_status()
        if command == "check":
            return cmd_check()
        if command == "list":
            return cmd_list()
        if command == "upgrade":
            return cmd_upgrade(args.target)
        if command == "downgrade":
            return cmd_downgrade(args.target)
    except MigrationError as error:
        print(f"MIGRATION FAILED: {error}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
