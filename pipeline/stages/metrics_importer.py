# 檔案路徑: video-pipeline/pipeline/stages/metrics_importer.py
# 產生時間: 2026-06-25 17:00 +08:00
# 版本: v1.1
# 模組定位:
#   已發布影片之流量數據匯入與 Scale / Iterate / Kill 決策運算階段模組。
# 主要責任:
#   1. 從手動 CSV 或 JSON 匯入影片發布後的績效指標 (CTR, AVD, RPM 等)。
#   2. 依據門檻規則自動計算決策方向 (Scale, Iterate, Kill)。
#   3. 更新 ProductionArtifact 的 metrics_decision 欄位並寫入 decision_log。
# --------------------------------------------------------------------------

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from pipeline.models.metrics import MetricsDecision, ScaleDecision
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.review import DecisionLogEntry
from pipeline.settings import Settings
from pipeline.utils.files import write_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 核心決策運算
# ---------------------------------------------------------------------------


def calculate_decision(
    metrics: MetricsDecision,
    video_duration_seconds: float,
) -> ScaleDecision:
    """根據 CTR 與 AVD 門檻計算 Scale / Iterate / Kill 決策。

    門檻規則:
      - SCALE:   CTR >= 5 % **且** AVD >= 50 % 影片長度
      - ITERATE: CTR >= 3 % **或** AVD >= 30 % 影片長度
      - KILL:    以上皆不符合
    """
    ctr = metrics.ctr
    avd = metrics.average_view_duration_seconds

    if video_duration_seconds <= 0:
        logger.warning(
            "video_duration_seconds=%s 不合法，無法計算 AVD 比例，回傳 UNKNOWN",
            video_duration_seconds,
        )
        return ScaleDecision.UNKNOWN

    ctr_pct = ctr if ctr is not None else 0.0
    avd_ratio = (avd / video_duration_seconds) if avd is not None else 0.0

    meets_ctr_scale = ctr_pct >= 5.0
    meets_avd_scale = avd_ratio >= 0.5

    meets_ctr_iterate = ctr_pct >= 3.0
    meets_avd_iterate = avd_ratio >= 0.3

    if meets_ctr_scale and meets_avd_scale:
        return ScaleDecision.SCALE

    if meets_ctr_iterate or meets_avd_iterate:
        return ScaleDecision.ITERATE

    return ScaleDecision.KILL


# ---------------------------------------------------------------------------
# 摘要產生
# ---------------------------------------------------------------------------


def generate_metrics_summary(metrics: MetricsDecision) -> str:
    """將 MetricsDecision 格式化為人類可讀的純文字摘要。"""
    parts: list[str] = [f"[Metrics] project={metrics.project_id}  window={metrics.window}"]

    if metrics.ctr is not None:
        parts.append(f"  CTR: {metrics.ctr:.2f}%")
    if metrics.average_view_duration_seconds is not None:
        minutes, secs = divmod(int(metrics.average_view_duration_seconds), 60)
        parts.append(f"  AVD: {minutes}m{secs:02d}s ({metrics.average_view_duration_seconds:.1f}s)")
    if metrics.retention_30s is not None:
        parts.append(f"  30s Retention: {metrics.retention_30s:.1f}%")
    if metrics.rpm is not None:
        parts.append(f"  RPM: ${metrics.rpm:.2f}")
    if metrics.affiliate_clicks is not None:
        parts.append(f"  Affiliate Clicks: {metrics.affiliate_clicks}")

    parts.append(f"  Decision: {metrics.decision.value.upper()}")

    if metrics.notes:
        parts.append(f"  Notes: {metrics.notes}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON 匯入
# ---------------------------------------------------------------------------


def import_metrics_from_json(
    data: dict,
    artifact: ProductionArtifact,
    settings: Settings,
) -> ProductionArtifact:
    """從 dict (JSON 載入結果) 匯入績效指標並更新 artifact。

    ``data`` 需包含與 :class:`MetricsDecision` 相同的欄位。
    若未提供 ``project_id``，自動使用 ``artifact.project_id``。
    ``video_duration_seconds`` 欄位用於計算 AVD 比例，不會存入 metrics。
    """
    data.setdefault("project_id", artifact.project_id)

    video_duration_seconds: float = float(data.pop("video_duration_seconds", 0))
    if video_duration_seconds <= 0 and artifact.cue_ledger and artifact.cue_ledger.cues:
        video_duration_seconds = artifact.cue_ledger.cues[-1].end_ms / 1000.0

    metrics = MetricsDecision.model_validate(data)

    decision = calculate_decision(metrics, video_duration_seconds)
    metrics.decision = decision

    logger.info(
        "JSON 匯入完成 — project=%s  decision=%s",
        artifact.project_id,
        decision.value,
    )

    artifact.metrics_decision = metrics
    artifact.decision_log.append(
        DecisionLogEntry(
            action="metrics_import",
            actor="metrics_importer",
            note=f"Imported from JSON. Decision: {decision.value}. "
                 f"CTR={metrics.ctr}%, AVD={metrics.average_view_duration_seconds}s",
        )
    )
    artifact.touch()

    _persist_metrics(artifact, metrics, settings)

    return artifact


# ---------------------------------------------------------------------------
# CSV 匯入
# ---------------------------------------------------------------------------

_CSV_FIELD_MAP: dict[str, str] = {
    "project_id": "project_id",
    "window": "window",
    "ctr": "ctr",
    "average_view_duration_seconds": "average_view_duration_seconds",
    "avd": "average_view_duration_seconds",
    "retention_30s": "retention_30s",
    "rpm": "rpm",
    "affiliate_clicks": "affiliate_clicks",
    "video_duration_seconds": "video_duration_seconds",
    "notes": "notes",
}


def _parse_csv_row(row: dict[str, str]) -> dict:
    """將 CSV 原始 row (str→str) 正規化為可交給 MetricsDecision 的 dict。"""
    normalised: dict[str, object] = {}
    for csv_col, model_field in _CSV_FIELD_MAP.items():
        raw = row.get(csv_col)
        if raw is None or raw.strip() == "":
            continue
        raw = raw.strip()
        if model_field in (
            "ctr",
            "average_view_duration_seconds",
            "retention_30s",
            "rpm",
            "video_duration_seconds",
        ):
            normalised[model_field] = float(raw)
        elif model_field == "affiliate_clicks":
            normalised[model_field] = int(raw)
        else:
            normalised[model_field] = raw
    return normalised


def import_metrics_from_csv(
    csv_path: Path,
    artifact: ProductionArtifact,
    settings: Settings,
) -> ProductionArtifact:
    """從 CSV 檔案匯入績效指標。

    CSV 檔應至少包含表頭列，與一列對應該 ``artifact.project_id`` 的數據。
    若 CSV 內含多列，僅匹配 ``project_id`` 相同的列；若無匹配列，取第一列。
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV 檔案不存在: {csv_path}")

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [_parse_csv_row(r) for r in reader]

    if not rows:
        raise ValueError(f"CSV 檔案無有效數據列: {csv_path}")

    target_row: dict | None = None
    for row in rows:
        if row.get("project_id") == artifact.project_id:
            target_row = row
            break

    if target_row is None:
        logger.info(
            "CSV 中未找到 project_id=%s，使用第一列資料",
            artifact.project_id,
        )
        target_row = rows[0]

    target_row.setdefault("project_id", artifact.project_id)

    video_duration_seconds: float = float(target_row.pop("video_duration_seconds", 0))
    if video_duration_seconds <= 0 and artifact.cue_ledger and artifact.cue_ledger.cues:
        video_duration_seconds = artifact.cue_ledger.cues[-1].end_ms / 1000.0

    metrics = MetricsDecision.model_validate(target_row)

    decision = calculate_decision(metrics, video_duration_seconds)
    metrics.decision = decision

    logger.info(
        "CSV 匯入完成 — project=%s  csv=%s  decision=%s",
        artifact.project_id,
        csv_path.name,
        decision.value,
    )

    artifact.metrics_decision = metrics
    artifact.decision_log.append(
        DecisionLogEntry(
            action="metrics_import",
            actor="metrics_importer",
            note=f"Imported from CSV ({csv_path.name}). Decision: {decision.value}. "
                 f"CTR={metrics.ctr}%, AVD={metrics.average_view_duration_seconds}s",
        )
    )
    artifact.touch()

    _persist_metrics(artifact, metrics, settings)

    return artifact


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------


def _persist_metrics(
    artifact: ProductionArtifact,
    metrics: MetricsDecision,
    settings: Settings,
) -> None:
    """將 metrics JSON 持久化到專案資料夾以供後續查閱。"""
    project_dir = settings.data_dir / "projects" / artifact.project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    metrics_path = project_dir / "metrics_decision.json"
    write_json(metrics_path, metrics)

    summary_path = project_dir / "metrics_summary.txt"
    summary_path.write_text(
        generate_metrics_summary(metrics),
        encoding="utf-8",
    )

    logger.info("已寫入 %s 與 %s", metrics_path, summary_path)
