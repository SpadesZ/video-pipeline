# 檔案路徑: video-pipeline/pipeline/capability/provider_spec.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   平台規格 ProviderSpec 的宣告式定義與載入。
# 主要責任:
#   1. 以資料而非程式碼描述各平台的能力、限制、必填欄位與參數對應。
#   2. 提供人工操作說明，供 manual transport 產生 job package 的 README。
# 說明:
#   各平台欄位不同，但差異必須以資料表達，不得寫成
#   `if provider == "kling"` 這類分支，否則每接一個平台就要動 transport
#   程式碼。轉為 API transport 時同一份 spec 繼續沿用，只換 transport。
# --------------------------------------------------------------------------

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pipeline.models.capability import Capability
from pipeline.models.variant import TransportKind

CATALOG_DIR = Path(__file__).resolve().parent / "catalog" / "providers"


class DurationUnit(StrEnum):
    """平台介面接受的片長單位。

    內部一律以毫秒表達，輸出至 job package 時依平台換算。多數平台的
    操作介面以秒為單位，若直接輸出毫秒，人工照著填會得到錯誤的片長。
    """

    MILLISECONDS = "ms"
    SECONDS = "seconds"

    def from_ms(self, duration_ms: int) -> int | float:
        if self is DurationUnit.MILLISECONDS:
            return duration_ms
        seconds = duration_ms / 1000
        return int(seconds) if seconds.is_integer() else round(seconds, 2)


class DurationLimits(BaseModel):
    min_ms: int | None = None
    max_ms: int | None = None

    def violation(self, duration_ms: int | None) -> str | None:
        if duration_ms is None:
            return None
        if self.min_ms is not None and duration_ms < self.min_ms:
            return f"duration {duration_ms}ms 低於下限 {self.min_ms}ms"
        if self.max_ms is not None and duration_ms > self.max_ms:
            return f"duration {duration_ms}ms 超過上限 {self.max_ms}ms"
        return None


class ProviderSpec(BaseModel):
    provider_id: str
    label: str
    transport: TransportKind = TransportKind.MANUAL

    supported_capabilities: list[Capability] = Field(default_factory=list)
    duration_limits: DurationLimits = Field(default_factory=DurationLimits)
    # 平台介面填寫片長時使用的單位，內部恆為毫秒
    duration_unit: DurationUnit = DurationUnit.SECONDS
    max_reference_assets: int | None = None
    supported_aspect_ratios: list[str] = Field(default_factory=list)

    # job manifest 產生時必須具備的欄位
    required_fields: list[str] = Field(default_factory=list)
    # 內部欄位名 -> 平台欄位名
    parameter_mapping: dict[str, str] = Field(default_factory=dict)
    # 人工操作說明，寫入 job package 的 README
    human_instructions: str = ""

    api_key_env: str | None = None
    base_url: str | None = None
    # 人工操作時要開的平台網址。manual transport 的流程靠真人在這裡生成，
    # 讓他自己去記或搜尋網址是沒必要的摩擦。這是給人點的連結，不是 API endpoint。
    console_url: str | None = None
    notes: str | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.supported_capabilities

    def map_parameters(self, values: dict) -> dict:
        """將內部參數名轉為平台欄位名。未列於對應表者原樣保留。"""
        return {self.parameter_mapping.get(key, key): value for key, value in values.items()}


def load_provider_specs(catalog_dir: Path | None = None) -> dict[str, ProviderSpec]:
    directory = catalog_dir or CATALOG_DIR
    specs: dict[str, ProviderSpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:
            continue
        spec = ProviderSpec.model_validate(data)
        if spec.provider_id in specs:
            raise ValueError(f"provider_id 重複: {spec.provider_id} ({path.name})")
        specs[spec.provider_id] = spec
    return specs


@lru_cache(maxsize=1)
def provider_specs() -> dict[str, ProviderSpec]:
    return load_provider_specs()


def get_provider(provider_id: str) -> ProviderSpec | None:
    return provider_specs().get(provider_id)


def providers_supporting(capability: Capability) -> list[ProviderSpec]:
    return [spec for spec in provider_specs().values() if spec.supports(capability)]
