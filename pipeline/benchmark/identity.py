# 檔案路徑: video-pipeline/pipeline/benchmark/identity.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   Benchmark 比較對象的不可變身份。
# 主要責任:
#   1. 定義一份資料「當時是用哪個模型產生的」這個問題的唯一答案。
#   2. 區分派工當下的身份與 catalog 目前的身份，兩者不得混為一談。
# 說明:
#   target_id 不是身份，它只是一個位置。同一個 target_id 今天指向 v1、
#   明天指向 v2，若報表以 target_id 聚合，兩個版本的成績會被加在一起，
#   而使用者看到的是「kling_default 的分數」——那個數字不對應任何
#   真實存在的模型。
#
#   身份因此必須包含版本，而且必須取自派工當下的快照。catalog 是可變的，
#   快照不是。任何以 catalog 現值回推歷史資料身份的行為，都會讓舊資料
#   被冒充成新版本。
#
#   ui_label 不納入 key：它是平台介面上的顯示字串，修正錯字不應該讓
#   整輪資料失效。平台若真的提供兩個不同選項，catalog 的規範是各建一個
#   target，那時 target_id 已經不同。ui_label 仍記在 snapshot 裡供追溯。
# --------------------------------------------------------------------------

from __future__ import annotations

from pydantic import BaseModel

# 派工快照中身份欄位的鍵。與 builder.dispatch_target 寫入的一致。
TARGET_ID_KEY = "_bm_target_id"
MODEL_VERSION_KEY = "_bm_model_version"
UI_LABEL_KEY = "_bm_ui_label"
PROVISIONAL_KEY = "_bm_provisional"

# 版本未填時的顯示字串。空字串與 None 視為同一件事：都代表未確認。
UNVERSIONED = "__unversioned__"


class BenchmarkIdentity(BaseModel):
    """一個比較對象在某個時間點的完整身份。

    兩份資料只有在這四個欄位全部相同時，才可以放進同一份報表比較。
    """

    target_id: str
    provider: str
    model_id: str
    model_version: str | None = None
    # 僅供顯示與追溯，不參與 key
    ui_label: str = ""

    model_config = {"protected_namespaces": ()}

    @property
    def version_key(self) -> str:
        version = (self.model_version or "").strip()
        return version or UNVERSIONED

    @property
    def key(self) -> str:
        """比較用的字串鍵。欄位以 | 相接，內容本身不含 |。"""
        return "|".join(
            [
                self.target_id,
                self.provider,
                self.model_id,
                self.version_key,
            ]
        )

    @property
    def display(self) -> str:
        version = (self.model_version or "").strip() or "版本未確認"
        return f"{self.provider}/{self.model_id}@{version}"

    def matches(self, other: "BenchmarkIdentity | None") -> bool:
        return other is not None and self.key == other.key


def from_target(target) -> BenchmarkIdentity:
    """catalog 目前的身份。只用於判斷「現在這一輪是什麼」。"""
    return BenchmarkIdentity(
        target_id=target.target_id,
        provider=target.provider,
        model_id=target.model_id,
        model_version=target.model_version,
        ui_label=target.ui_label,
    )


def from_job(job) -> BenchmarkIdentity | None:
    """派工當下的身份。歷史資料的唯一真相來源。

    快照沒有 target 標記時回傳 None：這份派工不屬於任何比較對象，
    不能猜一個給它。
    """
    parameters = (job.request_snapshot or {}).get("parameters") or {}
    target_id = parameters.get(TARGET_ID_KEY)
    if not target_id:
        return None
    return BenchmarkIdentity(
        target_id=str(target_id),
        provider=job.provider,
        model_id=job.model_id or "",
        # job 欄位優先，其次快照參數。兩者都是派工當下寫入的。
        model_version=job.model_version or parameters.get(MODEL_VERSION_KEY) or None,
        ui_label=str(parameters.get(UI_LABEL_KEY) or ""),
    )


def from_row(
    target_id: str,
    provider: str,
    model_id: str,
    model_version: str | None,
    ui_label: str = "",
) -> BenchmarkIdentity:
    """從已寫入的紀錄（如 CSV 列）重建身份。欄位原樣採用，不查 catalog。"""
    return BenchmarkIdentity(
        target_id=(target_id or "").strip(),
        provider=(provider or "").strip(),
        model_id=(model_id or "").strip(),
        model_version=((model_version or "").strip() or None),
        ui_label=(ui_label or "").strip(),
    )


def current_identities() -> dict[str, BenchmarkIdentity]:
    """catalog 目前每個 target 的身份，以 target_id 索引。"""
    from pipeline.benchmark.target import targets

    return {item.target_id: from_target(item) for item in targets().targets}


def current_keys() -> set[str]:
    """目前這一輪所有合格的身份 key。"""
    return {item.key for item in current_identities().values()}
