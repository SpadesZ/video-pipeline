# 檔案路徑: video-pipeline/pipeline/models/variant.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   生成工作 CapabilityJob 與生成候選 AssetVariant 資料表。
# 主要責任:
#   1. 以獨立資料表累積每一次生成嘗試與其產物，支撐 Benchmark 統計。
#   2. 保留完整生成脈絡，使日後能回答「這顆好鏡頭是怎麼生成的」。
#   3. 以 parent_variant_id 記錄重生成血緣。
# 說明:
#   刻意不存放於 ProductionArtifact 的 JSON 欄位。一支片可能有 20 顆鏡頭
#   乘上每顆 5-10 個候選，再加 QC、成本與血緣，塞進 project JSON 會使
#   查詢、更新與統計都難以進行。
#   CapabilityJob 獨立於 AssetVariant，因為失敗與逾期的工作也必須留存，
#   否則 retry rate 無從計算。
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import JSON, Column, Integer, String
from sqlmodel import Field, SQLModel

from pipeline.models.json_column import PydanticJSON


class JobStatus(StrEnum):
    """商業影音 API 多半不是 request-response 即時完成，狀態機一開始就給足。"""

    PENDING_MANUAL = "pending_manual"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.EXPIRED.value,
    }
)

UNUSABLE_JOB_STATUSES: frozenset[str] = frozenset(
    {
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
        JobStatus.EXPIRED.value,
    }
)


class TransportKind(StrEnum):
    MANUAL = "manual"
    API = "api"


class VariantStatus(StrEnum):
    PENDING = "pending"
    IMPORTED = "imported"
    SELECTED = "selected"
    REJECTED = "rejected"


class GenerationMode(StrEnum):
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_VIDEO = "reference_video"
    IMAGE = "image"
    LIP_SYNC = "lip_sync"


class GenerationCost(BaseModel):
    """第一階段為人工於平台會員方案操作，記錄可觀測量而非估算金額。

    API 上線後再由 credits 與供應商計價換算真實成本。
    """

    credits_used: float | None = None
    generations_attempted: int = PydanticField(default=0, ge=0)
    wall_clock_minutes: float | None = None
    human_minutes: float | None = None
    usable_variants: int = PydanticField(default=0, ge=0)

    @property
    def attempts_per_usable(self) -> float | None:
        if not self.usable_variants:
            return None
        return self.generations_attempted / self.usable_variants


class CapabilityJob(SQLModel, table=True):
    __tablename__ = "capability_jobs"

    job_id: str = Field(primary_key=True)
    project_id: str = Field(index=True)
    shot_id: str | None = Field(default=None, index=True)

    capability: str = Field(sa_column=Column(String, nullable=False, index=True))
    provider: str = Field(sa_column=Column(String, nullable=False, index=True))
    model_id: str | None = None
    model_version: str | None = None
    transport: str = Field(
        default=TransportKind.MANUAL.value,
        sa_column=Column(String, nullable=False),
    )

    status: str = Field(
        default=JobStatus.PENDING_MANUAL.value,
        sa_column=Column(String, nullable=False, index=True),
    )

    # job manifest 的冪等鍵，用於阻擋同一份工作被重複匯入
    request_hash: str | None = Field(default=None, index=True)
    provider_job_id: str | None = None

    # 派工當下實際送出的規格。ShotPlan.target_duration_ms 會經
    # ProductionProfile 的鏡頭長度政策夾住，兩者可能不同，
    # 匯入時的落差比對必須以此為基準。
    requested_duration_ms: int | None = Field(default=None, sa_column=Column(Integer))
    requested_aspect_ratio: str | None = None

    # 派工當下的完整請求快照。匯入候選時的血緣只能取自此處，
    # 不可回推當前 ShotPlan：分鏡在派工後可能已被修改，
    # 那樣記錄下來的來源會是從未真正送出去的內容。
    request_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    provider_parameters: dict = Field(default_factory=dict, sa_column=Column(JSON))
    reference_asset_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    manifest_path: str | None = None

    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    @property
    def is_failure(self) -> bool:
        return self.status in UNUSABLE_JOB_STATUSES


class AssetVariant(SQLModel, table=True):
    __tablename__ = "asset_variants"

    variant_id: str = Field(primary_key=True)
    project_id: str = Field(index=True)
    shot_id: str = Field(index=True)
    job_id: str | None = Field(default=None, index=True)
    # 重生成血緣：本候選是由哪個候選重試而來
    parent_variant_id: str | None = Field(default=None, index=True)

    provider: str = Field(sa_column=Column(String, nullable=False, index=True))
    model_id: str | None = Field(default=None, index=True)
    model_version: str | None = None
    generation_mode: str | None = Field(default=None, sa_column=Column(String))

    # 生成當下的實際提示詞快照，不可事後由 ShotPlan 回推
    prompt_snapshot: str = ""
    negative_prompt: str = ""
    reference_asset_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    provider_parameters: dict = Field(default_factory=dict, sa_column=Column(JSON))

    requested_duration_ms: int | None = Field(default=None, sa_column=Column(Integer))
    actual_duration_ms: int | None = Field(default=None, sa_column=Column(Integer))
    resolution: str | None = None
    fps: float | None = None

    file_hash: str | None = Field(default=None, index=True)
    local_path: str | None = None
    # 匯入時檔案會改名為 variant_id，保留原始檔名才能與嘗試紀錄的
    # output_file 精確對應，不必依匯入順序猜測。
    original_filename: str | None = Field(default=None, index=True)

    status: str = Field(
        default=VariantStatus.PENDING.value,
        sa_column=Column(String, nullable=False, index=True),
    )
    selected_reason: str | None = None

    # benchmark 專用。production 的 status=selected 是每顆鏡頭全域單選，
    # 用於決定成片素材；benchmark 需要每個比較對象各自選一支代表作，
    # 同一顆鏡頭會同時有多支被選中。兩者語義不同，不可共用欄位。
    benchmark_selected: bool | None = Field(default=None, index=True)

    cost: GenerationCost | None = Field(
        default=None, sa_column=Column(PydanticJSON(GenerationCost))
    )

    generation_timestamp: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_selected(self) -> bool:
        return self.status == VariantStatus.SELECTED

    @property
    def duration_matches_request(self) -> bool | None:
        """生成長度是否符合請求長度。缺任一值時回傳 None 表示無法判定。"""
        if self.requested_duration_ms is None or self.actual_duration_ms is None:
            return None
        tolerance_ms = max(200, int(self.requested_duration_ms * 0.1))
        return abs(self.actual_duration_ms - self.requested_duration_ms) <= tolerance_ms
