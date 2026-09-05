# 檔案路徑: video-pipeline/pipeline/assistant/knowledge.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   AI 助手的系統知識來源與檢索。
# 主要責任:
#   1. 載入 YAML 知識庫：資料模型、頁面與控制項、錯誤。
#   2. 依問題檢索相關條目，而非每次把整份塞進 prompt。
# 說明:
#   知識庫刻意與 source code 分離。原始碼描述的是實作，
#   使用者問的是「這是什麼」「我該做什麼」，兩者不是同一件事；
#   把 model 定義丟進 prompt 只會得到照著欄位名稱複述的回答。
#
#   檢索用關鍵字比對而非向量。條目數是數十筆而非數萬筆，
#   引入向量庫只會增加一個會壞的東西，換不到準確度。
# --------------------------------------------------------------------------

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

# 檢索時忽略的詞。中文不斷詞，改以字元 n-gram 比對，
# 這些詞出現在幾乎每個問題裡，留著只會讓每一則都命中。
STOPWORDS = frozenset(
    {
        "什麼", "怎麼", "為什麼", "可以", "如果", "這個", "那個", "現在",
        "應該", "需要", "the", "what", "why", "how", "and", "for", "is",
    }
)


class ControlDoc(BaseModel):
    id: str
    label: str = ""
    effect: str = ""


class PageDoc(BaseModel):
    route: str
    title: str = ""
    purpose: str = ""
    detail: str = ""
    controls: list[ControlDoc] = Field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"頁面 {self.route}（{self.title}）：{self.purpose}"]
        if self.detail:
            lines.append(self.detail.strip())
        for control in self.controls:
            lines.append(f"- 控制項「{control.label}」：{control.effect}")
        return "\n".join(lines)


class EntityDoc(BaseModel):
    key: str
    name: str
    summary: str = ""
    detail: str = ""

    def as_text(self) -> str:
        return f"{self.name}：{self.summary}\n{self.detail.strip()}".strip()


class ErrorDoc(BaseModel):
    code: str
    title: str = ""
    what: str = ""
    why: str = ""
    fix: str = ""
    next: str = ""

    def as_text(self) -> str:
        return (
            f"錯誤 {self.code}（{self.title}）\n"
            f"發生什麼：{self.what}\n"
            f"為什麼：{self.why.strip()}\n"
            f"怎麼修：{self.fix}\n"
            f"修完去哪：{self.next}"
        )


class KnowledgeCard(BaseModel):
    """檢索結果。source 讓回答可以說明依據來自哪一類知識。"""

    source: str
    key: str
    text: str


def _load(name: str) -> dict:
    path = KNOWLEDGE_DIR / name
    if not path.exists():  # pragma: no cover - 檔案隨程式碼一起發佈
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def entities() -> list[EntityDoc]:
    return [EntityDoc.model_validate(item) for item in _load("entities.yaml").get("entities", [])]


@lru_cache(maxsize=1)
def pages() -> list[PageDoc]:
    return [PageDoc.model_validate(item) for item in _load("pages.yaml").get("pages", [])]


@lru_cache(maxsize=1)
def errors() -> list[ErrorDoc]:
    return [ErrorDoc.model_validate(item) for item in _load("errors.yaml").get("errors", [])]


def page_for_route(route: str) -> PageDoc | None:
    """以最長前綴比對頁面。/benchmark/jobs/xxx 應命中單一派工頁而非 /benchmark。"""
    matches = [item for item in pages() if route.startswith(item.route)]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.route))


def error_for_code(code: str) -> ErrorDoc | None:
    return next((item for item in errors() if item.code == code), None)


def _tokens(text: str) -> tuple[set[str], set[str]]:
    """把文字切成可比對的片段，識別字與中文片段分開回傳。

    中英混雜且沒有斷詞器，所以英數以單字切，中文以 2-gram 與 3-gram 切。
    n-gram 足以區分「連戲」「代表作」「派工」這類詞，
    也不會像單字比對那樣讓「的」命中所有條目。

    兩者分開是因為權重不同：request_hash 這種識別字命中一次就幾乎確定
    相關，中文 2-gram 命中一次多半是巧合。
    """
    lowered = text.lower()
    words = {
        item
        for item in re.findall(r"[a-z0-9_]{2,}", lowered)
        if item not in STOPWORDS
    }
    fragments: set[str] = set()
    for run in re.findall(r"[一-鿿]+", lowered):
        for size in (2, 3):
            for index in range(len(run) - size + 1):
                fragment = run[index : index + size]
                if fragment not in STOPWORDS:
                    fragments.add(fragment)
    return words, fragments


# 識別字命中的權重。request_hash、ContinuityQC 這類詞出現在問題裡時，
# 使用者問的幾乎確定就是那一則；中文片段則需要多個一起命中才算數。
IDENTIFIER_WEIGHT = 2


def _score(query: tuple[set[str], set[str]], text: str) -> int:
    words, fragments = _tokens(text)
    return (
        len(query[0] & words) * IDENTIFIER_WEIGHT + len(query[1] & fragments)
    )


# 命中門檻。單一片段相符多半是巧合——「不能」會同時出現在好幾則
# 說明裡。要求至少兩個片段相符，才把條目當成真的相關。
MIN_OVERLAP = 2


def retrieve(question: str, limit: int = 4) -> list[KnowledgeCard]:
    """依問題檢索知識條目。

    只有問到系統概念時才會有命中，一般的「下一步做什麼」由 workflow
    狀態回答，不需要動用知識庫。無命中就回空 list，不硬塞。
    """
    query = _tokens(question)
    if not any(query):
        return []

    scored: list[tuple[int, KnowledgeCard]] = []
    for item in entities():
        overlap = _score(
            query, f"{item.name} {item.key} {item.summary} {item.detail}"
        )
        if overlap >= MIN_OVERLAP:
            scored.append(
                (overlap, KnowledgeCard(source="entity", key=item.key, text=item.as_text()))
            )
    for item in errors():
        overlap = _score(
            query, f"{item.code} {item.title} {item.what} {item.why} {item.fix}"
        )
        if overlap >= MIN_OVERLAP:
            scored.append(
                (overlap, KnowledgeCard(source="error", key=item.code, text=item.as_text()))
            )

    scored.sort(key=lambda pair: (-pair[0], pair[1].key))
    return [card for _, card in scored[:limit]]


def system_summary() -> str:
    """給模型的常駐系統描述。刻意簡短，細節走檢索。"""
    return (
        "這是一套 AI 影片製作流水線。核心分層："
        "NarrativeIR 是故事真相，ShotPlan 是導演意圖，"
        "AssetVariant 是實際生成的產物，EditDecision 是剪輯真相。"
        "LAVA 是能力路由器：呼叫端描述需要的能力，由它決定用哪個模型與平台，"
        "不是單純的 API 轉發。"
        "目前 V1 階段所有影片生成都由真人在各平台手動完成，"
        "系統不呼叫任何影片生成 API。"
        "Benchmark 的歸屬一律走 CapabilityJob 派工血緣，不以平台名稱推測。"
    )
