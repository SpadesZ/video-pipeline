# 檔案路徑: video-pipeline/pipeline/benchmark/target.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   Benchmark 比較對象 BenchmarkTarget 定義與載入。
# 主要責任:
#   1. 以 provider + 精確 model_id + 版本 + 介面標籤唯一識別比較對象。
#   2. 標記尚未經真人確認的型號為 provisional。
# 說明:
#   只比較 provider 是不夠的。同一個平台會同時提供多個版本，
#   介面上的選項名稱也未必等同 catalog 的 model_id。若只記錄
#   「這是 Kling 生的」，日後無從得知當時選的是哪一版，
#   跨輪次比較會失效。
#
#   catalog 中的 model_id 目前是通稱（kling-video、runway-gen3 等），
#   全部標記 provisional。真人登入平台確認實際版本後，
#   應更新 v1_targets.yaml 並將 provisional 設為 false。
# --------------------------------------------------------------------------

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "v1_targets.yaml"


class BenchmarkTarget(BaseModel):
    """一個明確的比較對象。

    target_id 是所有紀錄的關聯鍵：job package、attempt ledger、
    score sheet 與 aggregation 都以它為單位，而非以 provider。
    """

    target_id: str
    provider: str
    model_id: str
    # 平台介面上顯示的版本字串。真人確認前為空。
    model_version: str | None = None
    # 平台介面上該選項的名稱，供人工操作時對照。
    ui_label: str = ""
    transport: str = "manual"
    # 尚未經真人於平台確認實際型號者為 True，不得視為最終模型真相。
    provisional: bool = True
    notes: str | None = None

    @property
    def display(self) -> str:
        version = self.model_version or "unverified"
        return f"{self.provider}/{self.model_id}@{version}"

    def identity(self) -> dict:
        """寫入 manifest 與 ledger 的身份欄位。"""
        return {
            "target_id": self.target_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "ui_label": self.ui_label,
            "transport": self.transport,
            "provisional": self.provisional,
        }


class TargetRegistry(BaseModel):
    targets: list[BenchmarkTarget] = Field(default_factory=list)

    def by_id(self, target_id: str) -> BenchmarkTarget | None:
        return next(
            (item for item in self.targets if item.target_id == target_id), None
        )

    def for_provider(self, provider: str) -> list[BenchmarkTarget]:
        return [item for item in self.targets if item.provider == provider]

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(item.target_id for item in self.targets)

    @property
    def providers(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in self.targets:
            if item.provider not in seen:
                seen.append(item.provider)
        return tuple(seen)

    @property
    def provisional_targets(self) -> list[BenchmarkTarget]:
        return [item for item in self.targets if item.provisional]


def load_targets(catalog_path: Path | None = None) -> TargetRegistry:
    path = catalog_path or CATALOG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets = [BenchmarkTarget.model_validate(item) for item in data.get("targets", [])]

    seen: set[str] = set()
    for target in targets:
        if target.target_id in seen:
            raise ValueError(f"target_id 重複: {target.target_id}")
        seen.add(target.target_id)
    return TargetRegistry(targets=targets)


@lru_cache(maxsize=1)
def targets() -> TargetRegistry:
    return load_targets()


class TargetValidationError(ValueError):
    """比較對象與 catalog 不一致。"""


def validate_target(target: BenchmarkTarget, capability) -> None:
    """驗證比較對象確實對應到 catalog 中一個可用的模型。

    僅在請求上貼 target 標籤而不限制路由，標籤與實際生成的模型可能不同，
    比較結果就會歸錯對象。派工前必須確認 model_id 存在、由該平台託管、
    且支援所需能力，任一不符即拒絕。
    """
    from pipeline.capability.model_registry import model_registry
    from pipeline.capability.provider_spec import get_provider

    entry = model_registry().get(target.model_id)
    if entry is None:
        raise TargetValidationError(
            f"{target.target_id}: model_id {target.model_id!r} 未登錄於 registry"
        )
    if not entry.hosted_on(target.provider):
        raise TargetValidationError(
            f"{target.target_id}: {target.model_id} 未由 {target.provider} 託管"
        )
    if not entry.supports(capability):
        raise TargetValidationError(
            f"{target.target_id}: {target.model_id} 不支援 {capability.value}"
        )
    spec = get_provider(target.provider)
    if spec is None:
        raise TargetValidationError(
            f"{target.target_id}: 平台 {target.provider} 未登錄"
        )
    if not spec.supports(capability):
        raise TargetValidationError(
            f"{target.target_id}: {target.provider} 不支援 {capability.value}"
        )


def models_for_provider(provider: str) -> list[str]:
    """該平台在 catalog 中託管、且支援影片生成的模型。

    確認頁只讓使用者從這裡挑，不開放自由輸入：打錯一個字元的 model_id
    會通過表單卻在派工時才失敗，而那時他已經以為自己確認完了。
    """
    from pipeline.capability.model_registry import model_registry
    from pipeline.models.capability import Capability

    found: list[str] = []
    for entry in model_registry().models_on(provider):
        if not any(
            entry.supports(capability)
            for capability in (Capability.VIDEO_I2V, Capability.VIDEO_T2V)
        ):
            continue
        if entry.model_id not in found:
            found.append(entry.model_id)
    return sorted(found)


def save_target(
    target_id: str,
    model_id: str,
    model_version: str,
    ui_label: str,
    notes: str | None = None,
    confirmed: bool = False,
    catalog_path: Path | None = None,
) -> BenchmarkTarget:
    """更新比較對象並寫回 catalog。

    provisional 只有在使用者明確勾選「我已在平台確認」時才會變成 false，
    而且必須同時填入版本字串：沒有版本的「已確認」沒有意義，
    跨輪次比較時仍然無法得知當時用的是哪一版。

    寫入前先跑一次 validate_target。若寫進去才發現模型不存在，
    使用者會以為自己已經確認完成，直到 Build 失敗才知道。
    """
    from pipeline.models.capability import Capability

    path = catalog_path or CATALOG_PATH
    registry = load_targets(path)
    existing = registry.by_id(target_id)
    if existing is None:
        raise ValueError(f"未登錄的比較對象: {target_id}")

    model_id = (model_id or "").strip() or existing.model_id
    model_version = (model_version or "").strip()
    ui_label = (ui_label or "").strip()

    if confirmed and not model_version:
        raise ValueError(
            "確認平台版本時必須填入版本字串，否則日後無法得知當時用的是哪一版"
        )
    if confirmed and not ui_label:
        raise ValueError("確認平台版本時必須填入平台上顯示的選項名稱")

    updated = existing.model_copy(
        update={
            "model_id": model_id,
            "model_version": model_version or None,
            "ui_label": ui_label,
            "notes": (notes or "").strip() or existing.notes,
            "provisional": not confirmed,
        }
    )

    for capability in (Capability.VIDEO_I2V, Capability.VIDEO_T2V):
        validate_target(updated, capability)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("targets") or []
    for row in rows:
        if row.get("target_id") == target_id:
            row["model_id"] = updated.model_id
            row["model_version"] = updated.model_version
            row["ui_label"] = updated.ui_label
            row["provisional"] = updated.provisional
            if updated.notes:
                row["notes"] = updated.notes
            break
    else:  # pragma: no cover - by_id 已確認存在
        raise ValueError(f"catalog 中找不到 {target_id}")

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    targets.cache_clear()
    return updated


def resolve(target_id: str) -> BenchmarkTarget:
    target = targets().by_id(target_id)
    if target is None:
        raise ValueError(
            f"未登錄的 benchmark target: {target_id}. "
            f"可用: {', '.join(targets().target_ids)}"
        )
    return target
