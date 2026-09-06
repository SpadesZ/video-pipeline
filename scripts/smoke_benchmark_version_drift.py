# 檔案路徑: video-pipeline/scripts/smoke_benchmark_version_drift.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   跨版本資料污染的確定性冒煙測試。
# 主要責任:
#   1. 同一個 target_id 先後指向兩個版本時，兩輪資料不得互相污染。
#   2. 舊版本資料必須完整保留且可辨識，而不是被刪除或被冒充成新版本。
#   3. 代表作更換後，舊的連戲評分不得被當成新配對的分數。
# 說明:
#   這是最容易在真實使用中發生、卻最不容易被發現的一類錯誤。
#   平台改版、使用者更新 catalog、再跑一輪——如果報表只以 target_id
#   聚合，兩個版本的成績會被加在一起，而畫面上不會有任何異常，
#   只會出現一個不對應任何真實模型的數字。
#
#   本測試因此走完整的兩階段流程，並逐項斷言舊資料沒有流進新報表。
#   不需要任何 API 金鑰，也不進行真實影片生成。
# --------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
API_ROOT = ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="benchmark_drift_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
for _key in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(_key, None)

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.main import app  # noqa: E402
from pipeline.benchmark import (  # noqa: E402
    aggregation,
    attempts,
    attribution,
    identity,
    job_view,
    target,
    v1_pack,
    workflow,
)
from pipeline.db import engine, init_db  # noqa: E402
from pipeline.models.qc import ContinuityQC, VariantQC  # noqa: E402
from pipeline.settings import get_settings  # noqa: E402

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID
V1 = "1.0-phase-one"
V2 = "2.0-phase-two"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def png(size, colour=(40, 60, 90)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def clip(seed: str) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + seed.encode("utf-8") + os.urandom(400)


def ledger_path() -> Path:
    return workflow.attempts_path(get_settings())


def build_report():
    """以與 /benchmark/results 相同的方式產生報表。"""
    ledger = attempts.read_ledger(ledger_path())
    index = attribution.build_index(PROJECT_ID)

    with Session(engine) as session:
        qc_rows = {
            row.variant_id: row
            for row in session.exec(
                select(VariantQC).where(VariantQC.project_id == PROJECT_ID)
            ).all()
        }
        continuity_rows = session.exec(
            select(ContinuityQC).where(ContinuityQC.project_id == PROJECT_ID)
        ).all()

    variant_records = []
    for item in index.items:
        qc = qc_rows.get(item.variant_id)
        if qc is None:
            continue
        variant_records.append(
            {
                "target_id": item.target_id,
                "identity_key": item.identity_key,
                "shot_id": item.shot_id,
                "weighted_score": qc.weighted_score(aggregation.BENCHMARK_WEIGHTS),
                "usable": bool(qc.usable_without_repair),
                "human_minutes": qc.human_correction_minutes,
                "dimensions": {"identity_consistency": qc.identity_consistency},
            }
        )

    continuity_records = []
    for row in continuity_rows:
        entry = index.by_variant_id(row.variant_id) if row.variant_id else None
        if entry is None:
            continue
        reference = (
            index.by_variant_id(row.ref_variant_id) if row.ref_variant_id else None
        )
        matched = reference is not None and reference.identity_key == entry.identity_key
        continuity_records.append(
            {
                "target_id": entry.target_id,
                "identity_key": entry.identity_key if matched else None,
                "shot_id": row.shot_id,
                "weighted_score": (
                    row.weighted_score(aggregation.BENCHMARK_WEIGHTS)
                    if matched
                    else None
                ),
                "cross_shot_identity": row.cross_shot_identity if matched else None,
            }
        )

    return aggregation.build_report(
        ledger, variant_records, continuity_records, index.unattributed
    )


def confirm_all(client: TestClient, version: str) -> None:
    """把所有比較對象確認到指定版本。"""
    for item in target.targets().targets:
        response = client.post(
            f"/benchmark/targets/{item.target_id}",
            data={
                "model_id": item.model_id,
                "model_version": version,
                "ui_label": f"{item.provider} {version}",
                "confirmed": "1",
            },
            follow_redirects=False,
        )
        check(
            "ok=1" in response.headers["location"],
            f"{item.target_id} 應可確認為 {version}",
        )


def setup_assets(client: TestClient) -> None:
    for required in v1_pack.REQUIRED_ASSETS:
        size = (576, 1024) if required.require_vertical else (800, 800)
        client.post(
            f"/benchmark/assets/{required.asset_id}",
            files={"file": (f"{required.asset_id}.png", png(size), "image/png")},
            follow_redirects=False,
        )


def jobs_for(target_id: str) -> list:
    return [
        item for item in job_view.list_jobs(PROJECT_ID) if item.target_id == target_id
    ]


def run_phase(client: TestClient, label: str, target_id: str) -> dict:
    """跑一輪：派工、記錄嘗試、匯入、評分、選代表作、評連戲。"""
    response = client.post("/benchmark/build", follow_redirects=False)
    check(response.status_code == 303, f"{label} 派工應成功")

    views = jobs_for(target_id)
    check(views, f"{label} 應有派工")

    for index_no, view in enumerate(views):
        if view.variants_imported:
            continue
        # 每顆鏡頭都先失敗一次再成功，讓重試次數有意義
        client.post(
            f"/benchmark/jobs/{view.job_id}/attempt",
            data={
                "status": "failed",
                "failure_reason": f"{label} 平台拒絕",
                "generation_seconds": "20",
                "credits_used": "3",
            },
            follow_redirects=False,
        )
        name = f"{label}_{view.shot_id}.mp4"
        client.post(
            f"/benchmark/jobs/{view.job_id}/attempt",
            data={
                "status": "success",
                "output_file": name,
                "generation_seconds": str(60 + index_no),
                "credits_used": "10",
            },
            follow_redirects=False,
        )
        result = client.post(
            f"/benchmark/jobs/{view.job_id}/import",
            files=[("files", (name, clip(f"{label}{view.job_id}"), "video/mp4"))],
            follow_redirects=False,
        )
        check("ok=1" in result.headers["location"], f"{label} 匯入應成功")

    # 這一輪的候選 = 身份與目前 catalog 相符者。用「還沒匯入過」判斷會
    # 在第二輪抓到第一輪的候選，因為它們同屬一個 target_id。
    index = attribution.build_index(PROJECT_ID)
    fresh = {
        item.shot_id: item.variant_id
        for item in index.current
        if item.target_id == target_id
    }
    check(
        len(fresh) == len(v1_pack.shots()),
        f"{label} 應有 {len(v1_pack.shots())} 支當輪候選，實際 {len(fresh)}",
    )

    # 評分並選代表作
    with Session(engine) as session:
        for shot_id, variant_id in fresh.items():
            existing = session.exec(
                select(VariantQC).where(VariantQC.variant_id == variant_id)
            ).first()
            if existing is not None:
                continue
            session.add(
                VariantQC(
                    qc_id=f"qc_{variant_id}",
                    variant_id=variant_id,
                    project_id=PROJECT_ID,
                    shot_id=shot_id,
                    identity_consistency=90 if label == "v1" else 40,
                    usable_without_repair=True,
                    human_correction_minutes=2.0 if label == "v1" else 25.0,
                )
            )
        session.commit()

    for variant_id in fresh.values():
        attribution.select_benchmark_candidate(PROJECT_ID, variant_id)

    pair = next(
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.target_id == target_id and item.ready
    )
    saved = client.post(
        f"/benchmark/continuity/{pair.target_id}/{pair.shot_id}",
        data={
            "cross_shot_identity": "95" if label == "v1" else "30",
            "wardrobe_continuity": "90" if label == "v1" else "35",
            "notes": label,
        },
        follow_redirects=False,
    )
    check(saved.status_code == 303, f"{label} 連戲評分應儲存成功")

    return {"variants": fresh, "pair": pair}


def main() -> int:
    init_db()
    settings = get_settings()

    with TestClient(app) as client:
        setup_assets(client)
        primary = target.targets().targets[0].target_id

        # ---------------- Phase 1: v1 ----------------
        confirm_all(client, V1)
        v1_identity = identity.from_target(target.targets().by_id(primary))
        phase1 = run_phase(client, "v1", primary)

        report_v1 = build_report()
        aggregate_v1 = report_v1.by_target(primary)
        check(aggregate_v1 is not None, "v1 報表應含該比較對象")
        check(
            aggregate_v1.model_version == V1,
            f"v1 報表版本應為 {V1}，實際 {aggregate_v1.model_version}",
        )
        v1_attempts = aggregate_v1.total_attempts
        v1_failed = aggregate_v1.failed_attempts
        v1_credits = aggregate_v1.total_credits
        check(v1_attempts >= 12, f"v1 應有至少 12 次嘗試，實際 {v1_attempts}")
        check(v1_failed >= 6, f"v1 應有至少 6 次失敗，實際 {v1_failed}")
        check(not report_v1.has_excluded, "v1 階段不應有被排除的資料")

        v1_variant_ids = set(phase1["variants"].values())
        v1_continuity_count = len(
            [
                row
                for row in _all_continuity()
                if row.variant_id in v1_variant_ids
            ]
        )
        check(v1_continuity_count == 1, "v1 應有一筆連戲評分")

        # ---------------- Phase 2: 同一個 target 改成 v2 ----------------
        confirm_all(client, V2)
        v2_identity = identity.from_target(target.targets().by_id(primary))
        check(v1_identity.key != v2_identity.key, "改版後身份 key 必須不同")

        phase2 = run_phase(client, "v2", primary)
        report_v2 = build_report()
        aggregate_v2 = report_v2.by_target(primary)
        check(aggregate_v2 is not None, "v2 報表應含該比較對象")
        check(
            aggregate_v2.model_version == V2,
            f"v2 報表版本應為 {V2}，實際 {aggregate_v2.model_version}",
        )

        index = attribution.build_index(PROJECT_ID)
        v2_variant_ids = set(phase2["variants"].values())

        # A. v1 候選不進 v2 報表
        current_ids = {item.variant_id for item in index.current}
        check(
            not (v1_variant_ids & current_ids),
            "A. v1 的候選不得出現在目前這一輪",
        )
        check(
            v2_variant_ids <= current_ids,
            "A. v2 的候選必須全部在目前這一輪",
        )
        check(
            report_v2.excluded_variants >= len(v1_variant_ids),
            f"A. 應排除 v1 的 {len(v1_variant_ids)} 支候選，"
            f"實際 {report_v2.excluded_variants}",
        )

        # B. v1 的 VariantQC 不進 v2 報表
        # v1 打 90 分、v2 打 40 分；混在一起中位數會落在兩者之間
        v2_identity_score = aggregate_v2.overall.dimension("identity_consistency")
        check(
            v2_identity_score == 40.0,
            f"B. v2 的身份分數應只含 v2 的 40 分，實際 {v2_identity_score}",
        )

        # C. v1 的重試與失敗次數不進 v2
        check(
            aggregate_v2.total_attempts == v1_attempts,
            f"C. v2 的嘗試數應與 v1 相同（各自一輪），"
            f"實際 v1={v1_attempts} v2={aggregate_v2.total_attempts}",
        )
        check(
            aggregate_v2.failed_attempts == v1_failed,
            f"C. v2 的失敗數不得累加 v1，實際 {aggregate_v2.failed_attempts}",
        )
        check(
            report_v2.excluded_attempts >= v1_attempts,
            f"C. 應排除 v1 的 {v1_attempts} 次嘗試，"
            f"實際 {report_v2.excluded_attempts}",
        )

        # D. v1 的耗時與點數不進 v2
        check(
            aggregate_v2.total_credits == v1_credits,
            f"D. v2 的點數不得累加 v1，v1={v1_credits} v2={aggregate_v2.total_credits}",
        )
        v2_minutes = aggregate_v2.overall.total_human_minutes
        check(
            v2_minutes == 25.0 * len(v2_variant_ids),
            f"D. v2 的人工分鐘應只含 v2，實際 {v2_minutes}",
        )

        # E. v1 的連戲評分不進 v2
        v2_continuity = aggregate_v2.overall.continuity_identity
        check(
            v2_continuity is None or v2_continuity == 30.0,
            f"E. v2 的連戲分數不得含 v1 的 95 分，實際 {v2_continuity}",
        )

        # F. v1 的歷史紀錄全部仍存在
        all_variant_ids = {item.variant_id for item in index.items}
        check(
            v1_variant_ids <= all_variant_ids,
            "F. v1 的候選必須仍在索引中，只是不計入目前這一輪",
        )
        with Session(engine) as session:
            surviving_qc = session.exec(
                select(VariantQC).where(VariantQC.project_id == PROJECT_ID)
            ).all()
        surviving_ids = {row.variant_id for row in surviving_qc}
        check(
            v1_variant_ids <= surviving_ids,
            "F. v1 的 VariantQC 不得被刪除",
        )
        ledger = attempts.read_ledger(ledger_path())
        v1_rows = [
            item for item in ledger.attempts if item.model_version == V1
        ]
        check(
            len(v1_rows) >= v1_attempts,
            f"F. v1 的嘗試紀錄不得消失，實際 {len(v1_rows)}",
        )
        check(
            all(row.model_version == V1 for row in v1_rows),
            "F. v1 的嘗試紀錄版本不得被改寫成 v2",
        )

        # G. v1 可被識別為歷史
        historical = {item.variant_id for item in index.historical}
        check(
            v1_variant_ids <= historical,
            "G. v1 的候選應可被辨識為歷史資料",
        )
        check(
            all(item.version_drift for item in index.historical),
            "G. 歷史候選應標記 version_drift",
        )
        check(
            v1_identity.key in report_v2.excluded_identities,
            f"G. 報表應列出被排除的身份，實際 {report_v2.excluded_identities}",
        )

        # H. v2 報表只含 v2 身份
        for item in index.current:
            if item.target_id != primary:
                continue
            check(
                item.model_version == V2,
                f"H. 目前這一輪不得含非 v2 的候選: {item.variant_id}",
            )
        current_attempts = ledger.current(identity.current_keys())
        check(
            all(item.model_version == V2 for item in current_attempts),
            "H. 目前這一輪的嘗試紀錄應全為 v2",
        )

        # 自動選片必須挑當輪的候選。沿用舊版本的代表作，會讓連戲評分
        # 全部落在舊影片上，而畫面上看起來一切正常。
        auto = attribution.auto_select_benchmark_candidates(PROJECT_ID)
        after_auto = attribution.build_index(PROJECT_ID)
        stale_reps = [
            item.variant_id
            for item in after_auto.historical
            if item.benchmark_selected
        ]
        check(
            not stale_reps,
            f"自動選片後不得有舊版本仍是代表作: {stale_reps}",
        )
        current_reps = {
            item.shot_id
            for item in after_auto.current
            if item.target_id == primary and item.benchmark_selected
        }
        check(
            len(current_reps) == len(v1_pack.shots()),
            f"每顆鏡頭都應有當輪代表作，實際 {len(current_reps)}（新選 {auto} 組）",
        )

        # 報表頁必須說出少算了什麼，而不是靜靜地少算
        results = client.get("/benchmark/results")
        check(results.status_code == 200, "結果頁應可開啟")
        check(
            "屬於舊版本，未計入本表" in results.text,
            "報表頁必須明示有資料因版本不符而未計入",
        )
        check(
            f"{report_v2.excluded_variants} 支候選" in results.text,
            "報表頁應顯示被排除的候選數",
        )
        check(
            f"{report_v2.excluded_attempts} 次嘗試" in results.text,
            "報表頁應顯示被排除的嘗試數",
        )
        check(
            V1 in results.text,
            "報表頁應列出被排除的身份，讓使用者知道是哪一版",
        )

        # I. 換掉任一端代表作後，current pair 回到未評分
        pairs_before = workflow.continuity_pairs(PROJECT_ID)
        scored_pair = next(
            item
            for item in pairs_before
            if item.target_id == primary and item.scored
        )
        # 為參照那一端多匯入一支當輪候選，才能真的改選代表作。
        # 刻意不挑 v1 的候選：那會變成測跨版本配對，不是測換代表作。
        ref_job = next(
            item
            for item in jobs_for(primary)
            if item.shot_id == scored_pair.ref_shot_id
            and item.model_version == V2
        )
        extra = client.post(
            f"/benchmark/jobs/{ref_job.job_id}/import",
            files=[
                ("files", ("v2_alternate.mp4", clip("alternate"), "video/mp4"))
            ],
            follow_redirects=False,
        )
        check("ok=1" in extra.headers["location"], "應可再匯入一支候選")

        index = attribution.build_index(PROJECT_ID)
        siblings = [
            item
            for item in index.current
            if item.target_id == primary
            and item.shot_id == scored_pair.ref_shot_id
            and item.variant_id != scored_pair.ref_variant_id
        ]
        check(siblings, "應有另一支當輪候選可改選為代表作")
        attribution.select_benchmark_candidate(PROJECT_ID, siblings[0].variant_id)

        pairs_after = workflow.continuity_pairs(PROJECT_ID)
        same_pair = next(
            item
            for item in pairs_after
            if item.target_id == primary
            and item.shot_id == scored_pair.shot_id
            and item.ref_shot_id == scored_pair.ref_shot_id
        )
        check(
            same_pair.ref_variant_id == siblings[0].variant_id,
            "代表作應已更換",
        )
        check(
            not same_pair.scored,
            "I. 換掉參照端的代表作後，這一組必須回到未評分",
        )
        check(
            same_pair.stale_scores >= 1,
            "I. 應標示存在舊的評分紀錄",
        )

        # J. 舊的連戲紀錄未被刪除或覆寫
        before_rows = _all_continuity()
        rescored = client.post(
            f"/benchmark/continuity/{same_pair.target_id}/{same_pair.shot_id}",
            data={"cross_shot_identity": "55", "notes": "after reselect"},
            follow_redirects=False,
        )
        check(
            rescored.status_code == 303,
            f"J. 重新評分應成功，實際 {rescored.status_code}: "
            f"{rescored.text[:200]}",
        )
        after_rows = _all_continuity()
        check(
            len(after_rows) == len(before_rows) + 1,
            f"J. 重新評分應新增一列而非覆寫，"
            f"before={len(before_rows)} after={len(after_rows)}",
        )
        old = next(
            row for row in after_rows if row.ref_variant_id == scored_pair.ref_variant_id
        )
        check(
            old.cross_shot_identity == 30,
            f"J. 舊的連戲分數不得被改寫，實際 {old.cross_shot_identity}",
        )
        fresh_row = next(
            row for row in after_rows if row.ref_variant_id == siblings[0].variant_id
        )
        check(
            fresh_row.cross_shot_identity == 55,
            "J. 新配對應寫入新的分數",
        )

    print(
        f"OK benchmark version drift smoke "
        f"v1_attempts={v1_attempts} v2_attempts={aggregate_v2.total_attempts} "
        f"excluded_variants={report_v2.excluded_variants} "
        f"excluded_attempts={report_v2.excluded_attempts} "
        f"continuity_rows={len(after_rows)}"
    )
    return 0


def _all_continuity() -> list[ContinuityQC]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(ContinuityQC).where(ContinuityQC.project_id == PROJECT_ID)
            ).all()
        )


if __name__ == "__main__":
    raise SystemExit(main())
