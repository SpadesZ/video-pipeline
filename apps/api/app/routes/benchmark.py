# 檔案路徑: video-pipeline/apps/api/app/routes/benchmark.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark 控制台頁面。
# 主要責任:
#   1. 以單一頁面呈現素材、比較對象、派工、候選、評分與結果的進度。
#   2. 提供每個階段的下一步操作，使流程不必倚賴 CLI 與 CSV。
# 說明:
#   人工 benchmark 會反覆在平台與系統之間切換，若每一步都要記住指令、
#   複製 request_hash、手動編輯 CSV，出錯的機會遠高於實際判斷影片好壞。
#   本頁把流程收斂成可點擊的步驟，CSV 保留為進階與備援途徑。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from app.deps import settings_dep
from pipeline.benchmark import attempts, attribution, builder, score_sheet, target, v1_pack
from pipeline.db import engine
from pipeline.models.qc import ContinuityQC, VariantQC
from pipeline.models.variant import AssetVariant, CapabilityJob
from pipeline.project_store import load_project
from pipeline.settings import Settings
from pipeline.stages.shot_dispatcher import ShotReadinessState, assess_project_readiness

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID


def sheets_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "benchmark" / "v1" / "sheets"


def _step(number: int, title: str, state: str, detail: str, cta: str = "") -> str:
    """一個流程步驟。state 決定顏色與圖示。"""
    icons = {"done": "✓", "active": "→", "todo": "○", "blocked": "!"}
    icon = icons.get(state, "○")
    return f"""
    <div class="bm-step bm-{escape(state)}">
      <div class="bm-step-head">
        <span class="bm-icon">{icon}</span>
        <strong>{number}. {escape(title)}</strong>
      </div>
      <div class="bm-detail">{detail}</div>
      <div class="bm-cta">{cta}</div>
    </div>
    """


def _collect_state(settings: Settings) -> dict:
    """收集頁面所需的全部狀態。任何一段失敗都不應讓整頁無法開啟。"""
    state: dict = {
        "assets": None,
        "targets": target.targets(),
        "artifact": None,
        "readiness": {},
        "jobs": {},
        "index": None,
        "qc_count": 0,
        "continuity_count": 0,
        "ledger": None,
        "error": None,
    }
    try:
        state["assets"] = builder.check_assets(settings)
        state["artifact"] = load_project(settings, PROJECT_ID)
        if state["artifact"] is not None:
            state["readiness"] = assess_project_readiness(state["artifact"])
            state["index"] = attribution.build_index(PROJECT_ID)
            with Session(engine) as session:
                jobs = session.exec(
                    select(CapabilityJob).where(
                        CapabilityJob.project_id == PROJECT_ID
                    )
                ).all()
                for job in jobs:
                    snapshot = (job.request_snapshot or {}).get("parameters") or {}
                    key = snapshot.get("_bm_target_id") or job.provider
                    state["jobs"][key] = state["jobs"].get(key, 0) + 1
                state["qc_count"] = len(
                    session.exec(
                        select(VariantQC).where(VariantQC.project_id == PROJECT_ID)
                    ).all()
                )
                state["continuity_count"] = len(
                    session.exec(
                        select(ContinuityQC).where(
                            ContinuityQC.project_id == PROJECT_ID
                        )
                    ).all()
                )
        state["ledger"] = attempts.read_ledger(
            sheets_dir(settings) / attempts.ATTEMPTS_SHEET
        )
    except Exception as error:  # noqa: BLE001 - 控制台不應因單一區塊失敗而全毀
        logger.warning("Benchmark state collection failed: %s", error)
        state["error"] = str(error)[:300]
    return state


def _asset_step(state: dict) -> str:
    report = state["assets"]
    if report is None:
        return _step(1, "準備參考素材", "blocked", "無法讀取素材目錄")

    total = len(report.statuses)
    ready = total - len(report.missing) - len(report.invalid)
    if report.ready:
        detail = f"{ready}/{total} 已就緒並通過驗證"
        return _step(1, "準備參考素材", "done", detail)

    rows = "".join(
        f"<li><code>{escape(item.filename)}</code> — "
        f"{escape('; '.join(item.problems) if item.problems else '尚未放入')}</li>"
        for item in [*report.missing, *report.invalid][:10]
    )
    detail = (
        f"{ready}/{total} 就緒。放入 <code>{escape(report.assets_dir)}</code>："
        f"<ul class='bm-list'>{rows}</ul>"
        "<p class='muted'>首幀需 9:16、短邊至少 512px，必須是可開啟的圖片。</p>"
    )
    return _step(1, "準備參考素材", "blocked", detail)


def _target_step(state: dict) -> str:
    registry = state["targets"]
    provisional = registry.provisional_targets
    rows = "".join(
        f"<tr><td class='mono'>{escape(item.target_id)}</td>"
        f"<td>{escape(item.provider)}</td>"
        f"<td class='mono'>{escape(item.model_id)}</td>"
        f"<td class='mono'>{escape(item.model_version or '—')}</td>"
        f"<td>{escape(item.ui_label or '—')}</td>"
        f"<td>{'待確認' if item.provisional else '已確認'}</td></tr>"
        for item in registry.targets
    )
    table = (
        "<table class='data-table'><thead><tr>"
        "<th>Target</th><th>Provider</th><th>Model</th><th>Version</th>"
        "<th>UI Label</th><th>狀態</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    if provisional:
        detail = (
            f"{len(provisional)}/{len(registry.targets)} 個比較對象的實際版本尚未確認。"
            "<p class='muted'>登入各平台確認版本與選項名稱後，更新 "
            "<code>pipeline/benchmark/catalog/v1_targets.yaml</code> 並將 "
            "<code>provisional</code> 改為 false，再重新建立派工。</p>"
            + table
        )
        return _step(2, "確認平台版本", "active", detail)
    return _step(2, "確認平台版本", "done", "全部已確認" + table)


def _dispatch_step(state: dict) -> str:
    artifact = state["artifact"]
    registry = state["targets"]
    if artifact is None:
        return _step(
            3, "建立派工", "todo",
            "尚未建立 benchmark 專案。",
            '<form method="post" action="/benchmark/build">'
            '<button class="primary" type="submit">建立專案並產生 Job Packages</button>'
            "</form>",
        )

    not_ready = [
        shot_id
        for shot_id, item in state["readiness"].items()
        if item.state is not ShotReadinessState.READY
    ]
    jobs = state["jobs"]
    expected = len(artifact.shot_plans)
    rows = "".join(
        f"<tr><td class='mono'>{escape(item.target_id)}</td>"
        f"<td>{jobs.get(item.target_id, 0)}/{expected}</td></tr>"
        for item in registry.targets
    )
    table = (
        "<table class='data-table'><thead><tr><th>Target</th>"
        "<th>Job Packages</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

    cta = (
        '<form method="post" action="/benchmark/build" class="inline-form">'
        '<button class="ghost-button" type="submit">重新產生</button></form>'
        f'<a class="ghost-button" href="/projects/{PROJECT_ID}/job-packages.zip">'
        "下載 Job Packages</a>"
    )
    if not_ready:
        detail = (
            f"{len(not_ready)} 顆鏡頭未就緒：{escape(', '.join(not_ready))}"
            + table
        )
        return _step(3, "建立派工", "blocked", detail, cta)

    total_jobs = sum(jobs.get(item.target_id, 0) for item in registry.targets)
    if total_jobs == 0:
        return _step(3, "建立派工", "active", "尚未產生 job packages" + table, cta)
    detail = f"{total_jobs} 份 job package 已產生" + table
    return _step(3, "建立派工", "done", detail, cta)


def _generation_step(state: dict) -> str:
    ledger = state["ledger"]
    index = state["index"]
    recorded = len(ledger.attempts) if ledger else 0
    failed = (
        sum(1 for item in ledger.attempts if not item.succeeded) if ledger else 0
    )
    imported = len(index.items) if index else 0
    unattributed = len(index.unattributed) if index else 0

    detail = (
        f"已記錄 {recorded} 次嘗試（其中未成功 {failed} 次）、"
        f"已匯入 {imported} 支候選"
    )
    if unattributed:
        detail += (
            f"<p class='bm-warn'>{unattributed} 支候選沒有派工來源，"
            "不會計入 benchmark。匯入時請選擇對應的 Job。</p>"
        )
    detail += (
        "<p class='muted'>每按一次平台的 Generate 就記一列，"
        "失敗與取消也要記，重試次數本身就是評估指標。</p>"
    )

    cta = (
        f'<a class="ghost-button" href="/projects/{PROJECT_ID}/view#variants">'
        "匯入候選影片</a>"
        '<a class="ghost-button" href="/benchmark/attempts">記錄生成嘗試</a>'
    )
    state_name = "done" if imported else ("active" if recorded else "todo")
    return _step(4, "生成與匯入", state_name, detail, cta)


def _scoring_step(state: dict) -> str:
    index = state["index"]
    imported = len(index.items) if index else 0
    scored = state["qc_count"]
    selected = (
        sum(1 for item in index.items if item.benchmark_selected) if index else 0
    )
    continuity = state["continuity_count"]

    detail = (
        f"已評分 {scored}/{imported} 支候選、"
        f"已選定 {selected} 組代表作、"
        f"已完成 {continuity} 組連戲評分"
    )
    cta = (
        f'<a class="ghost-button" href="/projects/{PROJECT_ID}/view#variants">'
        "評分與選片</a>"
        '<form method="post" action="/benchmark/sync" class="inline-form">'
        '<button class="ghost-button" type="submit">同步評分表</button></form>'
    )
    if not imported:
        return _step(5, "評分與選定代表作", "todo", "尚無候選可評分")
    state_name = "done" if scored >= imported and selected else "active"
    return _step(5, "評分與選定代表作", state_name, detail, cta)


def _result_step(state: dict) -> str:
    scored = state["qc_count"]
    if not scored:
        return _step(6, "查看情境結果", "todo", "尚無評分資料")
    detail = (
        "依情境分別評選，不產生跨情境總冠軍。"
        "<p class='muted'>低重試獎為明確的跨情境指標，其餘只讀該情境資料。</p>"
    )
    cta = '<a class="ghost-button" href="/benchmark/results">查看結果</a>'
    return _step(6, "查看情境結果", "active", detail, cta)


@router.get("/benchmark", response_class=HTMLResponse)
def benchmark_console(settings: Settings = Depends(settings_dep)) -> str:
    from app.routes.web import page

    state = _collect_state(settings)
    error_banner = (
        f'<div class="bm-warn">載入部分資料時發生問題：{escape(state["error"])}</div>'
        if state["error"]
        else ""
    )

    steps = "".join(
        [
            _asset_step(state),
            _target_step(state),
            _dispatch_step(state),
            _generation_step(state),
            _scoring_step(state),
            _result_step(state),
        ]
    )

    return page(
        title="V1 Benchmark",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell">
          <header class="project-hero">
            <div>
              <h1>V1 Benchmark</h1>
              <p class="muted">
                比較 {len(state["targets"].targets)} 個對象在
                {len(v1_pack.shots())} 顆固定鏡頭上的表現。
                真實生成需在各平台手動完成。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/projects/{PROJECT_ID}/view">專案詳情</a>
            </div>
          </header>
          {error_banner}
          <div class="bm-steps">{steps}</div>
        </section>
        <style>
          .bm-steps {{ display: grid; gap: 12px; margin-top: 16px; }}
          .bm-step {{ border: 1px solid #2a3344; border-radius: 8px;
                      padding: 14px 16px; background: #131a26; }}
          .bm-step-head {{ display: flex; align-items: center; gap: 8px;
                           margin-bottom: 6px; }}
          .bm-icon {{ display: inline-flex; width: 22px; height: 22px;
                      align-items: center; justify-content: center;
                      border-radius: 50%; font-size: 13px; }}
          .bm-done .bm-icon {{ background: #1f4d33; color: #7ee2a8; }}
          .bm-active .bm-icon {{ background: #1f3a4d; color: #7ec8e2; }}
          .bm-todo .bm-icon {{ background: #2a3344; color: #8a94a6; }}
          .bm-blocked .bm-icon {{ background: #4d2020; color: #e88; }}
          .bm-detail {{ font-size: 14px; line-height: 1.6; }}
          .bm-cta {{ margin-top: 10px; display: flex; gap: 8px;
                     flex-wrap: wrap; align-items: center; }}
          .bm-cta form {{ display: inline; }}
          .bm-list {{ margin: 8px 0; padding-left: 18px; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .bm-steps table {{ margin-top: 10px; }}
          @media (max-width: 720px) {{
            .bm-cta {{ flex-direction: column; align-items: stretch; }}
          }}
        </style>
        """,
    )


@router.post("/benchmark/build")
async def benchmark_build(settings: Settings = Depends(settings_dep)):
    from app.routes.web import WebException

    report = builder.check_assets(settings)
    if not report.ready:
        raise WebException(
            detail="素材尚未就緒：" + "; ".join(report.instructions()[:5]),
            back_link="/benchmark",
        )

    await run_in_threadpool(builder.register_assets, settings)
    artifact = await run_in_threadpool(builder.build_artifact, settings)
    await builder.dispatch_all(artifact)

    from pipeline.project_store import save_project

    await run_in_threadpool(save_project, settings, artifact)
    directory = sheets_dir(settings)
    await run_in_threadpool(score_sheet.write_variant_sheet, directory, None, None)
    await run_in_threadpool(score_sheet.write_continuity_sheet, directory, None)
    await run_in_threadpool(attempts.write_blank_ledger, directory, None, None)

    return RedirectResponse(url="/benchmark", status_code=303)


@router.post("/benchmark/sync")
async def benchmark_sync(settings: Settings = Depends(settings_dep)):
    directory = sheets_dir(settings)
    await run_in_threadpool(
        attribution.auto_select_benchmark_candidates, PROJECT_ID
    )
    await run_in_threadpool(score_sheet.sync_variant_sheet, directory, PROJECT_ID)
    await run_in_threadpool(score_sheet.sync_continuity_sheet, directory, PROJECT_ID)
    await run_in_threadpool(
        attempts.sync_variant_ids,
        directory / attempts.ATTEMPTS_SHEET,
        PROJECT_ID,
    )
    return RedirectResponse(url="/benchmark", status_code=303)


@router.post("/benchmark/variants/{variant_id}/benchmark-select")
async def benchmark_select(
    variant_id: str, settings: Settings = Depends(settings_dep)
):
    from app.routes.web import WebException

    try:
        await run_in_threadpool(
            attribution.select_benchmark_candidate, PROJECT_ID, variant_id
        )
    except ValueError as error:
        raise WebException(
            detail=str(error),
            back_link=f"/projects/{PROJECT_ID}/view#variants",
        ) from error
    return RedirectResponse(
        url=f"/projects/{PROJECT_ID}/view#variants", status_code=303
    )


@router.get("/benchmark/attempts", response_class=HTMLResponse)
def attempts_form(settings: Settings = Depends(settings_dep)) -> str:
    """記錄生成嘗試。每按一次平台的 Generate 就記一列。"""
    from app.routes.web import page

    registry = target.targets()
    ledger = attempts.read_ledger(sheets_dir(settings) / attempts.ATTEMPTS_SHEET)

    shot_options = "".join(
        f'<option value="{escape(shot.shot_id)}">{escape(shot.shot_id)}</option>'
        for shot in v1_pack.shots()
    )
    target_options = "".join(
        f'<option value="{escape(item.target_id)}">'
        f"{escape(item.target_id)} ({escape(item.provider)})</option>"
        for item in registry.targets
    )

    recent = "".join(
        f"<tr><td class='mono'>{escape(item.shot_id)}</td>"
        f"<td class='mono'>{escape(item.target_id)}</td>"
        f"<td>{item.attempt_no}</td><td>{escape(item.status)}</td>"
        f"<td class='mono'>{escape(item.output_file or '—')}</td>"
        f"<td>{escape(item.failure_reason or '—')}</td></tr>"
        for item in reversed(ledger.attempts[-15:])
    ) or '<tr><td colspan="6" class="muted empty">尚無紀錄</td></tr>'

    return page(
        title="記錄生成嘗試",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell">
          <header class="project-hero">
            <div>
              <h1>記錄生成嘗試</h1>
              <p class="muted">
                每按一次平台的 Generate 就記一列，失敗與取消也要記。
                只統計成功的候選會嚴重高估平台表現。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>

          <section class="panel">
            <form method="post" action="/benchmark/attempts">
              <div class="split">
                <div class="field">
                  <label for="shot">鏡頭</label>
                  <select id="shot" name="shot_id">{shot_options}</select>
                </div>
                <div class="field">
                  <label for="target">比較對象</label>
                  <select id="target" name="target_id">{target_options}</select>
                </div>
              </div>
              <div class="split">
                <div class="field">
                  <label for="status">結果</label>
                  <select id="status" name="status">
                    <option value="success">success（有產出影片）</option>
                    <option value="failed">failed（平台報錯或崩壞）</option>
                    <option value="cancelled">cancelled（自行中斷）</option>
                  </select>
                </div>
                <div class="field">
                  <label for="output">產出檔名</label>
                  <input id="output" name="output_file"
                         placeholder="成功時必填，需與匯入的檔名一致" />
                </div>
              </div>
              <div class="split">
                <div class="field">
                  <label for="secs">生成耗時（秒）</label>
                  <input id="secs" name="generation_seconds" />
                </div>
                <div class="field">
                  <label for="credits">消耗點數</label>
                  <input id="credits" name="credits_used" />
                </div>
              </div>
              <div class="field">
                <label for="reason">失敗原因</label>
                <input id="reason" name="failure_reason"
                       placeholder="未成功時填寫" />
              </div>
              <button class="primary" type="submit">記錄這次嘗試</button>
            </form>
          </section>

          <section class="panel">
            <div class="section-head">
              <h2>最近紀錄</h2>
              <span class="muted">共 {len(ledger.attempts)} 次</span>
            </div>
            <table class="data-table">
              <thead><tr><th>Shot</th><th>Target</th><th>#</th>
              <th>結果</th><th>檔名</th><th>原因</th></tr></thead>
              <tbody>{recent}</tbody>
            </table>
          </section>
        </section>
        """,
    )


@router.post("/benchmark/attempts")
async def record_attempt(
    shot_id: str = Form(...),
    target_id: str = Form(...),
    status: str = Form(...),
    output_file: str = Form(""),
    generation_seconds: str = Form(""),
    credits_used: str = Form(""),
    failure_reason: str = Form(""),
    settings: Settings = Depends(settings_dep),
):
    from app.routes.web import WebException

    try:
        await run_in_threadpool(
            attempts.append_attempt,
            sheets_dir(settings) / attempts.ATTEMPTS_SHEET,
            shot_id,
            target_id,
            status,
            output_file.strip() or None,
            generation_seconds.strip() or None,
            credits_used.strip() or None,
            failure_reason.strip() or None,
        )
    except ValueError as error:
        raise WebException(
            detail=str(error), back_link="/benchmark/attempts"
        ) from error
    return RedirectResponse(url="/benchmark/attempts", status_code=303)


@router.get("/benchmark/results", response_class=HTMLResponse)
def benchmark_results(settings: Settings = Depends(settings_dep)) -> str:
    """依情境呈現結果。刻意不產生跨情境總冠軍。"""
    from app.routes.web import page
    from pipeline.benchmark import aggregation

    ledger = attempts.read_ledger(sheets_dir(settings) / attempts.ATTEMPTS_SHEET)
    index = attribution.build_index(PROJECT_ID)

    variant_records = []
    continuity_records = []
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

    for item in index.items:
        qc = qc_rows.get(item.variant_id)
        if qc is None:
            continue
        variant_records.append(
            {
                "target_id": item.target_id,
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

    for row in continuity_rows:
        entry = index.by_variant_id(row.variant_id) if row.variant_id else None
        if entry is None:
            continue
        continuity_records.append(
            {
                "target_id": entry.target_id,
                "shot_id": row.shot_id,
                "weighted_score": row.weighted_score(aggregation.BENCHMARK_WEIGHTS),
                "cross_shot_identity": row.cross_shot_identity,
            }
        )

    report = aggregation.build_report(
        ledger, variant_records, continuity_records, index.unattributed
    )

    def fmt(value, suffix: str = "") -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:g}{suffix}"
        return f"{value}{suffix}"

    award_cards = "".join(
        f"""
        <div class="panel">
          <div class="section-head">
            <h2>{escape(item.award.replace('best_for_', '').replace('_', ' ').title())}</h2>
            <span class="muted">{escape(item.scope)}</span>
          </div>
          <p><strong>{escape(item.winner or '資料不足')}</strong></p>
          <p class="muted">排序依據：{escape(item.reason)}</p>
          <p class="muted">{escape(item.note)}</p>
          <p class="mono muted">{escape(' > '.join(item.ranking)) or '—'}</p>
        </div>
        """
        for item in report.awards
    )

    rows = "".join(
        f"<tr><td class='mono'>{escape(item.target_id)}</td>"
        f"<td class='mono'>{escape(item.model_id)}</td>"
        f"<td>{escape(item.model_version or '待確認')}</td>"
        f"<td>{fmt(item.usable_shot_rate)}</td>"
        f"<td>{fmt(item.retries_per_usable)}</td>"
        f"<td>{fmt(item.human_minutes_per_usable)}</td>"
        f"<td>{item.total_attempts}</td>"
        f"<td>{item.failed_attempts}</td>"
        f"<td>{fmt(item.total_credits)}</td>"
        f"<td>{fmt(item.median_generation_seconds, 's')}</td></tr>"
        for item in report.aggregates
    ) or '<tr><td colspan="10" class="muted empty">尚無資料</td></tr>'

    warnings = ""
    if report.provisional_targets:
        warnings += (
            f'<p class="bm-warn">以下比較對象的版本尚未確認，'
            f"跨輪次比較時視為版本不明："
            f'{escape(", ".join(report.provisional_targets))}</p>'
        )
    if report.unattributed_variants:
        warnings += (
            f'<p class="bm-warn">{len(report.unattributed_variants)} 支候選沒有'
            "派工來源，未計入統計。</p>"
        )

    return page(
        title="Benchmark 結果",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell">
          <header class="project-hero">
            <div>
              <h1>Benchmark 結果</h1>
              <p class="muted">
                統計公式版本 {escape(report.aggregation_version)}，
                於生成開始前即已固定。依情境分別評選，不產生總冠軍。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>
          {warnings}
          <section class="panel">
            <div class="section-head"><h2>整體指標</h2></div>
            <table class="data-table">
              <thead><tr><th>Target</th><th>Model</th><th>Version</th>
              <th>可用率</th><th>重試/可用</th><th>人工分鐘/可用</th>
              <th>嘗試</th><th>失敗</th><th>點數</th><th>耗時中位</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </section>
          {award_cards}
        </section>
        <style>.bm-warn {{ color: #e8b; margin: 8px 0; }}</style>
        """,
    )
