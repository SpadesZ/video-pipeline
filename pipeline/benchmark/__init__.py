# 檔案路徑: video-pipeline/pipeline/benchmark/__init__.py
# 產生時間: 2026-09-05 +08:00
# 版本: v2.0
# 模組定位:
#   Benchmark Pack 套件入口。
# 主要責任:
#   1. 對外導出 V1 固定測試案例、比較對象、嘗試紀錄與統計公式。
# 說明:
#   Benchmark 的目的不是選出單一最佳模型，而是找出各情境下的適用平台，
#   供 RoutingPolicy 依 scenario 決定「這顆鏡頭該給誰拍」。
#   統計公式於 aggregation.py 預先固定，不得在看到結果後調整。
# --------------------------------------------------------------------------

from pipeline.benchmark import aggregation, attempts, v1_pack
from pipeline.benchmark.asset_validation import ImageCheck, validate_image
from pipeline.benchmark.attempts import (
    Attempt,
    AttemptLedger,
    AttemptStatus,
    read_ledger,
    write_blank_ledger,
)
from pipeline.benchmark.builder import (
    AssetReport,
    AssetStatus,
    build_artifact,
    check_assets,
    dispatch_all,
    dispatch_target,
    jobs_summary,
    package_index,
    register_assets,
)
from pipeline.benchmark.target import (
    BenchmarkTarget,
    TargetRegistry,
    load_targets,
    resolve,
    targets,
)

__all__ = [
    "AssetReport",
    "AssetStatus",
    "Attempt",
    "AttemptLedger",
    "AttemptStatus",
    "BenchmarkTarget",
    "ImageCheck",
    "TargetRegistry",
    "aggregation",
    "attempts",
    "build_artifact",
    "check_assets",
    "dispatch_all",
    "dispatch_target",
    "jobs_summary",
    "load_targets",
    "package_index",
    "read_ledger",
    "register_assets",
    "resolve",
    "targets",
    "v1_pack",
    "validate_image",
    "write_blank_ledger",
]
