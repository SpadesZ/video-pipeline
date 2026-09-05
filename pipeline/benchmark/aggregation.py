# 檔案路徑: video-pipeline/pipeline/benchmark/aggregation.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark 統計公式（預先註冊）。
# 主要責任:
#   1. 在看到任何真實結果前，固定所有聚合方式與排序規則。
#   2. 依情境產生各自的優勝對象，不產生跨情境總冠軍。
# 說明:
#   本檔案的公式在真人生成開始前就已寫死。測完再挑對自己有利的算法
#   會讓 benchmark 失去意義，因此聚合方式、失敗計入方式與 tie-break
#   都不得於看到結果後修改。若要調整，必須重跑全部資料。
#
#   聚合一律採中位數而非平均或最佳值：
#     平均會被單次崩壞或單次神來一筆拉走；
#     最佳值等於獎勵運氣，與「穩定產出可用鏡頭」的目標相反。
# --------------------------------------------------------------------------

from __future__ import annotations

import statistics
from enum import StrEnum

from pydantic import BaseModel, Field

from pipeline.benchmark import v1_pack
from pipeline.benchmark.attempts import AttemptLedger
from pipeline.benchmark.target import BenchmarkTarget, targets
from pipeline.models.production_profile import QCWeights

AGGREGATION_VERSION = "v1.0"

# 各維度在 benchmark 排名中的權重。與 ProductionProfile 的權重分開，
# 因為 benchmark 要衡量的是模型能力，不是某支片的製作偏好。
BENCHMARK_WEIGHTS = QCWeights(
    identity_consistency=3.0,
    temporal_stability=2.0,
    prompt_adherence=1.5,
    motion_quality=1.5,
    camera_control=1.5,
    facial_acting=1.0,
    artifact_severity=1.5,
    cross_shot_identity=3.0,
    wardrobe_continuity=2.0,
    location_continuity=1.5,
    # V1 沒有音訊 ground truth，嘴型不參與排名
    lip_sync_quality=0.0,
)


class ScenarioAward(StrEnum):
    CHARACTER_DIALOGUE = "best_for_character_dialogue"
    CINEMATIC_CAMERA = "best_for_cinematic_camera"
    ANIME_COMIC_MOTION = "best_for_anime_comic_motion"
    HIGH_DYNAMIC_ACTION = "best_for_high_dynamic_action"
    LOW_RETRY_PRODUCTION = "best_for_low_retry_production"


# 各獎項的排序依據。依序比較，前者相同才看後者。
# 元組第二項為 True 表示數值越小越好。
AWARD_CRITERIA: dict[ScenarioAward, tuple[tuple[str, bool], ...]] = {
    ScenarioAward.CHARACTER_DIALOGUE: (
        ("continuity_identity", False),
        ("identity_consistency", False),
        ("facial_acting", False),
        ("usable_shot_rate", False),
    ),
    ScenarioAward.CINEMATIC_CAMERA: (
        ("camera_control", False),
        ("temporal_stability", False),
        ("usable_shot_rate", False),
    ),
    ScenarioAward.ANIME_COMIC_MOTION: (
        ("motion_quality", False),
        ("artifact_cleanliness", False),
        ("identity_consistency", False),
    ),
    ScenarioAward.HIGH_DYNAMIC_ACTION: (
        ("temporal_stability", False),
        ("artifact_cleanliness", False),
        ("motion_quality", False),
    ),
    ScenarioAward.LOW_RETRY_PRODUCTION: (
        ("usable_shot_rate", False),
        ("retries_per_usable", True),
        ("human_minutes_per_usable", True),
    ),
}

# 各獎項對應的情境。低重試獎跨全部情境，其餘限定於相關情境。
AWARD_SCENARIOS: dict[ScenarioAward, tuple[str, ...]] = {
    ScenarioAward.CHARACTER_DIALOGUE: ("two_character_dialogue",),
    ScenarioAward.CINEMATIC_CAMERA: ("single_character_cinematic",),
    ScenarioAward.ANIME_COMIC_MOTION: (
        "single_character_cinematic",
        "two_character_dialogue",
    ),
    ScenarioAward.HIGH_DYNAMIC_ACTION: ("high_dynamic_action",),
    ScenarioAward.LOW_RETRY_PRODUCTION: tuple(
        item.scenario_id for item in v1_pack.SCENARIOS
    ),
}

# tie-break：上述依據全部相同時的最終排序，一律以生產成本較低者勝出。
FINAL_TIE_BREAK: tuple[tuple[str, bool], ...] = (
    ("retries_per_usable", True),
    ("human_minutes_per_usable", True),
    ("median_generation_seconds", True),
    ("target_id", True),
)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _iqr(values: list[float]) -> float | None:
    """四分位距。樣本少於四個時退回全距，仍能反映離散程度。"""
    if len(values) < 2:
        return None
    if len(values) < 4:
        return round(max(values) - min(values), 3)
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    return round(quartiles[2] - quartiles[0], 3)


class ShotAggregate(BaseModel):
    shot_id: str
    scenario: str
    attempts: int = 0
    successes: int = 0
    usable: int = 0
    candidate_qualities: list[float] = Field(default_factory=list)

    @property
    def median_quality(self) -> float | None:
        return _median(self.candidate_qualities)

    @property
    def has_usable(self) -> bool:
        return self.usable > 0


class TargetAggregate(BaseModel):
    """單一比較對象的統計結果。所有欄位的計算方式皆預先固定。"""

    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    provisional: bool = True

    shots: list[ShotAggregate] = Field(default_factory=list)

    total_attempts: int = 0
    failed_attempts: int = 0
    total_credits: float = 0.0
    total_human_minutes: float = 0.0
    generation_seconds: list[float] = Field(default_factory=list)

    dimension_scores: dict[str, list[float]] = Field(default_factory=dict)
    continuity_scores: list[float] = Field(default_factory=list)
    continuity_identity_scores: list[float] = Field(default_factory=list)

    # ---- 核心指標 ----

    @property
    def attempted_shots(self) -> int:
        """有嘗試過的鏡頭數。分母只計實際派工並嘗試過的鏡頭。"""
        return sum(1 for shot in self.shots if shot.attempts > 0)

    @property
    def usable_shots(self) -> int:
        return sum(1 for shot in self.shots if shot.has_usable)

    @property
    def usable_shot_rate(self) -> float | None:
        if not self.attempted_shots:
            return None
        return round(self.usable_shots / self.attempted_shots, 3)

    @property
    def retries_per_usable(self) -> float | None:
        """每產出一顆可用鏡頭的平均嘗試次數，含失敗。"""
        if not self.usable_shots:
            return None
        return round(self.total_attempts / self.usable_shots, 3)

    @property
    def human_minutes_per_usable(self) -> float | None:
        if not self.usable_shots:
            return None
        return round(self.total_human_minutes / self.usable_shots, 3)

    @property
    def median_generation_seconds(self) -> float | None:
        return _median(self.generation_seconds)

    @property
    def failure_rate(self) -> float | None:
        if not self.total_attempts:
            return None
        return round(self.failed_attempts / self.total_attempts, 3)

    @property
    def median_quality(self) -> float | None:
        values = [
            shot.median_quality for shot in self.shots if shot.median_quality is not None
        ]
        return _median(values)

    @property
    def continuity_identity(self) -> float | None:
        return _median(self.continuity_identity_scores)

    @property
    def continuity_overall(self) -> float | None:
        return _median(self.continuity_scores)

    def dimension(self, name: str) -> float | None:
        return _median(self.dimension_scores.get(name, []))

    @property
    def artifact_cleanliness(self) -> float | None:
        """瑕疵嚴重度取反，使所有排序依據方向一致（越高越好）。"""
        severity = self.dimension("artifact_severity")
        return None if severity is None else round(100 - severity, 3)

    def scenario_qualities(self, scenario_id: str) -> list[float]:
        return [
            shot.median_quality
            for shot in self.shots
            if shot.scenario == scenario_id and shot.median_quality is not None
        ]

    def scenario_quality(self, scenario_id: str) -> float | None:
        return _median(self.scenario_qualities(scenario_id))

    def scenario_stability(self, scenario_id: str) -> float | None:
        """情境內各鏡頭品質的四分位距。數值越小代表表現越穩定。"""
        return _iqr(self.scenario_qualities(scenario_id))

    def metric(self, name: str) -> float | str | None:
        """供排序使用的統一取值入口。"""
        if name == "target_id":
            return self.target_id
        if hasattr(self, name):
            return getattr(self, name)
        if name == "continuity_identity":
            return self.continuity_identity
        return self.dimension(name)


class AwardResult(BaseModel):
    award: str
    scenarios: list[str] = Field(default_factory=list)
    winner: str | None = None
    ranking: list[str] = Field(default_factory=list)
    reason: str = ""


class BenchmarkReport(BaseModel):
    aggregation_version: str = AGGREGATION_VERSION
    pack_version: str = v1_pack.BENCHMARK_PACK_VERSION
    project_id: str = v1_pack.BENCHMARK_PROJECT_ID
    aggregates: list[TargetAggregate] = Field(default_factory=list)
    awards: list[AwardResult] = Field(default_factory=list)
    ledger_errors: list[str] = Field(default_factory=list)
    provisional_targets: list[str] = Field(default_factory=list)

    def by_target(self, target_id: str) -> TargetAggregate | None:
        return next(
            (item for item in self.aggregates if item.target_id == target_id), None
        )


def _sort_key(
    aggregate: TargetAggregate, criteria: tuple[tuple[str, bool], ...]
) -> tuple:
    key: list = []
    for name, ascending in (*criteria, *FINAL_TIE_BREAK):
        value = aggregate.metric(name)
        if value is None:
            # 缺資料一律排在最後，不因缺漏而意外勝出
            key.append((1, 0))
            continue
        if isinstance(value, str):
            key.append((0, value if ascending else value[::-1]))
            continue
        key.append((0, value if ascending else -value))
    return tuple(key)


def build_report(
    ledger: AttemptLedger,
    variant_records: list[dict],
    continuity_records: list[dict],
) -> BenchmarkReport:
    """依預先註冊的公式產生報表。

    variant_records 每筆需含 target_id、shot_id、weighted_score、
    usable、human_minutes 與各維度分數。
    continuity_records 每筆需含 target_id、weighted_score 與 cross_shot_identity。
    """
    registry = targets()
    report = BenchmarkReport(ledger_errors=list(ledger.errors))
    report.provisional_targets = [
        item.target_id for item in registry.provisional_targets
    ]

    aggregates: dict[str, TargetAggregate] = {}

    def ensure(target: BenchmarkTarget) -> TargetAggregate:
        if target.target_id not in aggregates:
            aggregates[target.target_id] = TargetAggregate(
                target_id=target.target_id,
                provider=target.provider,
                model_id=target.model_id,
                model_version=target.model_version,
                provisional=target.provisional,
                shots=[
                    ShotAggregate(
                        shot_id=shot.shot_id,
                        scenario=v1_pack.SHOT_SCENARIOS[shot.shot_id],
                    )
                    for shot in v1_pack.shots()
                ],
            )
        return aggregates[target.target_id]

    # 嘗試紀錄是重試、耗時與成本的唯一來源
    for attempt in ledger.attempts:
        target = registry.by_id(attempt.target_id)
        if target is None:
            continue
        aggregate = ensure(target)
        aggregate.total_attempts += 1
        if not attempt.succeeded:
            aggregate.failed_attempts += 1
        if attempt.credits_used:
            aggregate.total_credits += attempt.credits_used
        if attempt.generation_seconds and attempt.succeeded:
            aggregate.generation_seconds.append(attempt.generation_seconds)

        shot = next(
            (item for item in aggregate.shots if item.shot_id == attempt.shot_id), None
        )
        if shot is not None:
            shot.attempts += 1
            if attempt.succeeded:
                shot.successes += 1

    for record in variant_records:
        target = registry.by_id(record["target_id"])
        if target is None:
            continue
        aggregate = ensure(target)
        shot = next(
            (item for item in aggregate.shots if item.shot_id == record["shot_id"]),
            None,
        )
        if shot is None:
            continue
        quality = record.get("weighted_score")
        if quality is not None:
            shot.candidate_qualities.append(quality)
        if record.get("usable"):
            shot.usable += 1
        if record.get("human_minutes"):
            aggregate.total_human_minutes += record["human_minutes"]
        for name, value in (record.get("dimensions") or {}).items():
            if value is not None:
                aggregate.dimension_scores.setdefault(name, []).append(value)

    for record in continuity_records:
        target = registry.by_id(record["target_id"])
        if target is None:
            continue
        aggregate = ensure(target)
        if record.get("weighted_score") is not None:
            aggregate.continuity_scores.append(record["weighted_score"])
        if record.get("cross_shot_identity") is not None:
            aggregate.continuity_identity_scores.append(record["cross_shot_identity"])

    report.aggregates = [aggregates[key] for key in sorted(aggregates)]

    # 各情境獨立評選，刻意不產生跨情境總冠軍
    for award, criteria in AWARD_CRITERIA.items():
        scenarios = AWARD_SCENARIOS[award]
        eligible = [
            item
            for item in report.aggregates
            if any(item.scenario_qualities(scenario) for scenario in scenarios)
        ]
        if not eligible:
            report.awards.append(
                AwardResult(
                    award=award.value,
                    scenarios=list(scenarios),
                    reason="無足夠資料",
                )
            )
            continue
        ranked = sorted(eligible, key=lambda item: _sort_key(item, criteria))
        report.awards.append(
            AwardResult(
                award=award.value,
                scenarios=list(scenarios),
                winner=ranked[0].target_id,
                ranking=[item.target_id for item in ranked],
                reason=" > ".join(name for name, _ in criteria),
            )
        )

    return report
