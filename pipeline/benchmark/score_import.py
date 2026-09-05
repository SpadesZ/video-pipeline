# 檔案路徑: video-pipeline/pipeline/benchmark/score_import.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   人工評分表匯入。
# 主要責任:
#   1. 將填好的 CSV 評分寫回 VariantQC 與 ContinuityQC。
#   2. 留空與 n/a 一律視為不適用，不轉為 0 分。
# 說明:
#   0 分代表「該維度極差」，與「不適用」是兩件事。若把空白填成 0，
#   非對話鏡頭的表情分數會把整體加權拉低，讓平台比較失真。
# --------------------------------------------------------------------------

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.qc import ContinuityScope
from pipeline.stages.shot_qc import (
    QCValidationError,
    record_continuity_qc,
    record_variant_qc,
)

NA_VALUES = {"", "n/a", "na", "-", "none", "null"}
TRUE_VALUES = {"1", "true", "yes", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "n", "f"}

VARIANT_SCORE_COLUMNS = (
    "identity_consistency",
    "temporal_stability",
    "prompt_adherence",
    "motion_quality",
    "camera_control",
    "facial_acting",
    "artifact_severity",
)

CONTINUITY_SCORE_COLUMNS = (
    "cross_shot_identity",
    "wardrobe_continuity",
    "location_continuity",
    "lip_sync_quality",
)


class ImportReport(BaseModel):
    source: str
    applied: int = 0
    skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _score(raw: str | None) -> int | None:
    text = (raw or "").strip().lower()
    if text in NA_VALUES:
        return None
    return int(float(text))


def _number(raw: str | None) -> float | None:
    text = (raw or "").strip().lower()
    if text in NA_VALUES:
        return None
    return float(text)


def _flag(raw: str | None) -> bool | None:
    text = (raw or "").strip().lower()
    if text in NA_VALUES:
        return None
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"無法判讀的布林值: {raw!r}")


def import_variant_scores(
    artifact: ProductionArtifact, csv_path: Path, reviewer: str = "local"
) -> ImportReport:
    report = ImportReport(source=str(csv_path))
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            variant_id = (row.get("variant_id") or "").strip()
            if not variant_id:
                report.skipped.append(f"line {line_no}: 無 variant_id")
                continue

            scores = {
                column: row.get(column) for column in VARIANT_SCORE_COLUMNS
            }
            if all((value or "").strip().lower() in NA_VALUES for value in scores.values()):
                report.skipped.append(f"line {line_no}: {variant_id} 尚未評分")
                continue

            try:
                record_variant_qc(
                    artifact,
                    variant_id,
                    scores={
                        column: _score(value) for column, value in scores.items()
                    },
                    usable_without_repair=_flag(row.get("usable_without_repair")),
                    human_correction_minutes=_number(
                        row.get("human_correction_minutes")
                    ),
                    retries_to_usable=(
                        int(_number(row.get("retries_to_usable")))
                        if _number(row.get("retries_to_usable")) is not None
                        else None
                    ),
                    generation_seconds=_number(row.get("generation_seconds")),
                    reviewer=reviewer,
                    notes=(row.get("notes") or "").strip() or None,
                )
                report.applied += 1
            except (QCValidationError, ValueError) as error:
                report.errors.append(f"line {line_no}: {variant_id}: {error}")
    return report


def import_continuity_scores(
    artifact: ProductionArtifact, csv_path: Path, reviewer: str = "local"
) -> ImportReport:
    report = ImportReport(source=str(csv_path))
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            shot_id = (row.get("shot_id") or "").strip()
            ref_shot_id = (row.get("ref_shot_id") or "").strip()
            if not shot_id or not ref_shot_id:
                report.skipped.append(f"line {line_no}: 缺少鏡頭識別")
                continue

            scores = {
                column: row.get(column) for column in CONTINUITY_SCORE_COLUMNS
            }
            if all((value or "").strip().lower() in NA_VALUES for value in scores.values()):
                report.skipped.append(f"line {line_no}: {shot_id} 尚未評分")
                continue

            try:
                record_continuity_qc(
                    artifact,
                    shot_id=shot_id,
                    ref_shot_id=ref_shot_id,
                    scope=ContinuityScope.PAIR.value,
                    scores={
                        column: _score(value) for column, value in scores.items()
                    },
                    variant_id=(row.get("variant_id") or "").strip() or None,
                    ref_variant_id=(row.get("ref_variant_id") or "").strip() or None,
                    reviewer=reviewer,
                    notes=(row.get("notes") or "").strip() or None,
                )
                report.applied += 1
            except (QCValidationError, ValueError) as error:
                report.errors.append(f"line {line_no}: {shot_id}: {error}")
    return report
