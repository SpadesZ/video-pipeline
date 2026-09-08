# 檔案路徑: video-pipeline/pipeline/assistant/service.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   AI 助手的模型呼叫。
# 主要責任:
#   1. 以既有的 capability router 取得文字推理，不另建一套 LLM client。
#   2. 助手不可用時明確降級，不影響主流程。
# 說明:
#   刻意重用 dispatch_capability 而非直接呼叫 provider：模型的選擇、
#   fallback 與金鑰處理已經在那一層，另開一條路只會讓兩邊的行為分岔。
#   助手的模型因此也可以在 LAVA 設定裡換，不寫死在程式碼裡。
#
#   這一版是唯讀 copilot。沒有 tool、沒有 function calling、
#   沒有任何會寫入資料的路徑。要讓它能動作，得先有
#   「建議 -> 使用者確認 -> 執行」的流程，那不在這一版。
# --------------------------------------------------------------------------

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pipeline.assistant.context import AssistantContext

logger = logging.getLogger("assistant.service")

# 助手在 LAVA 任務註冊表中的識別。未綁定時走預設路由。
ASSISTANT_TASK_ID = "assistant_copilot"

SYSTEM_PROMPT = """你是這套 AI 影片製作系統的操作助手。

你的職責是根據下面提供的「目前狀態」回答使用者的問題：
解釋這一頁在做什麼、某個欄位是什麼意思、某個按鈕按下去會發生什麼、
為什麼某個按鈕不能按、使用者下一步該做什麼。

規則：
- 只根據提供的脈絡回答。脈絡裡沒有的事實就說不知道，不要猜。
- 使用者問「為什麼不能按」時，直接說出停用原因與解法，不要反問他在哪一頁。
- 需要引導時給出明確的頁面路徑，例如 /benchmark/targets。
- 用繁體中文，簡潔具體。不要客套開場白。
- 你只能讀取與說明。你沒有能力也不應該宣稱可以幫使用者
  建立派工、刪除候選、修改比較對象、選片或提交評分。
  使用者要求你執行這類動作時，說明你只能引導，並告訴他該去哪一頁自己操作。
"""


class AssistantReply(BaseModel):
    ok: bool = False
    reply: str = ""
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    # 使用者可以直接點過去的建議頁面
    suggested_routes: list[str] = Field(default_factory=list)

    @property
    def unavailable(self) -> bool:
        return not self.ok


UNAVAILABLE_MESSAGE = (
    "助手暫時無法使用。這不影響你目前的操作，"
    "頁面上的狀態與按鈕都仍然正常。"
)


def _suggested_routes(context: AssistantContext) -> list[str]:
    routes: list[str] = []
    for item in context.errors:
        if item.next_route and item.next_route not in routes:
            routes.append(item.next_route)
    return routes[:3]


async def ask(context: AssistantContext, question: str) -> AssistantReply:
    """向模型提問。任何失敗都回傳 unavailable，不往外拋。

    助手壞掉時使用者仍要能完成 benchmark，因此這裡不允許例外逸出到
    路由層——一個聊天視窗不該有能力讓整頁 500。
    """
    question = (question or "").strip()
    if not question:
        return AssistantReply(ok=False, error="沒有問題內容")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"=== 目前狀態 ===\n{context.as_prompt()}\n\n"
                f"=== 使用者的問題 ===\n{question}"
            ),
        },
    ]

    try:
        from pipeline.adapters.llm.lava_dispatcher import dispatch_llm_task

        result = await dispatch_llm_task(
            ASSISTANT_TASK_ID, messages, temperature=0.2, max_tokens=900
        )
    except Exception as error:  # noqa: BLE001 - 助手失敗不得影響主流程
        logger.warning("assistant dispatch failed: %s", error)
        return AssistantReply(ok=False, error=str(error)[:200])

    if not result.get("ok"):
        return AssistantReply(
            ok=False, error=str(result.get("error", "unknown"))[:200]
        )

    return AssistantReply(
        ok=True,
        reply=str(result.get("content") or "").strip(),
        provider=result.get("provider"),
        model=result.get("model"),
        suggested_routes=_suggested_routes(context),
    )
