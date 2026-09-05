# 檔案路徑: video-pipeline/pipeline/benchmark/attempts.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   生成嘗試紀錄（attempt ledger）。
# 主要責任:
#   1. 每按一次 Generate 記錄一列，即使沒有產出影片。
#   2. 作為重試次數、耗時與成本統計的唯一基礎。
# 說明:
#   只統計成功匯入的候選會嚴重高估平台表現。一個試十次才成功兩次的
#   平台，若只看那兩支成品，會看起來和一次就中的平台一樣好。
#   失敗、取消、逾時都必須留下紀錄，usable-shot rate 與 retries 才有意義。
#
#   ledger 是人工填寫的 CSV，為 attempt 資料的 source of truth。
#   sync 只會補齊 variant_id，不會刪除任何既有列。
# --------------------------------------------------------------------------

from __future__ import annotations

import csv
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline.benchmark import v1_pack
from pipeline.benchmark.target import BenchmarkTarget, targets

ATTEMPTS_SHEET = "v1_attempts.csv"

ATTEMPT_COLUMNS = (
    "scenario",
    "shot_id",
    "target_id",
    "provider",
    "model_id",
    "model_version",
    "attempt_no",
    "status",
    "generation_seconds",
    "credits_used",
    "output_file",
    "variant_id",
    "failure_reason",
    "notes",
)


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_STATUSES = {item.value for item in AttemptStatus}
NA_VALUES = {"", "n/a", "na", "-", "none", "null"}


class Attempt(BaseModel):
    scenario: str
    shot_id: str
    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    attempt_no: int
    status: str
    generation_seconds: float | None = None
    credits_used: float | None = None
    output_file: str | None = None
    variant_id: str | None = None
    failure_reason: str | None = None
    notes: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == AttemptStatus.SUCCESS


class AttemptLedger(BaseModel):
    path: str
    attempts: list[Attempt] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def for_target(self, target_id: str) -> list[Attempt]:
        return [item for item in self.attempts if item.target_id == target_id]

    def for_shot(self, target_id: str, shot_id: str) -> list[Attempt]:
        return [
            item
            for item in self.attempts
            if item.target_id == target_id and item.shot_id == shot_id
        ]

    @property
    def target_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.attempts:
            if item.target_id not in seen:
                seen.append(item.target_id)
        return seen


def _number(raw: str | None) -> float | None:
    text = (raw or "").strip().lower()
    if text in NA_VALUES:
        return None
    return float(text)


def _text(raw: str | None) -> str | None:
    text = (raw or "").strip()
    return text or None


def write_blank_ledger(
    output_dir: Path,
    selected: list[BenchmarkTarget] | None = None,
    candidates: int | None = None,
) -> Path:
    """產出空白 ledger，每個目標每顆鏡頭預留 N 列。

    人工可自行增列：實際重試次數常多於預期，多出來的嘗試同樣要記錄。
    """
    target_list = selected or list(targets().targets)
    per_shot = candidates or v1_pack.CANDIDATES_PER_SHOT
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ATTEMPTS_SHEET

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTEMPT_COLUMNS)
        writer.writeheader()
        for shot in v1_pack.shots():
            scenario = v1_pack.SHOT_SCENARIOS[shot.shot_id]
            for target in target_list:
                for index in range(1, per_shot + 1):
                    writer.writerow(
                        {
                            "scenario": scenario,
                            "shot_id": shot.shot_id,
                            "target_id": target.target_id,
                            "provider": target.provider,
                            "model_id": target.model_id,
                            "model_version": target.model_version or "",
                            "attempt_no": index,
                            "status": "",
                            "generation_seconds": "",
                            "credits_used": "",
                            "output_file": "",
                            "variant_id": "",
                            "failure_reason": "",
                            "notes": "",
                        }
                    )
    return path


def read_ledger(path: Path) -> AttemptLedger:
    """讀取 ledger。未填 status 的列視為尚未執行，直接略過。"""
    ledger = AttemptLedger(path=str(path))
    if not Path(path).exists():
        return ledger

    registry = targets()
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            status = (row.get("status") or "").strip().lower()
            if not status:
                continue
            if status not in VALID_STATUSES:
                ledger.errors.append(
                    f"line {line_no}: 無效的 status {status!r}，"
                    f"應為 {'/'.join(sorted(VALID_STATUSES))}"
                )
                continue

            target_id = (row.get("target_id") or "").strip()
            target = registry.by_id(target_id)
            if target is None:
                ledger.errors.append(
                    f"line {line_no}: 未登錄的 target_id {target_id!r}"
                )
                continue

            shot_id = (row.get("shot_id") or "").strip()
            if shot_id not in v1_pack.SHOT_SCENARIOS:
                ledger.errors.append(f"line {line_no}: 未知的 shot_id {shot_id!r}")
                continue

            try:
                attempt_no = int(float((row.get("attempt_no") or "0").strip()))
            except ValueError:
                ledger.errors.append(f"line {line_no}: attempt_no 非數字")
                continue

            try:
                ledger.attempts.append(
                    Attempt(
                        scenario=v1_pack.SHOT_SCENARIOS[shot_id],
                        shot_id=shot_id,
                        target_id=target_id,
                        provider=target.provider,
                        model_id=target.model_id,
                        model_version=target.model_version,
                        attempt_no=attempt_no,
                        status=status,
                        generation_seconds=_number(row.get("generation_seconds")),
                        credits_used=_number(row.get("credits_used")),
                        output_file=_text(row.get("output_file")),
                        variant_id=_text(row.get("variant_id")),
                        failure_reason=_text(row.get("failure_reason")),
                        notes=_text(row.get("notes")),
                    )
                )
            except ValueError as error:
                ledger.errors.append(f"line {line_no}: {error}")

    return ledger


def append_attempt(
    path: Path,
    shot_id: str,
    target_id: str,
    status: str,
    output_file: str | None = None,
    generation_seconds: str | None = None,
    credits_used: str | None = None,
    failure_reason: str | None = None,
) -> int:
    """追加一筆嘗試紀錄，回傳該組合的第幾次嘗試。

    以追加而非覆寫的方式寫入：實際重試次數常多於預留列數，
    而每一次嘗試都必須留下痕跡。
    """
    registry = targets()
    target = registry.by_id(target_id)
    if target is None:
        raise ValueError(f"未登錄的比較對象: {target_id}")
    if shot_id not in v1_pack.SHOT_SCENARIOS:
        raise ValueError(f"未知的鏡頭: {shot_id}")
    normalised = (status or "").strip().lower()
    if normalised not in VALID_STATUSES:
        raise ValueError(
            f"無效的結果: {status!r}，應為 {'/'.join(sorted(VALID_STATUSES))}"
        )
    if normalised == AttemptStatus.SUCCESS and not (output_file or "").strip():
        raise ValueError("成功的嘗試必須填寫產出檔名，否則無法與匯入的候選對應")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    existing = [
        row
        for row in rows
        if (row.get("shot_id") or "").strip() == shot_id
        and (row.get("target_id") or "").strip() == target_id
        and (row.get("status") or "").strip()
    ]
    attempt_no = len(existing) + 1

    # 優先填入預留但尚未使用的空白列，避免檔案越來越零散
    placeholder = next(
        (
            row
            for row in rows
            if (row.get("shot_id") or "").strip() == shot_id
            and (row.get("target_id") or "").strip() == target_id
            and not (row.get("status") or "").strip()
        ),
        None,
    )
    payload = {
        "scenario": v1_pack.SHOT_SCENARIOS[shot_id],
        "shot_id": shot_id,
        "target_id": target_id,
        "provider": target.provider,
        "model_id": target.model_id,
        "model_version": target.model_version or "",
        "attempt_no": attempt_no,
        "status": normalised,
        "generation_seconds": (generation_seconds or "").strip(),
        "credits_used": (credits_used or "").strip(),
        "output_file": (output_file or "").strip(),
        "variant_id": "",
        "failure_reason": (failure_reason or "").strip(),
        "notes": "",
    }
    if placeholder is not None:
        placeholder.update(payload)
    else:
        rows.append(payload)

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTEMPT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return attempt_no


class SyncReport(BaseModel):
    updated: int = 0
    total: int = 0
    unresolved: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved


def sync_variant_ids(path: Path, project_id: str) -> SyncReport:
    """依已匯入的候選補齊 variant_id，不刪除任何既有列。

    對應依據為 target_id + shot_id + output_file 三者精確相符。
    刻意不以匯入順序推測：一旦某次生成沒有匯入或匯入順序與記錄順序
    不同，後續整批都會錯位，而錯位不會有任何徵兆。
    找不到或同名多筆時留空並記入 unresolved，交由人工釐清。

    失敗的嘗試本來就沒有 variant_id，不會被移除也不會被硬塞。
    """
    from pipeline.benchmark.attribution import AmbiguousAttribution, build_index

    report = SyncReport()
    if not Path(path).exists():
        return report

    index = build_index(project_id)

    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    report.total = len(rows)

    for line_no, row in enumerate(rows, start=2):
        status = (row.get("status") or "").strip().lower()
        if status != AttemptStatus.SUCCESS:
            continue
        if (row.get("variant_id") or "").strip():
            continue

        target_id = (row.get("target_id") or "").strip()
        shot_id = (row.get("shot_id") or "").strip()
        output_file = (row.get("output_file") or "").strip()
        if not output_file:
            report.unresolved.append(
                f"line {line_no}: {target_id}/{shot_id} 成功的嘗試未填 output_file"
            )
            continue

        try:
            match = index.find_by_file(target_id, shot_id, output_file)
        except (AmbiguousAttribution, LookupError) as error:
            report.unresolved.append(f"line {line_no}: {error}")
            continue

        row["variant_id"] = match.variant_id
        report.updated += 1

    with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTEMPT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return report
