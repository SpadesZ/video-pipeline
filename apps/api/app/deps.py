from functools import lru_cache
from typing import Generator
from sqlmodel import Session
from pipeline.settings import Settings, get_settings
from pipeline.db import get_session as db_get_session


@lru_cache()
def settings_dep() -> Settings:
    return get_settings()

def get_session() -> Generator[Session, None, None]:
    yield from db_get_session()
