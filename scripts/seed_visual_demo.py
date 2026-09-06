# 檔案路徑: video-pipeline/scripts/seed_visual_demo.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   視覺稽核用的示範資料。
# 主要責任:
#   1. 讓每個主要頁面都有足夠內容可以實際看，而不是空狀態。
#   2. 以固定順序與固定分數產生，重跑得到相同畫面。
# 說明:
#   空頁面看不出版面問題：表格擠不擠、影片會不會過長、評分欄位離影片
#   多遠，都要有資料才看得出來。這支腳本把流程推到指定的狀態，
#   不呼叫任何影片生成 API，影片是本地產生的測試檔。
#
#   用法:
#     python scripts/seed_visual_demo.py <stage>
#   stage:
#     empty     初始狀態
#     assets    素材齊全、版本未確認
#     jobs      版本已確認、派工已建立
#     full      已匯入、已評分、已選代表作、含一組已評與一組待重評的連戲
# --------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
API_ROOT = ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

STAGE = (sys.argv[1] if len(sys.argv) > 1 else "full").strip().lower()
DATA_DIR = ROOT / "data" / "visual_audit"

os.environ["DATA_DIR"] = str(DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(DATA_DIR / 'audit.db').as_posix()}"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

if DATA_DIR.exists():
    shutil.rmtree(DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

from PIL import Image, ImageDraw  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.main import app  # noqa: E402
from pipeline.benchmark import attribution, job_view, target, v1_pack, workflow  # noqa: E402
from pipeline.db import engine, init_db  # noqa: E402
from pipeline.models.qc import VariantQC  # noqa: E402
from pipeline.models.variant import AssetVariant  # noqa: E402

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID
VERSION = "1.6-standard"

PALETTE = {
    "ref_char_a_sheet": (58, 74, 112),
    "ref_char_b_sheet": (96, 74, 58),
    "ref_frame_a1": (32, 44, 68),
    "ref_frame_a2": (48, 40, 60),
    "ref_frame_b1": (40, 58, 52),
    "ref_frame_b2": (60, 48, 44),
    "ref_frame_b3": (44, 44, 66),
    "ref_frame_c1": (66, 42, 42),
}


def labelled_png(size, colour, caption: str) -> bytes:
    """帶字的示範圖，截圖時看得出哪一格是哪一張。"""
    image = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [size[0] * 0.08, size[1] * 0.08, size[0] * 0.92, size[1] * 0.92],
        outline=(200, 214, 235),
        width=4,
    )
    draw.text((size[0] * 0.12, size[1] * 0.12), caption, fill=(226, 234, 246))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ffmpeg() -> str | None:
    """找得到的 ffmpeg。PATH 沒有時退回 imageio 附帶的靜態執行檔。

    視覺稽核要真的播放影片，位元組佔位檔在瀏覽器裡只會是黑框，
    看不出播放器尺寸與版面問題。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 僅為稽核方便，缺了就退回佔位檔
        return None


def demo_video(seed: str, seconds: int = 5) -> bytes:
    """可播放的測試影片。有 ffmpeg 就產生真影片，否則退回位元組佔位檔。"""
    binary = _ffmpeg()
    if binary:
        out = DATA_DIR / "_tmp_clip.mp4"
        # 內容必須隨 seed 改變：匯入會以檔案雜湊去重，每支都一樣的話
        # 只會留下第一支，換代表作那種需要兩支候選的畫面就做不出來。
        tint = abs(hash(seed)) % 360
        completed = subprocess.run(
            [
                binary, "-y", "-v", "error",
                "-f", "lavfi",
                "-i", f"testsrc=size=540x960:duration={seconds}:rate=12",
                "-vf", f"hue=h={tint}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(out),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0 and out.exists():
            payload = out.read_bytes()
            out.unlink()
            return payload
    return b"\x00\x00\x00\x18ftypmp42" + seed.encode("utf-8") + os.urandom(2048)


def upload_assets(client: TestClient) -> None:
    for required in v1_pack.REQUIRED_ASSETS:
        vertical = required.require_vertical
        size = (576, 1024) if vertical else (768, 768)
        payload = labelled_png(
            size,
            PALETTE.get(required.asset_id, (48, 60, 80)),
            required.asset_id,
        )
        response = client.post(
            f"/benchmark/assets/{required.asset_id}",
            files={"file": (f"{required.asset_id}.png", payload, "image/png")},
            follow_redirects=False,
        )
        assert "ok=1" in response.headers["location"], required.asset_id
    print("  assets: 8 uploaded")


def confirm_targets(client: TestClient) -> None:
    labels = {
        "kling": "Kling 1.6 Standard",
        "seedance": "Seedance Pro",
        "veo": "Veo 3 Fast",
        "runway": "Gen-3 Alpha Turbo",
    }
    for item in target.targets().targets:
        response = client.post(
            f"/benchmark/targets/{item.target_id}",
            data={
                "model_id": item.model_id,
                "model_version": VERSION,
                "ui_label": labels.get(item.provider, item.provider),
                "confirmed": "1",
            },
            follow_redirects=False,
        )
        assert "ok=1" in response.headers["location"], item.target_id
    print("  targets: 4 confirmed")


def build_jobs(client: TestClient) -> None:
    response = client.post("/benchmark/build", follow_redirects=False)
    assert response.status_code == 303, response.status_code
    print(f"  jobs: {len(job_view.list_jobs(PROJECT_ID))} created")


def generate_round(client: TestClient) -> None:
    """對前兩個對象匯入影片、記錄嘗試並評分，讓比較頁有東西可看。"""
    targets_in_scope = [item.target_id for item in target.targets().targets[:2]]
    imported = 0
    for index_no, view in enumerate(job_view.list_jobs(PROJECT_ID)):
        if view.target_id not in targets_in_scope:
            continue
        client.post(
            f"/benchmark/jobs/{view.job_id}/attempt",
            data={
                "status": "failed",
                "failure_reason": "平台回報內容政策拒絕",
                "generation_seconds": "22",
                "credits_used": "3",
            },
            follow_redirects=False,
        )
        name = f"{view.target_id}_{view.shot_id}.mp4"
        client.post(
            f"/benchmark/jobs/{view.job_id}/attempt",
            data={
                "status": "success",
                "output_file": name,
                "generation_seconds": str(58 + index_no),
                "credits_used": "10",
            },
            follow_redirects=False,
        )
        result = client.post(
            f"/benchmark/jobs/{view.job_id}/import",
            files=[("files", (name, demo_video(view.job_id), "video/mp4"))],
            follow_redirects=False,
        )
        assert "ok=1" in result.headers["location"], result.headers["location"]
        imported += 1
    print(f"  variants: {imported} imported")


def score_variants() -> None:
    index = attribution.build_index(PROJECT_ID)
    ordered = sorted(index.current, key=lambda item: (item.target_id, item.shot_id))
    with Session(engine) as session:
        for position, item in enumerate(ordered):
            if session.exec(
                select(VariantQC).where(VariantQC.variant_id == item.variant_id)
            ).first():
                continue
            strong = position % 2 == 0
            session.add(
                VariantQC(
                    qc_id=f"qc_{item.variant_id}",
                    variant_id=item.variant_id,
                    project_id=PROJECT_ID,
                    shot_id=item.shot_id,
                    identity_consistency=88 if strong else 62,
                    temporal_stability=85 if strong else 58,
                    prompt_adherence=80 if strong else 66,
                    motion_quality=78 if strong else 54,
                    camera_control=82 if strong else 60,
                    facial_acting=(
                        76 if v1_pack.evaluates_facial_acting(item.shot_id) else None
                    ),
                    artifact_severity=12 if strong else 38,
                    usable_without_repair=strong,
                    human_correction_minutes=3.0 if strong else 18.0,
                    generation_seconds=58.0,
                )
            )
        session.commit()
    print(f"  qc: {len(ordered)} variants scored")


def score_continuity(client: TestClient) -> None:
    """評一組連戲，另一組換掉代表作留成待重評，讓兩種狀態都看得到。"""
    attribution.auto_select_benchmark_candidates(PROJECT_ID)
    pairs = [item for item in workflow.continuity_pairs(PROJECT_ID) if item.ready]
    if not pairs:
        print("  continuity: no ready pair")
        return

    first = pairs[0]
    client.post(
        f"/benchmark/continuity/{first.target_id}/{first.shot_id}",
        data={
            "cross_shot_identity": "84",
            "wardrobe_continuity": "78",
            "location_continuity": "90",
            "notes": "臉部一致，外套顏色略有差異",
        },
        follow_redirects=False,
    )

    # 第二組：先評分，再換掉參照端的代表作，製造「需重評」狀態
    if len(pairs) > 1:
        second = pairs[1]
        client.post(
            f"/benchmark/continuity/{second.target_id}/{second.shot_id}",
            data={
                "cross_shot_identity": "71",
                "wardrobe_continuity": "69",
                "notes": "先前的配對",
            },
            follow_redirects=False,
        )
        ref_job = next(
            (
                item
                for item in job_view.list_jobs(PROJECT_ID)
                if item.target_id == second.target_id
                and item.shot_id == second.ref_shot_id
            ),
            None,
        )
        if ref_job is not None:
            client.post(
                f"/benchmark/jobs/{ref_job.job_id}/import",
                files=[
                    ("files", ("alternate_take.mp4", demo_video("alt"), "video/mp4"))
                ],
                follow_redirects=False,
            )
            index = attribution.build_index(PROJECT_ID)
            alternate = next(
                (
                    item.variant_id
                    for item in index.current
                    if item.target_id == second.target_id
                    and item.shot_id == second.ref_shot_id
                    and item.variant_id != second.ref_variant_id
                ),
                None,
            )
            if alternate:
                attribution.select_benchmark_candidate(PROJECT_ID, alternate)

    state = workflow.continuity_pairs(PROJECT_ID)
    scored = sum(1 for item in state if item.scored)
    stale = sum(1 for item in state if item.stale_scores and not item.scored)
    print(f"  continuity: {scored} scored, {stale} needing re-score")


def main() -> int:
    init_db()
    with TestClient(app) as client:
        print(f"seeding stage={STAGE} into {DATA_DIR}")
        if STAGE == "empty":
            print("  (nothing seeded)")
            return 0

        upload_assets(client)
        if STAGE == "assets":
            return 0

        confirm_targets(client)
        build_jobs(client)
        if STAGE == "jobs":
            return 0

        generate_round(client)
        score_variants()
        score_continuity(client)

    with Session(engine) as session:
        variants = len(
            session.exec(
                select(AssetVariant).where(AssetVariant.project_id == PROJECT_ID)
            ).all()
        )
    print(f"done. variants in db = {variants}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
