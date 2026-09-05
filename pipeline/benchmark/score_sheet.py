# 檔案路徑: video-pipeline/pipeline/benchmark/score_sheet.py
# 產生時間: 2026-09-05 +08:00
# 版本: v2.0
# 模組定位:
#   V1 人工評分表產生器。
# 主要責任:
#   1. 產出單鏡頭評分表，以 BenchmarkTarget 為單位而非 provider。
#   2. 產出跨鏡頭連戲評分表，並綁定實際的候選配對。
# 說明:
#   評分表以 CSV 產出，供人工邊看影片邊填。留空即代表該維度不適用，
#   不可填 0：0 分代表「極差」，與「不適用」是兩件事，混用會拉低加權。
#
#   表情演技依鏡頭而非情境判定。bm_a2 雖屬單角色情境，但它是特寫且
#   明確要求表情變化，是表情演技的主要觀察對象。
#
#   V1 沒有音訊，嘴型一律標為 n/a 且不參與排名。
# --------------------------------------------------------------------------

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.benchmark import v1_pack
from pipeline.benchmark.target import BenchmarkTarget, targets

VARIANT_SHEET = "v1_variant_scores.csv"
CONTINUITY_SHEET = "v1_continuity_scores.csv"

VARIANT_COLUMNS = (
    "scenario",
    "shot_id",
    # 比較對象身份。只記 provider 無法區分同平台的不同版本。
    "target_id",
    "provider",
    "model_id",
    "model_version",
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
    "target_id",
    "provider",
    "model_id",
    # 連戲評的是兩支實際影片的關係，必須指名是哪兩支
    "variant_id",
    "ref_variant_id",
    "cross_shot_identity",
    "wardrobe_continuity",
    "location_continuity",
    "lip_sync_quality",
    "notes",
)


def _facial_cell(shot_id: str) -> str:
    return "" if v1_pack.evaluates_facial_acting(shot_id) else "n/a"


def _lip_sync_cell() -> str:
    # V1 沒有音訊 ground truth，嘴型一律不評
    return "" if v1_pack.LIP_SYNC_ENABLED else "n/a"


def _selected_targets(
    selected: list[BenchmarkTarget] | None,
) -> list[BenchmarkTarget]:
    return selected or list(targets().targets)


def write_variant_sheet(
    output_dir: Path,
    selected: list[BenchmarkTarget] | None = None,
    candidates: int | None = None,
) -> Path:
    target_list = _selected_targets(selected)
    per_shot = candidates or v1_pack.CANDIDATES_PER_SHOT
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VARIANT_SHEET

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=VARIANT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for target in target_list:
                for index in range(1, per_shot + 1):
                    row = {column: "" for column in VARIANT_COLUMNS}
                    row["scenario"] = scenario
                    row["shot_id"] = shot.shot_id
                    row["target_id"] = target.target_id
                    row["provider"] = target.provider
                    row["model_id"] = target.model_id
                    row["model_version"] = target.model_version or ""
                    row["candidate_no"] = index
                    row["facial_acting"] = _facial_cell(shot.shot_id)
                    writer.writerow(row)
    return path


def write_continuity_sheet(
    output_dir: Path, selected: list[BenchmarkTarget] | None = None
) -> Path:
    target_list = _selected_targets(selected)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CONTINUITY_SHEET

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTINUITY_COLUMNS)
        writer.writeheader()
        for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
            scenario = v1_pack.SHOT_SCENARIOS[shot_id]
            for target in target_list:
                row = {column: "" for column in CONTINUITY_COLUMNS}
                row["scenario"] = scenario
                row["shot_id"] = shot_id
                row["ref_shot_id"] = ref_shot_id
                row["target_id"] = target.target_id
                row["provider"] = target.provider
                row["model_id"] = target.model_id
                row["lip_sync_quality"] = _lip_sync_cell()
                writer.writerow(row)
    return path


def write_all(
    output_dir: Path, selected: list[BenchmarkTarget] | None = None
) -> tuple[Path, Path]:
    return (
        write_variant_sheet(output_dir, selected),
        write_continuity_sheet(output_dir, selected),
    )


def _variants_by_key(project_id: str) -> dict[tuple[str, str], list]:
    from pipeline.stages.variant_importer import list_variants

    grouped: dict[tuple[str, str], list] = {}
    for variant in list_variants(project_id):
        grouped.setdefault((variant.shot_id, variant.provider), []).append(variant)
    for items in grouped.values():
        items.sort(key=lambda item: item.created_at)
    return grouped


def sync_variant_sheet(output_dir: Path, project_id: str) -> tuple[Path, int]:
    """依實際匯入的候選重寫評分表，回填 variant_id 與檔名。"""
    grouped = _variants_by_key(project_id)
    target_list = list(targets().targets)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VARIANT_SHEET
    rows = 0

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=VARIANT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for target in target_list:
                items = grouped.get((shot.shot_id, target.provider), [])
                for index, variant in enumerate(items, start=1):
                    row = {column: "" for column in VARIANT_COLUMNS}
                    row["scenario"] = scenario
                    row["shot_id"] = shot.shot_id
                    row["target_id"] = target.target_id
                    row["provider"] = target.provider
                    row["model_id"] = target.model_id
                    row["model_version"] = target.model_version or ""
                    row["candidate_no"] = index
                    row["variant_id"] = variant.variant_id
                    row["video_filename"] = (
                        Path(variant.local_path).name if variant.local_path else ""
                    )
                    row["facial_acting"] = _facial_cell(shot.shot_id)
                    writer.writerow(row)
                    rows += 1
    return path, rows


def sync_continuity_sheet(output_dir: Path, project_id: str) -> tuple[Path, int]:
    """依已選定的候選建立連戲配對。

    連戲比較的是兩支實際影片，因此只針對已選定（selected）的候選建立
    配對。兩支必須來自同一個比較對象，跨平台或跨版本的配對沒有意義。
    """
    from pipeline.models.variant import VariantStatus
    from pipeline.stages.variant_importer import list_variants

    selected_by_key: dict[tuple[str, str], object] = {}
    for variant in list_variants(project_id):
        if variant.status == VariantStatus.SELECTED:
            selected_by_key[(variant.shot_id, variant.provider)] = variant

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CONTINUITY_SHEET
    rows = 0

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTINUITY_COLUMNS)
        writer.writeheader()
        for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
            scenario = v1_pack.SHOT_SCENARIOS[shot_id]
            for target in targets().targets:
                primary = selected_by_key.get((shot_id, target.provider))
                reference = selected_by_key.get((ref_shot_id, target.provider))
                row = {column: "" for column in CONTINUITY_COLUMNS}
                row["scenario"] = scenario
                row["shot_id"] = shot_id
                row["ref_shot_id"] = ref_shot_id
                row["target_id"] = target.target_id
                row["provider"] = target.provider
                row["model_id"] = target.model_id
                row["variant_id"] = getattr(primary, "variant_id", "") or ""
                row["ref_variant_id"] = getattr(reference, "variant_id", "") or ""
                row["lip_sync_quality"] = _lip_sync_cell()
                writer.writerow(row)
                if row["variant_id"] and row["ref_variant_id"]:
                    rows += 1
    return path, rows
