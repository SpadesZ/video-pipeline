# 檔案路徑: video-pipeline/pipeline/benchmark/score_sheet.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 人工評分表產生器。
# 主要責任:
#   1. 產出單鏡頭評分表，每個平台每顆鏡頭各 N 列候選。
#   2. 產出跨鏡頭連戲評分表。
# 說明:
#   評分表以 CSV 產出，供人工邊看影片邊填。留空即代表該維度不適用，
#   不可填 0：0 分代表「極差」，與「不適用」是兩件事，混用會拉低加權。
#   成本相關欄位（credits）目前無法由系統取得，一併放在此表由人工記錄。
# --------------------------------------------------------------------------

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.benchmark import v1_pack

VARIANT_SHEET = "v1_variant_scores.csv"
CONTINUITY_SHEET = "v1_continuity_scores.csv"

VARIANT_COLUMNS = (
    "scenario",
    "shot_id",
    "provider",
    "candidate_no",
    # 影片匯入後由 sync 指令回填，匯入評分時據此對應
    "variant_id",
    "video_filename",
    "generation_seconds",
    "credits_used",
    "retries_to_usable",
    "human_correction_minutes",
    "identity_consistency",
    "temporal_stability",
    "prompt_adherence",
    "motion_quality",
    "camera_control",
    "facial_acting",
    "artifact_severity",
    "usable_without_repair",
    "notes",
)

CONTINUITY_COLUMNS = (
    "scenario",
    "shot_id",
    "ref_shot_id",
    "provider",
    "variant_id",
    "ref_variant_id",
    "cross_shot_identity",
    "wardrobe_continuity",
    "location_continuity",
    "lip_sync_quality",
    "notes",
)

# 對話情境以外的鏡頭沒有明顯的表情演技可評，預先標為 n/a 提醒評分者留空
NON_DIALOGUE_SCENARIOS = {"single_character_cinematic", "high_dynamic_action"}


def write_variant_sheet(
    output_dir: Path,
    providers: tuple[str, ...] | None = None,
    candidates: int | None = None,
) -> Path:
    targets = providers or v1_pack.TARGET_PROVIDERS
    per_shot = candidates or v1_pack.CANDIDATES_PER_SHOT
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VARIANT_SHEET

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=VARIANT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for provider in targets:
                for index in range(1, per_shot + 1):
                    row = {column: "" for column in VARIANT_COLUMNS}
                    row["scenario"] = scenario
                    row["shot_id"] = shot.shot_id
                    row["provider"] = provider
                    row["candidate_no"] = index
                    if scenario in NON_DIALOGUE_SCENARIOS:
                        row["facial_acting"] = "n/a"
                    writer.writerow(row)
    return path


def write_continuity_sheet(
    output_dir: Path, providers: tuple[str, ...] | None = None
) -> Path:
    targets = providers or v1_pack.TARGET_PROVIDERS
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CONTINUITY_SHEET

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTINUITY_COLUMNS)
        writer.writeheader()
        for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
            scenario = v1_pack.SHOT_SCENARIOS[shot_id]
            for provider in targets:
                row = {column: "" for column in CONTINUITY_COLUMNS}
                row["scenario"] = scenario
                row["shot_id"] = shot_id
                row["ref_shot_id"] = ref_shot_id
                row["provider"] = provider
                if scenario in NON_DIALOGUE_SCENARIOS:
                    row["lip_sync_quality"] = "n/a"
                writer.writerow(row)
    return path


def write_all(output_dir: Path) -> tuple[Path, Path]:
    return write_variant_sheet(output_dir), write_continuity_sheet(output_dir)


def sync_variant_sheet(output_dir: Path, project_id: str) -> tuple[Path, int]:
    """依實際匯入的候選重寫評分表，回填 variant_id 與檔名。

    影片匯入後執行。原本的空白表只有平台與候選編號，人工無從得知
    某一列對應哪個候選；回填後才能把分數安全地寫回資料庫。
    """
    from pipeline.stages.variant_importer import list_variants

    variants = list_variants(project_id)
    grouped: dict[tuple[str, str], list] = {}
    for variant in variants:
        grouped.setdefault((variant.shot_id, variant.provider), []).append(variant)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VARIANT_SHEET
    rows = 0

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=VARIANT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for provider in v1_pack.TARGET_PROVIDERS:
                items = sorted(
                    grouped.get((shot.shot_id, provider), []),
                    key=lambda item: item.created_at,
                )
                for index, variant in enumerate(items, start=1):
                    row = {column: "" for column in VARIANT_COLUMNS}
                    row["scenario"] = scenario
                    row["shot_id"] = shot.shot_id
                    row["provider"] = provider
                    row["candidate_no"] = index
                    row["variant_id"] = variant.variant_id
                    row["video_filename"] = (
                        Path(variant.local_path).name if variant.local_path else ""
                    )
                    if scenario in NON_DIALOGUE_SCENARIOS:
                        row["facial_acting"] = "n/a"
                    writer.writerow(row)
                    rows += 1
    return path, rows
