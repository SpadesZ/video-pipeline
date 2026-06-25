# 檔案路徑: video-pipeline/pipeline/stages/topic_optimizer.py
# 產生時間: 2026-06-25 14:33 +08:00
# 版本: v1.0
# 模組定位:
#   選題優化閉環模組 (Topic Optimization Feedback Loop)。
#   將已發佈影片的流量與轉化數據回饋至選題階段，形成數據驅動的迭代策略。
# 主要責任:
#   1. 從資料庫載入所有已產生 MetricsDecision 的專案。
#   2. 依照 genre 分組計算平均指標，建立主題績效索引 (Performance Index)。
#   3. 根據績效索引產生文字摘要，作為 LLM topic_research 的附加上下文。
#   4. 結合歷史績效與使用者提示，呼叫 run_topic_research 產生下一輪選題建議。
#   5. 提供 Web UI dashboard 所需的主題績效摘要。
# 綠標提醒:
#   - genre 為 None 的專案統一歸類為 "uncategorized"，避免分組遺漏。
#   - 平均值計算時排除 None 欄位，僅以實際數據筆數為分母。
#   - ScaleDecision 分佈以 Counter 計數，確保每個決策方向皆有統計。
# --------------------------------------------------------------------------

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from pipeline.db import engine
from pipeline.models.metrics import MetricsDecision, ScaleDecision
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.settings import Settings
from pipeline.stages.llm_executors import run_topic_research

logger = logging.getLogger("Topic_Optimizer")
logger.setLevel(logging.INFO)

_UNCATEGORIZED = "uncategorized"


# ---------------------------------------------------------------------------
# 1. 載入全部含指標的專案
# ---------------------------------------------------------------------------

def load_all_project_metrics(
    settings: Settings,
) -> list[tuple[ProductionArtifact, MetricsDecision]]:
    """讀取資料庫中所有已附加 MetricsDecision 的 ProductionArtifact。

    Returns:
        以 (artifact, metrics_decision) 組成的清單，僅包含 metrics_decision
        不為 None 的專案，依 created_at 降序排列。
    """
    results: list[tuple[ProductionArtifact, MetricsDecision]] = []
    with Session(engine) as session:
        statement = (
            select(ProductionArtifact)
            .where(ProductionArtifact.metrics_decision.is_not(None))  # type: ignore[union-attr]
            .order_by(ProductionArtifact.created_at.desc())  # type: ignore[union-attr]
        )
        artifacts = session.exec(statement).all()
        for artifact in artifacts:
            if artifact.metrics_decision is not None:
                results.append((artifact, artifact.metrics_decision))
    logger.info("Loaded %d projects with metrics from database.", len(results))
    return results


# ---------------------------------------------------------------------------
# 2. 建立績效索引
# ---------------------------------------------------------------------------

def _safe_avg(values: list[float]) -> float | None:
    """計算平均值；空清單時回傳 None。"""
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def build_performance_index(
    projects_with_metrics: list[tuple[ProductionArtifact, MetricsDecision]],
) -> dict[str, Any]:
    """依 genre 分組，計算各類主題的平均績效指標及決策分佈。

    Returns:
        {
            "genres": {
                "<genre>": {
                    "project_count": int,
                    "avg_ctr": float | None,
                    "avg_avd_seconds": float | None,
                    "avg_retention_30s": float | None,
                    "avg_rpm": float | None,
                    "total_affiliate_clicks": int,
                    "decision_distribution": {"scale": N, "iterate": N, "kill": N, "unknown": N},
                    "dominant_decision": str,
                    "latest_project_date": str (ISO format),
                },
                ...
            },
            "total_projects": int,
            "generated_at": str (ISO format),
        }
    """
    genre_buckets: dict[str, list[tuple[ProductionArtifact, MetricsDecision]]] = defaultdict(list)
    for artifact, md in projects_with_metrics:
        genre_key = (artifact.genre or _UNCATEGORIZED).strip().lower()
        genre_buckets[genre_key].append((artifact, md))

    genres: dict[str, Any] = {}
    for genre_key, items in genre_buckets.items():
        ctrs: list[float] = []
        avds: list[float] = []
        retentions: list[float] = []
        rpms: list[float] = []
        affiliate_total = 0
        decision_counter: Counter[str] = Counter()
        latest_date: datetime | None = None

        for artifact, md in items:
            if md.ctr is not None:
                ctrs.append(md.ctr)
            if md.average_view_duration_seconds is not None:
                avds.append(md.average_view_duration_seconds)
            if md.retention_30s is not None:
                retentions.append(md.retention_30s)
            if md.rpm is not None:
                rpms.append(md.rpm)
            if md.affiliate_clicks is not None:
                affiliate_total += md.affiliate_clicks

            decision_counter[md.decision.value] += 1

            if latest_date is None or artifact.created_at > latest_date:
                latest_date = artifact.created_at

        # 找出最常見的決策方向
        dominant = decision_counter.most_common(1)[0][0] if decision_counter else ScaleDecision.UNKNOWN.value

        genres[genre_key] = {
            "project_count": len(items),
            "avg_ctr": _safe_avg(ctrs),
            "avg_avd_seconds": _safe_avg(avds),
            "avg_retention_30s": _safe_avg(retentions),
            "avg_rpm": _safe_avg(rpms),
            "total_affiliate_clicks": affiliate_total,
            "decision_distribution": {
                ScaleDecision.SCALE.value: decision_counter.get(ScaleDecision.SCALE.value, 0),
                ScaleDecision.ITERATE.value: decision_counter.get(ScaleDecision.ITERATE.value, 0),
                ScaleDecision.KILL.value: decision_counter.get(ScaleDecision.KILL.value, 0),
                ScaleDecision.UNKNOWN.value: decision_counter.get(ScaleDecision.UNKNOWN.value, 0),
            },
            "dominant_decision": dominant,
            "latest_project_date": latest_date.isoformat() if latest_date else None,
        }

    logger.info("Built performance index across %d genre(s).", len(genres))
    return {
        "genres": genres,
        "total_projects": len(projects_with_metrics),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 3. 將績效索引轉換為 LLM 可讀的文字摘要
# ---------------------------------------------------------------------------

def generate_optimization_context(performance_index: dict[str, Any]) -> str:
    """將績效索引轉換為 LLM 上下文純文字摘要。

    產出的文字會被附加至 topic_research 的使用者提示中，使 LLM 在選題時
    能參考歷史數據做出更精準的推薦。

    Returns:
        多行文字摘要，包含各 genre 的績效數據與策略建議。
    """
    genres: dict[str, Any] = performance_index.get("genres", {})
    total = performance_index.get("total_projects", 0)

    if not genres:
        return (
            "No historical performance data is available yet. "
            "This is the first topic research — explore broadly."
        )

    lines: list[str] = [
        f"=== Historical Performance Context ({total} published videos) ===",
        "",
    ]

    # 依 project_count 降序排列，讓 LLM 先看到資料最多的 genre
    sorted_genres = sorted(genres.items(), key=lambda kv: kv[1]["project_count"], reverse=True)

    for genre_key, stats in sorted_genres:
        decision = stats["dominant_decision"]
        count = stats["project_count"]
        ctr_str = f"{stats['avg_ctr']:.2%}" if stats["avg_ctr"] is not None else "N/A"
        avd_str = f"{stats['avg_avd_seconds']:.0f}s" if stats["avg_avd_seconds"] is not None else "N/A"
        ret_str = f"{stats['avg_retention_30s']:.1%}" if stats["avg_retention_30s"] is not None else "N/A"
        rpm_str = f"${stats['avg_rpm']:.2f}" if stats["avg_rpm"] is not None else "N/A"
        aff_str = str(stats["total_affiliate_clicks"])

        recommendation = _decision_to_recommendation(decision, stats)

        lines.append(f"Genre: {genre_key} ({count} videos)")
        lines.append(f"  CTR={ctr_str}  AVD={avd_str}  30s-Retention={ret_str}  RPM={rpm_str}  Affiliate-Clicks={aff_str}")
        lines.append(f"  Dominant decision: {decision.upper()}")
        lines.append(f"  Recommendation: {recommendation}")
        lines.append("")

    # 總結最佳與最差 genre
    best_genre = _find_best_genre(sorted_genres)
    worst_genre = _find_worst_genre(sorted_genres)

    if best_genre:
        lines.append(f"★ Top-performing genre: \"{best_genre}\" — consider doubling down or finding adjacent niches.")
    if worst_genre:
        lines.append(f"✗ Weakest genre: \"{worst_genre}\" — consider pivoting away or fundamentally rethinking the angle.")
    lines.append("")

    return "\n".join(lines)


def _decision_to_recommendation(decision: str, stats: dict[str, Any]) -> str:
    """根據主導決策方向產生策略建議文字。"""
    dist = stats.get("decision_distribution", {})
    scale_count = dist.get(ScaleDecision.SCALE.value, 0)
    iterate_count = dist.get(ScaleDecision.ITERATE.value, 0)
    kill_count = dist.get(ScaleDecision.KILL.value, 0)

    if decision == ScaleDecision.SCALE.value:
        return (
            f"SCALE — this genre consistently performs well "
            f"({scale_count} scale vs {kill_count} kill). "
            f"Produce more content in this space and explore sub-niches."
        )
    if decision == ScaleDecision.ITERATE.value:
        return (
            f"ITERATE — mixed results ({iterate_count} iterate, {scale_count} scale, {kill_count} kill). "
            f"Experiment with different angles, thumbnails, or hooks before committing further."
        )
    if decision == ScaleDecision.KILL.value:
        return (
            f"KILL — this genre underperforms "
            f"({kill_count} kill vs {scale_count} scale). "
            f"Avoid new content unless a fundamentally different approach is identified."
        )
    return "UNKNOWN — insufficient data to make a confident recommendation."


def _find_best_genre(sorted_genres: list[tuple[str, dict[str, Any]]]) -> str | None:
    """找出 SCALE 比例最高且至少有 2 筆資料的 genre。"""
    best: str | None = None
    best_ratio = -1.0
    for genre_key, stats in sorted_genres:
        count = stats["project_count"]
        if count < 2:
            continue
        scale_count = stats["decision_distribution"].get(ScaleDecision.SCALE.value, 0)
        ratio = scale_count / count
        if ratio > best_ratio:
            best_ratio = ratio
            best = genre_key
    return best


def _find_worst_genre(sorted_genres: list[tuple[str, dict[str, Any]]]) -> str | None:
    """找出 KILL 比例最高且至少有 2 筆資料的 genre。"""
    worst: str | None = None
    worst_ratio = -1.0
    for genre_key, stats in sorted_genres:
        count = stats["project_count"]
        if count < 2:
            continue
        kill_count = stats["decision_distribution"].get(ScaleDecision.KILL.value, 0)
        ratio = kill_count / count
        if ratio > worst_ratio:
            worst_ratio = ratio
            worst = genre_key
    return worst


# ---------------------------------------------------------------------------
# 4. 結合歷史上下文進行下一輪選題
# ---------------------------------------------------------------------------

async def suggest_next_topics(
    settings: Settings,
    user_prompt: str = "",
) -> dict[str, Any]:
    """使用歷史績效資料增強 topic_research LLM 呼叫。

    流程:
        1. 從 DB 載入歷史指標。
        2. 建立績效索引。
        3. 產生文字摘要附加至使用者提示。
        4. 呼叫 run_topic_research 取得 LLM 選題建議。

    Args:
        settings: 應用程式設定，用於取得 data_dir 等參數。
        user_prompt: 使用者的選題方向描述（可為空）。

    Returns:
        包含 LLM 選題結果以及績效上下文的字典:
        {
            "topic_research": { ... },           # run_topic_research 的原始回傳
            "optimization_context": "...",        # 附加至 LLM 的績效摘要文字
            "performance_index": { ... },         # 完整績效索引
            "projects_analyzed": int,
        }
    """
    projects_with_metrics = load_all_project_metrics(settings)
    performance_index = build_performance_index(projects_with_metrics)
    optimization_context = generate_optimization_context(performance_index)

    # 組合最終提示：歷史上下文 + 使用者需求
    combined_prompt_parts: list[str] = []
    if optimization_context:
        combined_prompt_parts.append(optimization_context)
    if user_prompt.strip():
        combined_prompt_parts.append(f"User request: {user_prompt.strip()}")
    else:
        combined_prompt_parts.append(
            "User request: Suggest the next best video topic based on historical performance data above."
        )

    combined_prompt = "\n\n".join(combined_prompt_parts)
    logger.info(
        "Running optimized topic research with %d historical projects.",
        len(projects_with_metrics),
    )

    topic_research = await run_topic_research(combined_prompt)

    return {
        "topic_research": topic_research,
        "optimization_context": optimization_context,
        "performance_index": performance_index,
        "projects_analyzed": len(projects_with_metrics),
    }


# ---------------------------------------------------------------------------
# 5. Web UI Dashboard 摘要
# ---------------------------------------------------------------------------

def get_topic_performance_summary(settings: Settings) -> list[dict[str, Any]]:
    """產生 Web UI dashboard 所需的主題績效摘要列表。

    每個 genre 回傳一筆 dict，包含前端儀表板展示所需的所有欄位。
    列表依照 project_count 降序排列。

    Returns:
        [
            {
                "genre": str,
                "project_count": int,
                "avg_ctr": float | None,
                "avg_avd_seconds": float | None,
                "avg_retention_30s": float | None,
                "avg_rpm": float | None,
                "total_affiliate_clicks": int,
                "dominant_decision": str,
                "recommendation": str,
                "latest_project_date": str | None,
                "trend": "up" | "down" | "stable" | "insufficient_data",
            },
            ...
        ]
    """
    projects_with_metrics = load_all_project_metrics(settings)
    performance_index = build_performance_index(projects_with_metrics)
    genres: dict[str, Any] = performance_index.get("genres", {})

    summary: list[dict[str, Any]] = []
    sorted_genres = sorted(genres.items(), key=lambda kv: kv[1]["project_count"], reverse=True)

    for genre_key, stats in sorted_genres:
        trend = _compute_trend(genre_key, projects_with_metrics)
        recommendation = _decision_to_recommendation(stats["dominant_decision"], stats)

        summary.append({
            "genre": genre_key,
            "project_count": stats["project_count"],
            "avg_ctr": stats["avg_ctr"],
            "avg_avd_seconds": stats["avg_avd_seconds"],
            "avg_retention_30s": stats["avg_retention_30s"],
            "avg_rpm": stats["avg_rpm"],
            "total_affiliate_clicks": stats["total_affiliate_clicks"],
            "dominant_decision": stats["dominant_decision"],
            "recommendation": recommendation,
            "latest_project_date": stats["latest_project_date"],
            "trend": trend,
        })

    logger.info("Generated dashboard summary for %d genre(s).", len(summary))
    return summary


def _compute_trend(
    genre_key: str,
    projects_with_metrics: list[tuple[ProductionArtifact, MetricsDecision]],
) -> str:
    """比較 genre 中最近一半與較早一半專案的 CTR，判斷趨勢方向。

    Returns:
        "up" | "down" | "stable" | "insufficient_data"
    """
    # 篩選出該 genre 的專案，按 created_at 升序排列
    genre_projects = [
        (a, m)
        for a, m in projects_with_metrics
        if (a.genre or _UNCATEGORIZED).strip().lower() == genre_key
    ]
    # 按時間升序，使前半段為較舊、後半段為較新
    genre_projects.sort(key=lambda pair: pair[0].created_at)

    ctrs = [(a.created_at, m.ctr) for a, m in genre_projects if m.ctr is not None]
    if len(ctrs) < 4:
        return "insufficient_data"

    midpoint = len(ctrs) // 2
    older_ctrs = [c for _, c in ctrs[:midpoint]]
    newer_ctrs = [c for _, c in ctrs[midpoint:]]

    older_avg = sum(older_ctrs) / len(older_ctrs)
    newer_avg = sum(newer_ctrs) / len(newer_ctrs)

    # 以 10% 相對變化作為趨勢門檻
    if older_avg == 0:
        return "up" if newer_avg > 0 else "stable"

    change_ratio = (newer_avg - older_avg) / abs(older_avg)
    if change_ratio > 0.10:
        return "up"
    if change_ratio < -0.10:
        return "down"
    return "stable"
