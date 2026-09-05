# 檔案路徑: video-pipeline/pipeline/capability/base.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Capability Router 的請求、結果與轉接器契約。
# 主要責任:
#   1. 定義 CapabilityRequest 作為所有能力的統一請求格式。
#   2. 定義 CapabilityResult 及其非即時的工作狀態。
#   3. 定義 CapabilityAdapter Protocol，使 manual 與 API transport 共用契約。
# 說明:
#   CapabilityResult.status 採用完整的工作生命週期而非布林成敗。
#   商業影音 API 多半不是 request-response 即時完成，manual transport
#   更是必然回傳 pending_manual 等待人工完成。上層 orchestration 因此
#   在 manual 換成 API 時不需改寫，差別只在等人或等平台。
# --------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from pipeline.models.capability import Capability
from pipeline.models.variant import JobStatus

REQUEST_SCHEMA_VERSION = "1.0"


class ChatPayload(BaseModel):
    """文字推理與視覺分析類能力的輸入。"""

    messages: list[dict] = Field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 2048


class VisualPayload(BaseModel):
    """影像與影片生成類能力的輸入。"""

    prompt: str = ""
    negative_prompt: str = ""
    reference_asset_ids: list[str] = Field(default_factory=list)
    first_frame_ref: str | None = None
    duration_ms: int | None = None
    aspect_ratio: str | None = None
    camera: str | None = None
    character_refs: list[str] = Field(default_factory=list)
    # asset_id -> 檔案內容 SHA256。素材識別碼不變但圖片換了的情況，
    # 若只記錄 asset_id，兩次生成會被視為同一份請求，血緣就錯了。
    # 內容雜湊納入請求後，換圖必然產生新的 request identity。
    reference_hashes: dict[str, str] = Field(default_factory=dict)


class AudioPayload(BaseModel):
    """語音、音樂與音效類能力的輸入。"""

    text: str = ""
    voice_ref: str | None = None
    duration_ms: int | None = None
    style: str | None = None


class CapabilityRequest(BaseModel):
    """統一請求格式。manual 與 API transport 吃同一份，job.json 即其序列化。"""

    request_id: str
    capability: Capability
    schema_version: str = REQUEST_SCHEMA_VERSION

    project_id: str | None = None
    shot_id: str | None = None
    shot_plan_version: int | None = None
    profile_version: int | None = None

    chat: ChatPayload | None = None
    visual: VisualPayload | None = None
    audio: AudioPayload | None = None

    # 平台專屬參數，由 ProviderSpec.parameter_mapping 轉換後填入
    parameters: dict = Field(default_factory=dict)

    created_at: datetime | None = None

    def content_hash(self) -> str:
        """內容指紋，用於阻擋同一份工作被重複匯入。

        刻意排除 request_id 與 created_at，使同樣的生成需求得到相同雜湊。
        """
        payload = self.model_dump(
            mode="json", exclude={"request_id", "created_at"}
        )
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def active_payload(self) -> BaseModel | None:
        return self.chat or self.visual or self.audio


class AttemptRecord(BaseModel):
    """單次轉接器嘗試的結果，用於保留 fallback 軌跡。"""

    provider: str
    model_id: str | None = None
    status: str
    error: str | None = None


class CapabilityResult(BaseModel):
    ok: bool = False
    status: JobStatus = JobStatus.FAILED

    job_id: str | None = None
    provider_job_id: str | None = None
    provider: str | None = None
    model_id: str | None = None
    model_version: str | None = None

    # 文字類能力的回應
    content: str | None = None
    # 檔案類能力的產出路徑
    outputs: list[str] = Field(default_factory=list)

    error_code: str | None = None
    error_message: str | None = None

    submitted_at: datetime | None = None
    completed_at: datetime | None = None

    attempts: list[AttemptRecord] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        }

    @property
    def awaiting_human(self) -> bool:
        return self.status is JobStatus.PENDING_MANUAL

    @classmethod
    def failure(
        cls,
        error_message: str,
        error_code: str = "adapter_error",
        attempts: list[AttemptRecord] | None = None,
    ) -> "CapabilityResult":
        return cls(
            ok=False,
            status=JobStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            attempts=attempts or [],
        )


@runtime_checkable
class CapabilityAdapter(Protocol):
    """所有能力轉接器的統一介面。

    manual 與 API transport 實作同一個 Protocol，因此第一階段以人工操作
    驗證流程後，替換為 API 時上層 orchestration 不需改寫。
    """

    capability: Capability
    provider: str

    async def execute(self, request: CapabilityRequest) -> CapabilityResult:
        ...
