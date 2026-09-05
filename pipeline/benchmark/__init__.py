# 檔案路徑: video-pipeline/pipeline/benchmark/__init__.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   Benchmark Pack 套件入口。
# 主要責任:
#   1. 對外導出 V1 固定測試案例與建立流程。
# 說明:
#   Benchmark 的目的不是選出單一最佳模型，而是找出各情境下的適用平台，
#   供 RoutingPolicy 依 scenario 決定「這顆鏡頭該給誰拍」。
# --------------------------------------------------------------------------

from pipeline.benchmark import v1_pack
from pipeline.benchmark.builder import (
    AssetReport,
    AssetStatus,
    build_artifact,
    check_assets,
    dispatch_all,
    dispatch_provider,
    jobs_summary,
    package_index,
    register_assets,
)

__all__ = [
    "AssetReport",
    "AssetStatus",
    "build_artifact",
    "check_assets",
    "dispatch_all",
    "dispatch_provider",
    "jobs_summary",
    "package_index",
    "register_assets",
    "v1_pack",
]
