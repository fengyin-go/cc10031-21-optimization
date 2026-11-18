"""执行记录领域服务 — 记录和查询 RPA 执行日志，输出 domain_snapshot。

数据工程核心：数据校验与去重策略执行 — 跨模块数据产物

异常恢复联动：
- retry_failure 模拟失败执行重试，异常事件聚合进同一异常组；
- reconcile_recovery 按执行编号核对处理项与异常事件台账；
- write_back_recovery 按异常组编号把最终恢复结果写回执行记录，
  并联动刷新执行记录侧的待办状态。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.anomaly_recovery import (
    BOUNDARY_MERGE,
    BOUNDARY_REOPEN,
    GROUP_TERMINALS,
    anomaly_groups,
)
from app.services.base import BaseModuleService
from app.store import store


class ExecutionLogService(BaseModuleService):
    MODULE = "execution_log"
    ENTITY = "执行日志"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["执行编号", "所属流程", "开始时间", "结束时间", "执行状态", "采集行数", "错误摘要"]
    REQUIRED_FIELDS = ["执行编号", "所属流程", "开始时间"]
    STATUS_ORDER = ["执行中", "执行成功", "执行失败", "已超时", "已取消"]
    ACTION_RULES = {"归档日志": "执行成功"}
    NEGATIVE_ACTIONS = []

    def query_execution_history(self,
                                 bot_name: str | None = None,
                                 days: int = 7) -> list[dict[str, Any]]:
        """查询执行历史记录。"""
        rows = store.rows(self.MODULE)
        if bot_name:
            rows = [
                r for r in rows if bot_name in r.get("所属流程", "")
            ]
        return [
            {
                "id": r.get("id"), "exec_no": r.get("执行编号"),
                "bot": r.get("所属流程"), "status": r.get("status"),
                "start": r.get("开始时间"), "end": r.get("结束时间"),
                "error": r.get("错误摘要"),
            }
            for r in rows
        ]

    def get_execution_stats(self) -> dict[str, Any]:
        """获取执行统计概览。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        success = sum(1 for r in rows if r.get("status") == "执行成功")
        failed = sum(1 for r in rows if r.get("status") == "执行失败")
        return {
            "total_executions": total,
            "success_rate": round(success / max(total, 1), 2),
            "success": success,
            "failed": failed,
            "in_progress": sum(1 for r in rows if r.get("status") == "执行中"),
            "timeout": sum(1 for r in rows if r.get("status") == "已超时"),
        }

    # ------------------------------------------------------------------
    # 异常恢复联动
    # ------------------------------------------------------------------

    def retry_failure(self, exec_no: str, error_type: str,
                      step: str, detail: str,
                      boundary: str = BOUNDARY_MERGE,
                      row_count: int = 0) -> dict[str, Any]:
        """模拟失败执行的一次重试。

        每次重试都按当前台账行数生成新事件编号，但重复异常被聚合进同一
        执行对象的异常组，恢复待办不会膨胀。
        """
        if boundary not in (BOUNDARY_MERGE, BOUNDARY_REOPEN):
            return {"ok": False, "message": f"未知处理边界：{boundary}"}

        source = next(
            (r for r in store.rows(self.MODULE)
             if r.get("执行编号") == exec_no),
            None,
        )
        bot_ref = str(source["所属流程"]) if source else exec_no

        rows = store.rows(self.MODULE)
        retry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        retry_row = {
            "id": retry_id,
            "执行编号": exec_no,
            "所属流程": bot_ref,
            "开始时间": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "结束时间": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "执行状态": "执行失败",
            "采集行数": row_count,
            "错误摘要": detail,
            "status": "执行失败",
            "pending": True,
            "abnormal": True,
            "异常组编号": "",
            "恢复结果": "",
        }
        rows.append(retry_row)

        recorded = anomaly_groups.record_failure(
            exec_no=exec_no,
            error_type=error_type,
            step=step,
            detail=detail,
            bot_ref=bot_ref,
            boundary=boundary,
        )
        retry_row["异常组编号"] = recorded["group_no"]
        return {
            "ok": True,
            "execution_id": retry_id,
            "exec_no": exec_no,
            **recorded,
        }

    def reconcile_recovery(self, exec_no: str) -> dict[str, Any]:
        """核对执行记录处理项与聚合后的异常事件台账是否一致。"""
        return anomaly_groups.reconcile(exec_no)

    def write_back_recovery(self, group_no: str,
                            result_note: str = "异常组已恢复，执行记录同步更新") -> dict[str, Any]:
        """以异常组编号把最终恢复结果写回该组对应的执行记录。

        沿用既有处理边界：只处理仍未关闭的异常组；终态写回后联动刷新
        执行记录侧待办，并核对聚合处理项与异常事件台账。
        """
        resolved = anomaly_groups.resolve_group(group_no, note=result_note)
        if not resolved.get("ok"):
            # 已关闭组的重复提交只返回幂等提示，不重复写回
            return {
                **resolved,
                "write_back_rows": 0,
            }

        group = resolved["group"]
        exec_no = group["exec_no"]
        terminal = group["status"]
        written = 0
        for row in store.rows(self.MODULE):
            if row.get("执行编号") != exec_no:
                continue
            row["异常组编号"] = group_no
            row["恢复结果"] = f"{group_no}:{terminal}｜{result_note}"
            if terminal == "已恢复":
                # 联动刷新待办：失败执行的恢复项关闭
                row["pending"] = False
                row["abnormal"] = False
            written += 1

        reconciliation = anomaly_groups.reconcile(exec_no)
        return {
            "ok": True,
            "message": (
                f"异常组 {group_no} 终态「{terminal}」已按组编号写回 "
                f"{written} 条执行记录，待办已联动刷新"
            ),
            "group_no": group_no,
            "exec_no": exec_no,
            "terminal": terminal,
            "write_back_rows": written,
            "reconciliation": reconciliation,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        stats = self.get_execution_stats()
        return {**base, **stats}
