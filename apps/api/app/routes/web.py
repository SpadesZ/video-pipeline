# 檔案路徑: video-pipeline/apps/api/app/routes/web.py
# 產生時間: 2026-06-25 17:00 +08:00
# 版本: v1.1
# 模組定位:
#   FastAPI Web 控制台前端 HTML 路由頁面渲染模組。
# 主要責任:
#   1. 提供視覺化專案列表、專案詳情頁面渲染（配合 HTML 元件）。
#   2. 管理人工作業審核閘門、ASR 字幕匯入及素材標記審查。
#   3. 新增 YouTube Analytics API 指標同步與本地 CSV 匯入，閉環選題優化接入。
# --------------------------------------------------------------------------

from html import escape
from pathlib import Path
import io
import re
import logging
import zipfile

logger = logging.getLogger(__name__)


from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from app.deps import settings_dep, get_session
from pipeline.adapters.llm.lava_settings import get_llm_brain_status, update_task_binding
from pipeline.adapters.llm.lava_verifier import verify_connection
from pipeline.capability.provider_spec import provider_specs
from pipeline.db import engine
from pipeline.models.asset_manifest import RightsStatus
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.variant import CapabilityJob, TransportKind
from pipeline.models.review import DecisionLogEntry, ReviewStatus
from pipeline.models.transcript import TranscriptFormat
from pipeline.project_store import (
    list_projects as store_list_projects,
    load_project as store_load_project,
    project_file_path as store_project_file_path,
    save_project as store_save_project,
)
from pipeline.settings import Settings
from pipeline.stages.compliance_checker import check_compliance
from pipeline.stages.run_mvp import run_mvp_pipeline
from pipeline.stages.shot_dispatcher import (
    ShotReadinessState,
    assess_project_readiness,
    dispatch_project_shots,
    job_package_root,
)
from pipeline.stages.shot_qc import (
    QCValidationError,
    record_variant_qc,
    summarize_project_qc,
)
from pipeline.stages.transcript_importer import import_transcript
from pipeline.stages.variant_importer import (
    JobLinkError,
    import_variants,
    list_variants,
    select_variant,
)
from pipeline.utils.files import ensure_project_dir
from pipeline.stages.llm_executors import run_topic_research, run_script_outline, run_packaging
from pipeline.stages.script_splitter import split_script_into_shorts
from pipeline.models.metrics import MetricsDecision, ScaleDecision

router = APIRouter(include_in_schema=False)


class WebException(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int = 400,
        back_link: str = "/",
        error_code: str = "",
    ):
        self.detail = detail
        self.status_code = status_code
        self.back_link = back_link
        # 對應 pipeline/assistant/knowledge/errors.yaml 的 code。
        # 有 code 時助手能給出確定的成因與修法，而不是重述訊息。
        self.error_code = error_code


def error_page(
    title: str,
    message: str,
    back_link: str = "/",
    status_code: int = 400,
    error_code: str = "",
) -> HTMLResponse:
    """錯誤頁。附上 Ask AI，讓使用者不必自己判斷這串訊息是什麼意思。

    data-error-code 供助手讀取，助手因此能直接說明成因與修法，
    而不是把錯誤訊息換句話說一遍。
    """
    return HTMLResponse(
        content=page(
            title=title,
            body=f"""
            <section class="app-shell" data-route="{escape(back_link)}">
              <header class="topbar">
                <div>
                  <p class="eyebrow" style="color: var(--danger);">System Error</p>
                  <h1>{escape(title)}</h1>
                </div>
              </header>
              <section class="panel" style="border-color: var(--danger); padding: 24px;"
                       data-error-code="{escape(error_code)}">
                <p style="font-size: 16px; margin-bottom: 20px; line-height: 1.6;">{escape(message)}</p>
                <a class="button-link primary" href="{escape(back_link)}">Go Back</a>
                <a class="button-link" href="#"
                   data-ask-ai="這個錯誤是什麼意思？我該怎麼修？">Ask AI</a>
              </section>
            </section>
            """
        ),
        status_code=status_code
    )


PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
ALLOWED_FILES = {
    "production_artifact.json",
    "cue_ledger.json",
    "asset_manifest.json",
    "compliance_report.json",
    "subtitles.srt",
    "upload_package.md",
    "preview.mp4",
    "transcript_import.json",
    "visual_contract.json",
    "visual_qc_report.json",
}

STEPS = [
    ("cues", "Cues"),
    ("assets", "Assets"),
    ("preview", "Preview"),
    ("approved", "Approved"),
]

SAMPLE_SCRIPT = """<VISUAL_BREAK: clean workflow diagram>
Start with a reviewed script and turn it into a cue ledger.
<BROLL: editing timeline and analytics dashboard>
Then create an asset manifest, subtitles, a preview, and an upload package.
<RISK_DISCLOSURE: no guaranteed revenue>
Human review remains the final gate before publishing."""


@router.get("/", response_class=HTMLResponse)
def home(settings: Settings = Depends(settings_dep)) -> str:
    projects = list_projects(settings)
    rows = "\n".join(project_row(item) for item in projects)
    if not rows:
        rows = '<tr><td colspan="5" class="muted empty">No projects yet</td></tr>'

    total_assets = sum(len(p.asset_manifest.assets) for p in projects if p.asset_manifest)
    approved_assets = sum(
        1
        for p in projects
        if p.asset_manifest
        for asset in p.asset_manifest.assets
        if asset.rights_status == RightsStatus.APPROVED
    )
    ready_count = sum(1 for p in projects if p.compliance_report and p.compliance_report.upload_ready)

    # Load Topic Performance Index
    try:
        from pipeline.stages.topic_optimizer import get_topic_performance_summary
        perf_summary = get_topic_performance_summary(settings)
    except Exception as e:
        logger.error(f"Failed to load topic performance summary: {e}")
        perf_summary = []

    perf_rows = ""
    for item in perf_summary:
        trend_arrow = "→"
        if item["trend"] == "up":
            trend_arrow = "▲"
        elif item["trend"] == "down":
            trend_arrow = "▼"
        
        avg_ctr_str = f"{item['avg_ctr']:.1f}%" if item['avg_ctr'] is not None else "0.0%"
        avg_avd_str = f"{int(item['avg_avd_seconds'])}s" if item['avg_avd_seconds'] is not None else "0s"
        avg_rpm_str = f"${item['avg_rpm']:.2f}" if item['avg_rpm'] is not None else "$0.00"
        
        perf_rows += f"""
        <tr>
          <td><span class="status ok" style="background:#2a3b4c; color:#a5d6ff; border:none; padding:4px 8px; border-radius:4px; font-weight:bold;">{escape(item["genre"])}</span></td>
          <td class="mono">{item["project_count"]}</td>
          <td class="mono">{avg_ctr_str}</td>
          <td class="mono">{avg_avd_str}</td>
          <td class="mono">{avg_rpm_str}</td>
          <td><span class="pill" style="font-weight:bold; text-transform:uppercase;">{escape(item["dominant_decision"])}</span></td>
          <td class="mono" style="font-weight:bold; color:{'#3fb950' if item['trend']=='up' else '#f85149' if item['trend']=='down' else '#8b949e'}">{trend_arrow}</td>
        </tr>
        """
    if not perf_rows:
        perf_rows = '<tr><td colspan="7" class="muted empty" style="text-align:center; padding:15px;">No performance index data yet</td></tr>'
        
    perf_dashboard_html = f"""
    <div class="workspace-card" style="margin-top: 30px; border: 1px solid var(--line); border-radius: 8px; padding: 20px; background: var(--panel);">
      <div class="section-head" style="margin-bottom: 15px; display:flex; justify-content:space-between; align-items:center;">
        <h2 style="margin:0; font-size:18px;">Topic Performance Index</h2>
        <span class="pill" style="background:#1f6feb; color:white; border:none;">Closed Loop Feedback</span>
      </div>
      <table style="width: 100%; border-collapse: collapse; font-size:14px;">
        <thead>
          <tr style="text-align: left; border-bottom: 1px solid var(--line); font-size: 13px; color: var(--muted); height:35px;">
            <th style="padding: 8px;">Genre</th>
            <th style="padding: 8px;">Count</th>
            <th style="padding: 8px;">Avg CTR</th>
            <th style="padding: 8px;">Avg AVD</th>
            <th style="padding: 8px;">Avg RPM</th>
            <th style="padding: 8px;">Dominant Action</th>
            <th style="padding: 8px;">Trend</th>
          </tr>
        </thead>
        <tbody>
          {perf_rows}
        </tbody>
      </table>
    </div>
    """

    return page(
        title="Video Pipeline",
        active_nav="projects",
        body=f"""
        <section class="app-shell" data-route="/" data-entity-type="home">
          <header class="topbar">
            <div>
              <p class="eyebrow">Local production desk</p>
              <h1>Video Pipeline Dashboard</h1>
            </div>
          </header>

          <section class="panel benchmark-entry">
            <div>
              <strong>要比較各平台的生成品質？</strong>
              <p class="muted">
                V1 Benchmark 把四個平台在六顆固定鏡頭上的表現走成六個步驟，
                從上傳參考素材開始。真實生成在各平台手動完成。
              </p>
            </div>
            <a class="button-link primary" href="/benchmark">開始 V1 Benchmark</a>
          </section>

          <section class="overview-grid">
            {stat_card("Projects", str(len(projects)), "built locally")}
            {stat_card("Upload Ready", str(ready_count), "final approved")}
            {stat_card("Asset Rights", f"{approved_assets}/{total_assets}", "approved")}
          </section>
 
          <section class="workspace">
            <div class="create-pane">
              <div class="section-head">
                <h2>New Project</h2>
                <span class="pill">CPU-first</span>
              </div>
              <form action="/" method="post" class="project-form">
                <div class="field">
                  <label for="project-title">Title</label>
                  <input id="project-title" name="title" required value="CPU First Video Pipeline MVP" />
                </div>
                <div class="split">
                  <div class="field">
                    <label for="project-language">Language</label>
                    <input id="project-language" name="language" value="en" />
                  </div>
                  <div class="field">
                    <label for="project-persona">Persona</label>
                    <input id="project-persona" name="persona" value="operator" />
                  </div>
                </div>
                <div class="field">
                  <label for="project-script">Script</label>
                  <textarea id="project-script" name="script_markdown" required>{escape(SAMPLE_SCRIPT)}</textarea>
                </div>
                <button type="submit" class="primary">Build Project</button>
              </form>
              <div class="section-head" style="margin-top: 25px; border-top: 1px solid var(--line); padding-top: 20px;">
                <h2>AI Niche Research</h2>
                <span class="pill">LAVA Brain</span>
              </div>
              <form action="/projects/trend-research" method="post" class="project-form">
                <div class="field">
                  <label for="research-topic">Keyword / Topic</label>
                  <input id="research-topic" name="topic_prompt" required value="AI SaaS變現" />
                </div>
                <button type="submit" class="primary">Research Trends</button>
              </form>
            </div>
            <div class="list-pane">
              <div class="section-head">
                <h2>Recent Projects</h2>
                <span class="muted">{len(projects)} total</span>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Title</th>
                    <th>Cues</th>
                    <th>Status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>{rows}</tbody>
              </table>
              {perf_dashboard_html}
            </div>
          </section>
        </section>
        """,
    )


@router.get("/settings/lava/status")
def lava_status_json(settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return JSONResponse(get_llm_brain_status(settings).model_dump(mode="json"))


@router.get("/settings/lava", response_class=HTMLResponse)
def lava_settings_page(settings: Settings = Depends(settings_dep)) -> str:
    return lava_settings_view(settings)


@router.post("/settings/lava/binding")
def update_lava_task_binding(
    task_id: str = Form(...),
    connection_id: str = Form(...),
    model_id: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    try:
        update_task_binding(task_id, connection_id, settings=settings, model_id=model_id)
    except ValueError as exc:
        raise WebException(detail=str(exc), status_code=400, back_link="/settings/lava")
    return RedirectResponse(url="/settings/lava", status_code=303)


@router.post("/settings/lava/verify", response_class=HTMLResponse)
async def verify_lava_task_connection(
    connection_id: str = Form(...),
    capability: str = Form("chat"),
    settings: Settings = Depends(settings_dep),
) -> str:
    result = await verify_connection(connection_id, capability=capability)
    return lava_settings_view(settings, verification_result=result)


@router.post("/")
async def create_project_from_form(
    title: str = Form(...),
    script_markdown: str = Form(...),
    language: str = Form("en"),
    persona: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    title_stripped = title.strip() if title else ""
    script_stripped = script_markdown.strip() if script_markdown else ""
    if not title_stripped:
        raise WebException(detail="Project title cannot be empty", status_code=400)
    if not script_stripped:
        raise WebException(detail="Script content cannot be empty", status_code=400)

    try:
        artifact = await run_mvp_pipeline(
            settings=settings,
            title=title_stripped,
            script_markdown=script_stripped,
            language=language,
            persona=persona or None,
        )
    except Exception as e:
        logger.error(f"Failed to create project: {e}")
        raise WebException(detail=f"Failed to run pipeline: {str(e)}", status_code=500)

    artifact.decision_log.append(
        DecisionLogEntry(
            action="project_created",
            note="Pipeline artifacts generated from the web console.",
            to_status=artifact.review_status,
        )
    )
    artifact.compliance_report = check_compliance(artifact)
    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{artifact.project_id}/view", status_code=303)


@router.get("/projects/{project_id}/view", response_class=HTMLResponse)
def project_detail(project_id: str, settings: Settings = Depends(settings_dep)) -> str:
    artifact = load_project(settings, project_id)
    asset_count = len(artifact.asset_manifest.assets) if artifact.asset_manifest else 0
    approved_asset_count = approved_assets_count(artifact)
    cue_count = len(artifact.cue_ledger.cues) if artifact.cue_ledger else 0
    upload_ready = bool(artifact.compliance_report and artifact.compliance_report.upload_ready)

    preview = preview_panel(settings, artifact)
    cue_strip = cue_strip_panel(artifact)
    cue_rows = "\n".join(cue_row(cue) for cue in (artifact.cue_ledger.cues if artifact.cue_ledger else []))
    if not cue_rows:
        cue_rows = '<tr><td colspan="5" class="muted empty">No cues</td></tr>'

    return page(
        title=artifact.title,
        active_nav="projects",
        body=f"""
        <section class="app-shell detail-page"
                 data-route="/projects/{escape(artifact.project_id)}/view"
                 data-entity-type="project"
                 data-entity-id="{escape(artifact.project_id)}">
          <header class="project-hero">
            <div>
              <h1>{escape(artifact.title)}</h1>
              <p class="muted mono">{escape(project_id)} | {escape(artifact.language)} | {escape(artifact.persona or "no persona")}</p>
            </div>
            <div class="actions">
              {file_link(project_id, "upload_package.md", "Upload Package")}
              {file_link(project_id, "subtitles.srt", "Subtitles")}
              {file_link(project_id, "production_artifact.json", "Artifact JSON")}
            </div>
          </header>

          {stepper(artifact.review_status)}

          <section class="overview-grid">
            {stat_card("Cues", str(cue_count), "timeline units")}
            {stat_card("Asset Rights", f"{approved_asset_count}/{asset_count}", "approved")}
            {stat_card("Review State", status_label(artifact.review_status), "current gate")}
            {stat_card("Upload Ready", "Yes" if upload_ready else "No", "hard stop/go")}
          </section>

          {status_flow_panel(artifact)}

          <section class="review-layout">
            <main class="main-col">
              {preview}
              {transcript_panel(artifact)}
              {shot_dispatch_panel(artifact)}
              {variant_panel(artifact)}
              {cue_strip}
              {visual_quality_panel(artifact)}
              <section class="panel">
                <div class="section-head">
                  <h2>Cue Ledger</h2>
                  {file_link(project_id, "cue_ledger.json", "Download JSON")}
                </div>
                <table>
                  <thead><tr><th>Cue</th><th>Time</th><th>Voice</th><th>Visual</th><th>Short</th></tr></thead>
                  <tbody>{cue_rows}</tbody>
                </table>
              </section>
            </main>
            <aside class="side-col">
              {llm_brain_panel(project_id, settings)}
              {review_panel(artifact)}
              {packaging_panel(artifact)}
              {shorts_panel(artifact)}
              {metrics_panel(artifact)}
              {asset_panel(artifact)}
              {compliance_panel(artifact)}
              {decision_log_panel(artifact)}
            </aside>
          </section>
        </section>
        """,
    )


@router.post("/projects/{project_id}/transcript/import")
async def import_project_transcript(
    project_id: str,
    transcript_format: TranscriptFormat = Form(TranscriptFormat.AUTO),
    transcript_content: str = Form(...),
    source_name: str = Form("paste"),
    actor: str = Form("local"),
    note: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = await run_in_threadpool(load_project, settings, project_id)
    try:
        artifact = await import_transcript(
            settings=settings,
            artifact=artifact,
            transcript_format=transcript_format,
            content=transcript_content,
            source_name=source_name or "paste",
            actor=actor or "local",
            note=note or None,
        )
    except Exception as error:
        artifact.decision_log.append(
            DecisionLogEntry(
                action="transcript_import_failed",
                actor=actor or "local",
                note=str(error)[:300],
                from_status=artifact.review_status,
                to_status=artifact.review_status,
            )
        )
        artifact.touch()
    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#transcript", status_code=303)


@router.post("/projects/{project_id}/asr/run")
def trigger_local_asr(
    project_id: str,
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)

    from app.services.task_client import enqueue_asr_job
    from pipeline.stages.asr_transcriber import find_asr_audio_path

    if not find_asr_audio_path(settings, artifact):
        artifact.decision_log.append(
            DecisionLogEntry(
                action="local_asr_missing_audio",
                actor="local",
                note="No project audio file was found. Add voiceover.wav, voiceover.mp3, audio.wav, or audio.mp3 before running local ASR.",
            )
        )
        artifact.touch()
        save_project(settings, artifact)
        return RedirectResponse(url=f"/projects/{project_id}/view#transcript", status_code=303)

    async_result = enqueue_asr_job(project_id)
    task_id = getattr(async_result, "id", None)
    task_note = "Queued local CPU ASR. Start the Docker ASR profile if it is not already running."
    if task_id:
        task_note = f"{task_note} Celery task: {task_id}"
    artifact.decision_log.append(
        DecisionLogEntry(
            action="local_asr_enqueued",
            actor="local",
            note=task_note,
        )
    )
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#transcript", status_code=303)


@router.post("/projects/{project_id}/shots/dispatch")
async def dispatch_shots(
    project_id: str,
    preferred_provider: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    """為專案的 ShotPlan 產生 job package。

    第一階段走人工 transport，結果為 pending_manual，代表已交付人工生成。
    """
    artifact = await run_in_threadpool(load_project, settings, project_id)
    if not artifact.shot_plans:
        raise WebException(
            detail="This project has no shot plans to dispatch.",
            back_link=f"/projects/{project_id}/view",
        )

    try:
        report = await dispatch_project_shots(
            artifact, preferred_provider=preferred_provider or None
        )
        note = report.summary()
        failures = [item for item in report.results if not item.dispatched]
        if failures:
            note += f" | first failure: {failures[0].shot_id} {failures[0].message}"
            logger.warning("Shot dispatch had %d failures", len(failures))
    except Exception as error:  # noqa: BLE001 - 派工失敗需留下稽核軌跡
        artifact.decision_log.append(
            DecisionLogEntry(
                action="shots_dispatch_failed",
                actor="local",
                note=str(error)[:300],
            )
        )
        artifact.touch()
        await run_in_threadpool(save_project, settings, artifact)
        raise WebException(
            detail=f"Shot dispatch failed: {error}",
            back_link=f"/projects/{project_id}/view",
        ) from error

    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#shots", status_code=303)


@router.get("/projects/{project_id}/job-packages.zip")
def download_job_packages(
    project_id: str,
    settings: Settings = Depends(settings_dep),
) -> Response:
    """將專案的 job packages 打包下載，供人工於各平台操作。"""
    root = job_package_root(settings.data_dir, project_id)
    if not root.is_dir():
        raise WebException(
            detail="No job packages generated yet.",
            back_link=f"/projects/{project_id}/view#shots",
            status_code=404,
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root))
    buffer.seek(0)

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{project_id}_job_packages.zip"'
            )
        },
    )


def _optional_int(raw: str) -> int | None:
    """空字串代表 N/A，不可轉為 0，否則會與「極差」混淆。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise QCValidationError(f"評分必須為整數或留空: {raw}") from error


def _optional_float(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@router.post("/projects/{project_id}/variants/import")
async def import_project_variants(
    project_id: str,
    shot_id: str = Form(...),
    provider: str = Form(...),
    files: list[UploadFile] = File(...),
    request_hash: str = Form(""),
    actor: str = Form("local"),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    """匯入人工於平台生成的候選影片。"""
    artifact = await run_in_threadpool(load_project, settings, project_id)

    payloads: list[tuple[str, bytes]] = []
    for upload in files:
        payloads.append((upload.filename or "unnamed", await upload.read()))

    try:
        report = await run_in_threadpool(
            import_variants,
            settings,
            artifact,
            shot_id,
            provider,
            payloads,
            request_hash.strip() or None,
            None,
            None,
            actor or "local",
            None,
        )
        if report.skipped:
            logger.warning("Variant import skipped %d files", len(report.skipped))
    except JobLinkError as error:
        # request_hash 不符時直接拒絕，不猜測要接到哪一筆工作
        artifact.decision_log.append(
            DecisionLogEntry(
                action="variants_import_rejected",
                actor=actor or "local",
                note=str(error)[:300],
            )
        )
        artifact.touch()
        await run_in_threadpool(save_project, settings, artifact)
        raise WebException(
            detail=f"request_hash 不符，已拒絕匯入: {error}",
            back_link=f"/projects/{project_id}/view#variants",
        ) from error
    except Exception as error:  # noqa: BLE001 - 匯入失敗需留下稽核軌跡
        artifact.decision_log.append(
            DecisionLogEntry(
                action="variants_import_failed",
                actor=actor or "local",
                note=str(error)[:300],
            )
        )
        artifact.touch()
        await run_in_threadpool(save_project, settings, artifact)
        raise WebException(
            detail=f"Variant import failed: {error}",
            back_link=f"/projects/{project_id}/view#variants",
        ) from error

    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#variants", status_code=303)


@router.post("/projects/{project_id}/variants/{variant_id}/select")
async def select_project_variant(
    project_id: str,
    variant_id: str,
    reason: str = Form(""),
    actor: str = Form("local"),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = await run_in_threadpool(load_project, settings, project_id)
    try:
        await run_in_threadpool(
            select_variant, artifact, variant_id, reason, actor or "local"
        )
    except ValueError as error:
        raise WebException(
            detail=str(error),
            back_link=f"/projects/{project_id}/view#variants",
            status_code=404,
        ) from error

    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#variants", status_code=303)


@router.post("/projects/{project_id}/variants/{variant_id}/qc")
async def score_project_variant(
    project_id: str,
    variant_id: str,
    prompt_adherence: str = Form(""),
    temporal_stability: str = Form(""),
    motion_quality: str = Form(""),
    camera_control: str = Form(""),
    artifact_severity: str = Form(""),
    # 身份一致性是 benchmark 權重最高的維度（3.0），表情演技則決定
    # 對話情境的評選。兩者原本沒有輸入欄位，網頁上根本填不進去。
    identity_consistency: str = Form(""),
    facial_acting: str = Form(""),
    usable: str = Form(""),
    correction_minutes: str = Form(""),
    notes: str = Form(""),
    actor: str = Form("local"),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    """記錄單鏡頭品質評分。留空的欄位視為 N/A。"""
    artifact = await run_in_threadpool(load_project, settings, project_id)
    try:
        scores = {
            "prompt_adherence": _optional_int(prompt_adherence),
            "temporal_stability": _optional_int(temporal_stability),
            "motion_quality": _optional_int(motion_quality),
            "camera_control": _optional_int(camera_control),
            "artifact_severity": _optional_int(artifact_severity),
            "identity_consistency": _optional_int(identity_consistency),
            "facial_acting": _optional_int(facial_acting),
        }
        await run_in_threadpool(
            record_variant_qc,
            artifact,
            variant_id,
            scores,
            bool(usable),
            _optional_float(correction_minutes),
            None,
            actor or "local",
            notes or None,
        )
    except (QCValidationError, ValueError) as error:
        raise WebException(
            detail=f"QC scoring failed: {error}",
            back_link=f"/projects/{project_id}/view#variants",
        ) from error

    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#variants", status_code=303)


@router.post("/projects/{project_id}/lava/run")
def trigger_lava_workflow(
    project_id: str,
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    
    # Send non-blocking task to Celery worker
    from celery import Celery
    celery_app = Celery("video_pipeline", broker=settings.celery_broker_url)
    celery_app.send_task("video_pipeline.run_lava_workflow", args=[project_id])
    
    artifact.decision_log.append(
        DecisionLogEntry(
            action="lava_workflow_triggered",
            actor="local",
            note="Triggered LAVA workflow (Bible + Storyboard + TTS + ASR + Image Gen) via Web UI."
        )
    )
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/review/{action}")
def review_action(
    project_id: str,
    action: str,
    actor: str = Form("local"),
    note: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    from_status = artifact.review_status
    to_status = next_status_for_action(artifact, action)
    artifact.review_status = to_status
    artifact.decision_log.append(
        DecisionLogEntry(
            action=action,
            actor=actor or "local",
            note=note or None,
            from_status=from_status,
            to_status=to_status,
        )
    )
    artifact.compliance_report = check_compliance(artifact)
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/assets/{asset_id}/rights")
def update_asset_rights(
    project_id: str,
    asset_id: str,
    rights_status: RightsStatus = Form(...),
    actor: str = Form("local"),
    note: str = Form(""),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    if artifact.asset_manifest is None:
        raise WebException(detail="Asset manifest not found", status_code=404, back_link=f"/projects/{project_id}/view")

    asset = next((item for item in artifact.asset_manifest.assets if item.asset_id == asset_id), None)
    if asset is None:
        raise WebException(detail="Asset not found", status_code=404, back_link=f"/projects/{project_id}/view")

    previous = asset.rights_status
    asset.rights_status = rights_status
    if note:
        asset.notes = note
    artifact.decision_log.append(
        DecisionLogEntry(
            action="asset_rights_updated",
            actor=actor or "local",
            note=f"{asset_id}: {previous} -> {rights_status}" + (f"; {note}" if note else ""),
            from_status=artifact.review_status,
            to_status=artifact.review_status,
        )
    )
    artifact.compliance_report = check_compliance(artifact)
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view#assets", status_code=303)


@router.get("/projects/{project_id}/files/{filename}")
def project_file(project_id: str, filename: str, settings: Settings = Depends(settings_dep)) -> FileResponse:
    if filename not in ALLOWED_FILES:
        raise WebException(detail="File not found", status_code=404, back_link=f"/projects/{project_id}/view")
    path = project_file_path(settings, project_id, filename)
    if not path.exists() or not path.is_file():
        raise WebException(detail="File not found", status_code=404, back_link=f"/projects/{project_id}/view")
    return FileResponse(path)


@router.get("/projects/{project_id}/variants/{variant_id}/video")
def variant_video(
    project_id: str, variant_id: str, settings: Settings = Depends(settings_dep)
) -> FileResponse:
    """播放已匯入的候選。

    評分需要看得到影片。路徑由 variant_id 查表取得，並確認該候選確實
    屬於這個專案，不接受任意檔名。
    """
    from sqlmodel import Session

    from pipeline.db import engine
    from pipeline.models.variant import AssetVariant

    with Session(engine) as session:
        variant = session.get(AssetVariant, variant_id)
    back = f"/projects/{project_id}/view#variants"
    if variant is None or variant.project_id != project_id:
        raise WebException(
            detail=f"專案 {project_id} 中找不到候選 {variant_id}",
            status_code=404,
            back_link=back,
        )
    if not variant.local_path or not Path(variant.local_path).exists():
        raise WebException(
            detail=f"候選 {variant_id} 的影片檔不存在",
            status_code=404,
            back_link=back,
        )
    return FileResponse(Path(variant.local_path), media_type="video/mp4")


def next_status_for_action(artifact: ProductionArtifact, action: str) -> ReviewStatus:
    current = artifact.review_status
    if action == "approve_cues":
        if current not in {ReviewStatus.CUES_READY, ReviewStatus.CHANGES_REQUESTED}:
            raise WebException(detail="Cues are not ready for approval", status_code=400, back_link=f"/projects/{artifact.project_id}/view")
        return ReviewStatus.ASSETS_REVIEW

    if action == "approve_assets":
        if current != ReviewStatus.ASSETS_REVIEW:
            raise WebException(detail="Asset review is not the current stage", status_code=400, back_link=f"/projects/{artifact.project_id}/view")
        if not all_assets_approved(artifact):
            raise WebException(detail="All assets must be approved first", status_code=400, back_link=f"/projects/{artifact.project_id}/view")
        return ReviewStatus.PREVIEW_READY

    if action == "approve_final":
        if current != ReviewStatus.PREVIEW_READY:
            raise WebException(detail="Preview is not ready for final approval", status_code=400, back_link=f"/projects/{artifact.project_id}/view")
        if not all_assets_approved(artifact):
            raise WebException(detail="All assets must be approved first", status_code=400, back_link=f"/projects/{artifact.project_id}/view")
        return ReviewStatus.APPROVED

    if action == "request_changes":
        return ReviewStatus.CHANGES_REQUESTED

    raise WebException(detail="Unknown review action", status_code=404, back_link=f"/projects/{artifact.project_id}/view")


def list_projects(settings: Settings) -> list[ProductionArtifact]:
    return store_list_projects(settings)


def load_project(settings: Settings, project_id: str) -> ProductionArtifact:
    if not PROJECT_ID_RE.match(project_id):
        raise WebException(detail="Project not found", status_code=404)
    try:
        artifact = store_load_project(settings, project_id)
    except ValueError:
        raise WebException(detail="Project not found", status_code=404)
    if not artifact:
        raise WebException(detail="Project not found", status_code=404)
    artifact.compliance_report = check_compliance(artifact)
    return artifact


def save_project(settings: Settings, artifact: ProductionArtifact) -> None:
    store_save_project(settings, artifact)


def project_file_path(settings: Settings, project_id: str, filename: str) -> Path:
    if not PROJECT_ID_RE.match(project_id):
        raise WebException(detail="Project not found", status_code=404)
    try:
        return store_project_file_path(settings, project_id, filename)
    except ValueError:
        raise WebException(detail="Project not found", status_code=404)


def all_assets_approved(artifact: ProductionArtifact) -> bool:
    if artifact.asset_manifest is None or not artifact.asset_manifest.assets:
        return False
    return all(asset.rights_status == RightsStatus.APPROVED for asset in artifact.asset_manifest.assets)


def approved_assets_count(artifact: ProductionArtifact) -> int:
    if artifact.asset_manifest is None:
        return 0
    return sum(1 for asset in artifact.asset_manifest.assets if asset.rights_status == RightsStatus.APPROVED)


def page(title: str, body: str, active_nav: str = "") -> str:
    nav_html = f"""
    <nav class="global-nav" aria-label="Primary navigation">
      <a class="nav-brand" href="/">Video Pipeline MVP</a>
      <div class="nav-links">
        <a href="/" class="{'active' if active_nav == 'projects' else ''}">Projects</a>
        <a href="/benchmark" class="{'active' if active_nav == 'benchmark' else ''}">Benchmark</a>
        <a href="/settings/lava" class="{'active' if active_nav == 'lava' else ''}">LAVA Settings</a>
        <a href="/docs">API Docs</a>
      </div>
    </nav>
    """
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{escape(title)}</title>
        <style>{styles()}</style>
      </head>
      <body>
        {nav_html}
        {body}
        {assistant_widget()}
      </body>
    </html>
    """


def assistant_widget() -> str:
    """右下角的 AI 助手。唯讀，且刻意不參與任何主流程的表單。

    脈絡從 app-shell 的 data-* 屬性讀取，由後端渲染時寫入，
    助手因此不需要從 DOM 反推語義。對話存在 sessionStorage，
    換頁後仍在，關掉分頁才清空。
    """
    return """
    <div id="ai-bubble" class="ai-bubble" role="button" tabindex="0"
         aria-label="開啟 AI 製片助手">● AI</div>
    <div id="ai-panel" class="ai-panel" hidden aria-label="AI 製片助手">
      <div class="ai-head">
        <strong>AI 製片助手</strong>
        <span class="ai-head-actions">
          <button type="button" id="ai-min" title="最小化">—</button>
          <button type="button" id="ai-close" title="關閉">×</button>
        </span>
      </div>
      <div class="ai-where" id="ai-where">目前：讀取中…</div>
      <div class="ai-suggests">
        <button type="button" class="ai-chip">我現在下一步要做什麼？</button>
        <button type="button" class="ai-chip">為什麼這個按鈕不能按？</button>
        <button type="button" class="ai-chip">這一頁是做什麼的？</button>
      </div>
      <div class="ai-log" id="ai-log"></div>
      <form class="ai-form" id="ai-form">
        <input id="ai-input" placeholder="問問題…" autocomplete="off" />
        <button type="submit" title="送出">↑</button>
      </form>
      <p class="ai-note">助手只能讀取與說明，不會替你修改資料或執行操作。</p>
    </div>
    <style>
      /* 表格最後一欄的操作按鈕會落在右下角，窄視窗時 Bubble 會壓到它。
         給頁面尾端留出 Bubble 的高度，捲到底時按鈕就不會被蓋住。 */
      body { padding-bottom: 76px; }
      .ai-bubble {
        position: fixed; right: 18px; bottom: 18px; z-index: 60;
        background: #1f3a4d; color: #7ec8e2; border: 1px solid #2f5670;
        border-radius: 999px; padding: 10px 16px; cursor: pointer;
        font-size: 14px; user-select: none;
      }
      .ai-bubble:hover { background: #24455c; }
      .ai-panel {
        position: fixed; right: 18px; bottom: 18px; z-index: 61;
        width: min(380px, calc(100vw - 36px));
        max-height: min(560px, calc(100vh - 90px));
        display: flex; flex-direction: column;
        background: #131a26; border: 1px solid #2a3344; border-radius: 10px;
      }
      /* display: flex 會蓋掉 [hidden] 的 UA 樣式，少了這一條，
         聊天視窗會在每一頁載入時都是展開的，擋住右下角。 */
      .ai-panel[hidden] { display: none; }
      .benchmark-entry {
        display: flex; justify-content: space-between; align-items: center;
        gap: 16px; flex-wrap: wrap; margin-bottom: 16px;
      }
      .ai-head {
        display: flex; justify-content: space-between; align-items: center;
        padding: 10px 12px; border-bottom: 1px solid #2a3344;
      }
      .ai-head-actions button {
        background: none; border: none; color: #8a94a6; cursor: pointer;
        font-size: 15px; padding: 0 4px;
      }
      .ai-where { padding: 8px 12px; font-size: 12px; color: #7ec8e2; }
      .ai-suggests { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px; }
      .ai-chip {
        background: #1b2533; border: 1px solid #2a3344; color: #c7d0e0;
        border-radius: 999px; padding: 4px 10px; font-size: 12px; cursor: pointer;
      }
      .ai-chip:hover { background: #222e40; }
      .ai-log {
        flex: 1; overflow-y: auto; padding: 8px 12px; font-size: 13px;
        line-height: 1.65; min-height: 90px;
      }
      .ai-msg { margin-bottom: 10px; white-space: pre-wrap; }
      .ai-msg.user { color: #c7d0e0; }
      .ai-msg.user::before { content: '你：'; color: #6b7688; }
      .ai-msg.bot { color: #9fe6c0; }
      .ai-msg.err { color: #e8b; }
      .ai-form { display: flex; gap: 6px; padding: 8px 12px;
                 border-top: 1px solid #2a3344; }
      .ai-form input { flex: 1; }
      .ai-form button {
        background: #1f3a4d; border: 1px solid #2f5670; color: #7ec8e2;
        border-radius: 6px; padding: 0 12px; cursor: pointer;
      }
      .ai-note { padding: 0 12px 10px; font-size: 11px; color: #6b7688; }
      @media (max-width: 480px) {
        .ai-panel { right: 8px; left: 8px; width: auto; }
      }
    </style>
    <script>
    (function () {
      var bubble = document.getElementById('ai-bubble');
      var panel = document.getElementById('ai-panel');
      var log = document.getElementById('ai-log');
      var form = document.getElementById('ai-form');
      var input = document.getElementById('ai-input');
      if (!bubble || !panel || !log || !form || !input) return;

      var KEY = 'ai-assistant-log';
      var OPEN = 'ai-assistant-open';

      function pageContext() {
        var shell = document.querySelector('[data-route]');
        var banner = document.querySelector('[data-error-code]');
        return {
          route: (shell && shell.dataset.route) || window.location.pathname,
          entity_type: (shell && shell.dataset.entityType) || '',
          entity_id: (shell && shell.dataset.entityId) || '',
          error_code: (banner && banner.dataset.errorCode) || '',
          error_message: (banner && banner.textContent.trim().slice(0, 300)) || ''
        };
      }

      function describeWhere() {
        var shell = document.querySelector('[data-route]');
        var where = document.getElementById('ai-where');
        if (!where) return;
        var title = document.title || '';
        var step = shell && shell.dataset.step;
        where.textContent = '目前：' + title + (step ? ' · Step ' + step : '');
      }

      function render() {
        var items = JSON.parse(sessionStorage.getItem(KEY) || '[]');
        log.innerHTML = '';
        items.forEach(function (item) {
          var node = document.createElement('div');
          node.className = 'ai-msg ' + item.role;
          node.textContent = item.text;
          log.appendChild(node);
        });
        log.scrollTop = log.scrollHeight;
      }

      function push(role, text) {
        var items = JSON.parse(sessionStorage.getItem(KEY) || '[]');
        items.push({ role: role, text: text });
        sessionStorage.setItem(KEY, JSON.stringify(items.slice(-40)));
        render();
      }

      function open() {
        panel.hidden = false; bubble.style.display = 'none';
        sessionStorage.setItem(OPEN, '1');
        describeWhere(); render(); input.focus();
      }
      function close() {
        panel.hidden = true; bubble.style.display = '';
        sessionStorage.setItem(OPEN, '0');
      }

      bubble.addEventListener('click', open);
      bubble.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
      document.getElementById('ai-min').addEventListener('click', close);
      document.getElementById('ai-close').addEventListener('click', function () {
        sessionStorage.removeItem(KEY); close();
      });

      document.querySelectorAll('.ai-chip').forEach(function (chip) {
        chip.addEventListener('click', function () {
          input.value = chip.textContent.trim();
          form.dispatchEvent(new Event('submit', { cancelable: true }));
        });
      });

      // 錯誤橫幅或按鈕旁的 Ask AI。data-ask-control 指名問的是哪一顆，
      // 否則助手只知道這一頁有哪些按鈕，不知道使用者指的是哪一個。
      var focusedControl = '';
      document.querySelectorAll('[data-ask-ai]').forEach(function (link) {
        link.addEventListener('click', function (e) {
          e.preventDefault();
          open();
          focusedControl = link.dataset.askControl || '';
          input.value = link.dataset.askAi || '這個錯誤是什麼意思？';
          form.dispatchEvent(new Event('submit', { cancelable: true }));
        });
      });

      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var question = input.value.trim();
        if (!question) return;
        input.value = '';
        push('user', question);
        push('bot', '思考中…');

        var payload = pageContext();
        payload.question = question;
        payload.control_id = focusedControl;
        focusedControl = '';

        fetch('/assistant/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            var items = JSON.parse(sessionStorage.getItem(KEY) || '[]');
            items.pop();
            sessionStorage.setItem(KEY, JSON.stringify(items));
            if (data.ok) { push('bot', data.reply); }
            else { push('err', data.reply || '助手暫時無法使用。'); }
          })
          .catch(function () {
            var items = JSON.parse(sessionStorage.getItem(KEY) || '[]');
            items.pop();
            sessionStorage.setItem(KEY, JSON.stringify(items));
            push('err', '助手暫時無法使用。這不影響你目前的操作。');
          });
      });

      if (sessionStorage.getItem(OPEN) === '1') { open(); } else { close(); }
    })();
    </script>
    """


def lava_settings_view(settings: Settings, verification_result: dict | None = None) -> str:
    status = get_llm_brain_status(settings)
    binding_map = {binding.task_id: binding for binding in status.bindings}
    connection_map = {connection.connection_id: connection for connection in status.connections}
    configured_envs = set(status.configured_env_keys)

    verification_html = ""
    if verification_result is not None:
        ok = bool(verification_result.get("ok"))
        cls = "ok" if ok else "warn"
        summary = verification_result.get("reply") if ok else verification_result.get("error", "Verification failed")
        verification_html = f"""
        <section class="panel" style="border-color: {'var(--accent)' if ok else 'var(--amber)'};">
          <div class="section-head">
            <h2>Verification Result</h2>
            <span class="status {cls}">{'ok' if ok else 'needs attention'}</span>
          </div>
          <p class="muted">{escape(str(summary))}</p>
        </section>
        """

    connection_rows = []
    connection_options = []
    for connection in status.connections:
        configured = connection.api_key_env in configured_envs
        key_badge = '<span class="status ok">configured</span>' if configured else '<span class="status warn">missing key</span>'
        active_badge = '<span class="status ok">active</span>' if connection.is_active else '<span class="status warn">inactive</span>'
        connection_options.append(
            f'<option value="{escape(connection.connection_id)}">{escape(connection.label)}</option>'
        )
        connection_rows.append(
            f"""
            <tr>
              <td><strong>{escape(connection.label)}</strong><br><span class="muted mono">{escape(connection.connection_id)}</span></td>
              <td>{escape(connection.provider)}</td>
              <td class="mono">{escape(connection.model_id)}</td>
              <td>{key_badge}</td>
              <td>{active_badge}</td>
              <td>
                <form method="post" action="/settings/lava/verify">
                  <input type="hidden" name="connection_id" value="{escape(connection.connection_id)}" />
                  <input type="hidden" name="capability" value="chat" />
                  <button class="ghost-button" type="submit">Verify</button>
                </form>
              </td>
            </tr>
            """
        )

    option_html = "".join(connection_options)
    task_rows = []
    for task in status.tasks:
        binding = binding_map.get(task.task_id)
        selected_connection_id = binding.connection_id if binding else ""
        bound_connection = connection_map.get(selected_connection_id)
        model_value = binding.model_id if binding and binding.model_id else ""
        selected_options = []
        for connection in status.connections:
            selected = " selected" if connection.connection_id == selected_connection_id else ""
            selected_options.append(
                f'<option value="{escape(connection.connection_id)}"{selected}>{escape(connection.label)}</option>'
            )
        required = '<span class="status warn">required</span>' if task.required else '<span class="status ok">optional</span>'
        key_status = "unbound"
        if bound_connection:
            key_status = "configured" if bound_connection.api_key_env in configured_envs else "missing key"
        task_rows.append(
            f"""
            <tr>
              <td>
                <strong>{escape(task.label)}</strong><br>
                <span class="muted mono">{escape(task.task_id)}</span><br>
                <span class="muted">{escape(task.description)}</span>
              </td>
              <td>{required}<br><span class="pill">{escape(task.category)}</span></td>
              <td>
                <form method="post" action="/settings/lava/binding" class="lava-binding-form">
                  <input type="hidden" name="task_id" value="{escape(task.task_id)}" />
                  <select name="connection_id">{''.join(selected_options) or option_html}</select>
                  <input name="model_id" value="{escape(model_value)}" placeholder="{escape(bound_connection.model_id if bound_connection else 'optional model override')}" />
                  <button class="ghost-button" type="submit">Save</button>
                </form>
                <span class="muted">key: {escape(key_status)}</span>
              </td>
              <td class="muted">{escape(task.fallback_behavior)}</td>
            </tr>
            """
        )

    return page(
        title="LAVA Settings",
        active_nav="lava",
        body=f"""
        <section class="app-shell">
          <header class="topbar">
            <div>
              <p class="eyebrow">LLM control plane</p>
              <h1>LAVA Settings</h1>
              <p class="muted mono">binding source: {escape(status.binding_source)} | config: {escape(status.config_path or '-')}</p>
            </div>
            <a class="ghost-button" href="/settings/lava/status">Status JSON</a>
          </header>
          {verification_html}
          <section class="panel">
            <div class="section-head">
              <h2>Provider Connections</h2>
              <span class="muted">{len(status.configured_env_keys)} configured env keys</span>
            </div>
            <table>
              <thead><tr><th>Connection</th><th>Provider</th><th>Model</th><th>Key</th><th>State</th><th></th></tr></thead>
              <tbody>{''.join(connection_rows)}</tbody>
            </table>
          </section>
          <section class="panel">
            <div class="section-head">
              <h2>Task Bindings</h2>
              <span class="muted">{len(status.tasks)} registered tasks</span>
            </div>
            <table>
              <thead><tr><th>Task</th><th>Stage</th><th>Binding</th><th>Fallback</th></tr></thead>
              <tbody>{''.join(task_rows)}</tbody>
            </table>
          </section>
        </section>
        """,
    )


def project_row(artifact: ProductionArtifact) -> str:
    cues = len(artifact.cue_ledger.cues) if artifact.cue_ledger else 0
    ready = bool(artifact.compliance_report and artifact.compliance_report.upload_ready)
    snapshot = project_status_snapshot(artifact)
    mini_status = status_mini_strip(snapshot["items"])
    return f"""
    <tr>
      <td class="mono">{escape(artifact.project_id)}</td>
      <td>{escape(artifact.title)}</td>
      <td>{cues}</td>
      <td>
        <div class="project-status-stack">
          {mini_status}
          {status_chip(artifact.review_status, ready)}
          <small class="muted">{escape(snapshot["next_action_text"])}</small>
        </div>
      </td>
      <td><a class="button-link" href="/projects/{escape(artifact.project_id)}/view">Open</a></td>
    </tr>
    """


def preview_panel(settings: Settings, artifact: ProductionArtifact) -> str:
    project_id = artifact.project_id
    if not project_file_path(settings, project_id, "preview.mp4").exists():
        return ""
    return f"""
    <section class="panel preview-panel">
      <div class="section-head">
        <h2>Preview</h2>
        <a class="text-link" href="/projects/{escape(project_id)}/files/preview.mp4">Download</a>
      </div>
      <video controls preload="metadata" src="/projects/{escape(project_id)}/files/preview.mp4"></video>
    </section>
    """


def cue_strip_panel(artifact: ProductionArtifact) -> str:
    cues = artifact.cue_ledger.cues if artifact.cue_ledger else []
    blocks = "\n".join(cue_block(cue) for cue in cues)
    if not blocks:
        blocks = '<div class="muted empty">No cue strip</div>'
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Timeline Strip</h2>
        <span class="muted">{len(cues)} cues</span>
      </div>
      <div class="cue-strip">{blocks}</div>
    </section>
    """


def cue_block(cue) -> str:
    return f"""
    <div class="cue-block {escape(cue.asset_type)}" title="{escape(cue.voice_text)}">
      <span class="mono">{escape(cue.cue_id.replace("cue_", "#"))}</span>
      <strong>{escape(cue.asset_type)}</strong>
      <small>{format_ms(cue.start_ms)}</small>
    </div>
    """


def review_panel(artifact: ProductionArtifact) -> str:
    approve_cues = review_form(artifact, "approve_cues", "Approve Cues", artifact.review_status in {ReviewStatus.CUES_READY, ReviewStatus.CHANGES_REQUESTED})
    approve_assets = review_form(
        artifact,
        "approve_assets",
        "Approve Assets",
        artifact.review_status == ReviewStatus.ASSETS_REVIEW and all_assets_approved(artifact),
    )
    approve_final = review_form(
        artifact,
        "approve_final",
        "Approve Final Preview",
        artifact.review_status == ReviewStatus.PREVIEW_READY and all_assets_approved(artifact),
    )
    request_changes = review_form(artifact, "request_changes", "Request Changes", True, include_note=True, danger=True)
    return f"""
    <section class="panel command-panel">
      <div class="section-head">
        <h2>Review Gate</h2>
        {status_chip(artifact.review_status)}
      </div>
      <div class="gate-stack">
        {approve_cues}
        {approve_assets}
        {approve_final}
        {request_changes}
      </div>
    </section>
    """


def transcript_panel(artifact: ProductionArtifact) -> str:
    imported = artifact.transcript_import
    summary = "No transcript imported yet"
    warnings = ""
    if imported:
        duration = format_ms(imported.duration_ms)
        summary = f"{len(imported.segments)} segments | {duration} | {imported.format}"
        if imported.warnings:
            warnings = "<ul class=\"warning-list\">" + "".join(
                f"<li>{escape(item)}</li>" for item in imported.warnings[:5]
            ) + "</ul>"
    options = "\n".join(
        f'<option value="{escape(fmt)}">{escape(fmt)}</option>'
        for fmt in TranscriptFormat
    )
    return f"""
    <section id="transcript" class="panel">
      <div class="section-head">
        <h2>Import Transcript</h2>
        <span class="muted">{escape(summary)}</span>
      </div>
      <form method="post" action="/projects/{escape(artifact.project_id)}/transcript/import" class="transcript-form">
        <div class="split">
          <div class="field">
            <label for="transcript-format">Format</label>
            <select id="transcript-format" name="transcript_format">{options}</select>
          </div>
          <div class="field">
            <label for="transcript-source">Source</label>
            <input id="transcript-source" name="source_name" value="paste" />
          </div>
        </div>
        <input type="hidden" name="actor" value="local" />
        <div class="field">
          <label for="transcript-content">SRT / VTT / JSON / Text</label>
          <textarea id="transcript-content" name="transcript_content" required placeholder="Paste external ASR output here"></textarea>
        </div>
        <div class="split">
          <input name="note" placeholder="Import note" />
          <button class="primary" type="submit">Rebuild Timeline</button>
        </div>
      </form>
      {warnings}
      <div class="asr-run-box">
        <div>
          <strong>Local CPU ASR</strong>
          <span class="muted">Uses the optional Docker ASR worker and the project voiceover file.</span>
        </div>
        <form method="post" action="/projects/{escape(artifact.project_id)}/asr/run">
          <button class="ghost-button" type="submit">Run CPU ASR</button>
        </form>
      </div>
    </section>
    """


def shot_dispatch_panel(artifact: ProductionArtifact) -> str:
    """鏡頭派工面板。

    第一階段所有影片能力都走人工 transport，因此正常狀態是 pending_manual：
    job package 已產出，等待人工至平台生成後匯回。
    """
    project_id = artifact.project_id
    shots = sorted(artifact.shot_plans, key=lambda item: item.order)

    manual_providers = [
        spec for spec in provider_specs().values()
        if spec.transport is TransportKind.MANUAL
    ]
    provider_options = "\n".join(
        f'<option value="{escape(spec.provider_id)}">{escape(spec.label)}</option>'
        for spec in sorted(manual_providers, key=lambda s: s.provider_id)
    )

    if not shots:
        return f"""
    <section id="shots" class="panel">
      <div class="section-head">
        <h2>Shot Dispatch</h2>
        <span class="muted">No shot plans yet</span>
      </div>
      <p class="muted empty">建立 ShotPlan 後即可產生 job package 交付人工生成。</p>
    </section>
    """

    jobs: dict[str, list[CapabilityJob]] = {}
    try:
        with Session(engine) as session:
            for job in session.exec(
                select(CapabilityJob).where(CapabilityJob.project_id == project_id)
            ).all():
                jobs.setdefault(job.shot_id or "", []).append(job)
    except Exception as error:  # noqa: BLE001 - 派工狀態不可用時仍應顯示鏡頭清單
        logger.warning("Failed to load capability jobs: %s", error)

    try:
        readiness = assess_project_readiness(artifact)
    except Exception as error:  # noqa: BLE001 - 完備度不可用時仍顯示鏡頭清單
        logger.warning("Failed to assess readiness: %s", error)
        readiness = {}

    readiness_labels = {
        ShotReadinessState.READY: "Ready",
        ShotReadinessState.INCOMPLETE: "Incomplete",
        ShotReadinessState.BLOCKED: "Blocked",
    }

    rows = []
    for shot in shots:
        shot_jobs = jobs.get(shot.shot_id, [])
        if shot_jobs:
            state = ", ".join(
                sorted({f"{job.provider}: {job.status}" for job in shot_jobs})
            )
        else:
            state = "not dispatched"

        check = readiness.get(shot.shot_id)
        if check is None:
            ready_cell = '<span class="muted">unknown</span>'
        else:
            label = readiness_labels[check.state]
            title = escape("; ".join(check.issues)) if check.issues else ""
            css = f"readiness-{check.state.value}"
            ready_cell = f'<span class="{css}" title="{title}">{label}</span>'

        rows.append(
            f"""
        <tr>
          <td class="mono">{escape(shot.shot_id)}</td>
          <td>{escape(shot.capability.value)}</td>
          <td>{escape(shot.camera.describe())}</td>
          <td class="mono">{shot.target_duration_ms / 1000:g}s</td>
          <td>{escape(shot.prompt[:70])}</td>
          <td>{ready_cell}</td>
          <td class="muted">{escape(state)}</td>
        </tr>
        """
        )

    dispatched = sum(1 for shot in shots if jobs.get(shot.shot_id))
    not_ready = sum(
        1
        for check in readiness.values()
        if check.state is not ShotReadinessState.READY
    )
    summary = f"{dispatched}/{len(shots)} dispatched"
    if not_ready:
        summary += f" | {not_ready} not ready"

    return f"""
    <section id="shots" class="panel">
      <div class="section-head">
        <h2>Shot Dispatch</h2>
        <span class="muted">{escape(summary)}</span>
      </div>
      <form method="post" action="/projects/{escape(project_id)}/shots/dispatch" class="split">
        <div class="field">
          <label for="dispatch-provider">Provider</label>
          <select id="dispatch-provider" name="preferred_provider">
            <option value="">Auto (routing policy)</option>
            {provider_options}
          </select>
        </div>
        <button class="primary" type="submit">Generate Job Packages</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th>Shot</th><th>Capability</th><th>Camera</th><th>Target</th><th>Prompt</th><th>Ready</th><th>Dispatch</th></tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <p class="muted">
        僅 Ready 的鏡頭會被派工。Incomplete 代表參考素材檔案缺失，
        Blocked 代表缺少必要資訊，兩者都不會送出殘缺的 job package。
      </p>
      <p class="muted">
        產出後下載 job packages，依各平台 README 的步驟人工生成，再將影片匯回。
        <a href="/projects/{escape(project_id)}/job-packages.zip">Download job packages</a>
      </p>
    </section>
    """


def _score_input(
    name: str, label: str, variant_id: str, hint: str = "", maximum: int | None = 100
) -> str:
    """一個評分欄位。

    原本只有 placeholder 當標籤（prompt / stable / motion…），四個字元
    看不出是哪個維度，也看不出範圍。評分是 V1 的主要產出，欄位必須寫清楚。
    """
    field_id = f"{name}_{variant_id[-8:]}"
    bounds = f'min="0" max="{maximum}" ' if maximum is not None else 'min="0" '
    note = f'<span class="score-hint">{escape(hint)}</span>' if hint else ""
    return (
        f'<div class="score-field">'
        f'<label for="{escape(field_id)}">{escape(label)}{note}</label>'
        f'<input id="{escape(field_id)}" name="{escape(name)}" type="number" '
        f'{bounds}step="1" placeholder="—" />'
        f"</div>"
    )


def _facial_acting_input(variant) -> str:
    """表情演技。看不清臉的鏡頭一律 N/A，欄位停用而非留給人猜。"""
    from pipeline.benchmark import v1_pack

    try:
        applicable = v1_pack.evaluates_facial_acting(variant.shot_id)
    except Exception:  # noqa: BLE001 - 非 benchmark 專案沒有這張表
        applicable = True
    if applicable:
        return _score_input("facial_acting", "表情演技", variant.variant_id)
    return (
        '<div class="score-field disabled">'
        '<label>表情演技<span class="score-hint">此鏡頭看不清臉</span></label>'
        '<input value="N/A" disabled />'
        "</div>"
    )


def variant_panel(artifact: ProductionArtifact) -> str:
    """候選管理面板：匯入人工生成的影片、評分、選片。"""
    project_id = artifact.project_id
    shots = sorted(artifact.shot_plans, key=lambda item: item.order)
    if not shots:
        return ""

    try:
        variants = list_variants(project_id)
        summary = summarize_project_qc(artifact)
    except Exception as error:  # noqa: BLE001 - 候選不可用時仍應顯示匯入表單
        logger.warning("Failed to load variants: %s", error)
        variants, summary = [], None

    # benchmark 歸屬走派工血緣。沒有血緣的候選不屬於任何比較對象，
    # 必須在介面上標示出來，否則使用者會以為它已計入統計。
    attributions: dict[str, object] = {}
    try:
        from pipeline.benchmark.attribution import build_index

        index = build_index(project_id)
        attributions = {item.variant_id: item for item in index.items}
    except Exception as error:  # noqa: BLE001 - 非 benchmark 專案無需歸屬
        logger.debug("Attribution unavailable for %s: %s", project_id, error)

    by_shot: dict[str, list] = {}
    for variant in variants:
        by_shot.setdefault(variant.shot_id, []).append(variant)

    shot_options = "\n".join(
        f'<option value="{escape(shot.shot_id)}">{escape(shot.shot_id)}</option>'
        for shot in shots
    )
    provider_options = "\n".join(
        f'<option value="{escape(spec.provider_id)}">{escape(spec.label)}</option>'
        for spec in sorted(provider_specs().values(), key=lambda s: s.provider_id)
        if spec.transport is TransportKind.MANUAL
    )

    cards = []
    for shot in shots:
        items = by_shot.get(shot.shot_id, [])
        if not items:
            cards.append(
                f'<div class="variant-group"><strong class="mono">{escape(shot.shot_id)}</strong>'
                f'<span class="muted"> no variants imported</span></div>'
            )
            continue

        rows = []
        for variant in items:
            duration = (
                f"{variant.actual_duration_ms / 1000:g}s"
                if variant.actual_duration_ms
                else "?"
            )
            selected = " selected" if variant.is_selected else ""
            attributed = attributions.get(variant.variant_id)
            if attributed is None:
                origin = (
                    f'<span class="mono">{escape(variant.provider)}</span>'
                    '<br /><span class="muted" title="沒有派工來源，不計入 benchmark">'
                    "未歸屬</span>"
                )
                bench_cta = '<span class="muted">無法選為代表作</span>'
            else:
                mark = " ★" if attributed.benchmark_selected else ""
                origin = (
                    f'<span class="mono">{escape(attributed.target_id)}</span>{mark}'
                    f'<br /><span class="muted mono">{escape(attributed.model_id)}'
                    f"{escape(' @' + attributed.model_version) if attributed.model_version else ''}"
                    "</span>"
                )
                bench_cta = (
                    f'<form method="post" action="/benchmark/variants/'
                    f'{escape(variant.variant_id)}/benchmark-select" class="inline-form">'
                    '<button class="ghost-button" type="submit">設為代表作</button>'
                    "</form>"
                )
            # 影片必須看得見才評得出分。原本播放器被塞在表格欄位裡，
            # 實際渲染只有 82-104px 寬，評 temporal_stability 或
            # artifact_severity 等於憑印象打分。改成左右並排：
            # 左邊播放，右邊填分，兩者同時在畫面上。
            preview = (
                f'<video class="variant-video" controls preload="metadata" '
                f'src="/projects/{escape(project_id)}/variants/'
                f'{escape(variant.variant_id)}/video"></video>'
                if variant.local_path
                else '<div class="variant-video empty">無影片檔</div>'
            )
            rows.append(
                f"""
            <article class="variant-card{selected}">
              <div class="variant-clip">
                {preview}
                <div class="variant-facts">
                  <div><span class="muted">來源</span>{origin}</div>
                  <div><span class="muted">片長</span><b class="mono">{escape(duration)}</b></div>
                  <div><span class="muted">解析度</span><b class="mono">{escape(variant.resolution or "?")}</b></div>
                  <div><span class="muted">狀態</span><b>{escape(variant.status)}</b></div>
                  <div><span class="muted">候選</span><b class="mono">{escape(variant.variant_id[-12:])}</b></div>
                </div>
              </div>

              <form class="variant-score" method="post"
                    action="/projects/{escape(project_id)}/variants/{escape(variant.variant_id)}/qc">
                <div class="score-head">
                  <strong>單支品質評分</strong>
                  <span class="muted">0-100，留白代表此鏡頭不適用（N/A）</span>
                </div>
                <div class="score-grid">
                  {_score_input("identity_consistency", "身份一致性", variant.variant_id, hint="權重最高")}
                  {_score_input("temporal_stability", "時間穩定性", variant.variant_id)}
                  {_score_input("prompt_adherence", "提示詞貼合", variant.variant_id)}
                  {_score_input("motion_quality", "動態品質", variant.variant_id)}
                  {_score_input("camera_control", "運鏡控制", variant.variant_id)}
                  {_score_input("artifact_severity", "瑕疵嚴重度", variant.variant_id, hint="越高越糟")}
                  {_facial_acting_input(variant)}
                  {_score_input("correction_minutes", "人工修補（分鐘）", variant.variant_id, maximum=None)}
                </div>
                <label class="usable-line">
                  <input type="checkbox" name="usable" value="1" />
                  <span>不需修補即可使用</span>
                </label>
                <button class="primary" type="submit">儲存評分</button>
              </form>

              <div class="variant-actions">
                <div class="action-block">
                  <strong>Benchmark 代表作</strong>
                  <p class="muted">這個比較對象在這顆鏡頭的代表，用於連戲比較。</p>
                  {bench_cta}
                </div>
                <div class="action-block">
                  <strong>成片選用</strong>
                  <p class="muted">這顆鏡頭最後要剪進成片的那一支。與代表作是兩回事。</p>
                  <form method="post" action="/projects/{escape(project_id)}/variants/{escape(variant.variant_id)}/select" class="inline-form">
                    <input name="reason" placeholder="選用理由" />
                    <button class="ghost-button" type="submit">選為成片</button>
                  </form>
                </div>
              </div>
            </article>
            """
            )
        cards.append(
            f"""
        <div class="variant-group">
          <strong class="mono shot-heading">{escape(shot.shot_id)}</strong>
          {"".join(rows)}
        </div>
        """
        )

    if summary and summary.planned_count:
        planned_rate = summary.usable_shot_rate_of_planned
        dispatched_rate = summary.usable_shot_rate_of_dispatched
        per_usable = summary.correction_minutes_per_usable_shot
        head = (
            f"usable shots {summary.usable_count}/{summary.planned_count} planned "
            f"({'n/a' if planned_rate is None else f'{planned_rate:.0%}'}) | "
            f"{summary.usable_count}/{summary.dispatched_count} dispatched "
            f"({'n/a' if dispatched_rate is None else f'{dispatched_rate:.0%}'}) | "
            f"correction {summary.total_correction_minutes:g} min"
            + (f" ({per_usable:g} min/usable)" if per_usable is not None else "")
        )
    else:
        head = "No shot plans yet"

    return f"""
    <section id="variants" class="panel">
      <div class="section-head">
        <h2>Variants</h2>
        <span class="muted">{escape(head)}</span>
      </div>
      <form method="post" action="/projects/{escape(project_id)}/variants/import"
            enctype="multipart/form-data" class="variant-import">
        <div class="split">
          <div class="field">
            <label for="variant-shot">Shot</label>
            <select id="variant-shot" name="shot_id">{shot_options}</select>
          </div>
          <div class="field">
            <label for="variant-provider">Provider</label>
            <select id="variant-provider" name="provider">{provider_options}</select>
          </div>
        </div>
        <div class="field">
          <label for="variant-files">Generated videos</label>
          <input id="variant-files" type="file" name="files" multiple accept="video/*" required />
        </div>
        <div class="split">
          <input name="request_hash" placeholder="request_hash from job.json (optional)" />
          <button class="primary" type="submit">Import Variants</button>
        </div>
      </form>
      {"".join(cards)}
    </section>
    """


def visual_quality_panel(artifact: ProductionArtifact) -> str:
    contract = artifact.visual_contract
    report = artifact.visual_qc_report
    if not contract or not report:
        return ""
    shots = "\n".join(visual_shot_card(shot) for shot in contract.shots[:12])
    findings = "\n".join(
        f'<li><span class="status {severity_class(finding.severity)}">{escape(finding.severity)}</span> '
        f'{escape(finding.cue_id or "")} {escape(finding.message)}</li>'
        for finding in report.findings
    )
    if not findings:
        findings = '<li class="muted">No visual QC findings</li>'
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Visual Quality Contract</h2>
        <div class="actions">
          <span class="status {'ok' if report.score >= 85 else 'warn'}">score {report.score}</span>
          {file_link(artifact.project_id, "visual_contract.json", "Contract")}
          {file_link(artifact.project_id, "visual_qc_report.json", "QC")}
        </div>
      </div>
      <div class="visual-shot-grid">{shots}</div>
      <ul class="warning-list">{findings}</ul>
    </section>
    """


def visual_shot_card(shot) -> str:
    return f"""
    <article class="visual-shot">
      <span class="mono">{escape(shot.cue_id)}</span>
      <strong>{escape(shot.shot_type)}</strong>
      <p>{escape(shot.prompt)}</p>
    </article>
    """


def llm_brain_panel(project_id: str, settings: Settings) -> str:
    status = get_llm_brain_status(settings)
    binding_map = {binding.task_id: binding for binding in status.bindings}
    connection_map = {connection.connection_id: connection for connection in status.connections}
    rows = []
    for task in status.tasks:
        binding = binding_map.get(task.task_id)
        connection = connection_map.get(binding.connection_id if binding else "")
        provider = connection.provider if connection else "unbound"
        model = binding.model_id or connection.model_id if binding and connection else "-"
        configured = bool(connection and connection.api_key_env in status.configured_env_keys)
        key_badge = '<span class="status ok">key ready</span>' if configured else '<span class="status warn">key missing</span>'
        rows.append(
            f"<tr><td>{escape(task.label)}</td><td>{escape(provider)}</td><td>{escape(model)}</td>"
            f"<td>{key_badge}</td></tr>"
        )
        
    trigger_btn = f"""
    <div style="margin-top: 15px; border-top: 1px solid #374151; padding-top: 15px;">
      <form method="post" action="/projects/{escape(project_id)}/lava/run">
        <button class="primary" type="submit" style="width: 100%;">Run LAVA Brain Workflow</button>
      </form>
    </div>
    """
    
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>LLM Brain</h2>
        <a class="button-link" href="/settings/lava">Settings</a>
      </div>
      <table>
        <thead><tr><th>Task</th><th>Provider</th><th>Model</th><th>Key</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      {trigger_btn}
    </section>
    """


def review_form(
    artifact: ProductionArtifact,
    action: str,
    label: str,
    enabled: bool,
    include_note: bool = False,
    danger: bool = False,
) -> str:
    disabled = "" if enabled else "disabled"
    note = '<input name="note" placeholder="Reason" />' if include_note else ""
    cls = "danger" if danger else "primary"
    return f"""
    <form method="post" action="/projects/{escape(artifact.project_id)}/review/{escape(action)}" class="gate-form">
      <input type="hidden" name="actor" value="local" />
      {note}
      <button class="{cls}" type="submit" {disabled}>{escape(label)}</button>
    </form>
    """


def asset_panel(artifact: ProductionArtifact) -> str:
    cards = "\n".join(asset_card(artifact, asset) for asset in (artifact.asset_manifest.assets if artifact.asset_manifest else []))
    if not cards:
        cards = '<div class="muted empty">No assets needed</div>'
    return f"""
    <section id="assets" class="panel">
      <div class="section-head">
        <h2>Asset Rights</h2>
        {file_link(artifact.project_id, "asset_manifest.json", "JSON")}
      </div>
      <div class="asset-grid">{cards}</div>
    </section>
    """


def asset_card(artifact: ProductionArtifact, asset) -> str:
    options = "\n".join(
        f'<option value="{escape(status)}" {"selected" if asset.rights_status == status else ""}>{escape(status)}</option>'
        for status in RightsStatus
    )
    return f"""
    <article class="asset-card {escape(asset.rights_status)}">
      <div class="asset-thumb {escape(asset.asset_type)}">{escape(asset.asset_type.replace("_", " "))}</div>
      <div class="asset-body">
        <div class="asset-title">
          <span class="mono">{escape(asset.asset_id)}</span>
          <span class="status {rights_class(asset.rights_status)}">{escape(asset.rights_status)}</span>
        </div>
        <p>{escape(asset.prompt_or_search or "")}</p>
        <form method="post" action="/projects/{escape(artifact.project_id)}/assets/{escape(asset.asset_id)}/rights" class="asset-form">
          <input type="hidden" name="actor" value="local" />
          <select name="rights_status">{options}</select>
          <input name="note" placeholder="Rights note" />
          <button type="submit">Save</button>
        </form>
      </div>
    </article>
    """


def compliance_panel(artifact: ProductionArtifact) -> str:
    findings = artifact.compliance_report.findings if artifact.compliance_report else []
    rows = "\n".join(finding_row(finding) for finding in findings)
    if not rows:
        rows = '<tr><td colspan="4" class="muted empty">No findings</td></tr>'
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Compliance</h2>
        {file_link(artifact.project_id, "compliance_report.json", "JSON")}
      </div>
      <table>
        <thead><tr><th>Severity</th><th>Code</th><th>Message</th><th>Cue</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def decision_log_panel(artifact: ProductionArtifact) -> str:
    entries = list(reversed(artifact.decision_log[-8:]))
    rows = "\n".join(log_row(entry) for entry in entries)
    if not rows:
        rows = '<tr><td colspan="4" class="muted empty">No decisions yet</td></tr>'
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Decision Log</h2>
        <span class="muted">{len(artifact.decision_log)} entries</span>
      </div>
      <table>
        <thead><tr><th>Time</th><th>Action</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def cue_row(cue) -> str:
    return f"""
    <tr>
      <td class="mono">{escape(cue.cue_id)}</td>
      <td>{format_ms(cue.start_ms)}-{format_ms(cue.end_ms)}</td>
      <td>{escape(cue.voice_text)}</td>
      <td>{escape(cue.visual_prompt or cue.asset_type)}</td>
      <td class="mono">{escape(cue.shorts_id or "")}</td>
    </tr>
    """


def finding_row(finding) -> str:
    severity = escape(finding.severity)
    return f"""
    <tr>
      <td><span class="status {severity_class(severity)}">{severity}</span></td>
      <td class="mono">{escape(finding.code)}</td>
      <td>{escape(finding.message)}</td>
      <td class="mono">{escape(finding.cue_id or "")}</td>
    </tr>
    """


def log_row(entry: DecisionLogEntry) -> str:
    status = ""
    if entry.from_status or entry.to_status:
        status = f"{entry.from_status or ''} -> {entry.to_status or ''}"
    return f"""
    <tr>
      <td>{entry.created_at.strftime("%H:%M:%S")}</td>
      <td class="mono">{escape(entry.action)}</td>
      <td>{escape(status)}</td>
      <td>{escape(entry.note or "")}</td>
    </tr>
    """


def stepper(status: ReviewStatus) -> str:
    active = step_index(status)
    items = []
    for index, (_, label) in enumerate(STEPS):
        if status == ReviewStatus.CHANGES_REQUESTED:
            state = "blocked" if index == active else ("done" if index < active else "todo")
        elif index < active:
            state = "done"
        elif index == active:
            state = "current"
        else:
            state = "todo"
        aria = ' aria-current="true"' if state in {"current", "blocked"} else ""
        items.append(
            f'<li class="{state}"{aria}><span>{index + 1}</span><strong>{escape(label)}</strong></li>'
        )
    return f'<ol class="stepper">{"".join(items)}</ol>'


def project_status_snapshot(artifact: ProductionArtifact) -> dict:
    asset_count = len(artifact.asset_manifest.assets) if artifact.asset_manifest else 0
    approved_asset_count = approved_assets_count(artifact)
    has_transcript = bool(artifact.transcript_import)
    has_cues = bool(artifact.cue_ledger and artifact.cue_ledger.cues)
    lava_ready = bool(artifact.visual_contract or artifact.video_packaging)
    assets_ready = asset_count > 0 and asset_count == approved_asset_count
    preview_ready = bool(artifact.preview_mp4) or artifact.review_status in {
        ReviewStatus.PREVIEW_READY,
        ReviewStatus.APPROVED,
    }
    compliance_ready = bool(artifact.compliance_report)
    upload_ready = bool(artifact.compliance_report and artifact.compliance_report.upload_ready)
    transcript_detail = "imported" if has_transcript else ("script cues" if has_cues else "missing")

    items = [
        {
            "key": "transcript",
            "short": "ASR",
            "label": "Transcript",
            "detail": transcript_detail,
            "state": "ok" if has_transcript else "warn",
        },
        {
            "key": "lava",
            "short": "LAVA",
            "label": "LAVA",
            "detail": "ready" if lava_ready else "not run",
            "state": "ok" if lava_ready else "warn",
        },
        {
            "key": "assets",
            "short": "AST",
            "label": "Assets",
            "detail": f"{approved_asset_count}/{asset_count} approved" if asset_count else "none",
            "state": "ok" if assets_ready else "warn",
        },
        {
            "key": "preview",
            "short": "PVW",
            "label": "Preview",
            "detail": "ready" if preview_ready else "pending",
            "state": "ok" if preview_ready else "warn",
        },
        {
            "key": "compliance",
            "short": "CMP",
            "label": "Compliance",
            "detail": "checked" if compliance_ready else "not checked",
            "state": "ok" if compliance_ready else "warn",
        },
        {
            "key": "upload",
            "short": "UP",
            "label": "Upload",
            "detail": "ready" if upload_ready else "blocked",
            "state": "ok" if upload_ready else "block",
        },
    ]

    next_action_html = '<a href="#transcript" class="text-link">Import Transcript</a> or Run CPU ASR'
    next_action_text = "Import Transcript or Run CPU ASR"
    missing = "Audio file or external ASR output"
    if not has_cues:
        if has_transcript:
            next_action_html = "Approve transcript import to rebuild cues"
            next_action_text = "Approve transcript import to rebuild cues"
            missing = "Cue ledger"
    elif not lava_ready:
        next_action_html = "Run LAVA Brain Workflow"
        next_action_text = "Run LAVA Brain Workflow"
        missing = "LAVA visual/packaging outputs"
    elif not assets_ready:
        next_action_html = '<a href="#assets" class="text-link">Review and approve assets</a>'
        next_action_text = "Review and approve assets"
        missing = "Approved asset rights"
    elif not preview_ready:
        next_action_html = "Review final preview"
        next_action_text = "Review final preview"
        missing = "Preview approval"
    elif not compliance_ready:
        next_action_html = "Run compliance check through the review flow"
        next_action_text = "Run compliance check through the review flow"
        missing = "Compliance report"
    elif not upload_ready:
        next_action_html = "Resolve compliance findings or approve final gate"
        next_action_text = "Resolve compliance findings or approve final gate"
        missing = "Final approval and clear compliance"
    else:
        next_action_html = "Ready for manual upload/export handoff"
        next_action_text = "Ready for manual upload/export handoff"
        missing = "None"

    return {
        "items": items,
        "next_action_html": next_action_html,
        "next_action_text": next_action_text,
        "missing": missing,
        "upload_ready": upload_ready,
        "review_status": status_label(artifact.review_status),
    }


def status_mini_strip(items: list[dict]) -> str:
    badges = "".join(
        f'<span class="status-mini {escape(str(item["state"]))}" title="{escape(str(item["label"]))}: {escape(str(item["detail"]))}">'
        f'{escape(str(item["short"]))}</span>'
        for item in items
    )
    return f'<div class="status-minis">{badges}</div>'


def status_flow_panel(artifact: ProductionArtifact) -> str:
    snapshot = project_status_snapshot(artifact)
    nodes = "".join(
        f"""
        <div class="status-node {escape(str(item["state"]))}">
          <span>{escape(str(item["label"]))}</span>
          <strong>{escape(str(item["detail"]))}</strong>
        </div>
        """
        for item in snapshot["items"]
    )
    return f"""
    <section class="panel status-flow-panel">
      <div class="section-head">
        <h2>Project Status Flow</h2>
        <span class="status {'ok' if snapshot["upload_ready"] else 'warn'}">{escape(str(snapshot["review_status"]))}</span>
      </div>
      <div class="status-pipeline">{nodes}</div>
      <div class="action-box">
        <p><strong>Next Recommended Action:</strong> {snapshot["next_action_html"]}</p>
        <p class="muted"><strong>Missing Prerequisites:</strong> {escape(str(snapshot["missing"]))}</p>
      </div>
    </section>
    """


def step_index(status: ReviewStatus) -> int:
    if status in {ReviewStatus.DRAFT, ReviewStatus.CUES_READY, ReviewStatus.CHANGES_REQUESTED}:
        return 0
    if status == ReviewStatus.ASSETS_REVIEW:
        return 1
    if status == ReviewStatus.PREVIEW_READY:
        return 2
    return 3


def file_link(project_id: str, filename: str, label: str) -> str:
    return f'<a class="button-link" href="/projects/{escape(project_id)}/files/{escape(filename)}">{escape(label)}</a>'


def stat_card(label: str, value: str, detail: str) -> str:
    return f"""
    <div class="stat-card">
      <span>{escape(label)}</span>
      <strong>{escape(value)}</strong>
      <small>{escape(detail)}</small>
    </div>
    """


def status_chip(status: ReviewStatus, ready: bool = False) -> str:
    if ready:
        return '<span class="status ok">upload ready</span>'
    cls = "block" if status == ReviewStatus.CHANGES_REQUESTED else "warn"
    if status == ReviewStatus.APPROVED:
        cls = "ok"
    return f'<span class="status {cls}">{escape(status_label(status))}</span>'


def status_label(status: ReviewStatus) -> str:
    return status.value.replace("_", " ")


def rights_class(status: RightsStatus) -> str:
    if status == RightsStatus.APPROVED:
        return "ok"
    if status == RightsStatus.REJECTED:
        return "block"
    return "warn"


def packaging_panel(artifact: ProductionArtifact) -> str:
    pkg = artifact.video_packaging
    title_info = "No title optimized yet"
    desc_info = "No SEO description yet"
    thumb_info = "No thumbnail prompt yet"
    if pkg:
        selected = pkg.get("selected_title") or (pkg.get("candidate_titles", [""])[0] if pkg.get("candidate_titles") else "")
        title_info = f"<strong>Selected Title:</strong> {escape(selected)}"
        desc_info = f"<strong>Description Draft:</strong><pre style='white-space: pre-wrap; font-size:12px;'>{escape(pkg.get('description', '')[:200])}...</pre>"
        thumb_info = f"<strong>Thumbnail Prompt:</strong> <small>{escape(pkg.get('thumbnail_prompt', ''))}</small>"
        
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Video Packaging & SEO</h2>
        <span class="pill">LAVA Brain</span>
      </div>
      <div style="display:grid; gap:8px; margin-bottom: 12px;">
        <div>{title_info}</div>
        <div>{desc_info}</div>
        <div>{thumb_info}</div>
      </div>
      <form method="post" action="/projects/{escape(artifact.project_id)}/optimize-packaging">
        <button class="primary" type="submit" style="width: 100%;">Optimize Packaging via LLM</button>
      </form>
    </section>
    """


def shorts_panel(artifact: ProductionArtifact) -> str:
    if "_short_" in artifact.project_id:
        return ""
    has_script = bool(artifact.approved_script_markdown and "<SHORT_BREAK>" in artifact.approved_script_markdown)
    disabled = "" if has_script else "disabled"
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Short-Video Splitting</h2>
        <span class="pill">Shorts Boundary</span>
      </div>
      <p style="margin-bottom: 12px; font-size: 12px; color: var(--muted);">
        Splits script by &lt;SHORT_BREAK&gt; into up to 10 subprojects.
      </p>
      <form method="post" action="/projects/{escape(artifact.project_id)}/split">
        <button class="primary" type="submit" style="width: 100%;" {disabled}>Split Script to Shorts</button>
      </form>
    </section>
    """


def metrics_panel(artifact: ProductionArtifact) -> str:
    metrics = artifact.metrics_decision
    results_html = ""
    if metrics:
        results_html = f"""
        <div style="margin-bottom: 12px; border-bottom: 1px solid var(--line); padding-bottom: 10px;">
          <strong>CTR:</strong> {metrics.ctr}% | 
          <strong>AVD:</strong> {metrics.average_view_duration_seconds}s | 
          <strong>RPM:</strong> ${metrics.rpm} <br>
          <strong>Decision:</strong> <span class="status ok">{metrics.decision}</span>
        </div>
        """
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Metrics & Optimization</h2>
        <span class="pill">Feedback Loop</span>
      </div>
      {results_html}
      
      <h3 style="margin: 10px 0; font-size: 14px; color: var(--text);">Manual Metrics Feedback</h3>
      <form method="post" action="/projects/{escape(artifact.project_id)}/metrics" class="project-form">
        <div class="split">
          <div class="field">
            <label for="metrics-ctr">CTR (%)</label>
            <input id="metrics-ctr" name="ctr" type="number" step="0.1" required value="4.5" />
          </div>
          <div class="field">
            <label for="metrics-avd">AVD (sec)</label>
            <input id="metrics-avd" name="avd" type="number" step="1" required value="120" />
          </div>
        </div>
        <div class="split">
          <div class="field">
            <label for="metrics-rpm">RPM ($)</label>
            <input id="metrics-rpm" name="rpm" type="number" step="0.01" required value="8.50" />
          </div>
          <div class="field">
            <label for="metrics-decision">Scale Decision</label>
            <select id="metrics-decision" name="decision">
              <option value="scale">Scale (加碼)</option>
              <option value="iterate">Iterate (優化題材)</option>
              <option value="kill">Kill (停止投資)</option>
            </select>
          </div>
        </div>
        <button class="primary" type="submit" style="width: 100%;">Feedback Metrics</button>
      </form>

      <div style="margin: 20px 0; border-top: 1px solid var(--line);"></div>

      <h3 style="margin: 10px 0; font-size: 14px; color: var(--text);">Sync with YouTube API</h3>
      <form method="post" action="/projects/{escape(artifact.project_id)}/metrics/youtube" class="project-form">
        <div class="field">
          <label for="metrics-youtube-id">YouTube Video ID</label>
          <input id="metrics-youtube-id" name="video_id" required placeholder="e.g. dQw4w9WgXcQ" />
        </div>
        <button class="primary" type="submit" style="width: 100%;">Sync via OAuth</button>
      </form>

      <div style="margin: 20px 0; border-top: 1px solid var(--line);"></div>

      <h3 style="margin: 10px 0; font-size: 14px; color: var(--text);">Import from Local CSV</h3>
      <form method="post" action="/projects/{escape(artifact.project_id)}/metrics/csv" class="project-form">
        <div class="field">
          <label for="metrics-csv-path">Local CSV Path</label>
          <input id="metrics-csv-path" name="csv_path" required placeholder="e.g. C:/path/to/metrics.csv" />
        </div>
        <button class="primary" type="submit" style="width: 100%;">Import CSV</button>
      </form>
    </section>
    """


@router.post("/projects/trend-research", response_class=HTMLResponse)
async def trend_research_list(
    topic_prompt: str = Form(...),
    settings: Settings = Depends(settings_dep),
) -> str:
    try:
        from pipeline.stages.topic_optimizer import suggest_next_topics
        res = await suggest_next_topics(settings, topic_prompt)
        report = res["topic_research"]
    except Exception as e:
        logger.error(f"Topic research failed: {e}")
        report = {
            "primary_keyword": topic_prompt,
            "competitor_gaps": ["LAVA Brain connection error; using fallback niche suggestions."],
            "cpm_tier": "medium",
            "search_intent": "How to automate video production",
            "suggested_angles": [
                f"{topic_prompt}：一字不差的半自動影音生產線",
                f"2026年最新 {topic_prompt} 的極速變現法",
                f"別再全自動！為何半自動 {topic_prompt} 才是唯一能做長期的產線"
            ]
        }

    angles_html = ""
    for idx, angle in enumerate(report.get("suggested_angles", [])):
        angles_html += f"""
        <div class="asset-card" style="margin-bottom:12px;">
          <div class="asset-thumb generated_image">Angle {idx+1}</div>
          <div class="asset-body">
            <h3 style="margin:0 0 5px 0; font-size:15px; color:var(--text);">{escape(angle)}</h3>
            <p style="font-size:12px; margin-bottom:8px;"><strong>Keyword:</strong> {escape(report.get('primary_keyword'))} | <strong>CPM:</strong> {escape(report.get('cpm_tier'))}</p>
            <form method="post" action="/projects/trend-research/create">
              <input type="hidden" name="title" value="{escape(angle)}" />
              <input type="hidden" name="primary_keyword" value="{escape(report.get('primary_keyword'))}" />
              <input type="hidden" name="angle" value="{escape(angle)}" />
              <button class="primary" type="submit">Create Project & Draft Script via LLM</button>
            </form>
          </div>
        </div>
        """

    return page(
        title="AI Trend Research Results",
        active_nav="projects",
        body=f"""
        <section class="app-shell">
          <header class="project-hero">
            <div>
              <h1 style="margin-top:8px;">AI Niche Research: {escape(topic_prompt)}</h1>
              <p class="muted">Primary Keyword: {escape(report.get('primary_keyword'))} | Search Intent: {escape(report.get('search_intent'))}</p>
            </div>
          </header>
          
          <section class="review-layout">
            <main class="main-col">
              <section class="panel">
                <h2>Suggested Video Angles</h2>
                <div style="margin-top: 15px;">{angles_html}</div>
              </section>
            </main>
            <aside class="side-col">
              <section class="panel">
                <h2>Competitor Gaps & Pain points</h2>
                <ul class="warning-list">
                  {"".join(f"<li>{escape(gap)}</li>" for gap in report.get('competitor_gaps', []))}
                </ul>
              </section>
            </aside>
          </section>
        </section>
        """
    )


@router.post("/projects/trend-research/create")
async def create_project_from_trend(
    title: str = Form(...),
    primary_keyword: str = Form(...),
    angle: str = Form(...),
    settings: Settings = Depends(settings_dep),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    title_stripped = title.strip() if title else ""
    if not title_stripped:
        raise WebException(detail="Project title cannot be empty", status_code=400, back_link="/")

    topic_research = {
        "primary_keyword": primary_keyword,
        "suggested_angles": [angle],
        "cpm_tier": "high",
        "search_intent": "automated video production"
    }
    
    try:
        script_data = await run_script_outline(topic_research, tone="informative, conversational, professional", persona="expert host")
        script_markdown = script_data.get("script_markdown", f"# {title_stripped}\nThis is a drafted script for: {angle}\n<SHORT_BREAK>\nStay tuned for more updates!")
    except Exception as e:
        logger.error(f"Failed to draft script via LLM: {e}")
        script_markdown = f"# {title_stripped}\nThis is a drafted script for: {angle}\n<SHORT_BREAK>\nStay tuned for more updates!"

    try:
        artifact = await run_mvp_pipeline(
            session=session,
            settings=settings,
            title=title_stripped,
            script_markdown=script_markdown,
            language="en",
            persona="operator"
        )
    except Exception as e:
        logger.error(f"Failed to create project from trend: {e}")
        raise WebException(detail=f"Failed to run pipeline: {str(e)}", status_code=500, back_link="/")
    
    artifact.genre = "tech"
    artifact.decision_log.append(
        DecisionLogEntry(
            action="trend_script_created",
            actor="lava_brain",
            note=f"Project generated from AI Trend Research angle: {angle}"
        )
    )
    await run_in_threadpool(save_project, settings, artifact)
    
    return RedirectResponse(url=f"/projects/{artifact.project_id}/view", status_code=303)


@router.post("/projects/{project_id}/split")
def split_project_script(
    project_id: str,
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    try:
        shorts = split_script_into_shorts(artifact, settings)
        # Register generated shorts in the database
        for s in shorts:
            save_project(settings, s)
        artifact.decision_log.append(
            DecisionLogEntry(
                action="shorts_splitting_completed",
                actor="local",
                note=f"Successfully split script into {len(shorts)} Shorts subprojects."
            )
        )
    except Exception as e:
        artifact.decision_log.append(
            DecisionLogEntry(
                action="shorts_splitting_failed",
                actor="local",
                note=f"Splitting failed: {str(e)}"
            )
        )
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/optimize-packaging")
async def optimize_project_packaging(
    project_id: str,
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = await run_in_threadpool(load_project, settings, project_id)
    
    topic_research = {
        "primary_keyword": artifact.title,
        "suggested_angles": [artifact.title],
        "cpm_tier": "medium",
        "search_intent": "video search"
    }
    
    try:
        pkg_data = await run_packaging(artifact.approved_script_markdown or "", topic_research)
        artifact.video_packaging = pkg_data
        
        from pipeline.stages.packaging_generator import write_upload_package
        project_dir = ensure_project_dir(settings.data_dir, project_id)
        artifact.upload_package_path = await run_in_threadpool(write_upload_package, project_dir, artifact)
        
        artifact.decision_log.append(
            DecisionLogEntry(
                action="packaging_optimized",
                actor="lava_brain",
                note="YouTube titles, thumbnail prompt, and descriptions successfully optimized by LAVA Brain."
            )
        )
    except Exception as e:
        artifact.decision_log.append(
            DecisionLogEntry(
                action="packaging_optimization_failed",
                actor="lava_brain",
                note=f"Packaging optimization failed: {str(e)}"
            )
        )
    artifact.touch()
    await run_in_threadpool(save_project, settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/metrics")
def update_project_metrics(
    project_id: str,
    ctr: float = Form(...),
    avd: float = Form(...),
    rpm: float = Form(...),
    decision: str = Form(...),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    scale_decision = ScaleDecision.UNKNOWN
    if decision == "scale":
        scale_decision = ScaleDecision.SCALE
    elif decision == "iterate":
        scale_decision = ScaleDecision.ITERATE
    elif decision == "kill":
        scale_decision = ScaleDecision.KILL
        
    metrics = MetricsDecision(
        project_id=project_id,
        ctr=ctr,
        average_view_duration_seconds=avd,
        rpm=rpm,
        decision=scale_decision,
        notes="Metrics successfully feedback to pipeline."
    )
    artifact.metrics_decision = metrics
    artifact.decision_log.append(
        DecisionLogEntry(
            action="metrics_feedback_registered",
            actor="local",
            note=f"Feedback: CTR={ctr}%, AVD={avd}s, RPM=${rpm}, Decision={decision}."
        )
    )
    artifact.touch()
    save_project(settings, artifact)
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/metrics/youtube")
def sync_youtube_metrics(
    project_id: str,
    video_id: str = Form(...),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    secrets_dir = project_root / "secrets"
    
    from pipeline.adapters.platforms.youtube_analytics import fetch_video_metrics
    from pipeline.stages.metrics_importer import import_metrics_from_json
    
    try:
        metrics_data = fetch_video_metrics(video_id, secrets_dir)
        if not metrics_data:
            raise WebException(
                detail=f"Failed to fetch YouTube metrics for Video ID '{video_id}'. "
                       "Please ensure Google API credentials are set up in the secrets/.env file and the Video ID is correct.",
                back_link=f"/projects/{project_id}/view"
            )
            
        mapped_data = {
            "project_id": project_id,
            "window": "youtube_api",
            "ctr": metrics_data.get("impressions_ctr"),
            "average_view_duration_seconds": metrics_data.get("average_view_duration_seconds"),
            "rpm": metrics_data.get("estimated_rpm"),
            "notes": f"Synced from YouTube API with Video ID: {video_id}."
        }
        
        # Calculate video duration from cues
        video_duration_seconds = 60.0
        if artifact.cue_ledger and artifact.cue_ledger.cues:
            video_duration_seconds = artifact.cue_ledger.cues[-1].end_ms / 1000.0
        mapped_data["video_duration_seconds"] = video_duration_seconds
        
        artifact = import_metrics_from_json(mapped_data, artifact, settings)
        save_project(settings, artifact)
        
    except WebException as e:
        return error_page(
            title="YouTube Analytics Error",
            message=e.detail,
            back_link=e.back_link
        )
    except Exception as e:
        logger.error(f"Failed to sync YouTube metrics: {e}")
        return error_page(
            title="YouTube Sync Failed",
            message=f"An unexpected error occurred during YouTube metrics sync: {str(e)}",
            back_link=f"/projects/{project_id}/view"
        )
        
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


@router.post("/projects/{project_id}/metrics/csv")
def import_csv_metrics(
    project_id: str,
    csv_path: str = Form(...),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    artifact = load_project(settings, project_id)
    path = Path(csv_path)
    
    from pipeline.stages.metrics_importer import import_metrics_from_csv
    
    try:
        if not path.exists():
            raise WebException(
                detail=f"CSV file not found at path: {csv_path}",
                back_link=f"/projects/{project_id}/view"
            )
            
        artifact = import_metrics_from_csv(path, artifact, settings)
        save_project(settings, artifact)
        
    except WebException as e:
        return error_page(
            title="CSV Import Error",
            message=e.detail,
            back_link=e.back_link
        )
    except Exception as e:
        logger.error(f"Failed to import CSV metrics: {e}")
        return error_page(
            title="CSV Import Failed",
            message=f"An unexpected error occurred during CSV import: {str(e)}",
            back_link=f"/projects/{project_id}/view"
        )
        
    return RedirectResponse(url=f"/projects/{project_id}/view", status_code=303)


def severity_class(severity: str) -> str:
    if severity == "blocker":
        return "block"
    if severity == "warning":
        return "warn"
    return "ok"


def format_ms(ms: int) -> str:
    seconds = ms // 1000
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


VARIANT_STYLES = """
    /* 候選評分：影片與評分欄位並排，評分時看得到影片。 */
    .shot-heading { display: block; margin: 18px 0 8px; font-size: 13px; }
    .variant-card {
      display: grid; gap: 16px; align-items: start;
      grid-template-columns: minmax(220px, 300px) minmax(280px, 1fr) minmax(200px, 260px);
      border: 1px solid var(--line); border-radius: 8px;
      padding: 14px; margin-bottom: 12px; background: var(--panel-2);
    }
    .variant-card.selected { border-color: #4c7a52; }
    .variant-video {
      width: 100%; max-height: 300px; border-radius: 6px;
      background: #0d1219; display: block;
    }
    .variant-video.empty {
      height: 160px; display: flex; align-items: center;
      justify-content: center; color: var(--muted); font-size: 12px;
    }
    .variant-facts { display: grid; gap: 2px; margin-top: 8px; font-size: 12px; }
    .variant-facts > div {
      display: flex; justify-content: space-between; gap: 8px;
      padding: 2px 0; border-bottom: 1px solid var(--line);
    }
    .score-head { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
    .score-head .muted { font-size: 11px; }
    .score-grid {
      display: grid; gap: 8px;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    }
    .score-field { display: flex; flex-direction: column; gap: 3px; }
    .score-field label { font-size: 12px; display: flex; gap: 6px; align-items: baseline; }
    .score-field input { width: 100%; }
    .score-hint { color: var(--muted); font-size: 10px; }
    .usable-line {
      display: flex; align-items: center; gap: 8px;
      margin: 10px 0; font-size: 13px;
    }
    .usable-line input { width: auto; }
    .variant-actions { display: grid; gap: 10px; }
    .action-block {
      border: 1px solid var(--line); border-radius: 6px; padding: 10px;
    }
    .action-block strong { font-size: 12px; display: block; margin-bottom: 2px; }
    .action-block p { font-size: 11px; margin: 0 0 8px; line-height: 1.5; }
    .action-block input { font-size: 12px; }
    @media (max-width: 1100px) {
      .variant-card { grid-template-columns: minmax(200px, 260px) 1fr; }
      .variant-actions { grid-column: 1 / -1; grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 720px) {
      .variant-card { grid-template-columns: 1fr; }
      .variant-actions { grid-template-columns: 1fr; }
    }
"""


def styles() -> str:
    return VARIANT_STYLES + """
    :root {
      color-scheme: dark;
      --bg: #121311;
      --panel: #1a1c19;
      --panel-2: #22251f;
      --panel-3: #292d25;
      --line: #3a4037;
      --text: #f4f2ec;
      --muted: #aaa99f;
      --accent: #37b67a;
      --accent-strong: #75dfa7;
      --amber: #d7a942;
      --danger: #df6761;
      --cyan: #58b9ce;
      --input: #0d0e0c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Microsoft JhengHei", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    a { color: inherit; }
    h1, h2, p { margin: 0; }
    h1 { font-size: 28px; line-height: 1.2; }
    h2 { font-size: 16px; line-height: 1.3; }
    .global-nav {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 48px;
      padding: 0 16px;
      background: #151713;
      border-bottom: 1px solid var(--line);
    }
    .nav-brand {
      color: var(--accent-strong);
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }
    .nav-links {
      display: flex;
      align-items: center;
      gap: 8px;
      justify-content: flex-end;
    }
    .nav-links a {
      min-height: 32px;
      display: inline-flex;
      align-items: center;
      padding: 0 10px;
      border: 1px solid transparent;
      border-radius: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }
    .nav-links a:hover, .nav-links a.active {
      border-color: var(--line);
      background: var(--panel-2);
      color: var(--text);
    }
    .app-shell {
      display: grid;
      gap: 16px;
      min-height: calc(100vh - 48px);
      padding: 16px;
    }
    .topbar, .project-hero, .panel, .stat-card, .create-pane, .list-pane {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .topbar, .project-hero {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      padding: 18px;
    }
    .eyebrow {
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
      text-transform: uppercase;
    }
    .overview-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .stat-card {
      min-height: 92px;
      padding: 14px;
      display: grid;
      gap: 7px;
      border-left: 4px solid var(--accent);
    }
    .stat-card span, .stat-card small, .muted { color: var(--muted); }
    .stat-card strong { font-size: 24px; }
    .workspace {
      display: grid;
      grid-template-columns: minmax(360px, 520px) minmax(0, 1fr);
      gap: 16px;
    }
    .create-pane, .list-pane, .panel { padding: 16px; }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .project-form { display: grid; gap: 12px; }
    .lava-binding-form {
      display: grid;
      grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) auto;
      gap: 8px;
      align-items: center;
      margin-bottom: 6px;
    }
    .field { display: grid; gap: 6px; }
    label { color: var(--muted); font-size: 12px; }
    input, textarea, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--input);
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
      outline: none;
    }
    input:focus, textarea:focus, select:focus { border-color: var(--accent); }
    textarea {
      min-height: 360px;
      resize: vertical;
      line-height: 1.5;
      font-family: Consolas, "Microsoft JhengHei", monospace;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    button, .button-link, .ghost-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      text-decoration: none;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      border-color: #2e8d60;
      background: var(--accent);
      color: #07140e;
    }
    button.danger {
      border-color: #8a3935;
      background: #3a1d1c;
      color: #ffd2cd;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.42;
    }
    .text-link {
      color: var(--muted);
      text-decoration: none;
      font-weight: 700;
    }
    .pill, .status {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      font-size: 12px;
      white-space: nowrap;
    }
    .status.ok { color: #84e8ad; border-color: #2c7a55; }
    .status.warn { color: #f0c86b; border-color: #7b6429; }
    .status.block { color: #ff9b95; border-color: #884141; }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #151713;
    }
    .mono { font-family: Consolas, monospace; font-size: 12px; }
    .empty { text-align: center; padding: 28px 8px; }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }
    .stepper {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      padding: 0;
      margin: 0;
      list-style: none;
    }
    .stepper li {
      position: relative;
      display: grid;
      grid-template-columns: 30px 1fr;
      align-items: center;
      gap: 9px;
      min-height: 56px;
      padding: 10px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .stepper span {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: var(--panel-3);
      color: var(--muted);
      font-weight: 700;
    }
    .stepper .done span, .stepper .current span { background: var(--accent); color: #07140e; }
    .stepper .blocked span { background: var(--danger); color: #160706; }
    .stepper .current, .stepper .blocked { border-color: var(--accent); }
    .stepper .todo { opacity: 0.7; }
    .project-status-stack {
      display: grid;
      gap: 7px;
      align-items: start;
    }
    .status-minis {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .status-mini {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 34px;
      min-height: 22px;
      padding: 0 6px;
      border-radius: 5px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--muted);
      font-family: Consolas, monospace;
      font-size: 11px;
      font-weight: 700;
    }
    .status-mini.ok { color: #84e8ad; border-color: #2c7a55; }
    .status-mini.warn { color: #f0c86b; border-color: #7b6429; }
    .status-mini.block { color: #ff9b95; border-color: #884141; }
    .status-flow-panel {
      background: var(--panel-2);
      border-color: #315f48;
    }
    .status-pipeline {
      display: grid;
      grid-template-columns: repeat(6, minmax(118px, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }
    .status-node {
      min-height: 74px;
      display: grid;
      gap: 6px;
      align-content: center;
      padding: 10px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--panel);
    }
    .status-node span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .status-node strong {
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .status-node.ok { border-color: #2c7a55; }
    .status-node.warn { border-color: #7b6429; }
    .status-node.block { border-color: #884141; }
    .action-box {
      display: grid;
      gap: 6px;
      padding: 12px 14px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--bg);
      line-height: 1.5;
    }
    .review-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(340px, 0.8fr);
      gap: 16px;
      align-items: start;
    }
    .main-col, .side-col {
      display: grid;
      gap: 16px;
    }
    video {
      width: 100%;
      max-height: 520px;
      background: #000;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .cue-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
      gap: 8px;
    }
    .cue-block {
      min-height: 86px;
      display: grid;
      gap: 5px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      border-top: 4px solid var(--muted);
    }
    .cue-block.generated_image { border-top-color: var(--accent); }
    .cue-block.broll { border-top-color: var(--amber); }
    .cue-block.screencast { border-top-color: var(--cyan); }
    .cue-block strong { font-size: 13px; overflow-wrap: anywhere; }
    .cue-block small { color: var(--muted); }
    .gate-stack, .asset-grid {
      display: grid;
      gap: 10px;
    }
    .gate-form {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .gate-form input[type="hidden"] { display: none; }
    .gate-form button:only-child { grid-column: 1 / -1; }
    .asset-card {
      display: grid;
      grid-template-columns: 96px minmax(0, 1fr);
      gap: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }
    .asset-thumb {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 96px;
      border-radius: 6px;
      background: #10110f;
      border: 1px solid var(--line);
      color: var(--muted);
      text-align: center;
      font-size: 12px;
      padding: 8px;
    }
    .asset-thumb.generated_image { box-shadow: inset 0 0 0 2px rgba(55, 182, 122, 0.35); }
    .asset-thumb.broll { box-shadow: inset 0 0 0 2px rgba(215, 169, 66, 0.35); }
    .asset-thumb.screencast { box-shadow: inset 0 0 0 2px rgba(88, 185, 206, 0.35); }
    .asset-body { display: grid; gap: 8px; min-width: 0; }
    .asset-title {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
    }
    .asset-card p {
      color: var(--text);
      line-height: 1.45;
    }
    .asset-form {
      display: grid;
      grid-template-columns: minmax(110px, 0.7fr) minmax(120px, 1fr) auto;
      gap: 8px;
    }
    .transcript-form {
      display: grid;
      gap: 12px;
    }
    .transcript-form textarea {
      min-height: 180px;
    }
    .asr-run-box {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }
    .asr-run-box > div {
      display: grid;
      gap: 4px;
    }
    .visual-shot-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .visual-shot {
      display: grid;
      gap: 7px;
      min-height: 150px;
      padding: 10px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--panel-2);
    }
    .visual-shot p {
      color: var(--muted);
      line-height: 1.45;
      font-size: 12px;
    }
    .warning-list {
      display: grid;
      gap: 8px;
      margin: 10px 0 0 0;
      padding: 0;
      list-style: none;
    }
    .warning-list li {
      padding: 8px;
      border-radius: 6px;
      background: #151713;
      border: 1px solid var(--line);
      line-height: 1.45;
    }
    @media (max-width: 1100px) {
      .workspace, .review-layout, .overview-grid, .stepper, .status-pipeline { grid-template-columns: 1fr; }
      .project-hero, .topbar { display: grid; }
      .actions { justify-content: flex-start; }
    }
    @media (max-width: 720px) {
      .split, .asset-card, .asset-form, .gate-form { grid-template-columns: 1fr; }
      .lava-binding-form { grid-template-columns: 1fr; }
      .global-nav { align-items: flex-start; flex-direction: column; padding: 10px 12px; }
      .nav-links { flex-wrap: wrap; justify-content: flex-start; }
      h1 { font-size: 23px; }
    }
    """
