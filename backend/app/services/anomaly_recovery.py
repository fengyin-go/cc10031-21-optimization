"""异常恢复追踪 — 重复异常聚合与恢复入口管理。

背景：同一执行对象（按执行编号识别）重试失败时，旧实现每次都新建异常事件和
恢复记录，重试五次就产生五条待恢复任务，恢复待办被重复异常撑大。

本管理器把同一执行对象的重复异常事件聚合为「异常组」：

- 异常事件（anomaly_log 表行）与异常组同时记录，每条事件都带异常组编号；
- 同一执行对象同一时刻只允许存在一个「活动」异常组，组与恢复入口一一对应；
- 恢复入口（FailureRecovery 记录）只处理仍未关闭的异常组；
- 持续故障到达新事件时支持两套处理边界：
  * 并入当前组（默认）：新事件并入活动组，不新增恢复入口；
  * 关旧开新：关闭当前组（终态=已关闭），新开持续故障组并接管恢复入口；
- 并发/重复提交在组状态维度幂等，同一组只处理一次。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

GROUP_ACTIVE = "活动"
GROUP_RECOVERED = "已恢复"
GROUP_CLOSED = "已关闭"
GROUP_IGNORED = "已忽略"
GROUP_TERMINALS = (GROUP_RECOVERED, GROUP_CLOSED, GROUP_IGNORED)

BOUNDARY_MERGE = "并入当前组"
BOUNDARY_REOPEN = "关旧开新"
BOUNDARIES = (BOUNDARY_MERGE, BOUNDARY_REOPEN)

_EVENT_STATUS_BY_TERMINAL = {
    GROUP_RECOVERED: "已恢复",
    GROUP_CLOSED: "已忽略",
    GROUP_IGNORED: "已忽略",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class AnomalyGroupManager:
    """异常组聚合管理器：事件台账、异常组、恢复入口三者一致性的唯一入口。"""

    MODULE = "anomaly_log"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._group_seq = 0
        self._groups: list[dict[str, Any]] = []
        self._bootstrapped = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _next_group_no(self) -> str:
        self._group_seq += 1
        return f"AGP-{self._group_seq:04d}"

    def _new_group(self, exec_no: str, bot_ref: str,
                   error_type: str, step: str, boundary: str) -> dict[str, Any]:
        group_no = self._next_group_no()
        now = _now()
        group: dict[str, Any] = {
            "group_no": group_no,
            "exec_no": exec_no,
            "bot_ref": bot_ref,
            "error_type": error_type,
            "step": step,
            "boundary": boundary,
            "status": GROUP_ACTIVE,
            "event_ids": [],
            "event_nos": [],
            "recovery_id": None,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "result_note": "",
        }
        recovery = store.recovery.log_group(
            module=self.MODULE,
            group_no=group_no,
            exec_no=exec_no,
            step=step,
            error_detail=error_type,
        )
        group["recovery_id"] = recovery.recovery_id
        self._groups.append(group)
        return group

    def _find_active_group(self, exec_no: str) -> dict[str, Any] | None:
        for group in self._groups:
            if group["exec_no"] == exec_no and group["status"] == GROUP_ACTIVE:
                return group
        return None

    @staticmethod
    def _find_group_or_raise(groups: list[dict[str, Any]],
                             group_no: str) -> dict[str, Any]:
        for group in groups:
            if group["group_no"] == group_no:
                return group
        raise KeyError(group_no)

    def _close_group(self, group: dict[str, Any],
                     terminal: str, note: str) -> None:
        group["status"] = terminal
        group["closed_at"] = _now()
        group["updated_at"] = group["closed_at"]
        group["result_note"] = note
        recovery = store.recovery.find_by_group(group["group_no"])
        if recovery is not None and not recovery.terminal:
            store.recovery.resolve(
                recovery.recovery_id, terminal=terminal, note=note,
            )
        event_status = _EVENT_STATUS_BY_TERMINAL.get(terminal)
        if event_status:
            for event_id in group["event_ids"]:
                row = store.find(self.MODULE, event_id)
                if row is not None and row.get("status") not in (
                    "已恢复", "已忽略",
                ):
                    row["status"] = event_status
                    row["恢复状态"] = event_status
                    row["pending"] = False

    def _append_event_row(self, exec_no: str, bot_ref: str,
                          error_type: str, step: str,
                          detail: str, group_no: str) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        event_no = f"ANOM-{new_id:04d}"
        row = {
            "id": new_id,
            "事件编号": event_no,
            "执行编号": exec_no,
            "异常组编号": group_no,
            "关联流程": bot_ref,
            "异常类型": error_type,
            "异常详情": detail,
            "发生步骤": step,
            "发生时间": _now(),
            "恢复状态": "未处理",
            "status": "未处理",
            "pending": True,
            "abnormal": True,
        }
        rows.append(row)
        return row

    # ------------------------------------------------------------------
    # 历史数据引导：把已有的异常事件行纳入异常组台账
    # ------------------------------------------------------------------

    def bootstrap(self) -> int:
        """把种子/历史异常事件按执行编号聚合建组，幂等只执行一次。"""
        with self._lock:
            if self._bootstrapped:
                return len(self._groups)
            rows = store.rows(self.MODULE)
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                if row.get("异常组编号"):
                    continue
                exec_no = str(row.get("执行编号") or row.get("关联流程") or "")
                row["执行编号"] = exec_no
                grouped.setdefault(exec_no, []).append(row)

            for exec_no, members in grouped.items():
                unresolved = [
                    r for r in members
                    if r.get("status") in ("未处理", "处理中")
                ]
                if unresolved:
                    terminal = ""
                elif all(r.get("status") == "已恢复" for r in members):
                    terminal = GROUP_RECOVERED
                elif all(r.get("status") == "已忽略" for r in members):
                    terminal = GROUP_IGNORED
                else:
                    terminal = GROUP_CLOSED
                first = members[0]
                group = self._new_group(
                    exec_no=exec_no,
                    bot_ref=str(first.get("关联流程", "")),
                    error_type=str(first.get("异常类型", "")),
                    step=str(first.get("发生步骤", "")),
                    boundary=BOUNDARY_MERGE,
                )
                for row in members:
                    group["event_ids"].append(int(row["id"]))
                    group["event_nos"].append(row["事件编号"])
                    row["异常组编号"] = group["group_no"]
                if terminal:
                    self._close_group(
                        group, terminal,
                        note="历史异常事件引导归档",
                    )
            self._bootstrapped = True
            return len(self._groups)

    # ------------------------------------------------------------------
    # 失败登记：重复异常聚合的主入口
    # ------------------------------------------------------------------

    def record_failure(self, exec_no: str, error_type: str,
                       step: str, detail: str,
                       bot_ref: str = "",
                       boundary: str | None = None,
                       max_attempts: int = 5) -> dict[str, Any]:
        """登记一次执行失败异常并完成聚合。

        每次失败仍按当前行数生成新的事件编号；但同一执行对象只保留一个
        活动异常组和一条活动恢复记录（重试五次 = 五事件 / 一组 / 一入口）。
        """
        if not exec_no:
            raise ValueError("执行编号不能为空")
        if boundary is not None and boundary not in BOUNDARIES:
            raise ValueError(f"未知处理边界：{boundary}")

        with self._lock:
            existing = self._find_active_group(exec_no)
            # 未显式指定边界时沿用组上已配置的处理边界，再退默认并入当前组
            policy = boundary or (
                existing["boundary"] if existing else BOUNDARY_MERGE
            )
            reopened = False
            if existing is not None and policy == BOUNDARY_REOPEN:
                # 边界二：关闭当前组并新开持续故障组
                self._close_group(
                    existing, GROUP_CLOSED,
                    note="持续故障：按关旧开新边界关闭当前异常组",
                )
                existing = None
                reopened = True

            if existing is None:
                group = self._new_group(
                    exec_no=exec_no,
                    bot_ref=bot_ref or exec_no,
                    error_type=error_type,
                    step=step,
                    boundary=policy,
                )
            else:
                group = existing
                group["boundary"] = policy
                group["updated_at"] = _now()
                # 恢复入口已存在时 log_group 幂等返回原记录，不新增待办
                store.recovery.log_group(
                    module=self.MODULE,
                    group_no=group["group_no"],
                    exec_no=exec_no,
                    step=step,
                    error_detail=error_type,
                    max_attempts=max_attempts,
                )

            event = self._append_event_row(
                exec_no=exec_no,
                bot_ref=group["bot_ref"],
                error_type=error_type,
                step=step,
                detail=detail,
                group_no=group["group_no"],
            )
            group["event_ids"].append(int(event["id"]))
            group["event_nos"].append(event["事件编号"])
            group["updated_at"] = _now()

            recovery = store.recovery.find_by_group(group["group_no"])
            return {
                "ok": True,
                "event_no": event["事件编号"],
                "group_no": group["group_no"],
                "exec_no": exec_no,
                "group_status": group["status"],
                "boundary": group["boundary"],
                "reopened": reopened,
                "event_count": len(group["event_ids"]),
                "recovery_id": recovery.recovery_id if recovery else None,
                "active_recovery_entries": len(store.recovery.active_entries()),
            }

    # ------------------------------------------------------------------
    # 恢复入口操作：重试 / 确认恢复 / 忽略 / 边界切换
    # ------------------------------------------------------------------

    def retry_group(self, group_no: str) -> dict[str, Any]:
        """通过恢复入口重试活动异常组；中断时返回明确原因。"""
        with self._lock:
            group = self._find_group_or_raise(self._groups, group_no)
            if group["status"] != GROUP_ACTIVE:
                return {
                    "ok": False,
                    "deduplicated": True,
                    "message": (
                        f"异常组 {group_no} 状态为「{group['status']}」，"
                        "恢复入口不再处理该异常组"
                    ),
                    "group": self._group_view(group),
                }
            recovery = store.recovery.find_by_group(group_no)
            if recovery is None:
                return {
                    "ok": False,
                    "message": f"异常组 {group_no} 缺少恢复入口，无法重试",
                    "group": self._group_view(group),
                }
            ok, message = store.recovery.retry(recovery.recovery_id)
            group["updated_at"] = _now()
            return {
                "ok": ok,
                "message": message,
                "interrupted": not ok,
                "group": self._group_view(group),
            }

    def resolve_group(self, group_no: str,
                      note: str = "异常组处理完成，恢复成功") -> dict[str, Any]:
        """确认恢复：关闭活动异常组并以异常组编号写回终态。

        并发/重复提交只生效一次，后续提交返回 deduplicated 提示。
        """
        with self._lock:
            group = self._find_group_or_raise(self._groups, group_no)
            if group["status"] != GROUP_ACTIVE:
                return {
                    "ok": False,
                    "deduplicated": True,
                    "message": (
                        f"异常组 {group_no} 终态为「{group['status']}」，"
                        "重复提交未生效，只处理一次"
                    ),
                    "group": self._group_view(group),
                }
            self._close_group(group, GROUP_RECOVERED, note)
            return {
                "ok": True,
                "message": f"异常组 {group_no} 已恢复，组内 {len(group['event_ids'])} 条事件同步关闭",
                "group": self._group_view(group),
            }

    def ignore_group(self, group_no: str,
                     note: str = "人工判定忽略该异常组") -> dict[str, Any]:
        with self._lock:
            group = self._find_group_or_raise(self._groups, group_no)
            if group["status"] != GROUP_ACTIVE:
                return {
                    "ok": False,
                    "deduplicated": True,
                    "message": f"异常组 {group_no} 已{group['status']}，重复提交未生效",
                    "group": self._group_view(group),
                }
            self._close_group(group, GROUP_IGNORED, note)
            return {
                "ok": True,
                "message": f"异常组 {group_no} 已忽略",
                "group": self._group_view(group),
            }

    def set_boundary(self, group_no: str, policy: str) -> dict[str, Any]:
        """切换持续故障处理边界，仅对仍在活动的异常组有效。"""
        if policy not in BOUNDARIES:
            raise ValueError(f"未知处理边界：{policy}")
        with self._lock:
            group = self._find_group_or_raise(self._groups, group_no)
            if group["status"] != GROUP_ACTIVE:
                return {
                    "ok": False,
                    "message": (
                        f"异常组 {group_no} 状态为「{group['status']}」，"
                        "不能再切换边界"
                    ),
                    "group": self._group_view(group),
                }
            group["boundary"] = policy
            group["updated_at"] = _now()
            return {
                "ok": True,
                "message": f"异常组 {group_no} 处理边界已切换为「{policy}」",
                "group": self._group_view(group),
            }

    # ------------------------------------------------------------------
    # 查询：异常组、成员事件、聚合台账、不变量核对
    # ------------------------------------------------------------------

    def _group_view(self, group: dict[str, Any]) -> dict[str, Any]:
        recovery = store.recovery.find_by_group(group["group_no"])
        return {
            "group_no": group["group_no"],
            "exec_no": group["exec_no"],
            "bot_ref": group["bot_ref"],
            "error_type": group["error_type"],
            "step": group["step"],
            "boundary": group["boundary"],
            "status": group["status"],
            "event_count": len(group["event_ids"]),
            "event_nos": list(group["event_nos"]),
            "recovery_id": group["recovery_id"],
            "recovery_attempt": recovery.attempt if recovery else 0,
            "recovery_max_attempts": recovery.max_attempts if recovery else 0,
            "recovery_terminal": recovery.terminal if recovery else "",
            "active_recovery_entry": bool(recovery and not recovery.terminal),
            "created_at": group["created_at"],
            "updated_at": group["updated_at"],
            "closed_at": group["closed_at"],
            "result_note": group["result_note"],
        }

    def list_groups(self, exec_no: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            groups = list(self._groups)
            if exec_no:
                groups = [g for g in groups if g["exec_no"] == exec_no]
            if status:
                groups = [g for g in groups if g["status"] == status]
            return [self._group_view(g) for g in groups]

    def active_groups(self) -> list[dict[str, Any]]:
        return self.list_groups(status=GROUP_ACTIVE)

    def get_group_detail(self, group_no: str) -> dict[str, Any]:
        with self._lock:
            group = self._find_group_or_raise(self._groups, group_no)
            view = self._group_view(group)
            events = [
                store.find(self.MODULE, event_id)
                for event_id in group["event_ids"]
            ]
            view["events"] = [e for e in events if e is not None]
            recovery = store.recovery.find_by_group(group_no)
            view["recovery_entry"] = (
                {
                    "recovery_id": recovery.recovery_id,
                    "attempt": recovery.attempt,
                    "max_attempts": recovery.max_attempts,
                    "terminal": recovery.terminal,
                    "result_note": recovery.result_note,
                    "created_at": recovery.created_at,
                }
                if recovery else None
            )
            return view

    def list_events(self, exec_no: str | None = None,
                    group_no: str | None = None,
                    include_history: bool = True) -> list[dict[str, Any]]:
        """异常事件台账明细，可按执行编号/异常组编号过滤，含历史事件。"""
        rows = store.rows(self.MODULE)
        result = []
        for row in rows:
            if exec_no and row.get("执行编号") != exec_no:
                continue
            if group_no and row.get("异常组编号") != group_no:
                continue
            if not include_history and row.get("status") in (
                "已恢复", "已忽略",
            ):
                continue
            result.append(dict(row))
        return result

    def ledger(self) -> list[dict[str, Any]]:
        """聚合后的异常事件台账数据集：一行一个异常组。"""
        with self._lock:
            ledger_rows: list[dict[str, Any]] = []
            for group in self._groups:
                events = [
                    store.find(self.MODULE, event_id)
                    for event_id in group["event_ids"]
                ]
                events = [e for e in events if e is not None]
                times = [str(e.get("发生时间", "")) for e in events]
                ledger_rows.append({
                    **self._group_view(group),
                    "first_event_at": min(times) if times else "",
                    "last_event_at": max(times) if times else "",
                })
            return ledger_rows

    def reconcile(self, exec_no: str) -> dict[str, Any]:
        """按执行编号核对处理项与异常事件台账、恢复入口的一致性。"""
        with self._lock:
            groups = [
                g for g in self._groups if g["exec_no"] == exec_no
            ]
            violations: list[str] = []
            events = [
                row for row in store.rows(self.MODULE)
                if row.get("执行编号") == exec_no
            ]
            for row in events:
                if not row.get("异常组编号"):
                    violations.append(
                        f"事件 {row.get('事件编号')} 未归属任何异常组"
                    )
            active = [g for g in groups if g["status"] == GROUP_ACTIVE]
            if len(active) > 1:
                violations.append(
                    f"执行对象 {exec_no} 存在 {len(active)} 个活动异常组，"
                    "同一执行对象只能指向一个活动恢复入口"
                )
            for group in groups:
                recovery = store.recovery.find_by_group(group["group_no"])
                if group["status"] == GROUP_ACTIVE:
                    if recovery is None or recovery.terminal:
                        violations.append(
                            f"活动异常组 {group['group_no']} 缺少活动恢复入口"
                        )
                elif recovery is not None and not recovery.terminal:
                    violations.append(
                        f"已关闭异常组 {group['group_no']} 仍占用活动恢复入口"
                    )
            return {
                "ok": not violations,
                "exec_no": exec_no,
                "group_count": len(groups),
                "active_group_count": len(active),
                "event_count": len(events),
                "active_recovery_entries": sum(
                    1 for g in groups
                    if (r := store.recovery.find_by_group(g["group_no"]))
                    and not r.terminal
                ),
                "groups": [self._group_view(g) for g in groups],
                "violations": violations,
            }

    def validate_invariants(self) -> list[str]:
        """全量不变量校验，供测试与巡检使用。"""
        with self._lock:
            violations: list[str] = []
            active_by_exec: dict[str, int] = {}
            for group in self._groups:
                if group["status"] == GROUP_ACTIVE:
                    active_by_exec[group["exec_no"]] = (
                        active_by_exec.get(group["exec_no"], 0) + 1
                    )
                for event_id in group["event_ids"]:
                    row = store.find(self.MODULE, event_id)
                    if row is None:
                        violations.append(
                            f"异常组 {group['group_no']} 成员事件 id={event_id} 缺失"
                        )
                    elif row.get("异常组编号") != group["group_no"]:
                        violations.append(
                            f"事件 {row.get('事件编号')} 的异常组编号回写不一致"
                        )
            for exec_no, count in active_by_exec.items():
                if count > 1:
                    violations.append(
                        f"执行对象 {exec_no} 同时存在 {count} 个活动异常组"
                    )
            # 恢复入口只处理未关闭异常组
            for recovery in store.recovery.active_entries():
                if recovery.group_no:
                    try:
                        group = self._find_group_or_raise(
                            self._groups, recovery.group_no,
                        )
                    except KeyError:
                        violations.append(
                            f"恢复入口 {recovery.recovery_id} 指向不存在的异常组 "
                            f"{recovery.group_no}"
                        )
                        continue
                    if group["status"] != GROUP_ACTIVE:
                        violations.append(
                            f"恢复入口 {recovery.recovery_id} 仍处理已"
                            f"{group['status']}的异常组 {recovery.group_no}"
                        )
            return violations

    def stats(self) -> dict[str, Any]:
        with self._lock:
            groups = self._groups
            active = [g for g in groups if g["status"] == GROUP_ACTIVE]
            return {
                "group_total": len(groups),
                "active_groups": len(active),
                "recovered_groups": sum(
                    1 for g in groups if g["status"] == GROUP_RECOVERED
                ),
                "closed_groups": sum(
                    1 for g in groups if g["status"] == GROUP_CLOSED
                ),
                "ignored_groups": sum(
                    1 for g in groups if g["status"] == GROUP_IGNORED
                ),
                "event_total": sum(len(g["event_ids"]) for g in groups),
                "active_recovery_entries": len(
                    store.recovery.active_entries()
                ),
            }


# 单例：与 runtime_session / callback_hook 相同的服务模式
anomaly_groups = AnomalyGroupManager()
anomaly_groups.bootstrap()
