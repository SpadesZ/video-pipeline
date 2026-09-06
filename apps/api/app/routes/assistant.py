# 檔案路徑: video-pipeline/apps/api/app/routes/assistant.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   AI 助手的 HTTP 介面。
# 主要責任:
#   1. 接收提問與頁面脈絡，回傳說明。
#   2. 提供脈絡檢視端點，供測試與除錯確認送出去的是什麼。
# 說明:
#   這裡只有兩個端點，而且都不寫入任何資料。第一版助手是唯讀 copilot，
#   刻意沒有任何 mutation 路徑：沒有 tool endpoint、沒有 action 參數、
#   也不轉發到其他會寫入的路由。要讓它能動作，得先做
#   「建議 -> 使用者確認 -> 執行」，那不在這一版。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.deps import settings_dep
from pipeline.assistant import context as assistant_context
from pipeline.assistant import service
from pipeline.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

MAX_QUESTION_CHARS = 1000


class AskRequest(BaseModel):
    question: str = ""
    route: str = "/"
    entity_type: str = ""
    entity_id: str = ""
    error_code: str = ""
    error_message: str = ""
    # 使用者從某顆按鈕旁邊按 Ask AI 時帶入，讓助手知道問的是哪一顆
    control_id: str = ""


def _build(payload: AskRequest, settings: Settings):
    return assistant_context.build(
        settings,
        route=payload.route,
        question=payload.question,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        error_code=payload.error_code,
        error_message=payload.error_message,
        control_id=payload.control_id,
    )


@router.post("/assistant/ask")
async def assistant_ask(
    payload: AskRequest, settings: Settings = Depends(settings_dep)
) -> JSONResponse:
    """回答一個問題。唯讀：不會修改任何資料。"""
    question = (payload.question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return JSONResponse(
            {"ok": False, "reply": "", "error": "請先輸入問題"}, status_code=200
        )

    try:
        context = _build(payload, settings)
        reply = await service.ask(context, question)
    except Exception as error:  # noqa: BLE001 - 助手的失敗不得變成頁面錯誤
        logger.warning("assistant ask failed: %s", error)
        return JSONResponse(
            {
                "ok": False,
                "reply": service.UNAVAILABLE_MESSAGE,
                "error": str(error)[:200],
            }
        )

    if not reply.ok:
        return JSONResponse(
            {
                "ok": False,
                "reply": service.UNAVAILABLE_MESSAGE,
                "error": reply.error,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "reply": reply.reply,
            "provider": reply.provider,
            "model": reply.model,
            "suggested_routes": reply.suggested_routes,
        }
    )


@router.post("/assistant/context")
def assistant_context_preview(
    payload: AskRequest, settings: Settings = Depends(settings_dep)
) -> JSONResponse:
    """回傳將要送給模型的脈絡。不呼叫模型，供測試檢查內容與邊界。"""
    try:
        context = _build(payload, settings)
    except Exception as error:  # noqa: BLE001
        logger.warning("assistant context failed: %s", error)
        return JSONResponse({"ok": False, "error": str(error)[:200]})
    return JSONResponse({"ok": True, "context": context.model_dump(mode="json")})
