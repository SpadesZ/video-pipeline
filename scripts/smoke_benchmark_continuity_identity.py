# 檔案路徑: video-pipeline/scripts/smoke_benchmark_continuity_identity.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   連戲代表作與連戲評分的 current / historical 邊界測試。
# 主要責任:
#   A. 換版本後自動選片只留當輪代表作，舊版本的選定旗標必須清掉。
#   B. 同版本內換代表作後，舊的連戲評分不得進入目前的報表。
# 說明:
#   這兩個情境都不會報錯，也不會少資料，只會讓數字悄悄變成別的東西：
#   A 會讓連戲比到舊版本的影片，B 會把同一顆鏡頭的連戲算兩次。
#   兩者都要靠斷言抓，肉眼看畫面看不出來。
#
#   A 刻意完全不呼叫 select_benchmark_candidate()，只用自動選片，
#   因為手動選片會順手清掉同組的旗標，那會蓋掉要測的問題。
#   B 刻意在同一個版本內進行，確認判斷依據不是版本而是實際配對。
#
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

_TMP_DIR = Path(tempfile.mkdtemp(prefix="benchmark_continuity_smoke_"))
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
    job_view,
    target,
    v1_pack,
    workflow,
)
from pipeline.db import engine, init_db  # noqa: E402
from pipeline.models.qc import ContinuityQC  # noqa: E402
from pipeline.models.variant import AssetVariant  # noqa: E402
from pipeline.settings import get_settings  # noqa: E402

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID
V1 = "1.0-alpha"
V2 = "2.0-beta"

# 兩個差距明顯的分數。混算時中位數會落在中間，一眼就能從斷言看出來。
OLD_SCORE = 90
NEW_SCORE = 60


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def png(size) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 60, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def clip(seed: str) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + seed.encode("utf-8") + os.urandom(320)


def setup_assets(client: TestClient) -> None:
    for required in v1_pack.REQUIRED_ASSETS:
        size = (576, 1024) if required.require_vertical else (800, 800)
        client.post(
            f"/benchmark/assets/{required.asset_id}",
            files={"file": (f"{required.asset_id}.png", png(size), "image/png")},
            follow_redirects=False,
        )


def confirm_all(client: TestClient, version: str) -> None:
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
        check("ok=1" in response.headers["location"], f"{item.target_id} 應可確認")


def import_round(client: TestClient, label: str, target_id: str) -> None:
    """對該對象所有尚無候選的派工各匯入一支影片。"""
    response = client.post("/benchmark/build", follow_redirects=False)
    check(response.status_code == 303, f"{label} 派工應成功")

    for view in job_view.list_jobs(PROJECT_ID):
        if view.target_id != target_id or view.variants_imported:
            continue
        name = f"{label}_{view.shot_id}.mp4"
        result = client.post(
            f"/benchmark/jobs/{view.job_id}/import",
            files=[("files", (name, clip(f"{label}{view.job_id}"), "video/mp4"))],
            follow_redirects=False,
        )
        check("ok=1" in result.headers["location"], f"{label} 匯入應成功")


def selected_rows() -> list[AssetVariant]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AssetVariant).where(
                    AssetVariant.project_id == PROJECT_ID,
                    AssetVariant.benchmark_selected == True,  # noqa: E712
                )
            ).all()
        )


def continuity_rows() -> list[ContinuityQC]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(ContinuityQC).where(ContinuityQC.project_id == PROJECT_ID)
            ).all()
        )


def build_report():
    """走與 /benchmark/results 完全相同的路徑。

    刻意不在測試裡複製一份組裝邏輯：那樣路由改壞了測試也不會紅。
    """
    from pipeline.benchmark import report as report_module

    return report_module.build_current_report(get_settings(), PROJECT_ID)


# --------------------------------------------------------------------------
# Regression A：只用自動選片，驗證舊版本代表作被清乾淨
# --------------------------------------------------------------------------

def regression_a(client: TestClient) -> str:
    primary = target.targets().targets[0].target_id

    confirm_all(client, V1)
    import_round(client, "v1", primary)
    attribution.auto_select_benchmark_candidates(PROJECT_ID)

    index = attribution.build_index(PROJECT_ID)
    v1_variants = {
        item.variant_id for item in index.items if item.target_id == primary
    }
    v1_reps = {
        item.shot_id: item.variant_id
        for item in index.current
        if item.target_id == primary and item.benchmark_selected
    }
    check(
        len(v1_reps) == len(v1_pack.shots()),
        f"v1 每顆鏡頭都應有代表作，實際 {len(v1_reps)}",
    )

    # 換版本，重新派工並匯入。全程不呼叫 select_benchmark_candidate。
    confirm_all(client, V2)
    import_round(client, "v2", primary)
    moved = attribution.auto_select_benchmark_candidates(PROJECT_ID)
    check(moved >= len(v1_pack.shots()), f"應為 v2 補選代表作，實際 {moved}")

    index = attribution.build_index(PROJECT_ID)
    current_for_primary = [
        item for item in index.current if item.target_id == primary
    ]
    historical_for_primary = [
        item for item in index.historical if item.target_id == primary
    ]

    # A1 每個 current (target, shot) 有且只有一支代表作
    by_shot: dict[str, list[str]] = {}
    for item in current_for_primary:
        if item.benchmark_selected:
            by_shot.setdefault(item.shot_id, []).append(item.variant_id)
    check(
        len(by_shot) == len(v1_pack.shots()),
        f"A1. 每顆鏡頭都應有當輪代表作，實際 {len(by_shot)}",
    )
    duplicated = {shot: ids for shot, ids in by_shot.items() if len(ids) != 1}
    check(not duplicated, f"A1. 每顆鏡頭只能有一支代表作: {duplicated}")

    # A2 舊版本候選全部不再是代表作
    stale = [
        item.variant_id
        for item in historical_for_primary
        if item.benchmark_selected
    ]
    check(not stale, f"A2. 舊版本候選不得保留代表作旗標: {stale}")
    persisted = {
        row.variant_id
        for row in selected_rows()
        if row.variant_id in {item.variant_id for item in historical_for_primary}
    }
    check(not persisted, f"A2. 資料庫中舊版本仍被標為代表作: {persisted}")

    # A3 配對用的兩支影片都必須是當輪身份
    current_ids = {item.variant_id for item in index.current}
    pairs = [
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.target_id == primary and item.ready
    ]
    check(pairs, "A3. 應有可評的配對")
    for pair in pairs:
        check(
            pair.variant_id in current_ids,
            f"A3. 配對主影片 {pair.variant_id} 不是當輪候選",
        )
        check(
            pair.ref_variant_id in current_ids,
            f"A3. 配對參照影片 {pair.ref_variant_id} 不是當輪候選",
        )

    # A4 不得因字典覆寫或排序而指回舊版本
    v1_ids = {item.variant_id for item in historical_for_primary}
    for pair in pairs:
        check(
            pair.variant_id not in v1_ids and pair.ref_variant_id not in v1_ids,
            f"A4. 配對指回了舊版本影片: {pair.identity}",
        )

    # A5 舊候選仍在資料庫，只是不再是代表作
    with Session(engine) as session:
        surviving = {
            row.variant_id
            for row in session.exec(
                select(AssetVariant).where(AssetVariant.project_id == PROJECT_ID)
            ).all()
        }
    check(
        v1_variants <= surviving,
        "A5. 舊版本候選不得被刪除",
    )
    check(
        v1_ids and v1_ids <= surviving,
        "A5. 舊版本候選應仍可查得到",
    )

    _verify_residual_flag_ignored(primary, pairs[0])
    return primary


def _verify_residual_flag_ignored(primary: str, sample) -> None:
    """舊資料殘留的選定旗標不得影響當輪配對。

    自動選片會清掉這些旗標，但資料庫可能是舊版程式寫出來的，或有人
    直接改過資料。讀取端因此不能假設旗標已經乾淨——這裡直接把殘留
    寫回去，確認配對與評分表都還是指向當輪的影片。

    刻意把當輪代表作的旗標拿掉、只留舊版本那支，讓「有沒有過濾」的
    差別不依賴 variant_id 的排序：沒過濾就只會挑到舊版本那支，
    有過濾則是這顆鏡頭暫時沒有代表作。variant_id 是隨機十六進位，
    靠排序碰運氣的測試會時好時壞。
    """
    index = attribution.build_index(PROJECT_ID)
    stale_candidates = [
        item
        for item in index.historical
        if item.target_id == primary and item.shot_id == sample.shot_id
    ]
    check(stale_candidates, "應有舊版本候選可用於模擬殘留")
    residue = stale_candidates[0]

    with Session(engine) as session:
        stale_row = session.get(AssetVariant, residue.variant_id)
        stale_row.benchmark_selected = True
        session.add(stale_row)
        current_row = session.get(AssetVariant, sample.variant_id)
        current_row.benchmark_selected = False
        session.add(current_row)
        session.commit()

    try:
        pair = next(
            item
            for item in workflow.continuity_pairs(PROJECT_ID)
            if item.target_id == primary and item.shot_id == sample.shot_id
        )
        check(
            pair.variant_id != residue.variant_id,
            f"A4. 配對指回了殘留的舊版本影片 {residue.variant_id}",
        )
        check(
            pair.variant_id is None,
            f"A4. 當輪沒有代表作時配對不該有主影片，實際 {pair.variant_id}",
        )
        check(
            not pair.ready,
            "A4. 只剩舊版本被標為代表作時，這組不該被視為可評分",
        )

        # 評分表同樣不得被殘留旗標帶走
        sheets = workflow.sheets_dir(get_settings())
        from pipeline.benchmark import score_sheet

        path, _ = score_sheet.sync_continuity_sheet(sheets, PROJECT_ID)
        import csv

        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [
                item
                for item in csv.DictReader(handle)
                if item["target_id"] == primary
                and item["shot_id"] == sample.shot_id
            ]
        check(rows, "評分表應有該配對的列")
        check(
            residue.variant_id
            not in {rows[0]["variant_id"], rows[0]["ref_variant_id"]},
            f"A4. 連戲評分表寫入了殘留的舊版本影片: {rows[0]}",
        )
    finally:
        with Session(engine) as session:
            stale_row = session.get(AssetVariant, residue.variant_id)
            stale_row.benchmark_selected = False
            session.add(stale_row)
            current_row = session.get(AssetVariant, sample.variant_id)
            current_row.benchmark_selected = True
            session.add(current_row)
            session.commit()

    restored = next(
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.target_id == primary and item.shot_id == sample.shot_id
    )
    check(
        restored.variant_id == sample.variant_id,
        "A4. 還原後配對應回到當輪代表作",
    )


# --------------------------------------------------------------------------
# Regression B：同一版本內換代表作，舊評分不得進報表
# --------------------------------------------------------------------------

def regression_b(client: TestClient, primary: str) -> None:
    pair = next(
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.target_id == primary and item.ready
    )
    shot_x, shot_y = pair.shot_id, pair.ref_shot_id
    variant_a, variant_b = pair.variant_id, pair.ref_variant_id

    before_rows = len(continuity_rows())
    saved = client.post(
        f"/benchmark/continuity/{primary}/{shot_x}",
        data={
            "cross_shot_identity": str(OLD_SCORE),
            "wardrobe_continuity": str(OLD_SCORE),
            "notes": "A-B pair",
        },
        follow_redirects=False,
    )
    check(saved.status_code == 303, f"A↔B 評分應成功，實際 {saved.status_code}")
    check(len(continuity_rows()) == before_rows + 1, "應新增一列 A↔B 評分")

    # 為 shot Y 再匯入一支同版本候選，改選為代表作
    ref_job = next(
        item
        for item in job_view.list_jobs(PROJECT_ID)
        if item.target_id == primary
        and item.shot_id == shot_y
        and item.model_version == V2
    )
    extra = client.post(
        f"/benchmark/jobs/{ref_job.job_id}/import",
        files=[("files", ("variant_c.mp4", clip("variant_c"), "video/mp4"))],
        follow_redirects=False,
    )
    check("ok=1" in extra.headers["location"], "應可再匯入一支同版本候選")

    index = attribution.build_index(PROJECT_ID)
    variant_c = next(
        item.variant_id
        for item in index.current
        if item.target_id == primary
        and item.shot_id == shot_y
        and item.variant_id != variant_b
    )
    attribution.select_benchmark_candidate(PROJECT_ID, variant_c)

    # B3 目前的配對必須是 A↔C，而且未評分
    current = next(
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.target_id == primary and item.shot_id == shot_x
    )
    check(
        current.variant_id == variant_a and current.ref_variant_id == variant_c,
        f"B3. 目前配對應為 A↔C，實際 {current.variant_id}↔{current.ref_variant_id}",
    )
    check(not current.scored, "B3. 換過代表作後目前配對必須是未評分")
    check(current.stale_scores >= 1, "B3. 應標示存在舊的評分")

    rescored = client.post(
        f"/benchmark/continuity/{primary}/{shot_x}",
        data={
            "cross_shot_identity": str(NEW_SCORE),
            "wardrobe_continuity": str(NEW_SCORE),
            "notes": "A-C pair",
        },
        follow_redirects=False,
    )
    check(rescored.status_code == 303, "A↔C 評分應成功")

    rows = continuity_rows()
    # B1 兩列都在
    check(
        len(rows) == before_rows + 2,
        f"B1. 應有兩列連戲評分，實際 {len(rows) - before_rows}",
    )
    # B2 舊的 A-B 仍存在且分數未被改寫
    old_row = next(
        (row for row in rows if row.ref_variant_id == variant_b), None
    )
    check(old_row is not None, "B2. 舊的 A↔B 評分不得消失")
    check(
        old_row.cross_shot_identity == OLD_SCORE,
        f"B2. 舊評分不得被改寫，實際 {old_row.cross_shot_identity}",
    )
    new_row = next(
        (row for row in rows if row.ref_variant_id == variant_c), None
    )
    check(new_row is not None, "B2. 新的 A↔C 評分應存在")
    check(new_row.cross_shot_identity == NEW_SCORE, "B2. 新評分應寫入")

    # B4 / B5 報表只採計新配對
    report = build_report()
    aggregate = report.by_target(primary)
    check(aggregate is not None, "報表應含該比較對象")
    identity_median = aggregate.overall.continuity_identity
    check(
        identity_median == float(NEW_SCORE),
        f"B4/B5. 目前連戲分數應只含 {NEW_SCORE}，實際 {identity_median}",
    )
    check(
        identity_median != float(OLD_SCORE),
        "B5. 舊分數不得成為目前的連戲分數",
    )
    midpoint = (OLD_SCORE + NEW_SCORE) / 2
    check(
        identity_median != midpoint,
        f"B5. 兩筆被混算成中位數 {midpoint}",
    )

    # B6 舊配對計入排除數
    check(
        report.excluded_continuity >= 1,
        f"B6. 舊配對應計入排除數，實際 {report.excluded_continuity}",
    )

    # B7 報表頁要說出有資料未計入
    results = client.get("/benchmark/results")
    check(results.status_code == 200, "B7. 結果頁應可開啟")
    check(
        "屬於舊版本，未計入本表" in results.text,
        "B7. 結果頁應說明有連戲資料未計入",
    )
    check(
        f"{report.excluded_continuity} 組連戲" in results.text,
        "B7. 結果頁應顯示被排除的連戲組數",
    )

    # B8 重複組裝結果必須一致
    again = build_report()
    check(
        report.model_dump() == again.model_dump(),
        "B8. 相同資料重複組裝必須得到相同報表",
    )
    third = build_report().by_target(primary).overall.continuity_identity
    check(
        third == identity_median,
        f"B8. 多次讀取結果不一致: {identity_median} vs {third}",
    )

    # 進度統計也只能算當輪的配對
    state = workflow.collect(get_settings())
    scored_now = sum(
        1
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.ready and item.scored
    )
    check(
        state.continuity_scored == scored_now,
        f"進度應只算當輪配對，實際 {state.continuity_scored} vs {scored_now}",
    )
    check(
        state.continuity_scored < len(rows),
        "進度不得把歷史列一起算進去",
    )


def main() -> int:
    init_db()
    with TestClient(app) as client:
        setup_assets(client)
        primary = regression_a(client)
        regression_b(client, primary)

    rows = continuity_rows()
    index = attribution.build_index(PROJECT_ID)
    print(
        f"OK benchmark continuity identity smoke "
        f"current={len(index.current)} historical={len(index.historical)} "
        f"continuity_rows={len(rows)} "
        f"current_pairs={len(workflow.current_pair_identities(PROJECT_ID))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
