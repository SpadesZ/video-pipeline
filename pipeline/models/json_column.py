# 檔案路徑: video-pipeline/pipeline/models/json_column.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Pydantic 模型與 JSON 欄位之間的 SQLAlchemy 型別裝飾器。
# 主要責任:
#   1. 提供 PydanticJSON，供各 SQLModel 資料表宣告巢狀 Pydantic 欄位。
# 說明:
#   本型別原先定義於 production_artifact.py，抽離至此以避免資料表模組與
#   巢狀模型模組互相 import 造成循環相依。production_artifact 仍會
#   re-export PydanticJSON，既有 import 路徑不受影響。
# --------------------------------------------------------------------------

from typing import Type

from pydantic import BaseModel
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator


class PydanticJSON(TypeDecorator):
    """
    SQLAlchemy TypeDecorator that transparently serializes/deserializes Pydantic models
    to/from JSON columns. Supports both single models and lists of models.
    """
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_model: Type[BaseModel], is_list: bool = False):
        super().__init__()
        self.pydantic_model = pydantic_model
        self.is_list = is_list

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if self.is_list:
            if isinstance(value, list):
                return [item.model_dump(mode='json') if isinstance(item, BaseModel) else item for item in value]
        else:
            if isinstance(value, BaseModel):
                return value.model_dump(mode='json')
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if self.is_list:
            if isinstance(value, list):
                return [self.pydantic_model.model_validate(item) for item in value]
        else:
            if isinstance(value, dict):
                return self.pydantic_model.model_validate(value)
        return value
