from pipeline.models.llm_control import LLMCapability, LLMTask


VIDEO_LLM_TASKS = [
    LLMTask(
        task_id="topic_research",
        label="選題研究",
        capability=LLMCapability.CHAT,
        required=False,
        category="planning",
        fallback_behavior="沒有設定模型時，改用固定的建議清單，不會中斷流程。",
        description="在寫腳本前，先找出切入角度、對手沒做到的地方，以及觀眾在搜什麼。",
    ),
    LLMTask(
        task_id="script_outline",
        label="腳本大綱",
        capability=LLMCapability.CHAT,
        required=False,
        category="script",
        fallback_behavior="模型呼叫失敗時，改用本機的腳本範本。",
        description="把選定的角度寫成有分段標記的影片腳本。",
    ),
    LLMTask(
        task_id="visual_bible",
        label="視覺風格設定",
        capability=LLMCapability.CHAT,
        required=True,
        category="visual",
        fallback_behavior="改用本機的保守預設風格。",
        description="訂出整支片的視覺風格、角色一致性規則與提示詞限制。",
    ),
    LLMTask(
        task_id="storyboard",
        label="分鏡",
        capability=LLMCapability.CHAT,
        required=True,
        category="visual",
        fallback_behavior="模型失敗時，用固定規則把字幕轉成鏡頭。",
        description="把字幕段落轉成一顆一顆鏡頭的規格與提示詞。",
    ),
    LLMTask(
        task_id="packaging",
        label="標題與封面文案",
        capability=LLMCapability.CHAT,
        required=False,
        category="packaging",
        fallback_behavior="改用本機的預設標題與說明。",
        description="產生標題、封面概念、影片說明、章節與必要聲明。",
    ),
    LLMTask(
        task_id="quality_review",
        label="品質審查",
        capability=LLMCapability.CHAT,
        required=True,
        category="quality",
        fallback_behavior="改用固定的檢查項目，最終仍以人工審核為準。",
        description="依畫面、原創性與平台規範檢查預覽成品。",
    ),
    LLMTask(
        task_id="assistant_copilot",
        label="AI 助手",
        capability=LLMCapability.CHAT,
        required=False,
        category="assistant",
        fallback_behavior=(
            "助手顯示為暫時無法使用。它只讀且不在製作流程上，"
            "比較流程完全不受影響。"
        ),
        description="解釋目前這一頁在做什麼、卡在哪、下一步是什麼。只讀，不會改資料。",
    ),
]

TASK_IDS = {task.task_id for task in VIDEO_LLM_TASKS}
