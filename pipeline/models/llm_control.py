# 檔案路徑: video-pipeline/pipeline/models/llm_control.py
# 產生時間: 2026-06-24 21:18 +08:00
# 版本: v1.0
# 模組定位:
#   LAVA 大腦 LLM 提供者連線、任務綁定與運行狀態之 Pydantic 模型定義。
# 主要責任:
#   1. 定義 LLMConnection (OpenRouter, Gemini) 連線參數。
#   2. 定義 LLMTask (選題、腳本大綱、Storyboard 等) 綁定關係。
#   3. 定義 LLMBrainStatus 反映當前啟用的 API 連線。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel, Field



class LLMCapability(StrEnum):
    CHAT = "chat"
    VISION = "vision"


class LLMConnection(BaseModel):
    connection_id: str
    provider: str
    label: str
    model_id: str
    api_key_env: str
    base_url: str | None = None
    is_active: bool = True
    rpm_limit: int = 60
    tpm_limit: int = 1_000_000


class LLMTask(BaseModel):
    task_id: str
    label: str
    capability: LLMCapability = LLMCapability.CHAT
    required: bool = False
    description: str


class LLMTaskBinding(BaseModel):
    task_id: str
    connection_id: str


class LLMBrainStatus(BaseModel):
    connections: list[LLMConnection] = Field(default_factory=list)
    tasks: list[LLMTask] = Field(default_factory=list)
    bindings: list[LLMTaskBinding] = Field(default_factory=list)
    configured_env_keys: list[str] = Field(default_factory=list)

