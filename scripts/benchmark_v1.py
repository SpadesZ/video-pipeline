# 檔案路徑: video-pipeline/scripts/benchmark_v1.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark 命令列工具。
# 主要責任:
#   1. check  - 檢查人工需準備的素材是否就緒。
#   2. build  - 建立 benchmark 專案並對四個平台各自產出 job packages。
#   3. sheets - 產生人工評分表。
#   4. sync   - 影片匯入後回填 variant_id 至評分表。
#   5. import - 將填好的評分寫回資料庫。
#   6. status - 顯示目前進度。
# 使用範例:
#   docker compose run --rm api python scripts/benchmark_v1.py check
#   docker compose run --rm api python scripts/benchmark_v1.py build
#   docker compose run --rm api python scripts/benchmark_v1.py sync
#   docker compose run --rm api python scripts/benchmark_v1.py import-scores
# --------------------------------------------------------------------------

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.benchmark import builder, score_import, score_sheet, v1_pack
from pipeline.db import init_db
from pipeline.project_store import load_project
from pipeline.settings import get_settings
from pipeline.stages.shot_dispatcher import ShotReadinessState


def sheets_dir(settings) -> Path:
    return Path(settings.data_dir) / "benchmark" / "v1" / "sheets"


def cmd_check() -> int:
    settings = get_settings()
    report = builder.check_assets(settings)
    print(f"素材目錄: {report.assets_dir}")
    print(f"需要 {len(report.statuses)} 個檔案，已就緒 "
          f"{len(report.statuses) - len(report.missing)} 個")
    if report.ready:
        print("OK 全部就緒，可執行 build")
        return 0
    print()
    print("尚缺以下檔案，請放入上述目錄後重試：")
    for line in report.instructions():
        print(f"  - {line}")
    return 1


def cmd_build(providers: tuple[str, ...] | None) -> int:
    settings = get_settings()
    init_db()

    report = builder.check_assets(settings)
    if not report.ready:
        print("素材尚未就緒，先執行 check 並補齊檔案", file=sys.stderr)
        for line in report.instructions():
            print(f"  - {line}", file=sys.stderr)
        return 1

    registered = builder.register_assets(settings)
    print(f"已登錄 {registered} 個參考素材")

    artifact = builder.build_artifact(settings)
    print(f"專案 {artifact.project_id}: {len(artifact.shot_plans)} 顆鏡頭, "
          f"{len(artifact.character_packs)} 個角色")

    targets = providers or v1_pack.TARGET_PROVIDERS
    reports = asyncio.run(builder.dispatch_all(artifact, targets))

    from pipeline.project_store import save_project

    save_project(settings, artifact)

    print()
    blocked_total = 0
    for provider, result in reports.items():
        blocked_total += result.blocked_count + (
            result.failed_count - result.blocked_count
        )
        print(f"{provider:10s} {result.summary()}")
        for item in result.results:
            if item.dispatched:
                continue
            print(f"    ! {item.shot_id} [{item.readiness}] {item.message}")

    variant_path = score_sheet.write_variant_sheet(sheets_dir(settings), targets)
    continuity_path = score_sheet.write_continuity_sheet(sheets_dir(settings), targets)
    print()
    print(f"評分表: {variant_path}")
    print(f"連戲表: {continuity_path}")
    print()
    print(f"預計人工生成次數: {len(v1_pack.shots())} 鏡頭 x {len(targets)} 平台 "
          f"x {v1_pack.CANDIDATES_PER_SHOT} 候選 = "
          f"{len(v1_pack.shots()) * len(targets) * v1_pack.CANDIDATES_PER_SHOT}")
    return 1 if blocked_total else 0


def cmd_sheets() -> int:
    settings = get_settings()
    variant_path, continuity_path = score_sheet.write_all(sheets_dir(settings))
    print(f"OK {variant_path}")
    print(f"OK {continuity_path}")
    return 0


def cmd_sync() -> int:
    settings = get_settings()
    init_db()
    path, rows = score_sheet.sync_variant_sheet(
        sheets_dir(settings), v1_pack.BENCHMARK_PROJECT_ID
    )
    print(f"OK 已回填 {rows} 列候選至 {path}")
    if rows == 0:
        print("尚未匯入任何候選影片，請先於專案頁匯入", file=sys.stderr)
        return 1
    return 0


def cmd_import(reviewer: str) -> int:
    settings = get_settings()
    init_db()
    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    if artifact is None:
        print("找不到 benchmark 專案，請先執行 build", file=sys.stderr)
        return 1

    directory = sheets_dir(settings)
    exit_code = 0
    for name, importer in (
        (score_sheet.VARIANT_SHEET, score_import.import_variant_scores),
        (score_sheet.CONTINUITY_SHEET, score_import.import_continuity_scores),
    ):
        path = directory / name
        if not path.exists():
            print(f"略過 {name}（不存在）")
            continue
        result = importer(artifact, path, reviewer=reviewer)
        print(f"{name}: 匯入 {result.applied} 列, 略過 {len(result.skipped)} 列")
        for error in result.errors:
            print(f"    ! {error}", file=sys.stderr)
        if not result.ok:
            exit_code = 1

    from pipeline.project_store import save_project

    save_project(settings, artifact)
    return exit_code


def cmd_status() -> int:
    settings = get_settings()
    init_db()

    assets = builder.check_assets(settings)
    print(f"素材: {len(assets.statuses) - len(assets.missing)}/"
          f"{len(assets.statuses)} 就緒")

    artifact = load_project(settings, v1_pack.BENCHMARK_PROJECT_ID)
    if artifact is None:
        print("專案: 尚未建立")
        return 0

    from pipeline.stages.shot_dispatcher import assess_project_readiness

    readiness = assess_project_readiness(artifact)
    not_ready = [
        shot_id
        for shot_id, item in readiness.items()
        if item.state is not ShotReadinessState.READY
    ]
    print(f"鏡頭: {len(artifact.shot_plans)} 顆, 未就緒 {len(not_ready)} 顆")
    for shot_id in not_ready:
        print(f"    ! {shot_id}: {'; '.join(readiness[shot_id].issues)}")

    jobs = builder.jobs_summary()
    print(f"派工: {sum(jobs.values())} 筆")
    for provider in v1_pack.TARGET_PROVIDERS:
        print(f"    {provider:10s} {jobs.get(provider, 0)}")

    from pipeline.stages.shot_qc import summarize_project_qc
    from pipeline.stages.variant_importer import list_variants

    variants = list_variants(v1_pack.BENCHMARK_PROJECT_ID)
    print(f"候選: {len(variants)} 個")

    summary = summarize_project_qc(artifact)
    print(f"已評分鏡頭: {sum(1 for s in summary.shots if s.scored_count)}")
    print(f"可用鏡頭: {summary.usable_count}/{summary.planned_count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V1 Benchmark 工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="檢查素材是否就緒")
    build = sub.add_parser("build", help="建立專案並產出 job packages")
    build.add_argument(
        "--providers", nargs="*", default=None,
        help=f"指定平台，預設全部: {' '.join(v1_pack.TARGET_PROVIDERS)}",
    )
    sub.add_parser("sheets", help="產生空白評分表")
    sub.add_parser("sync", help="影片匯入後回填 variant_id")
    imp = sub.add_parser("import-scores", help="匯入填好的評分")
    imp.add_argument("--reviewer", default="local")
    sub.add_parser("status", help="顯示目前進度")

    args = parser.parse_args()
    command = args.command or "status"

    if command == "check":
        return cmd_check()
    if command == "build":
        providers = tuple(args.providers) if args.providers else None
        return cmd_build(providers)
    if command == "sheets":
        return cmd_sheets()
    if command == "sync":
        return cmd_sync()
    if command == "import-scores":
        return cmd_import(args.reviewer)
    if command == "status":
        return cmd_status()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
