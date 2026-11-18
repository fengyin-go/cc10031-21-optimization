"""异常恢复追踪路由 — 重复异常聚合、异常组台账与恢复入口。

恢复入口（重试/确认恢复）只处理仍未关闭的异常组；
同一执行对象同一时刻只暴露一个活动恢复入口。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    BoundaryPayload,
    FailureRecordPayload,
    RecoveryResultPayload,
)
from app.services.anomaly_recovery import (
    BOUNDARIES,
    anomaly_groups,
)
from app.services.execution_log import ExecutionLogService

router = APIRouter(prefix="/api/anomaly-recovery", tags=["异常恢复追踪"])

_execution_service = ExecutionLogService()


@router.get("/groups")
def list_groups(
    exec_no: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict:
    """异常组列表：可按执行编号、组状态过滤；默认返回含历史的完整列表。"""
    return {
        "items": anomaly_groups.list_groups(exec_no=exec_no, status=status),
        "total": len(anomaly_groups.list_groups(exec_no=exec_no, status=status)),
        "stats": anomaly_groups.stats(),
    }


@router.get("/groups/active")
def list_active_groups() -> dict:
    """恢复入口工作台：只返回仍未关闭的异常组。"""
    items = anomaly_groups.active_groups()
    return {
        "items": items,
        "total": len(items),
        "empty": len(items) == 0,
        "empty_text": "暂无失败：所有异常组均已关闭，可在历史台账中查询",
        "stats": anomaly_groups.stats(),
    }


@router.get("/groups/{group_no}")
def get_group_detail(group_no: str) -> dict:
    """异常组详情：成员事件清单、恢复入口与处理结果。"""
    try:
        return anomaly_groups.get_group_detail(group_no)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"异常组 {group_no} 不存在")


@router.get("/events")
def list_events(
    exec_no: str | None = Query(default=None),
    group_no: str | None = Query(default=None),
    history: bool = Query(default=True),
) -> dict:
    """异常事件台账明细，含历史事件查询。"""
    items = anomaly_groups.list_events(
        exec_no=exec_no, group_no=group_no, include_history=history,
    )
    return {"items": items, "total": len(items)}


@router.get("/ledger")
def ledger(exec_no: str | None = Query(default=None)) -> dict:
    """聚合后的异常事件台账数据集：一行一个异常组。"""
    items = anomaly_groups.ledger()
    if exec_no:
        items = [row for row in items if row["exec_no"] == exec_no]
    return {"items": items, "total": len(items)}


@router.get("/reconcile/{exec_no}")
def reconcile(exec_no: str) -> dict:
    """按执行编号核对处理项与异常事件台账、恢复入口的一致性。"""
    return anomaly_groups.reconcile(exec_no)


@router.post("/failures")
def record_failure(payload: FailureRecordPayload) -> dict:
    """失败上报：生成新事件编号并聚合进异常组，恢复入口唯一化。"""
    try:
        result = anomaly_groups.record_failure(
            exec_no=payload.exec_no,
            error_type=payload.error_type,
            step=payload.step,
            detail=payload.detail,
            bot_ref=payload.bot_ref,
            boundary=payload.boundary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.post("/retry/{exec_no}")
def retry_execution(exec_no: str, payload: FailureRecordPayload) -> dict:
    """业务入口：执行记录重试，联动刷新待办与异常恢复追踪。"""
    if payload.exec_no and payload.exec_no != exec_no:
        raise HTTPException(
            status_code=400, detail="路径执行编号与请求体不一致",
        )
    return _execution_service.retry_failure(
        exec_no=exec_no,
        error_type=payload.error_type,
        step=payload.step,
        detail=payload.detail,
        boundary=payload.boundary or "并入当前组",
        row_count=0,
    )


@router.post("/groups/{group_no}/retry")
def retry_group(group_no: str) -> dict:
    """恢复入口重试：只处理仍未关闭的异常组，中断时给出原因。"""
    try:
        result = anomaly_groups.retry_group(group_no)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"异常组 {group_no} 不存在")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.post("/groups/{group_no}/resolve")
def resolve_group(group_no: str, payload: RecoveryResultPayload) -> dict:
    """确认恢复并把终态按异常组编号写回执行记录，并发两次提交只算一次。"""
    try:
        anomaly_groups.get_group_detail(group_no)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"异常组 {group_no} 不存在")
    return _execution_service.write_back_recovery(
        group_no=group_no, result_note=payload.note,
    )


@router.post("/groups/{group_no}/ignore")
def ignore_group(group_no: str) -> dict:
    try:
        result = anomaly_groups.ignore_group(group_no)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"异常组 {group_no} 不存在")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.post("/groups/{group_no}/boundary")
def switch_boundary(group_no: str, payload: BoundaryPayload) -> dict:
    """切换持续故障处理边界（并入当前组 / 关旧开新）。"""
    if payload.boundary not in BOUNDARIES:
        raise HTTPException(
            status_code=400,
            detail=f"处理边界仅支持：{'、'.join(BOUNDARIES)}",
        )
    try:
        result = anomaly_groups.set_boundary(group_no, payload.boundary)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"异常组 {group_no} 不存在")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.get("/invariants")
def invariants() -> dict:
    """巡检：核对恢复入口只处理仍未关闭的异常组等不变量。"""
    violations = anomaly_groups.validate_invariants()
    return {"ok": not violations, "violations": violations}
