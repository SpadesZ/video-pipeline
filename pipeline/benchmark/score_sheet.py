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


# 人工填寫的欄位。sync 重寫檔案時必須原樣保留，
# 否則已評好的分數會在補齊 variant_id 時被清空。
VARIANT_USER_COLUMNS = (
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

CONTINUITY_USER_COLUMNS = (
    "cross_shot_identity",
    "wardrobe_continuity",
    "location_continuity",
    "lip_sync_quality",
    "notes",
)


def _load_existing(path: Path, key_columns: tuple[str, ...]) -> dict[tuple, dict]:
    """讀取既有 CSV，以指定欄位組合為鍵。檔案不存在時回傳空字典。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    existing: dict[tuple, dict] = {}
    for row in rows:
        key = tuple((row.get(column) or "").strip() for column in key_columns)
        if any(key):
            existing[key] = row
    return existing


def _preserve(row: dict, previous: dict | None, columns: tuple[str, ...]) -> None:
    for column in columns:
        value = (previous or {}).get(column)
        if value is not None and str(value).strip() != "":
            row[column] = value


def sync_variant_sheet(output_dir: Path, project_id: str) -> tuple[Path, int]:
    """依實際匯入的候選重寫評分表。

    歸屬走 job lineage 而非 (shot_id, provider)：同一平台可能同時測試
    多個版本，以平台分組會把不同比較對象的候選混在一起。

    已填寫的人工欄位一律沿用。sync 只補系統欄位，不得把辛苦評好的分數
    連同 variant_id 一起重置。
    """
    from pipeline.benchmark.attribution import build_index

    attribution = build_index(project_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VARIANT_SHEET
    previous = _load_existing(path, ("variant_id",))
    rows = 0

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=VARIANT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for target in targets().targets:
                entries = attribution.for_shot(target.target_id, shot.shot_id)
                for index, entry in enumerate(entries, start=1):
                    row = {column: "" for column in VARIANT_COLUMNS}
                    row["scenario"] = scenario
                    row["shot_id"] = shot.shot_id
                    row["target_id"] = target.target_id
                    row["provider"] = target.provider
                    row["model_id"] = target.model_id
                    row["model_version"] = target.model_version or ""
                    row["candidate_no"] = index
                    row["variant_id"] = entry.variant_id
                    row["video_filename"] = entry.file_name or ""
                    row["facial_acting"] = _facial_cell(shot.shot_id)
                    _preserve(
                        row, previous.get((entry.variant_id,)), VARIANT_USER_COLUMNS
                    )
                    writer.writerow(row)
                    rows += 1
    return path, rows


def sync_continuity_sheet(output_dir: Path, project_id: str) -> tuple[Path, int]:
    """依 benchmark 代表作建立連戲配對。

    使用 benchmark_selected 而非 production 的 status=selected：
    後者每顆鏡頭全域只能有一支，四個比較對象就無法各自成對。

    代表作只取當輪候選。舊版本的候選即使還留著選定旗標也不算數，
    否則同一組會有兩支代表作，寫進表裡的是哪一支取決於排序。
    """
    from pipeline.benchmark.attribution import build_index

    attribution = build_index(project_id)
    selection = {
        (item.target_id, item.shot_id): item.variant_id
        for item in attribution.current
        if item.benchmark_selected
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CONTINUITY_SHEET
    # 保留人工填寫的分數時，鍵必須含兩支 variant_id。只用鏡頭位置當鍵，
    # 換過代表作之後，上一組配對的分數會被原樣搬到新的配對上，
    # 而那個分數是比另外兩支影片得到的。
    previous = _load_existing(
        path,
        ("target_id", "shot_id", "ref_shot_id", "variant_id", "ref_variant_id"),
    )
    rows = 0

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTINUITY_COLUMNS)
        writer.writeheader()
        for shot_id, ref_shot_id in v1_pack.CONTINUITY_PAIRS:
            scenario = v1_pack.SHOT_SCENARIOS[shot_id]
            for target in targets().targets:
                row = {column: "" for column in CONTINUITY_COLUMNS}
                row["scenario"] = scenario
                row["shot_id"] = shot_id
                row["ref_shot_id"] = ref_shot_id
                row["target_id"] = target.target_id
                row["provider"] = target.provider
                row["model_id"] = target.model_id
                row["variant_id"] = selection.get((target.target_id, shot_id), "")
                row["ref_variant_id"] = selection.get(
                    (target.target_id, ref_shot_id), ""
                )
                row["lip_sync_quality"] = _lip_sync_cell()
                _preserve(
                    row,
                    previous.get(
                        (
                            target.target_id,
                            shot_id,
                            ref_shot_id,
                            row["variant_id"],
                            row["ref_variant_id"],
                        )
                    ),
                    CONTINUITY_USER_COLUMNS,
                )
                writer.writerow(row)
                if row["variant_id"] and row["ref_variant_id"]:
                    rows += 1
    return path, rows
