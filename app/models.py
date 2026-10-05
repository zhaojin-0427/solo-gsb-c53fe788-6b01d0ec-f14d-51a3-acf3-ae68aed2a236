"""请求/响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---- 项目 / 文档 ----------------------------------------------------------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DocumentIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    # 词元边界 [start, end)；不传时由后端按 Unicode 字素切分（每字符一个词元）
    token_bounds: list[tuple[int, int]] | None = None


# ---- 标签 -----------------------------------------------------------------
class LabelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class LabelRuleIn(BaseModel):
    kind: Literal["nesting", "mutex"]
    label_a_id: int
    label_b_id: int
    # nesting 时 a 为外层、b 为内层


# ---- 标注者 ---------------------------------------------------------------
class AnnotatorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    weight: int = Field(ge=1, le=10)


# ---- 标注 -----------------------------------------------------------------
class AnnotationIn(BaseModel):
    annotator_id: int
    label_id: int
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class AnnotationBatchIn(BaseModel):
    annotations: list[AnnotationIn]


# ---- 共识 / 覆盖 / 报告 ----------------------------------------------------
class OverrideIn(BaseModel):
    action: Literal["select", "deselect"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    label_id: int
    reason: str = Field(min_length=1, max_length=2000)
