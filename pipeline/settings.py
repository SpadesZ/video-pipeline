# 檔案路徑: video-pipeline/pipeline/settings.py
# 產生時間: 2026-06-24 21:12 +08:00
# 版本: v1.0
# 模組定位:
#   Pydantic BaseSettings 全域設定檔管理。負責讀取系統環境變數與 .env 密鑰檔案。
# 主要責任:
#   1. 定義資料庫與 Redis 等服務的 SecretStr 連線 URL，防止 debug 輸出洩漏。
#   2. 導出 get_settings() 初始化設定。
# 綠標提醒:
#   - 敏感憑證屬性 (例如 database_url, redis_url) 須以 SecretStr 型態保護，不可直接洩漏。
#   - 預設值僅為 local fallback，正式環境將覆寫為環境變數。
# --------------------------------------------------------------------------

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from pipeline.secrets import load_runtime_secrets



class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    database_url: SecretStr = SecretStr("postgresql+psycopg://video_pipeline:video_pipeline@postgres:5432/video_pipeline")
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    celery_broker_url: SecretStr = SecretStr("redis://redis:6379/0")
    celery_result_backend: SecretStr = SecretStr("redis://redis:6379/1")

    asr_provider: str = "import"
    default_cue_seconds: int = Field(default=8, ge=3, le=30)

    llm_provider: str = "none"
    llm_api_key: SecretStr | None = None
    secrets_file: str | None = None


def get_settings() -> Settings:
    load_runtime_secrets()
    return Settings()
