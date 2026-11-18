"""异常日志领域服务 — 记录和追踪 RPA 执行异常，输出 domain_snapshot。

数据工程核心：水位追踪与增量采集调度 — 跨模块数据产物

重复异常聚合：失败登记不再逐条生成恢复待办，而是交给 AnomalyGroupManager
按执行编号聚合成异常组，同一执行对象只保留一个活动恢复入口。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.anomaly_recovery import (
    BOUNDARY_MERGE,
    GROUP_ACTIVE,
    anomaly_groups,
)
from app.store import store


class AnomalyLogService(BaseModuleService):
    MODULE = "anomaly_log"
    ENTITY = "异常事件"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "事件编号", "执行编号", "异常组编号", "关联流程", "异常类型",
        "异常详情", "发生步骤", "发生时间", "恢复状态",
    ]
    REQUIRED_FIELDS = ["事件编号", "关联流程", "异常类型"]
    STATUS_ORDER = ["未处理", "处理中", "已恢复", "已忽略"]
    ACTION_RULES = {"标记恢复": "未处理", "忽略事件": "处理中"}
    NEGATIVE_ACTIONS = ["忽略事件"]

    def log_exception(self, bot_ref: str, step: str,
                      error_type: str, detail: str,
                      exec_no: str = "",
                      boundary: str | None = None) -> dict[str, Any]:
        """记录一条执行异常事件并完成异常组聚合。

        每次失败都会生成新的事件编号；恢复入口按执行编号聚合，
        同一执行对象重试多次也只产生一个活动恢复入口。
        """
        target_exec = exec_no or bot_ref
        return anomaly_groups.record_failure(
            exec_no=target_exec,
            error_type=error_type,
            step=step,
            detail=detail,
            bot_ref=bot_ref,
            boundary=boundary or BOUNDARY_MERGE,
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """事件动作联动异常组：整组恢复/忽略，保证台账与终态一致。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        group_no = entry.get("异常组编号")
        if not group_no:
            # 历史游离事件兜底走通用状态流转
            return super().run_action(entry_id, action)

        if action == "标记恢复":
            result = anomaly_groups.resolve_group(group_no)
        elif action == "忽略事件":
            result = anomaly_groups.ignore_group(group_no)
        else:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        refreshed = store.find(self.MODULE, entry_id)
        if result.get("ok"):
            return refreshed, result["message"]
        return refreshed, result.get("message", "动作未生效")

    def get_pending_anomalies(self) -> list[dict[str, Any]]:
        """获取未关闭异常事件（恢复入口只处理仍未关闭的异常组）。"""
        active_group_nos = {
            g["group_no"] for g in anomaly_groups.active_groups()
        }
        rows = store.rows(self.MODULE)
        return [
            {
                "id": r.get("id"), "event_no": r.get("事件编号"),
                "exec_no": r.get("执行编号"),
                "group_no": r.get("异常组编号"),
                "bot": r.get("关联流程"), "type": r.get("异常类型"),
                "step": r.get("发生步骤"), "detail": r.get("异常详情"),
                "time": r.get("发生时间"),
            }
            for r in rows
            if r.get("异常组编号") in active_group_nos
            or (not r.get("异常组编号")
                and r.get("status") in ("未处理", "处理中"))
        ]

    def anomaly_ledger(self, exec_no: str | None = None) -> list[dict[str, Any]]:
        """聚合后的异常事件台账数据集。"""
        ledger = anomaly_groups.ledger()
        if exec_no:
            ledger = [row for row in ledger if row["exec_no"] == exec_no]
        return ledger

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        group_stats = anomaly_groups.stats()
        return {
            **base,
            "unresolved": sum(
                1 for r in rows if r.get("status") in ("未处理", "处理中")
            ),
            "resolved": sum(1 for r in rows if r.get("status") == "已恢复"),
            "ignored": sum(1 for r in rows if r.get("status") == "已忽略"),
            "recovery_pending": store.recovery.pending_count(),
            "active_groups": group_stats["active_groups"],
            "group_total": group_stats["group_total"],
            "active_recovery_entries": group_stats["active_recovery_entries"],
        }
