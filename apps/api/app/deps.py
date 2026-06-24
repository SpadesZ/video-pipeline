from functools import lru_cache
from pipeline.settings import Settings, get_settings


@lru_cache()
def settings_dep() -> Settings:
    return get_settings()

