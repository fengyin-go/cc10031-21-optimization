"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class FailureRecordPayload(BaseModel):
    """失败事件上报：同一执行编号的重复失败由后端聚合。"""
    exec_no: str
    error_type: str
    step: str
    detail: str = ""
    bot_ref: str = ""
    boundary: str | None = None


class BoundaryPayload(BaseModel):
    """持续故障处理边界切换：并入当前组 / 关旧开新。"""
    boundary: str


class RecoveryResultPayload(BaseModel):
    """恢复结果写回：以异常组编号确认最终处理终态。"""
    note: str = "异常组已恢复，执行记录同步更新"
