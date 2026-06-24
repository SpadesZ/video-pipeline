# 檔案路徑: video-pipeline/pipeline/secrets.py
# 產生時間: 2026-06-24 21:13 +08:00
# 版本: v1.0
# 模組定位:
#   動態載入環境變數及本機憑證檔。
# 主要責任:
#   1. 解析專案根目錄，絕對定位 secrets/.env.local 與 .env 進行加載。
#   2. 避免工作目錄改變時導致的相對路徑找不到檔案問題。
# 綠標提醒:
#   - 呼叫 load_dotenv 時預設 override=False，避免覆寫手動設定好的系統環境變數。
# --------------------------------------------------------------------------

from pathlib import Path
import os
import logging

from dotenv import load_dotenv


logger = logging.getLogger("Secrets_Loader")
logger.setLevel(logging.INFO)


def load_runtime_secrets() -> None:
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        os.environ.get("SECRETS_FILE"),
        str(project_root / "secrets" / ".env.local"),
        str(project_root / ".env"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and path.is_file():
            abs_path = path.resolve()
            load_dotenv(abs_path, override=False)
            logger.info(f"Loaded runtime secrets from: {abs_path}")


