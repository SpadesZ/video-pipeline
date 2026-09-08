# 檔案路徑: video-pipeline/pipeline/capability/transports/__init__.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Transport 層容器。
# 主要責任:
#   1. 收納人工與 API 兩種交付方式的 CapabilityAdapter 實作。
# 說明:
#   transport 只負責「如何把工作送出去、如何取回結果」。平台的能力範圍與
#   欄位限制由 ProviderSpec 表達，模型選擇由 RoutingPolicy 決定，
#   三者職責不重疊。
# --------------------------------------------------------------------------

from pipeline.capability.transports.manual import (
    ManualTransportAdapter,
    install_manual_adapters,
)

__all__ = ["ManualTransportAdapter", "install_manual_adapters"]
