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

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from app.deps import settings_dep
from pipeline.benchmark import (
    attempts,
    attribution,
    builder,
    job_view,
    score_sheet,
    target,
    v1_pack,
    workflow,
)
from pipeline.db import engine
from pipeline.models.qc import ContinuityQC, VariantQC
from pipeline.models.variant import AssetVariant, CapabilityJob
from pipeline.project_store import load_project
from pipeline.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

PROJECT_ID = v1_pack.BENCHMARK_PROJECT_ID


def sheets_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "benchmark" / "v1" / "sheets"


def _control_button(
    control: workflow.ControlState, form_action: str = "", primary: bool = False
) -> str:
    """把 ControlState 畫成按鈕。停用原因直接顯示，不讓使用者自己猜。

    label 與 disabled_reason 來自 workflow，AI 助手讀的是同一份資料，
    因此助手說的話不會和畫面上的狀態互相矛盾。
    """
    cls = "primary" if primary else "ghost-button"
    attrs = (
        f'data-control-id="{escape(control.control_id)}" '
        f'data-danger="{escape(control.danger_level)}"'
    )
    if control.enabled and control.href:
        body = (
            f'<a class="{cls}" href="{escape(control.href)}" {attrs}>'
            f"{escape(control.label)}</a>"
        )
    elif control.enabled and form_action:
        body = (
            f'<form method="post" action="{escape(form_action)}" class="inline-form">'
            f'<button class="{cls}" type="submit" {attrs}>'
            f"{escape(control.label)}</button></form>"
        )
    elif control.enabled:
        body = f'<span class="{cls}" {attrs}>{escape(control.label)}</span>'
    else:
        reason = control.disabled_reason or "目前無法執行"
        body = (
            f'<button class="{cls}" type="button" disabled {attrs} '
            f'title="{escape(reason)}">{escape(control.label)}</button>'
            f'<span class="disabled-why">{escape(reason)}</span>'
            f'<a href="#" class="ask-ai" '
            f'data-ask-control="{escape(control.control_id)}" '
            f'data-ask-ai="「{escape(control.label)}」為什麼不能按？">'
            "Ask AI</a>"
        )
    return body


# control_id 對應的 POST 端點。只有需要送出表單的控制項才列在這裡。
CONTROL_ACTIONS = {
    "benchmark.build": "/benchmark/build",
    "benchmark.sync": "/benchmark/sync",
}


def _step_block(step: workflow.StepState, current_step: int = 0) -> str:
    icons = {"done": "✓", "active": "→", "todo": "○", "blocked": "!"}
    blockers = ""
    if step.blockers:
        items = "".join(f"<li>{escape(item)}</li>" for item in step.blockers)
        # 卡住的步驟旁邊放 Ask AI：使用者看到原因後最常問的就是
        # 「所以我要做什麼」，讓他不必自己把問題打出來。
        ask = (
            f'<a href="#" class="ask-ai" data-ask-ai="'
            f'第 {step.number} 步「{escape(step.title)}」為什麼卡住？我該怎麼做？"'
            f">Ask AI</a>"
        )
        blockers = f"<ul class='bm-list'>{items}</ul>{ask}"

    # 強調色只給目前這一步的第一個可用操作。原本固定給 Build，於是
    # 步驟 3 完成後，整頁最醒目的按鈕仍是「重新建立派工」，
    # 而使用者該做的其實是下一步。最大的按鈕要指向下一步。
    highlight = ""
    if step.number == current_step:
        highlight = next(
            (
                item.control_id
                for item in step.controls
                if item.enabled and not item.href
            ),
            next(
                (item.control_id for item in step.controls if item.enabled),
                "",
            ),
        )

    buttons = "".join(
        _control_button(
            control,
            CONTROL_ACTIONS.get(control.control_id, ""),
            primary=bool(highlight) and control.control_id == highlight,
        )
        for control in step.controls
    )

    return f"""
    <div class="bm-step bm-{escape(step.state)}" data-step="{step.number}"
         data-step-key="{escape(step.key)}">
      <div class="bm-step-head">
        <span class="bm-icon">{icons.get(step.state, '○')}</span>
        <strong>{step.number}. {escape(step.title)}</strong>
      </div>
      <div class="bm-detail">{escape(step.summary)}{blockers}</div>
      <div class="bm-cta">{buttons}</div>
    </div>
    """


def _target_table(state: workflow.WorkflowState) -> str:
    rows = "".join(
        f"<tr><td class='mono'>{escape(item.target_id)}</td>"
        f"<td>{escape(item.provider)}</td>"
        f"<td class='mono'>{escape(item.model_id)}</td>"
        f"<td class='mono'>{escape(item.model_version or '—')}</td>"
        f"<td>{escape(item.ui_label or '—')}</td>"
        f"<td>{'待確認' if item.provisional else '已確認'}</td>"
        f"<td>{item.job_count}</td><td>{item.variant_count}</td></tr>"
        for item in state.targets
    )
    return (
        "<table class='data-table'><thead><tr>"
        "<th>Target</th><th>Provider</th><th>Model</th><th>Version</th>"
        "<th>平台選項名稱</th><th>狀態</th><th>派工</th><th>候選</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


@router.get("/benchmark", response_class=HTMLResponse)
def benchmark_console(settings: Settings = Depends(settings_dep)) -> str:
    """V1 Benchmark 控制台。狀態全部由 workflow.collect 推導。"""
    from app.routes.web import page

    state = workflow.collect(settings)

    degraded = "".join(
        f'<div class="bm-warn">載入部分資料時發生問題：{escape(item)}</div>'
        for item in state.degraded
    )
    steps = "".join(
        _step_block(step, state.current_step) for step in state.steps
    )

    return page(
        title="V1 Benchmark",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark"
                 data-step="{state.current_step}"
                 data-entity-type="benchmark_console">
          <header class="project-hero">
            <div>
              <h1>V1 Benchmark</h1>
              <p class="muted">
                比較 {len(state.targets)} 個對象在 {state.shots_total} 顆固定鏡頭上的表現。
                真實生成需在各平台手動完成，本系統不呼叫任何影片 API。
              </p>
              <p class="next-action">下一步：{escape(state.next_action())}</p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/projects/{PROJECT_ID}/view">專案詳情</a>
            </div>
          </header>
          {degraded}
          <div class="bm-steps">{steps}</div>
          <section class="panel">
            <div class="section-head"><h2>比較對象</h2></div>
            {_target_table(state)}
          </section>
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
          .bm-list {{ margin: 8px 0; padding-left: 18px; color: #e8b; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .next-action {{ color: #7ec8e2; font-size: 14px; margin-top: 6px; }}
          .disabled-why {{ color: #8a94a6; font-size: 12px; }}
          .ask-ai {{ color: #7ec8e2; font-size: 12px; text-decoration: underline; }}
          button[disabled] {{ opacity: 0.5; cursor: not-allowed; }}
          .bm-steps table {{ margin-top: 10px; }}
          @media (max-width: 720px) {{
            .bm-cta {{ flex-direction: column; align-items: stretch; }}
          }}
        </style>
        """,
    )


# --------------------------------------------------------------------------
# Step 1: 素材上傳
# --------------------------------------------------------------------------

def _asset_slot(status: builder.AssetStatus, flash: dict | None) -> str:
    """一格素材。預覽、規格、驗證結果與上傳表單都在同一格內。"""
    rejected = bool(flash and flash.get("asset_id") == status.asset_id
                    and not flash.get("ok"))
    if rejected:
        # 上傳被退回時，儲存的仍是先前那張合格的圖。若照舊顯示
        # 「已通過驗證」，使用者會以為剛才那張成功了。
        badge = '<span class="slot-badge bad">剛才的上傳被退回</span>'
    elif status.valid:
        badge = '<span class="slot-badge ok">已通過驗證</span>'
    elif status.present:
        badge = '<span class="slot-badge bad">未通過驗證</span>'
    else:
        badge = '<span class="slot-badge todo">尚未上傳</span>'

    preview = (
        f'<img class="slot-preview" alt="{escape(status.asset_id)} 預覽" '
        f'src="/benchmark/assets/{escape(status.asset_id)}/preview" />'
        if status.present
        else '<div class="slot-preview empty">尚無圖片</div>'
    )

    # 使用者要看的是解析度與比例。SHA256 是內容識別用的內部欄位，
    # 放在同一組規格裡會和真正要判斷的資訊搶注意力。
    spec_rows = "".join(
        f"<div><span class='muted'>{escape(label)}</span><b>{escape(value)}</b></div>"
        for label, value in (
            ("解析度", status.dimensions or "—"),
            ("比例", status.ratio or "—"),
            ("要求", "9:16 直式" if status.require_vertical else "不限比例"),
        )
    )
    if status.file_hash:
        spec_rows += (
            "<details class='slot-advanced'><summary>進階：內容識別</summary>"
            f"<span class='mono'>SHA256 {escape(status.file_hash[:24])}…</span>"
            "</details>"
        )

    problems = ""
    if status.problems:
        problems = (
            "<ul class='slot-problems'>"
            + "".join(f"<li>{escape(item)}</li>" for item in status.problems)
            + "</ul>"
        )

    note = ""
    if flash and flash.get("asset_id") == status.asset_id:
        cls = "ok" if flash.get("ok") else "bad"
        note = f"<p class='slot-flash {cls}'>{escape(flash.get('message', ''))}</p>"

    return f"""
    <div class="asset-slot" id="{escape(status.asset_id)}">
      <div class="slot-head">
        <strong>{escape(builder.asset_label(status.description))}</strong>
        {badge}
      </div>
      <p class="slot-desc">{escape(status.description)}</p>
      {preview}
      <div class="slot-specs">{spec_rows}</div>
      {problems}
      {note}
      <form method="post" enctype="multipart/form-data"
            action="/benchmark/assets/{escape(status.asset_id)}">
        <input type="file" name="file" accept="image/*" required />
        <button class="ghost-button" type="submit">
          {"更換" if status.present else "上傳"}
        </button>
      </form>
    </div>
    """


@router.get("/benchmark/assets", response_class=HTMLResponse)
def assets_page(
    slot: str = "",
    ok: str = "",
    message: str = "",
    settings: Settings = Depends(settings_dep),
) -> str:
    """素材上傳。使用者不需要知道檔案最後放在伺服器的哪裡。"""
    from app.routes.web import page

    report = builder.check_assets(settings)
    flash = (
        {"asset_id": slot, "ok": ok == "1", "message": message} if slot else None
    )
    ready = len(report.statuses) - len(report.missing) - len(report.invalid)

    slots = "".join(_asset_slot(item, flash) for item in report.statuses)
    banner = (
        '<div class="bm-ok">素材已全部就緒。</div>'
        if report.ready
        else f'<div class="bm-warn">還有 {len(report.statuses) - ready} 格未通過驗證。</div>'
    )

    return page(
        title="Benchmark 參考素材",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/assets"
                 data-step="1" data-entity-type="benchmark_assets">
          <header class="project-hero">
            <div>
              <h1>Step 1 · 參考素材</h1>
              <p class="muted">
                {ready}/{len(report.statuses)} 已就緒。
                首幀會實際上傳到各平台，必須是 9:16 直式、短邊至少 512px 的
                可開啟圖片；角色設定圖僅供你製作首幀時對照，不限比例。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>
          {banner}
          <div class="asset-grid">{slots}</div>
        </section>
        <style>
          .asset-grid {{ display: grid; gap: 14px; margin-top: 16px;
                         grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }}
          .asset-slot {{ border: 1px solid #2a3344; border-radius: 8px;
                         padding: 12px; background: #131a26; }}
          .slot-head {{ display: flex; justify-content: space-between;
                        gap: 8px; align-items: flex-start; margin-bottom: 8px;
                        font-size: 13px; line-height: 1.4; }}
          .slot-badge {{ font-size: 11px; padding: 2px 6px; border-radius: 4px;
                         white-space: nowrap; }}
          .slot-badge.ok {{ background: #1f4d33; color: #7ee2a8; }}
          .slot-badge.bad {{ background: #4d2020; color: #e88; }}
          .slot-badge.todo {{ background: #2a3344; color: #8a94a6; }}
          .slot-preview {{ width: 100%; height: 150px; object-fit: contain;
                           background: #0d1219; border-radius: 6px; }}
          .slot-preview.empty {{ display: flex; align-items: center;
                                 justify-content: center; color: #55607a;
                                 font-size: 12px; }}
          .slot-desc {{ font-size: 11px; color: #8a94a6; line-height: 1.5;
                        margin: 0 0 8px; }}
          .slot-specs {{ display: grid; gap: 2px; margin: 8px 0; font-size: 12px; }}
          .slot-specs div {{ display: flex; justify-content: space-between; gap: 8px; }}
          .slot-problems {{ margin: 6px 0; padding-left: 16px; color: #e88;
                            font-size: 12px; line-height: 1.5; }}
          .slot-flash {{ font-size: 12px; margin: 6px 0; }}
          .slot-flash.ok {{ color: #7ee2a8; }}
          .slot-flash.bad {{ color: #e88; }}
          .asset-slot form {{ display: flex; gap: 6px; align-items: center;
                              flex-wrap: wrap; }}
          .asset-slot input[type=file] {{ font-size: 11px; max-width: 100%; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .bm-ok {{ color: #7ee2a8; margin: 8px 0; }}
        </style>
        """,
    )


@router.get("/benchmark/assets/{asset_id}/preview")
def asset_preview(asset_id: str, settings: Settings = Depends(settings_dep)):
    """回傳素材縮圖。路徑由 asset_id 查表決定，不接受任意檔名。"""
    from app.routes.web import WebException

    try:
        path = builder.asset_path(settings, asset_id)
    except ValueError as error:
        raise WebException(detail=str(error), back_link="/benchmark/assets") from error
    if not path.exists():
        raise WebException(
            detail="此欄位尚未上傳圖片", back_link="/benchmark/assets"
        )
    return FileResponse(path)


@router.post("/benchmark/assets/{asset_id}")
async def upload_asset(
    asset_id: str,
    file: UploadFile = File(...),
    settings: Settings = Depends(settings_dep),
):
    """上傳單一素材。驗證不過就不落地，畫面仍保留先前那張。"""
    from urllib.parse import quote

    from app.routes.web import WebException

    payload = await file.read()
    try:
        result = await run_in_threadpool(
            builder.save_asset_upload,
            settings,
            asset_id,
            file.filename or "upload.png",
            payload,
        )
    except ValueError as error:
        raise WebException(detail=str(error), back_link="/benchmark/assets") from error

    if result.ok:
        message = f"已更新（{result.dimensions}，{result.ratio}）"
    else:
        message = "；".join(result.problems) or "驗證未通過"

    return RedirectResponse(
        url=(
            f"/benchmark/assets?slot={quote(asset_id)}"
            f"&ok={'1' if result.ok else '0'}&message={quote(message)}"
            f"#{quote(asset_id)}"
        ),
        status_code=303,
    )


# --------------------------------------------------------------------------
# Step 2: 比較對象版本確認
# --------------------------------------------------------------------------

def _target_card(
    item: target.BenchmarkTarget, job_count: int, variant_count: int, flash: dict | None
) -> str:
    """一個比較對象的確認表單。"""
    from pipeline.capability.provider_spec import get_provider

    badge = (
        '<span class="slot-badge todo">待確認</span>'
        if item.provisional
        else '<span class="slot-badge ok">已確認</span>'
    )

    options = "".join(
        f'<option value="{escape(model_id)}"'
        f'{" selected" if model_id == item.model_id else ""}>'
        f"{escape(model_id)}</option>"
        for model_id in target.models_for_provider(item.provider)
    ) or f'<option value="{escape(item.model_id)}" selected>{escape(item.model_id)}</option>'

    rebuild_warning = ""
    if job_count:
        rebuild_warning = f"""
        <p class="slot-flash bad">
          此對象已建立 {job_count} 份派工、匯入 {variant_count} 支候選。
          修改版本後必須重新 Build，新舊派工才不會混在一起。
          既有影片仍會保留它當初生成時的版本，不會被改寫成新版本。
        </p>
        """

    note = ""
    if flash and flash.get("target_id") == item.target_id:
        cls = "ok" if flash.get("ok") else "bad"
        note = f"<p class='slot-flash {cls}'>{escape(flash.get('message', ''))}</p>"

    spec = get_provider(item.provider)
    platform_link = (
        f'<a class="ghost-button" target="_blank" rel="noreferrer" '
        f'href="{escape(spec.console_url)}">開啟平台</a>'
        if spec is not None and getattr(spec, "console_url", None)
        else ""
    )

    return f"""
    <section class="panel target-card" id="{escape(item.target_id)}">
      <div class="section-head">
        <h2>{escape(item.target_id)} {badge}</h2>
        <span class="muted mono">{escape(item.provider)}</span>
      </div>
      {rebuild_warning}
      {note}
      <form method="post" action="/benchmark/targets/{escape(item.target_id)}">
        <div class="split">
          <div class="field">
            <label>Provider</label>
            <input value="{escape(item.provider)}" disabled />
          </div>
          <div class="field">
            <label for="model_{escape(item.target_id)}">Catalog model</label>
            <select id="model_{escape(item.target_id)}" name="model_id">{options}</select>
            <p class="field-help">本系統內部的模型代號，不是平台上的名稱。</p>
          </div>
        </div>
        <p class="field-help section-help">
          下面兩欄請照抄平台畫面上的字，不要自己translate或簡寫。
          日後要回答「當時用的是哪一版」，靠的就是這兩欄。
        </p>
        <div class="split">
          <div class="field">
            <label for="label_{escape(item.target_id)}">平台實際顯示名稱（UI label）</label>
            <input id="label_{escape(item.target_id)}" name="ui_label"
                   value="{escape(item.ui_label or '')}"
                   placeholder="例如 Kling 1.6 Standard" />
            <p class="field-help">
              在平台選模型的那個下拉選單或卡片上，這個選項寫的是什麼。
            </p>
          </div>
          <div class="field">
            <label for="ver_{escape(item.target_id)}">Model version</label>
            <input id="ver_{escape(item.target_id)}" name="model_version"
                   value="{escape(item.model_version or '')}"
                   placeholder="例如 1.6" />
            <p class="field-help">
              平台標示的版本字串。找不到獨立的版本欄位時，
              填選項名稱裡的版本部分即可。
            </p>
          </div>
        </div>
        <label class="checkline">
          <input type="checkbox" name="confirmed" value="1"
                 {"checked" if not item.provisional else ""} />
          我已在平台上確認這個版本確實存在
        </label>
        <div class="bm-cta">
          <button class="primary" type="submit">儲存</button>
          {platform_link}
        </div>
      </form>
    </section>
    """


@router.get("/benchmark/targets", response_class=HTMLResponse)
def targets_page(
    target_id: str = "",
    ok: str = "",
    message: str = "",
    settings: Settings = Depends(settings_dep),
) -> str:
    """比較對象確認。使用者不需要手改 YAML。"""
    from app.routes.web import page

    registry = target.targets()
    state = workflow.collect(settings)
    counts = {item.target_id: item for item in state.targets}
    flash = (
        {"target_id": target_id, "ok": ok == "1", "message": message}
        if target_id
        else None
    )

    cards = "".join(
        _target_card(
            item,
            counts[item.target_id].job_count if item.target_id in counts else 0,
            counts[item.target_id].variant_count if item.target_id in counts else 0,
            flash,
        )
        for item in registry.targets
    )
    pending = registry.provisional_targets
    banner = (
        '<div class="bm-ok">所有比較對象都已確認版本，可以建立正式派工。</div>'
        if not pending
        else (
            f'<div class="bm-warn">{len(pending)} 個對象尚未確認：'
            f'{escape(", ".join(item.target_id for item in pending))}。'
            "正式 Build 在全部確認前會被鎖住。</div>"
        )
    )

    return page(
        title="Benchmark 比較對象",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/targets"
                 data-step="2" data-entity-type="benchmark_targets">
          <header class="project-hero">
            <div>
              <h1>Step 2 · 確認平台版本</h1>
              <p class="muted">
                只記「這是 Kling 生的」，日後無從得知當時選的是哪一版。
                請登入各平台，把介面上實際顯示的選項名稱與版本字串填進來。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>
          {banner}
          {cards}
        </section>
        <style>
          .target-card {{ margin-top: 14px; }}
          .field-help {{ font-size: 11px; color: #8a94a6; margin: 4px 0 0;
                         line-height: 1.5; }}
          .section-help {{ margin: 12px 0 0; }}
          .checkline {{ display: flex; align-items: center; gap: 8px;
                        margin: 10px 0; font-size: 14px; }}
          .checkline input {{ width: auto; }}
          .slot-badge {{ font-size: 11px; padding: 2px 6px; border-radius: 4px; }}
          .slot-badge.ok {{ background: #1f4d33; color: #7ee2a8; }}
          .slot-badge.todo {{ background: #2a3344; color: #8a94a6; }}
          .slot-flash {{ font-size: 13px; line-height: 1.6; margin: 8px 0; }}
          .slot-flash.ok {{ color: #7ee2a8; }}
          .slot-flash.bad {{ color: #e8b; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .bm-ok {{ color: #7ee2a8; margin: 8px 0; }}
          .bm-cta {{ display: flex; gap: 8px; align-items: center;
                     flex-wrap: wrap; margin-top: 10px; }}
        </style>
        """,
    )


@router.post("/benchmark/targets/{target_id}")
async def save_target_route(
    target_id: str,
    model_id: str = Form(""),
    model_version: str = Form(""),
    ui_label: str = Form(""),
    confirmed: str = Form(""),
    settings: Settings = Depends(settings_dep),
):
    """儲存比較對象。confirmed 未勾選時一律維持 provisional。"""
    from urllib.parse import quote

    try:
        updated = await run_in_threadpool(
            target.save_target,
            target_id,
            model_id,
            model_version,
            ui_label,
            None,
            confirmed == "1",
        )
        ok = "1"
        message = (
            f"已確認為 {updated.display}"
            if not updated.provisional
            else "已儲存，但仍標記為待確認"
        )
    except Exception as error:  # noqa: BLE001 - 驗證失敗類型不一，一律回報給使用者
        ok = "0"
        message = str(error)[:200]

    return RedirectResponse(
        url=(
            f"/benchmark/targets?target_id={quote(target_id)}"
            f"&ok={ok}&message={quote(message)}#{quote(target_id)}"
        ),
        status_code=303,
    )


@router.post("/benchmark/build")
async def benchmark_build(settings: Settings = Depends(settings_dep)):
    from app.routes.web import WebException

    # fail closed。前端也會停用按鈕，但前端不是安全邊界：
    # 直接 POST 這個路徑一樣不得產生正式派工。
    gate = await run_in_threadpool(builder.evaluate_build_gate, settings)
    if not gate.ok:
        raise WebException(
            detail="尚未滿足正式派工條件：" + gate.summary(),
            back_link="/benchmark",
            error_code=gate.codes()[0] if gate.codes() else "",
        )

    await run_in_threadpool(builder.register_assets, settings)
    artifact = await run_in_threadpool(builder.build_artifact, settings)
    await builder.dispatch_all(artifact)

    from pipeline.project_store import save_project

    await run_in_threadpool(save_project, settings, artifact)

    # 只在缺檔時建立空白表。這些寫入函式以 "w" 開檔，會整份截斷：
    # 第二次按「重新產生」就會把已記錄的嘗試與已填的分數全部清空，
    # 而且不會有任何提示。既有檔案改由 sync 補列並保留人工填寫的欄位。
    directory = sheets_dir(settings)
    await run_in_threadpool(_ensure_sheets, directory)
    await run_in_threadpool(score_sheet.sync_variant_sheet, directory, PROJECT_ID)
    await run_in_threadpool(score_sheet.sync_continuity_sheet, directory, PROJECT_ID)

    return RedirectResponse(url="/benchmark", status_code=303)


def _ensure_sheets(directory: Path) -> None:
    """建立尚不存在的評分表與嘗試紀錄。已存在者一律不動。"""
    if not (directory / score_sheet.VARIANT_SHEET).exists():
        score_sheet.write_variant_sheet(directory, None, None)
    if not (directory / score_sheet.CONTINUITY_SHEET).exists():
        score_sheet.write_continuity_sheet(directory, None)
    if not (directory / attempts.ATTEMPTS_SHEET).exists():
        attempts.write_blank_ledger(directory, None, None)


# --------------------------------------------------------------------------
# Step 3-4: Job Workbench 與以派工為脈絡的匯入
# --------------------------------------------------------------------------

@router.get("/benchmark/jobs", response_class=HTMLResponse)
def jobs_page(settings: Settings = Depends(settings_dep)) -> str:
    """所有派工一覽。每一列都能直接進到該派工的操作台。"""
    from app.routes.web import page

    try:
        views = job_view.list_jobs(PROJECT_ID)
    except Exception as error:  # noqa: BLE001
        logger.warning("job list failed: %s", error)
        views = []

    if not views:
        rows = (
            '<tr><td colspan="7" class="muted empty">'
            "尚未建立任何派工。請先完成步驟 1 與 2，再回到 Benchmark 按「建立正式 Job Packages」。"
            "</td></tr>"
        )
    else:
        # 待生成的排在前面，並且視覺上要看得出來。24 列全部一樣重時，
        # 「我現在該開哪一份」只能靠逐列讀狀態欄。
        ordered = sorted(views, key=lambda item: (item.complete, item.target_id))
        rows = "".join(
            f"<tr class='{'job-done' if item.complete else 'job-todo'}'>"
            f"<td>{'✓ 已完成' if item.complete else '待生成'}</td>"
            f"<td class='mono'>{escape(item.target_id or '—')}</td>"
            f"<td class='mono'>{escape(item.shot_id)}</td>"
            f"<td>{escape(item.scenario)}</td>"
            f"<td class='mono'>{escape(item.provider)}</td>"
            f"<td class='mono'>{escape(item.model_display)}</td>"
            f"<td>{item.variants_imported}</td>"
            f"<td><a class='{'ghost-button' if item.complete else 'primary'}' "
            f"href='/benchmark/jobs/{escape(item.job_id)}'>"
            f"{'查看' if item.complete else '開始生成'}</a></td>"
            f"</tr>"
            for item in ordered
        )

    done = sum(1 for item in views if item.complete)
    return page(
        title="Job Workbench",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/jobs"
                 data-step="4" data-entity-type="benchmark_jobs">
          <header class="project-hero">
            <div>
              <h1>Job Workbench</h1>
              <p class="muted">
                {done}/{len(views)} 份派工已有匯入的影片。
                每一份都對應平台上的一次生成，從這裡複製提示詞、下載首幀，
                生成完再回到同一頁記錄與匯入。
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>
          <section class="panel">
            <table class="data-table">
              <thead><tr><th>狀態</th><th>Target</th><th>Shot</th><th>情境</th>
              <th>平台</th><th>模型 / 版本</th><th>已匯入</th><th></th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </section>
        </section>
        <style>
          .job-done td {{ opacity: 0.55; }}
          .job-todo td:first-child {{ color: #7ec8e2; }}
        </style>
        """,
    )


def _reference_block(view) -> str:
    if not view.references:
        return "<p class='muted'>此派工沒有參考素材。</p>"
    cards = "".join(
        f"""
        <div class="ref-card">
          <img class="ref-thumb" alt="{escape(item.asset_id)}"
               src="/benchmark/jobs/{escape(view.job_id)}/reference/{escape(item.asset_id)}" />
          <div class="ref-meta">
            <strong>{escape('首幀' if item.is_first_frame else '參考')}</strong>
            <span class="muted">{escape(item.label or item.asset_id)}</span>
          </div>
          <a class="ghost-button"
             href="/benchmark/jobs/{escape(view.job_id)}/reference/{escape(item.asset_id)}?download=1">
            下載
          </a>
        </div>
        """
        for item in view.references
    )
    hashes = "".join(
        f"<div><span class='mono muted'>{escape(item.asset_id)}</span>"
        f"<span class='mono'>{escape(item.sha256 or '—')}</span></div>"
        for item in view.references
    )
    return f"""
    <div class="ref-grid">{cards}</div>
    <details class="advanced">
      <summary>進階：參考素材 SHA256</summary>
      <div class="hash-list">{hashes}</div>
    </details>
    """


@router.get("/benchmark/jobs/{job_id}", response_class=HTMLResponse)
def job_workbench(
    job_id: str,
    ok: str = "",
    message: str = "",
    settings: Settings = Depends(settings_dep),
) -> str:
    """單一派工的操作台。人工在平台生成時需要的東西都在這一頁。"""
    from app.routes.web import WebException, page
    from pipeline.capability.provider_spec import get_provider

    try:
        view = job_view.load_job(PROJECT_ID, job_id)
    except LookupError as error:
        raise WebException(detail=str(error), back_link="/benchmark/jobs") from error

    spec = get_provider(view.provider)
    platform_link = (
        f'<a class="ghost-button" target="_blank" rel="noreferrer" '
        f'href="{escape(spec.console_url)}">開啟 {escape(spec.label)}</a>'
        if spec is not None and spec.console_url
        else ""
    )

    flash = ""
    if message:
        flash = (
            f'<div class="{"bm-ok" if ok == "1" else "bm-warn"}">'
            f"{escape(message)}</div>"
        )

    provisional_warning = ""
    if view.provisional_at_dispatch:
        provisional_warning = (
            '<div class="bm-warn">這份派工建立時，該比較對象的版本尚未確認，'
            "統計時會標記為版本不明。</div>"
        )

    facts = "".join(
        f"<div><span class='muted'>{escape(label)}</span>"
        f"<b class='mono'>{escape(value)}</b></div>"
        for label, value in (
            ("情境", view.scenario or "—"),
            ("鏡頭", view.shot_id),
            ("比較對象", view.target_id or "—"),
            ("平台", view.provider),
            ("模型", view.model_id or "—"),
            ("版本", view.model_version or "未確認"),
            ("平台選項名稱", view.ui_label or "—"),
            ("片長", f"{view.duration_seconds}s" if view.duration_seconds else "—"),
            ("畫面比例", view.aspect_ratio or "—"),
            ("運鏡", view.camera or "—"),
            ("狀態", view.status),
        )
    )

    parameter_rows = "".join(
        f"<div><span class='muted mono'>{escape(str(key))}</span>"
        f"<b class='mono'>{escape(str(value))}</b></div>"
        for key, value in sorted(view.parameters.items())
    ) or "<div class='muted'>無額外參數</div>"

    shot_options = "".join(
        f'<option value="{escape(shot.shot_id)}"'
        f'{" selected" if shot.shot_id == view.shot_id else ""}>'
        f"{escape(shot.shot_id)}</option>"
        for shot in v1_pack.shots()
    )

    return page(
        title=f"Job · {view.shot_id}",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/jobs/{escape(job_id)}"
                 data-step="4" data-entity-type="capability_job"
                 data-entity-id="{escape(job_id)}">
          <header class="project-hero">
            <div>
              <h1>{escape(view.shot_id)}</h1>
              <p class="muted">
                {escape(view.target_id or '未歸屬')} ·
                {escape(view.provider)} · {escape(view.model_display)}
              </p>
            </div>
            <div class="actions">
              {platform_link}
              <a class="ghost-button" href="/benchmark/jobs">全部派工</a>
            </div>
          </header>
          {flash}
          {provisional_warning}

          <section class="panel">
            <div class="section-head"><h2>這次生成的規格</h2></div>
            <div class="fact-grid">{facts}</div>
          </section>

          <section class="panel">
            <div class="section-head">
              <h2>提示詞</h2>
              <button class="ghost-button" type="button"
                      data-copy-target="prompt-text">複製提示詞</button>
            </div>
            <pre class="prompt-box" id="prompt-text">{escape(view.prompt)}</pre>
            <div class="section-head">
              <h2>負面提示詞</h2>
              <button class="ghost-button" type="button"
                      data-copy-target="negative-text">複製</button>
            </div>
            <pre class="prompt-box" id="negative-text">{escape(view.negative_prompt or '（無）')}</pre>
            <details class="advanced">
              <summary>進階：生成參數與 request_hash</summary>
              <div class="fact-grid">{parameter_rows}</div>
              <p class="mono muted" style="margin-top:8px">
                request_hash: {escape(view.request_hash or '—')}
              </p>
              <p class="muted">
                正常流程不需要用到這串雜湊，下方的匯入表單會自動帶入。
              </p>
            </details>
          </section>

          <section class="panel">
            <div class="section-head"><h2>參考素材</h2></div>
            {_reference_block(view)}
          </section>

          <section class="panel">
            <div class="section-head"><h2>記錄這次嘗試</h2></div>
            <p class="muted">
              每按一次平台的 Generate 就記一列，失敗與取消也要記。
              只統計成功的候選會嚴重高估平台表現。
            </p>
            <form method="post" action="/benchmark/jobs/{escape(job_id)}/attempt">
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
                  <label for="output">平台的產出檔名</label>
                  <input id="output" name="output_file"
                         placeholder="成功時必填，直接填平台下載下來的檔名" />
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
                <input id="reason" name="failure_reason" placeholder="未成功時填寫" />
              </div>
              <button class="primary" type="submit">記錄這次嘗試</button>
            </form>
          </section>

          <section class="panel">
            <div class="section-head"><h2>匯入這次生成結果</h2></div>
            <p class="muted">
              選檔上傳即可。這支影片屬於哪個專案、鏡頭、比較對象與派工，
              系統已經由本頁的派工脈絡得知，不需要你填。
            </p>
            <form method="post" enctype="multipart/form-data"
                  action="/benchmark/jobs/{escape(job_id)}/import">
              <div class="field">
                <label for="clip">影片檔</label>
                <input id="clip" type="file" name="files" accept="video/*" multiple required />
              </div>
              <details class="advanced">
                <summary>進階：改派到其他鏡頭</summary>
                <div class="field">
                  <label for="shot">鏡頭</label>
                  <select id="shot" name="shot_id">{shot_options}</select>
                  <p class="muted">
                    改成與派工不同的鏡頭時會被拒絕，避免影片掛錯位置。
                  </p>
                </div>
              </details>
              <button class="primary" type="submit">匯入結果</button>
            </form>
            <p class="muted">已匯入 {view.variants_imported} 支候選。</p>
          </section>
        </section>
        <style>
          .fact-grid {{ display: grid; gap: 4px;
                        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }}
          .fact-grid div {{ display: flex; justify-content: space-between;
                            gap: 10px; font-size: 13px; padding: 3px 0;
                            border-bottom: 1px solid #1d2534; }}
          .prompt-box {{ white-space: pre-wrap; background: #0d1219;
                         border: 1px solid #2a3344; border-radius: 6px;
                         padding: 10px; font-size: 13px; line-height: 1.6;
                         margin: 6px 0 14px; }}
          .ref-grid {{ display: grid; gap: 12px;
                       grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
          .ref-card {{ border: 1px solid #2a3344; border-radius: 8px;
                       padding: 10px; background: #0d1219; }}
          .ref-thumb {{ width: 100%; height: 140px; object-fit: contain; }}
          .ref-meta {{ display: flex; flex-direction: column; gap: 2px;
                       font-size: 12px; margin: 6px 0; }}
          .advanced {{ margin-top: 12px; }}
          .advanced summary {{ cursor: pointer; color: #8a94a6; font-size: 13px; }}
          .hash-list div {{ display: flex; justify-content: space-between;
                            gap: 10px; font-size: 11px; padding: 2px 0; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .bm-ok {{ color: #7ee2a8; margin: 8px 0; }}
        </style>
        <script>
          document.querySelectorAll('[data-copy-target]').forEach(function (button) {{
            button.addEventListener('click', function () {{
              var node = document.getElementById(button.dataset.copyTarget);
              if (!node) return;
              navigator.clipboard.writeText(node.textContent).then(function () {{
                var original = button.textContent;
                button.textContent = '已複製';
                setTimeout(function () {{ button.textContent = original; }}, 1500);
              }});
            }});
          }});
        </script>
        """,
    )


@router.get("/benchmark/jobs/{job_id}/reference/{asset_id}")
def job_reference(
    job_id: str,
    asset_id: str,
    download: str = "",
    settings: Settings = Depends(settings_dep),
):
    """下載派工使用的參考素材。只允許該派工實際引用的素材。"""
    from app.routes.web import WebException

    try:
        view = job_view.load_job(PROJECT_ID, job_id)
    except LookupError as error:
        raise WebException(detail=str(error), back_link="/benchmark/jobs") from error

    if not any(item.asset_id == asset_id for item in view.references):
        raise WebException(
            detail=f"派工 {job_id} 未引用素材 {asset_id}",
            back_link=f"/benchmark/jobs/{job_id}",
        )

    raw = job_view.reference_path(asset_id)
    if not raw or not Path(raw).exists():
        raise WebException(
            detail=f"素材 {asset_id} 的檔案不存在",
            back_link=f"/benchmark/jobs/{job_id}",
        )
    path = Path(raw)
    return FileResponse(
        path,
        filename=path.name if download else None,
        media_type="application/octet-stream" if download else None,
    )


@router.post("/benchmark/jobs/{job_id}/attempt")
async def job_record_attempt(
    job_id: str,
    status: str = Form(...),
    output_file: str = Form(""),
    generation_seconds: str = Form(""),
    credits_used: str = Form(""),
    failure_reason: str = Form(""),
    settings: Settings = Depends(settings_dep),
):
    """從派工脈絡記錄嘗試。shot 與 target 由派工決定，使用者不會選錯。"""
    from urllib.parse import quote

    from app.routes.web import WebException

    try:
        view = job_view.load_job(PROJECT_ID, job_id)
    except LookupError as error:
        raise WebException(detail=str(error), back_link="/benchmark/jobs") from error

    if not view.target_id:
        raise WebException(
            detail=f"派工 {job_id} 沒有比較對象標記，無法記錄嘗試",
            back_link=f"/benchmark/jobs/{job_id}",
        )

    try:
        attempt_no = await run_in_threadpool(
            attempts.append_attempt,
            sheets_dir(settings) / attempts.ATTEMPTS_SHEET,
            view.shot_id,
            view.target_id,
            status,
            output_file.strip() or None,
            generation_seconds.strip() or None,
            credits_used.strip() or None,
            failure_reason.strip() or None,
            # 身份取自這份派工的快照，不取 catalog 現值：使用者剛剛在
            # 平台上跑的是這份派工的模型，不是之後可能被改過的那個。
            view.identity,
        )
        ok, message = "1", f"已記錄第 {attempt_no} 次嘗試"
    except ValueError as error:
        ok, message = "0", str(error)[:200]

    return RedirectResponse(
        url=f"/benchmark/jobs/{job_id}?ok={ok}&message={quote(message)}",
        status_code=303,
    )


@router.post("/benchmark/jobs/{job_id}/import")
async def job_import_variant(
    job_id: str,
    files: list[UploadFile] = File(...),
    shot_id: str = Form(""),
    settings: Settings = Depends(settings_dep),
):
    """從派工脈絡匯入影片。request_hash 由後端自派工取得。"""
    from urllib.parse import quote

    from app.routes.web import WebException
    from pipeline.project_store import save_project
    from pipeline.stages.variant_importer import JobLinkError, import_variants

    try:
        view = job_view.load_job(PROJECT_ID, job_id)
    except LookupError as error:
        raise WebException(detail=str(error), back_link="/benchmark/jobs") from error

    # 進階選項可以改鏡頭，但改成與派工不符時一律拒絕：
    # 允許不一致等於允許把影片掛到別顆鏡頭上，而統計不會察覺。
    chosen_shot = (shot_id or "").strip() or view.shot_id
    if chosen_shot != view.shot_id:
        raise WebException(
            detail=(
                f"派工 {job_id} 屬於鏡頭 {view.shot_id}，"
                f"不可匯入為 {chosen_shot}。請改用該鏡頭自己的派工。"
            ),
            back_link=f"/benchmark/jobs/{job_id}",
        )
    if not view.request_hash:
        raise WebException(
            detail=f"派工 {job_id} 沒有 request_hash，無法建立血緣",
            back_link=f"/benchmark/jobs/{job_id}",
        )

    payloads = [(item.filename or "clip.mp4", await item.read()) for item in files]
    artifact = load_project(settings, PROJECT_ID)
    if artifact is None:
        raise WebException(
            detail="benchmark 專案尚未建立", back_link="/benchmark"
        )

    try:
        report = await run_in_threadpool(
            import_variants,
            settings,
            artifact,
            view.shot_id,
            view.provider,
            payloads,
            view.request_hash,
            view.model_id,
            None,
            "local",
            f"job workbench import ({job_id})",
        )
    except (JobLinkError, ValueError) as error:
        raise WebException(
            detail=str(error), back_link=f"/benchmark/jobs/{job_id}"
        ) from error

    await run_in_threadpool(save_project, settings, artifact)

    parts = [f"已匯入 {report.new_count} 支候選"]
    if report.skipped:
        parts.append("略過：" + "；".join(report.skipped[:3]))
    warnings = [w for item in report.imported for w in item.warnings]
    if warnings:
        parts.append("提醒：" + "；".join(warnings[:3]))

    return RedirectResponse(
        url=(
            f"/benchmark/jobs/{job_id}"
            f"?ok={'1' if report.new_count else '0'}&message={quote('；'.join(parts))}"
        ),
        status_code=303,
    )


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
        <section class="app-shell" data-route="/benchmark/attempts"
                 data-step="4" data-entity-type="benchmark_attempts">
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


# --------------------------------------------------------------------------
# Step 5: 連戲評分
# --------------------------------------------------------------------------

def variant_video_url(variant_id: str) -> str:
    """候選影片的播放路徑。與專案頁的播放器共用同一個端點。"""
    return f"/projects/{PROJECT_ID}/variants/{variant_id}/video"


@router.get("/benchmark/continuity", response_class=HTMLResponse)
def continuity_index(settings: Settings = Depends(settings_dep)) -> str:
    """連戲評分總覽。列出每個比較對象的每一組配對與其狀態。"""
    from app.routes.web import page

    try:
        pairs = workflow.continuity_pairs(PROJECT_ID)
    except Exception as error:  # noqa: BLE001
        logger.warning("continuity pairs failed: %s", error)
        pairs = []

    def status_cell(item) -> str:
        if item.scored:
            return "已評分"
        if not item.ready:
            return "缺代表作"
        # 換過代表作：舊分數比的是別的影片，不能沿用
        if item.stale_scores:
            return "需重評（代表作已更換）"
        return "可評分"

    rows = "".join(
        f"<tr>"
        f"<td class='mono'>{escape(item.target_id)}</td>"
        f"<td class='mono'>{escape(item.shot_id)} ↔ {escape(item.ref_shot_id)}</td>"
        f"<td>{escape(item.scenario)}</td>"
        f"<td>{status_cell(item)}</td>"
        f"<td>{escape('；'.join(item.missing) or ('舊評分 ' + str(item.stale_scores) + ' 筆保留為歷史' if item.stale_scores else '—'))}</td>"
        f"<td>"
        + (
            f"<a class='ghost-button' href='/benchmark/continuity/"
            f"{escape(item.target_id)}/{escape(item.shot_id)}'>"
            f"{'重新評分' if item.scored else '評分'}</a>"
            if item.ready
            else "<span class='muted'>—</span>"
        )
        + "</td></tr>"
        for item in pairs
    ) or '<tr><td colspan="6" class="muted empty">尚無配對</td></tr>'

    ready = [item for item in pairs if item.ready]
    done = [item for item in ready if item.scored]
    nxt = next((item for item in ready if not item.scored), None)
    cta = (
        f'<a class="primary button-link" href="/benchmark/continuity/'
        f'{escape(nxt.target_id)}/{escape(nxt.shot_id)}">開始評下一組</a>'
        if nxt
        else '<span class="muted">沒有待評的配對</span>'
    )

    return page(
        title="連戲評分",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/continuity"
                 data-step="5" data-entity-type="continuity_index">
          <header class="project-hero">
            <div>
              <h1>連戲評分</h1>
              <p class="muted">
                {len(done)}/{len(ready)} 組已完成（共 {len(pairs)} 組配對）。
                連戲評的是兩支實際影片的關係，配對只由各鏡頭的代表作組成。
              </p>
            </div>
            <div class="actions">{cta}
              <a class="ghost-button" href="/benchmark">返回 Benchmark</a>
            </div>
          </header>
          <section class="panel">
            <table class="data-table">
              <thead><tr><th>Target</th><th>配對</th><th>情境</th>
              <th>狀態</th><th>缺什麼</th><th></th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </section>
        </section>
        """,
    )


def _score_field(name: str, label: str, value, disabled: bool = False) -> str:
    """一個 0-100 的評分欄位。留白代表 N/A，不以 0 分混淆。"""
    current = "" if value is None else str(value)
    note = "<p class='muted'>此項目在 V1 不評分</p>" if disabled else ""
    return f"""
    <div class="field">
      <label for="{escape(name)}">{escape(label)}</label>
      <input id="{escape(name)}" name="{escape(name)}" type="number"
             min="0" max="100" step="1" value="{escape(current)}"
             placeholder="0-100，留白為 N/A" {"disabled" if disabled else ""} />
      {note}
    </div>
    """


@router.get("/benchmark/continuity/{target_id}/{shot_id}", response_class=HTMLResponse)
def continuity_form(
    target_id: str,
    shot_id: str,
    ok: str = "",
    message: str = "",
    settings: Settings = Depends(settings_dep),
) -> str:
    """並排評分。兩支影片與其歸屬都由代表作決定，使用者不需要挑檔案。"""
    from app.routes.web import WebException, page
    from pipeline.models.qc import ContinuityQC

    try:
        pair = workflow.find_continuity_pair(target_id, shot_id, PROJECT_ID)
    except LookupError as error:
        raise WebException(
            detail=str(error), back_link="/benchmark/continuity"
        ) from error

    if not pair.ready:
        raise WebException(
            detail=(
                f"{target_id} 的 {shot_id} ↔ {pair.ref_shot_id} 尚無法評分："
                + "；".join(pair.missing)
            ),
            back_link="/benchmark/continuity",
        )

    existing = None
    if pair.qc_id:
        with Session(engine) as session:
            existing = session.get(ContinuityQC, pair.qc_id)

    def value_of(field: str):
        return getattr(existing, field, None) if existing else None

    flash = ""
    if message:
        flash = (
            f'<div class="{"bm-ok" if ok == "1" else "bm-warn"}">'
            f"{escape(message)}</div>"
        )

    pairs = workflow.continuity_pairs(PROJECT_ID)
    remaining = [
        item for item in pairs if item.ready and not item.scored and item.key != pair.key
    ]

    return page(
        title=f"連戲 · {target_id}",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/continuity/{escape(target_id)}/{escape(shot_id)}"
                 data-step="5" data-entity-type="continuity_pair"
                 data-entity-id="{escape(pair.key)}">
          <header class="project-hero">
            <div>
              <h1>{escape(target_id)}</h1>
              <p class="muted">
                {escape(pair.scenario)} · {escape(pair.shot_id)} ↔ {escape(pair.ref_shot_id)}
                · 還有 {len(remaining)} 組待評
              </p>
            </div>
            <div class="actions">
              <a class="ghost-button" href="/benchmark/continuity">全部配對</a>
            </div>
          </header>
          {flash}
          <section class="panel">
            <div class="pair-grid">
              <div>
                <strong class="mono">{escape(pair.shot_id)}</strong>
                <video controls preload="metadata"
                       src="{escape(variant_video_url(pair.variant_id))}"></video>
                <p class="mono muted">{escape(pair.variant_id)}</p>
              </div>
              <div>
                <strong class="mono">{escape(pair.ref_shot_id)}</strong>
                <video controls preload="metadata"
                       src="{escape(variant_video_url(pair.ref_variant_id))}"></video>
                <p class="mono muted">{escape(pair.ref_variant_id)}</p>
              </div>
            </div>
          </section>

          <section class="panel">
            <div class="section-head"><h2>兩顆鏡頭之間的一致性</h2></div>
            <form method="post"
                  action="/benchmark/continuity/{escape(target_id)}/{escape(shot_id)}">
              <div class="split">
                {_score_field("cross_shot_identity", "跨鏡頭身份一致", value_of("cross_shot_identity"))}
                {_score_field("wardrobe_continuity", "服裝連戲", value_of("wardrobe_continuity"))}
              </div>
              <div class="split">
                {_score_field("location_continuity", "場景連戲", value_of("location_continuity"))}
                {_score_field("lip_sync_quality", "嘴型同步", None, disabled=not v1_pack.LIP_SYNC_ENABLED)}
              </div>
              <div class="field">
                <label for="notes">備註</label>
                <input id="notes" name="notes"
                       value="{escape(getattr(existing, 'notes', None) or '')}" />
              </div>
              <button class="primary" type="submit">儲存並下一組</button>
            </form>
          </section>
        </section>
        <style>
          .pair-grid {{ display: grid; gap: 16px;
                        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
          /* 限制高度，讓兩支影片與評分欄位能同時出現在一個畫面裡。
             不限制的話 9:16 會撐到 520px 高，評分欄位被推到摺線以下，
             變成看完影片要往下捲、填分時又看不到影片。 */
          .pair-grid video {{ width: 100%; max-height: 320px; border-radius: 6px;
                              background: #0d1219; margin-top: 6px; }}
          .bm-warn {{ color: #e8b; margin: 8px 0; }}
          .bm-ok {{ color: #7ee2a8; margin: 8px 0; }}
        </style>
        """,
    )


@router.post("/benchmark/continuity/{target_id}/{shot_id}")
async def continuity_save(
    target_id: str,
    shot_id: str,
    cross_shot_identity: str = Form(""),
    wardrobe_continuity: str = Form(""),
    location_continuity: str = Form(""),
    notes: str = Form(""),
    settings: Settings = Depends(settings_dep),
):
    """儲存連戲評分。配對合法性由 score_import 的守門函式判定。"""
    from urllib.parse import quote

    from app.routes.web import WebException
    from pipeline.benchmark import score_import

    def score(raw: str) -> int | None:
        text = (raw or "").strip()
        if not text:
            return None
        value = int(float(text))
        if not 0 <= value <= 100:
            raise ValueError(f"評分必須介於 0 與 100 之間，收到 {value}")
        return value

    try:
        pair = workflow.find_continuity_pair(target_id, shot_id, PROJECT_ID)
    except LookupError as error:
        raise WebException(
            detail=str(error), back_link="/benchmark/continuity"
        ) from error

    if not pair.ready:
        raise WebException(
            detail="；".join(pair.missing), back_link="/benchmark/continuity"
        )

    try:
        values = {
            "cross_shot_identity": score(cross_shot_identity),
            "wardrobe_continuity": score(wardrobe_continuity),
            "location_continuity": score(location_continuity),
        }
        await run_in_threadpool(
            score_import.validate_continuity_pair,
            PROJECT_ID,
            target_id,
            pair.shot_id,
            pair.ref_shot_id,
            pair.variant_id,
            pair.ref_variant_id,
        )
        await run_in_threadpool(
            _persist_continuity, PROJECT_ID, pair, values, notes.strip() or None
        )
    except (score_import.PairMismatch, ValueError) as error:
        raise WebException(
            detail=str(error),
            back_link=f"/benchmark/continuity/{target_id}/{shot_id}",
        ) from error

    remaining = [
        item
        for item in workflow.continuity_pairs(PROJECT_ID)
        if item.ready and not item.scored
    ]
    if remaining:
        nxt = remaining[0]
        return RedirectResponse(
            url=(
                f"/benchmark/continuity/{nxt.target_id}/{nxt.shot_id}"
                f"?ok=1&message={quote('上一組已儲存')}"
            ),
            status_code=303,
        )
    return RedirectResponse(url="/benchmark/continuity", status_code=303)


def _continuity_qc_id(project_id: str, pair) -> str:
    """連戲評分的主鍵，涵蓋實際被比較的那兩支影片。

    原本只用 project + target + shot，換一次代表作就會覆寫掉前一次的
    評分——那筆分數是比另外兩支片得到的，覆寫等於銷毀證據。
    納入兩個 variant_id 後，換代表作會產生新的一列，舊的留著可追溯。
    以雜湊固定長度，避免 variant_id 串接後超出主鍵長度。
    """
    import hashlib

    raw = "|".join(
        [
            project_id,
            pair.target_id,
            pair.shot_id,
            pair.ref_shot_id,
            pair.variant_id or "",
            pair.ref_variant_id or "",
        ]
    )
    return "cqc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _persist_continuity(
    project_id: str, pair, values: dict, notes: str | None
) -> None:
    """寫入或更新連戲評分。

    同一組「兩支影片」重評時覆寫該列；換過代表作則是另一組配對，
    會新增一列，舊列保留為歷史。
    """
    from pipeline.models.qc import ContinuityQC, ContinuityScope

    qc_id = _continuity_qc_id(project_id, pair)
    with Session(engine) as session:
        row = session.get(ContinuityQC, qc_id)
        if row is None:
            row = ContinuityQC(
                qc_id=qc_id,
                project_id=project_id,
                scope=ContinuityScope.PAIR.value,
                shot_id=pair.shot_id,
                ref_shot_id=pair.ref_shot_id,
            )
        row.variant_id = pair.variant_id
        row.ref_variant_id = pair.ref_variant_id
        row.cross_shot_identity = values["cross_shot_identity"]
        row.wardrobe_continuity = values["wardrobe_continuity"]
        row.location_continuity = values["location_continuity"]
        # V1 沒有音訊 ground truth，嘴型一律不評，不得寫入 0 分
        row.lip_sync_quality = None
        row.notes = notes
        session.add(row)
        session.commit()


# 排序依據的中文名稱。報表面向的是判斷要用哪個平台的人，
# 不是讀 aggregation 原始碼的人。
CRITERIA_LABELS = {
    "continuity_identity": "跨鏡頭身份一致",
    "identity_consistency": "單鏡頭身份穩定",
    "facial_acting": "表情演技",
    "usable_shot_rate": "可用鏡頭比例",
    "camera_control": "運鏡控制",
    "temporal_stability": "時間穩定性",
    "motion_quality": "動態品質",
    "artifact_cleanliness": "畫面乾淨度",
    "retries_per_usable": "每支可用所需重試",
    "human_minutes_per_usable": "每支可用的人工分鐘",
}


def _readable_criteria(reason: str) -> str:
    return " → ".join(
        CRITERIA_LABELS.get(part.strip(), part.strip())
        for part in reason.split(">")
        if part.strip()
    )


@router.get("/benchmark/results", response_class=HTMLResponse)
def benchmark_results(settings: Settings = Depends(settings_dep)) -> str:
    """依情境呈現結果。刻意不產生跨情境總冠軍。"""
    from app.routes.web import page
    from pipeline.benchmark import report as report_module

    # 「哪些資料算數」的判斷在 pipeline 層，路由只負責畫。
    report = report_module.build_current_report(settings, PROJECT_ID)

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
          <p class="muted">排序依據：{escape(_readable_criteria(item.reason))}</p>
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
    if report.has_excluded:
        reasons = "".join(
            f"<li>{escape(item)}</li>" for item in report.excluded_identities[:6]
        )
        counted = "、".join(
            part
            for part in (
                f"{report.excluded_variants} 支候選" if report.excluded_variants else "",
                f"{report.excluded_attempts} 次嘗試" if report.excluded_attempts else "",
                f"{report.excluded_continuity} 組連戲" if report.excluded_continuity else "",
            )
            if part
        )
        warnings += (
            '<div class="bm-warn" data-error-code="identity_drift">'
            f"<p><strong>有 {escape(counted)} 未計入本表。</strong></p>"
            f"<ul class='bm-list'>{reasons}</ul>"
            "<p>這些紀錄都完整保留，只是它們不屬於目前這一輪："
            "可能是用較舊的版本生成的，或是連戲評分比的不是現在的代表作。"
            "把它們和目前的成績加在一起，得到的數字不對應任何一次實際的比較。"
            '<a href="#" class="ask-ai" data-ask-ai="為什麼有資料沒有計入這份報表？">'
            "Ask AI</a></p></div>"
        )
    if report.identity_warnings:
        rows = "".join(
            f"<li>{escape(item)}</li>" for item in report.identity_warnings[:5]
        )
        warnings += (
            '<div class="bm-warn"><p>部分嘗試紀錄缺少記錄當下的版本資訊，'
            f"未計入本表：</p><ul>{rows}</ul></div>"
        )

    return page(
        title="Benchmark 結果",
        active_nav="benchmark",
        body=f"""
        <section class="app-shell" data-route="/benchmark/results"
                 data-step="6" data-entity-type="benchmark_results">
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
