# 檔案路徑: video-pipeline/pipeline/benchmark/attribution.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   候選與比較對象的歸屬解析。
# 主要責任:
#   1. 以 CapabilityJob 血緣解析候選所屬的 BenchmarkTarget。
#   2. 提供 benchmark 專用的候選選定，與 production 的 SELECTED 分離。
# 說明:
#   以 (shot_id, provider) 推測歸屬是不安全的：同一平台可能同時測多個
#   版本，屆時兩個 target 的候選會被混為一談。歸屬只能走派工血緣，
#   也就是 variant.job_id -> CapabilityJob.request_snapshot 中的 target 標記。
#   沒有血緣的候選無法歸屬，一律排除於 benchmark 之外，而非猜一個。
# --------------------------------------------------------------------------

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from pipeline.benchmark.target import BenchmarkTarget, targets
from pipeline.models.variant import AssetVariant, CapabilityJob

TARGET_PARAMETER_KEY = "_bm_target_id"


class AmbiguousAttribution(ValueError):
    """同一組條件對應到多個候選，無法安全歸屬。"""


def resolve_variant_target_id(variant: AssetVariant) -> str | None:
    """解析候選所屬的比較對象。

    僅接受派工血緣。無 job_id、工作不存在或快照未帶 target 標記時
    回傳 None，代表這支候選不屬於任何比較對象，不得計入 benchmark。
    """
    if not variant.job_id:
        return None

    from pipeline.db import engine

    with Session(engine) as session:
        job = session.get(CapabilityJob, variant.job_id)

    if job is None:
        return None
    snapshot = job.request_snapshot or {}
    parameters = snapshot.get("parameters") or {}
    target_id = parameters.get(TARGET_PARAMETER_KEY)
    return str(target_id) if target_id else None


class AttributedVariant(BaseModel):
    variant_id: str
    shot_id: str
    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    local_path: str | None = None
    file_name: str | None = None
    benchmark_selected: bool = False

    model_config = {"protected_namespaces": ()}


class AttributionIndex(BaseModel):
    """專案內所有可歸屬候選的索引。"""

    project_id: str
    items: list[AttributedVariant] = Field(default_factory=list)
    unattributed: list[str] = Field(default_factory=list)

    def for_target(self, target_id: str) -> list[AttributedVariant]:
        return [item for item in self.items if item.target_id == target_id]

    def for_shot(self, target_id: str, shot_id: str) -> list[AttributedVariant]:
        return [
            item
            for item in self.items
            if item.target_id == target_id and item.shot_id == shot_id
        ]

    def by_variant_id(self, variant_id: str) -> AttributedVariant | None:
        return next(
            (item for item in self.items if item.variant_id == variant_id), None
        )

    def find_by_file(
        self, target_id: str, shot_id: str, file_name: str
    ) -> AttributedVariant:
        """依檔名精確對應。多筆相符時拒絕，不挑第一個。"""
        matches = [
            item
            for item in self.for_shot(target_id, shot_id)
            if item.file_name == file_name
        ]
        if not matches:
            raise LookupError(
                f"{target_id}/{shot_id} 找不到檔名為 {file_name!r} 的候選"
            )
        if len(matches) > 1:
            raise AmbiguousAttribution(
                f"{target_id}/{shot_id} 有 {len(matches)} 支候選同名 {file_name!r}，"
                "無法安全對應"
            )
        return matches[0]


def build_index(project_id: str) -> AttributionIndex:
    """建立專案的歸屬索引。無血緣的候選列入 unattributed。"""
    from pathlib import Path

    from pipeline.db import engine

    registry = targets()
    index = AttributionIndex(project_id=project_id)

    with Session(engine) as session:
        variants = session.exec(
            select(AssetVariant).where(AssetVariant.project_id == project_id)
        ).all()
        jobs = {
            job.job_id: job
            for job in session.exec(
                select(CapabilityJob).where(CapabilityJob.project_id == project_id)
            ).all()
        }

    for variant in variants:
        job = jobs.get(variant.job_id) if variant.job_id else None
        target_id = None
        if job is not None:
            parameters = (job.request_snapshot or {}).get("parameters") or {}
            target_id = parameters.get(TARGET_PARAMETER_KEY)

        target: BenchmarkTarget | None = (
            registry.by_id(str(target_id)) if target_id else None
        )
        if target is None:
            index.unattributed.append(variant.variant_id)
            continue

        index.items.append(
            AttributedVariant(
                variant_id=variant.variant_id,
                shot_id=variant.shot_id,
                target_id=target.target_id,
                provider=target.provider,
                model_id=target.model_id,
                model_version=target.model_version,
                local_path=variant.local_path,
                file_name=(
                    Path(variant.local_path).name if variant.local_path else None
                ),
                benchmark_selected=bool(variant.benchmark_selected),
            )
        )

    index.items.sort(key=lambda item: (item.target_id, item.shot_id, item.variant_id))
    return index


def select_benchmark_candidate(
    project_id: str, variant_id: str
) -> AttributedVariant:
    """將某支候選設為該比較對象在該鏡頭的代表作。

    與 production 的 SELECTED 完全分離：同一顆鏡頭可以有四個比較對象
    各自選一支，這正是比較所需。互斥範圍限縮在同一個 (target, shot)。
    """
    from pipeline.db import engine
    from pipeline.stages.variant_importer import load_owned_variant

    target_id = None
    with Session(engine) as session:
        variant = load_owned_variant(session, project_id, variant_id)

    target_id = resolve_variant_target_id(variant)
    if target_id is None:
        raise ValueError(
            f"候選 {variant_id} 沒有派工血緣，無法歸屬到任何比較對象"
        )

    index = build_index(project_id)
    siblings = [
        item
        for item in index.for_shot(target_id, variant.shot_id)
        if item.variant_id != variant_id
    ]

    with Session(engine) as session:
        for sibling in siblings:
            row = session.get(AssetVariant, sibling.variant_id)
            if row is not None and row.benchmark_selected:
                row.benchmark_selected = False
                session.add(row)
        row = session.get(AssetVariant, variant_id)
        row.benchmark_selected = True
        session.add(row)
        session.commit()

    chosen = build_index(project_id).by_variant_id(variant_id)
    if chosen is None:  # pragma: no cover - 前面已驗證歸屬
        raise ValueError(f"候選 {variant_id} 選定後仍無法歸屬")
    return chosen


def auto_select_benchmark_candidates(project_id: str) -> int:
    """為尚未選定的 (target, shot) 自動指定代表作。

    確定性規則：取該組合中最早匯入的候選。人工可再手動改選，
    但至少保證每個組合都有一支可供連戲比較，不會因為漏選而缺資料。
    回傳新選定的組合數。
    """
    from pipeline.db import engine

    index = build_index(project_id)
    groups: dict[tuple[str, str], list[AttributedVariant]] = {}
    for item in index.items:
        groups.setdefault((item.target_id, item.shot_id), []).append(item)

    selected = 0
    with Session(engine) as session:
        for items in groups.values():
            if any(item.benchmark_selected for item in items):
                continue
            rows = [
                session.get(AssetVariant, item.variant_id) for item in items
            ]
            rows = [row for row in rows if row is not None]
            if not rows:
                continue
            rows.sort(key=lambda row: (row.created_at, row.variant_id))
            rows[0].benchmark_selected = True
            session.add(rows[0])
            selected += 1
        session.commit()
    return selected
