# 檔案路徑: video-pipeline/pipeline/benchmark/aggregation.py
# 產生時間: 2026-09-05 +08:00
# 版本: v2.0
# 模組定位:
#   V1 Benchmark 統計公式（預先註冊）。
# 主要責任:
#   1. 在看到任何真實結果前，固定所有聚合方式與排序規則。
#   2. 以情境為單位聚合，依情境產生各自的優勝對象。
# 說明:
#   本檔案的公式在真人生成開始前就已寫死。測完再挑對自己有利的算法
#   會讓 benchmark 失去意義，因此聚合方式、失敗計入方式與 tie-break
#   都不得於看到結果後修改。若要調整，必須重跑全部資料。
#
#   聚合一律採中位數而非平均或最佳值：
#     平均會被單次崩壞或單次神來一筆拉走；
#     最佳值等於獎勵運氣，與「穩定產出可用鏡頭」的目標相反。
#
#   情境獎項只能使用該情境的資料。用全域平均去評「對話戲最佳」，
#   等於讓高動態場景的表現污染對話戲的結論。唯一例外是明確跨情境的
#   低重試獎，它衡量的就是整體生產成本。
# --------------------------------------------------------------------------

from __future__ import annotations

import statistics
from enum import StrEnum

from pydantic import BaseModel, Field

from pipeline.benchmark import identity, v1_pack
from pipeline.benchmark.attempts import AttemptLedger
from pipeline.benchmark.target import BenchmarkTarget, targets
from pipeline.models.production_profile import QCWeights

AGGREGATION_VERSION = "v2.0"

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

GLOBAL_SCOPE = "__global__"


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

# 各獎項的評分範圍。單一情境的獎項只讀該情境的資料；
# 低重試獎明確跨情境，讀全域聚合。
AWARD_SCOPE: dict[ScenarioAward, str] = {
    ScenarioAward.CHARACTER_DIALOGUE: "two_character_dialogue",
    ScenarioAward.CINEMATIC_CAMERA: "single_character_cinematic",
    ScenarioAward.ANIME_COMIC_MOTION: "two_character_dialogue",
    ScenarioAward.HIGH_DYNAMIC_ACTION: "high_dynamic_action",
    ScenarioAward.LOW_RETRY_PRODUCTION: GLOBAL_SCOPE,
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


class ScopedAggregate(BaseModel):
    """某個範圍（單一情境或全域）內的統計。

    所有指標的計算方式與全域一致，差別只在納入哪些鏡頭。
    情境獎項讀對應情境的實例，跨情境獎項讀全域實例。
    """

    scope: str
    shots: list[ShotAggregate] = Field(default_factory=list)
    total_attempts: int = 0
    failed_attempts: int = 0
    total_credits: float = 0.0
    total_human_minutes: float = 0.0
    generation_seconds: list[float] = Field(default_factory=list)
    dimension_scores: dict[str, list[float]] = Field(default_factory=dict)
    continuity_scores: list[float] = Field(default_factory=list)
    continuity_identity_scores: list[float] = Field(default_factory=list)

    @property
    def attempted_shots(self) -> int:
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
            shot.median_quality
            for shot in self.shots
            if shot.median_quality is not None
        ]
        return _median(values)

    @property
    def stability(self) -> float | None:
        """各鏡頭品質的四分位距。數值越小代表表現越穩定。"""
        values = [
            shot.median_quality
            for shot in self.shots
            if shot.median_quality is not None
        ]
        return _iqr(values)

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

    @property
    def has_data(self) -> bool:
        return bool(self.shots) and self.attempted_shots > 0

    def metric(self, name: str) -> float | None:
        if hasattr(self, name):
            return getattr(self, name)
        return self.dimension(name)


class TargetAggregate(BaseModel):
    """單一比較對象的統計。global 為跨情境，scenarios 為各情境獨立。"""

    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    provisional: bool = True

    overall: ScopedAggregate
    scenarios: dict[str, ScopedAggregate] = Field(default_factory=dict)

    model_config = {"protected_namespaces": ()}

    def scope(self, scope_id: str) -> ScopedAggregate | None:
        if scope_id == GLOBAL_SCOPE:
            return self.overall
        return self.scenarios.get(scope_id)

    # 便利屬性，供報表摘要使用
    @property
    def usable_shot_rate(self) -> float | None:
        return self.overall.usable_shot_rate

    @property
    def retries_per_usable(self) -> float | None:
        return self.overall.retries_per_usable

    @property
    def human_minutes_per_usable(self) -> float | None:
        return self.overall.human_minutes_per_usable

    @property
    def total_attempts(self) -> int:
        return self.overall.total_attempts

    @property
    def failed_attempts(self) -> int:
        return self.overall.failed_attempts

    @property
    def total_credits(self) -> float:
        return self.overall.total_credits

    @property
    def median_generation_seconds(self) -> float | None:
        return self.overall.median_generation_seconds


class AwardResult(BaseModel):
    award: str
    scope: str
    winner: str | None = None
    ranking: list[str] = Field(default_factory=list)
    reason: str = ""
    note: str = ""


class BenchmarkReport(BaseModel):
    aggregation_version: str = AGGREGATION_VERSION
    pack_version: str = v1_pack.BENCHMARK_PACK_VERSION
    project_id: str = v1_pack.BENCHMARK_PROJECT_ID
    aggregates: list[TargetAggregate] = Field(default_factory=list)
    awards: list[AwardResult] = Field(default_factory=list)
    ledger_errors: list[str] = Field(default_factory=list)
    provisional_targets: list[str] = Field(default_factory=list)
    unattributed_variants: list[str] = Field(default_factory=list)

    # 身份與目前 catalog 不符而未計入的資料。保留計數與說明，
    # 讓報表能明說「這裡少算了什麼」，而不是靜靜地少算。
    excluded_attempts: int = 0
    excluded_variants: int = 0
    excluded_continuity: int = 0
    excluded_identities: list[str] = Field(default_factory=list)
    identity_warnings: list[str] = Field(default_factory=list)

    @property
    def has_excluded(self) -> bool:
        return bool(
            self.excluded_attempts
            or self.excluded_variants
            or self.excluded_continuity
        )

    def by_target(self, target_id: str) -> TargetAggregate | None:
        return next(
            (item for item in self.aggregates if item.target_id == target_id), None
        )

    def award(self, award_id: str) -> AwardResult | None:
        return next((item for item in self.awards if item.award == award_id), None)


def _sort_key(
    scoped: ScopedAggregate,
    target_id: str,
    criteria: tuple[tuple[str, bool], ...],
) -> tuple:
    key: list = []
    for name, ascending in (*criteria, *FINAL_TIE_BREAK):
        if name == "target_id":
            key.append((0, target_id))
            continue
        value = scoped.metric(name)
        if value is None:
            # 缺資料一律排在最後，不因缺漏而意外勝出
            key.append((1, 0.0))
            continue
        key.append((0, value if ascending else -value))
    return tuple(key)


def _new_scope(scope_id: str, shot_ids: list[str]) -> ScopedAggregate:
    return ScopedAggregate(
        scope=scope_id,
        shots=[
            ShotAggregate(
                shot_id=shot_id, scenario=v1_pack.SHOT_SCENARIOS[shot_id]
            )
            for shot_id in shot_ids
        ],
    )


def build_report(
    ledger: AttemptLedger,
    variant_records: list[dict],
    continuity_records: list[dict],
    unattributed: list[str] | None = None,
) -> BenchmarkReport:
    """依預先註冊的公式產生報表。

    variant_records 每筆需含 target_id、shot_id、weighted_score、
    usable、human_minutes 與各維度分數，以及 identity_key。
    continuity_records 每筆需含 target_id、shot_id、weighted_score、
    cross_shot_identity 與 identity_key；shot_id 用於判定所屬情境。

    只有 identity_key 等於該 target 目前身份的資料會進入報表。
    target_id 相同但版本不同的舊資料會被排除並計數：把 v1 與 v2 的
    成績加在一起，得到的數字不對應任何真實存在的模型。
    """
    registry = targets()
    report = BenchmarkReport(ledger_errors=list(ledger.errors))
    report.provisional_targets = [
        item.target_id for item in registry.provisional_targets
    ]
    report.unattributed_variants = list(unattributed or [])
    report.identity_warnings = list(getattr(ledger, "identity_warnings", []))

    current = identity.current_identities()
    current_keys = {item.key for item in current.values()}
    excluded_keys: list[str] = []

    def accepted(target_id: str, identity_key: str | None) -> bool:
        """這筆資料是否屬於目前這一輪。"""
        expected = current.get(target_id)
        if expected is None:
            return False
        if identity_key is None:
            # 呼叫端已判定這筆不屬於目前這一輪：版本不符，或連戲評的
            # 不是現在的代表作配對。不猜，一律排除。
            label = f"{target_id}（非目前這一輪）"
            if label not in excluded_keys:
                excluded_keys.append(label)
            return False
        if identity_key == expected.key:
            return True
        if identity_key not in excluded_keys:
            excluded_keys.append(identity_key)
        return False

    all_shots = [shot.shot_id for shot in v1_pack.shots()]
    by_scenario: dict[str, list[str]] = {}
    for shot_id in all_shots:
        by_scenario.setdefault(v1_pack.SHOT_SCENARIOS[shot_id], []).append(shot_id)

    aggregates: dict[str, TargetAggregate] = {}

    def ensure(target: BenchmarkTarget) -> TargetAggregate:
        if target.target_id not in aggregates:
            aggregates[target.target_id] = TargetAggregate(
                target_id=target.target_id,
                provider=target.provider,
                model_id=target.model_id,
                model_version=target.model_version,
                provisional=target.provisional,
                overall=_new_scope(GLOBAL_SCOPE, all_shots),
                scenarios={
                    scenario_id: _new_scope(scenario_id, shot_ids)
                    for scenario_id, shot_ids in by_scenario.items()
                },
            )
        return aggregates[target.target_id]

    def scopes_for(aggregate: TargetAggregate, shot_id: str) -> list[ScopedAggregate]:
        scenario_id = v1_pack.SHOT_SCENARIOS.get(shot_id)
        result = [aggregate.overall]
        if scenario_id and scenario_id in aggregate.scenarios:
            result.append(aggregate.scenarios[scenario_id])
        return result

    # 嘗試紀錄是重試、耗時與成本的唯一來源。身份取自寫入當下的欄位，
    # 版本不符者不計入：舊版本的重試次數不是新版本的重試次數。
    for attempt in ledger.attempts:
        target = registry.by_id(attempt.target_id)
        if target is None:
            continue
        if attempt.unresolved_identity or not attempt.is_current(current_keys):
            report.excluded_attempts += 1
            if not attempt.unresolved_identity:
                key = attempt.identity.key
                if key not in excluded_keys:
                    excluded_keys.append(key)
            continue
        aggregate = ensure(target)
        for scoped in scopes_for(aggregate, attempt.shot_id):
            scoped.total_attempts += 1
            if not attempt.succeeded:
                scoped.failed_attempts += 1
            if attempt.credits_used:
                scoped.total_credits += attempt.credits_used
            if attempt.generation_seconds and attempt.succeeded:
                scoped.generation_seconds.append(attempt.generation_seconds)
            shot = next(
                (item for item in scoped.shots if item.shot_id == attempt.shot_id),
                None,
            )
            if shot is not None:
                shot.attempts += 1
                if attempt.succeeded:
                    shot.successes += 1

    for record in variant_records:
        target = registry.by_id(record["target_id"])
        if target is None:
            continue
        if not accepted(record["target_id"], record.get("identity_key")):
            report.excluded_variants += 1
            continue
        aggregate = ensure(target)
        for scoped in scopes_for(aggregate, record["shot_id"]):
            shot = next(
                (item for item in scoped.shots if item.shot_id == record["shot_id"]),
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
                scoped.total_human_minutes += record["human_minutes"]
            for name, value in (record.get("dimensions") or {}).items():
                if value is not None:
                    scoped.dimension_scores.setdefault(name, []).append(value)

    for record in continuity_records:
        target = registry.by_id(record["target_id"])
        if target is None:
            continue
        if not accepted(record["target_id"], record.get("identity_key")):
            report.excluded_continuity += 1
            continue
        aggregate = ensure(target)
        # 連戲屬於主鏡頭所在的情境
        for scoped in scopes_for(aggregate, record.get("shot_id", "")):
            if record.get("weighted_score") is not None:
                scoped.continuity_scores.append(record["weighted_score"])
            if record.get("cross_shot_identity") is not None:
                scoped.continuity_identity_scores.append(
                    record["cross_shot_identity"]
                )

    report.aggregates = [aggregates[key] for key in sorted(aggregates)]
    report.excluded_identities = excluded_keys

    # 各情境獨立評選，刻意不產生跨情境總冠軍
    for award, criteria in AWARD_CRITERIA.items():
        scope_id = AWARD_SCOPE[award]
        eligible: list[tuple[str, ScopedAggregate]] = []
        for aggregate in report.aggregates:
            scoped = aggregate.scope(scope_id)
            if scoped is not None and scoped.has_data:
                eligible.append((aggregate.target_id, scoped))

        if not eligible:
            report.awards.append(
                AwardResult(
                    award=award.value,
                    scope=scope_id,
                    reason=" > ".join(name for name, _ in criteria),
                    note="無足夠資料",
                )
            )
            continue

        ranked = sorted(
            eligible, key=lambda item: _sort_key(item[1], item[0], criteria)
        )
        report.awards.append(
            AwardResult(
                award=award.value,
                scope=scope_id,
                winner=ranked[0][0],
                ranking=[target_id for target_id, _ in ranked],
                reason=" > ".join(name for name, _ in criteria),
                note=(
                    "跨情境指標" if scope_id == GLOBAL_SCOPE else "僅使用該情境資料"
                ),
            )
        )

    return report
