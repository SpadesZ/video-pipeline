# 檔案路徑: video-pipeline/pipeline/benchmark/report.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   目前這一輪 benchmark 報表的組裝。
# 主要責任:
#   1. 從資料庫與嘗試紀錄取出資料，判斷哪些屬於目前這一輪。
#   2. 交給 aggregation 依預先註冊的公式產生報表。
# 說明:
#   這段判斷原本寫在 /benchmark/results 的路由裡。放在路由層有兩個問題：
#   測試只能靠自己複製一份同樣的邏輯來驗證，於是路由改壞了測試也不會紅；
#   而「哪些資料算數」是 benchmark 的核心規則，不是畫面的事。
#
#   納入條件有兩層，兩層都必須成立：
#     1. 候選的派工身份等於該比較對象目前確認的身份（版本要對）
#     2. 連戲評分對應的那兩支影片，正好是目前的代表作配對
#   只驗第一層的話，同版本內換過代表作後，舊配對與新配對都符合條件，
#   同一顆鏡頭的連戲會被計兩次。
# --------------------------------------------------------------------------

from __future__ import annotations

from sqlmodel import Session, select

from pipeline.benchmark import aggregation, attempts, attribution, workflow
from pipeline.models.qc import ContinuityQC, VariantQC
from pipeline.settings import Settings

PROJECT_ID = workflow.PROJECT_ID


def collect_records(project_id: str = PROJECT_ID) -> tuple[list[dict], list[dict], list[str]]:
    """取出候選評分與連戲評分，並標記哪些屬於目前這一輪。

    不屬於目前這一輪者仍會出現在回傳值中，但 identity_key 為 None，
    由 aggregation 計入排除數。直接略過的話，報表就無從說明少了什麼。
    """
    from pipeline.db import engine

    index = attribution.build_index(project_id)

    with Session(engine) as session:
        qc_rows = {
            row.variant_id: row
            for row in session.exec(
                select(VariantQC).where(VariantQC.project_id == project_id)
            ).all()
        }
        continuity_rows = list(
            session.exec(
                select(ContinuityQC).where(ContinuityQC.project_id == project_id)
            ).all()
        )

    variant_records = []
    for item in index.items:
        qc = qc_rows.get(item.variant_id)
        if qc is None:
            continue
        variant_records.append(
            {
                "target_id": item.target_id,
                # 派工當下的身份。報表據此判斷這支影片屬於哪一輪。
                "identity_key": item.identity_key,
                "shot_id": item.shot_id,
                "weighted_score": qc.weighted_score(aggregation.BENCHMARK_WEIGHTS),
                "usable": bool(qc.usable_without_repair),
                "human_minutes": qc.human_correction_minutes,
                "dimensions": {
                    "identity_consistency": qc.identity_consistency,
                    "temporal_stability": qc.temporal_stability,
                    "prompt_adherence": qc.prompt_adherence,
                    "motion_quality": qc.motion_quality,
                    "camera_control": qc.camera_control,
                    "facial_acting": qc.facial_acting,
                    "artifact_severity": qc.artifact_severity,
                },
            }
        )

    owner = {item.variant_id: item.target_id for item in index.items}
    current_pairs = workflow.current_pair_identities(project_id)

    continuity_records = []
    for row in continuity_rows:
        entry = index.by_variant_id(row.variant_id) if row.variant_id else None
        if entry is None:
            continue
        if workflow.continuity_row_identity(row, owner) not in current_pairs:
            continuity_records.append(
                {
                    "target_id": entry.target_id,
                    "identity_key": None,
                    "shot_id": row.shot_id,
                    "weighted_score": None,
                    "cross_shot_identity": None,
                }
            )
            continue
        continuity_records.append(
            {
                "target_id": entry.target_id,
                "identity_key": entry.identity_key,
                "shot_id": row.shot_id,
                "weighted_score": row.weighted_score(aggregation.BENCHMARK_WEIGHTS),
                "cross_shot_identity": row.cross_shot_identity,
            }
        )

    return variant_records, continuity_records, list(index.unattributed)


def build_current_report(
    settings: Settings, project_id: str = PROJECT_ID
) -> aggregation.BenchmarkReport:
    """目前這一輪的報表。"""
    ledger = attempts.read_ledger(workflow.attempts_path(settings))
    variant_records, continuity_records, unattributed = collect_records(project_id)
    return aggregation.build_report(
        ledger, variant_records, continuity_records, unattributed
    )
