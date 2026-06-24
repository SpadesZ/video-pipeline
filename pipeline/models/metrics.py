# 檔案路徑: video-pipeline/pipeline/models/metrics.py
# 產生時間: 2026-06-24 21:19 +08:00
# 版本: v1.0
# 模組定位:
#   流量與轉化收益數據閉環分析模型。
# 主要責任:
#   1. 記錄影片發布後之流量數據 (CTR, AVD, RPM) 與聯盟行銷點擊。
#   2. 定義決策方向 (Scale, Iterate, Kill) 用以調整下一輪選題策略。
# --------------------------------------------------------------------------

from enum import StrEnum

from pydantic import BaseModel



class ScaleDecision(StrEnum):
    UNKNOWN = "unknown"
    SCALE = "scale"
    ITERATE = "iterate"
    KILL = "kill"


class MetricsDecision(BaseModel):
    project_id: str
    window: str = "manual"
    ctr: float | None = None
    average_view_duration_seconds: float | None = None
    retention_30s: float | None = None
    rpm: float | None = None
    affiliate_clicks: int | None = None
    decision: ScaleDecision = ScaleDecision.UNKNOWN
    notes: str | None = None

