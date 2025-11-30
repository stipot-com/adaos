from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class CodeEntry(BaseModel):
    """
    Запись о файле/сущности из code_map.yaml.
    """
    path: str
    tags: Optional[List[str]] = None
    funcs: Optional[str] = None
    endpoints: Optional[str] = None
    exports: Optional[str] = None
    types: Optional[str] = None
    interfaces: Optional[str] = None
    classes: Optional[List[dict[str, Any]]] = None


class ModelAnalysis(BaseModel):
    """
    Результат анализа нейросетью.
    """
    question: str = Field(..., description="Запрос пользователя / задача анализа")
    used_files: List[CodeEntry] = Field(
        default_factory=list,
        description="Файлы из code_map.yaml, по которым строился контекст",
    )
    answer: str = Field(..., description="Ответ нейронной модели в текстовом виде")
