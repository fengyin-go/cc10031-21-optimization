"""异常恢复追踪测试 — 重复异常聚合与恢复入口。

覆盖处理项：
1. 同一执行对象重试五次：五条事件、一个异常组、一条活动恢复入口
2. 异常组与组内事件同时记录，事件均回写异常组编号
3. 同一执行对象的恢复入口只能指向一个活动异常组
4. 恢复结果按异常组编号写回执行记录并联动刷新待办
5. 处理详情与结果清单同步更新
6. 并发/重复两次提交只算一次
7. 沿用既有处理边界（并入当前组）
8. 处理完后列表空态，且仍可查询历史事件
9. 重试中断（达上限）返回明确原因
10. 关旧开新边界：关闭当前组并新开持续故障组，仍只保留一个活动入口
11. 恢复入口只处理仍未关闭的异常组
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.anomaly_recovery import (  # noqa: E402
    BOUNDARY_MERGE,
    BOUNDARY_REOPEN,
    GROUP_ACTIVE,
    GROUP_CLOSED,
    GROUP_IGNORED,
    GROUP_RECOVERED,
    AnomalyGroupManager,
    anomaly_groups,
)
from app.services.execution_log import ExecutionLogService  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture()
def manager() -> AnomalyGroupManager:
    """每个用例在干净的内存表上运行，并在用例后恢复种子数据。

    模块级单例 anomaly_groups 持有异常组列表，需要连同 store 一起重置，
    避免组编号与恢复入口跨用例串扰。
    """
    saved_anomaly = [dict(row) for row in store.rows("anomaly_log")]
    saved_exec = [dict(row) for row in store.rows("execution_log")]
    saved_recovery = list(store.recovery._entries)  # noqa: SLF001
    saved_seq = store.recovery._seq  # noqa: SLF001

    store._tables["anomaly_log"] = []  # noqa: SLF001
    store._tables["execution_log"] = []  # noqa: SLF001
    store.recovery._entries = []  # noqa: SLF001
    store.recovery._seq = 0  # noqa: SLF001
    anomaly_groups._groups = []  # noqa: SLF001
    anomaly_groups._group_seq = 0  # noqa: SLF001
    anomaly_groups._bootstrapped = True  # noqa: SLF001

    yield anomaly_groups

    store._tables["anomaly_log"] = saved_anomaly  # noqa: SLF001
    store._tables["execution_log"] = saved_exec  # noqa: SLF001
    store.recovery._entries = saved_recovery  # noqa: SLF001
    store.recovery._seq = saved_seq  # noqa: SLF001
    anomaly_groups._groups = []  # noqa: SLF001
    anomaly_groups._group_seq = 0  # noqa: SLF001
    anomaly_groups._bootstrapped = False  # noqa: SLF001
    anomaly_groups.bootstrap()


def _fail_n_times(manager: AnomalyGroupManager, exec_no: str,
                  times: int, **kwargs) -> dict:
    result = {}
    for index in range(times):
        result = manager.record_failure(
            exec_no=exec_no,
            error_type="元素定位超时",
            step="提取结果表格",
            detail=f"第{index + 1}次失败",
            **kwargs,
        )
    return result


class TestRepeatedFailureAggregation:
    """处理项：重复异常聚合，五次重试不撑大恢复待办。"""

    def test_five_retries_produce_one_group_one_entry(self, manager: AnomalyGroupManager) -> None:
        exec_no = "EXEC-T1"
        result = _fail_n_times(manager, exec_no, 5)

        groups = manager.list_groups(exec_no=exec_no)
        assert len(groups) == 1
        group = groups[0]
        assert group["status"] == GROUP_ACTIVE
        assert group["event_count"] == 5
        assert result["group_no"] == group["group_no"]
        # 恢复入口始终只有一条活动记录
        assert result["active_recovery_entries"] == 1
        assert len(store.recovery.active_entries()) == 1

    def test_group_and_member_events_recorded_together(self, manager: AnomalyGroupManager) -> None:
        exec_no = "EXEC-T2"
        result = _fail_n_times(manager, exec_no, 3)
        detail = manager.get_group_detail(result["group_no"])

        assert len(detail["events"]) == 3
        for event in detail["events"]:
            assert event["异常组编号"] == result["group_no"]
            assert event["执行编号"] == exec_no
        # 每次失败都生成独立事件编号
        event_nos = [event["事件编号"] for event in detail["events"]]
        assert len(set(event_nos)) == 3

    def test_each_failure_still_creates_new_event_number(self, manager: AnomalyGroupManager) -> None:
        before = len(store.rows("anomaly_log"))
        _fail_n_times(manager, "EXEC-T3", 4)
        after = len(store.rows("anomaly_log"))
        assert after - before == 4

    def test_single_active_group_per_execution_object(self, manager: AnomalyGroupManager) -> None:
        _fail_n_times(manager, "EXEC-T4", 5)
        _fail_n_times(manager, "EXEC-T4", 2)
        active = manager.list_groups(exec_no="EXEC-T4", status=GROUP_ACTIVE)
        assert len(active) == 1
        assert manager.reconcile("EXEC-T4")["active_group_count"] == 1


class TestRecoveryEntryLifecycle:
    """处理项：恢复入口只处理未关闭异常组，结果写回执行记录。"""

    def test_recovery_result_written_back_by_group_no(self, manager: AnomalyGroupManager) -> None:
        exec_no = "EXEC-T5"
        result = manager.record_failure(
            exec_no, "接口限流", "翻页", "429",
            bot_ref="订单采集",
        )
        # 先补一条执行记录作为写回落点
        store.rows("execution_log").append({
            "id": 9001, "执行编号": exec_no, "所属流程": "订单采集",
            "status": "执行失败", "pending": True, "abnormal": True,
            "异常组编号": "", "恢复结果": "",
        })
        svc = ExecutionLogService()
        outcome = svc.write_back_recovery(result["group_no"], result_note="选择器已修复")

        assert outcome["ok"] is True
        assert outcome["write_back_rows"] == 1
        assert outcome["terminal"] == GROUP_RECOVERED
        row = next(
            r for r in store.rows("execution_log")
            if r["执行编号"] == exec_no and r.get("id") == 9001
        )
        assert row["异常组编号"] == result["group_no"]
        assert result["group_no"] in row["恢复结果"]
        # 待办联动刷新
        assert row["pending"] is False and row["abnormal"] is False

    def test_detail_and_result_list_update_together(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T6", "x", "y", "z")
        manager.resolve_group(result["group_no"], note="人工恢复")
        detail = manager.get_group_detail(result["group_no"])

        assert detail["status"] == GROUP_RECOVERED
        assert detail["closed_at"] is not None
        assert detail["recovery_entry"]["terminal"] == GROUP_RECOVERED
        assert all(event["status"] == "已恢复" for event in detail["events"])
        ledger_row = next(
            row for row in manager.ledger()
            if row["group_no"] == result["group_no"]
        )
        assert ledger_row["status"] == GROUP_RECOVERED

    def test_duplicated_resolve_submission_counts_once(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T7", "x", "y", "z")
        first = manager.resolve_group(result["group_no"])
        second = manager.resolve_group(result["group_no"])

        assert first["ok"] is True
        assert second["ok"] is False and second["deduplicated"] is True
        recovery = store.recovery.find_by_group(result["group_no"])
        assert recovery.terminal == GROUP_RECOVERED

    def test_concurrent_resolve_submissions_only_one_wins(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T8", "x", "y", "z")
        outcomes: list[dict] = []

        def submit() -> None:
            outcomes.append(manager.resolve_group(result["group_no"]))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [item for item in outcomes if item["ok"]]
        duplicates = [item for item in outcomes if item.get("deduplicated")]
        assert len(successes) == 1
        assert len(duplicates) == 1

    def test_recovery_entry_rejects_closed_group(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T9", "x", "y", "z")
        manager.ignore_group(result["group_no"])
        retry = manager.retry_group(result["group_no"])

        assert retry["ok"] is False
        assert GROUP_IGNORED in retry["message"]
        assert manager.get_group_detail(result["group_no"])["status"] == GROUP_IGNORED


class TestRetryInterruption:
    """处理项：重试中断必须说明原因。"""

    def test_retry_interrupted_after_max_attempts(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T10", "x", "y", "z")
        group_no = result["group_no"]
        max_attempts = store.recovery.find_by_group(group_no).max_attempts
        for _ in range(max_attempts):
            attempt = manager.retry_group(group_no)
            assert attempt["ok"] is True
        interrupted = manager.retry_group(group_no)

        assert interrupted["ok"] is False
        assert interrupted["interrupted"] is True
        assert "中断" in interrupted["message"]
        assert "最大重试次数" in interrupted["message"]

    def test_interrupted_group_keeps_single_active_entry(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T11", "x", "y", "z")
        recovery = store.recovery.find_by_group(result["group_no"])
        for _ in range(recovery.max_attempts + 1):
            manager.retry_group(result["group_no"])
        assert len(store.recovery.active_entries()) == 1
        assert manager.reconcile("EXEC-T11")["ok"] is True


class TestBoundaryPolicies:
    """处理项：两套持续故障边界都只保留一个活动恢复入口。"""

    def test_default_boundary_merges_new_events(self, manager: AnomalyGroupManager) -> None:
        first = manager.record_failure("EXEC-T12", "崩溃", "渲染", "首次")
        second = manager.record_failure("EXEC-T12", "崩溃", "渲染", "再来")
        assert first["group_no"] == second["group_no"]
        assert second["boundary"] == BOUNDARY_MERGE

    def test_reopen_boundary_closes_old_and_creates_new(self, manager: AnomalyGroupManager) -> None:
        first = manager.record_failure(
            "EXEC-T13", "崩溃", "渲染", "首次", boundary=BOUNDARY_MERGE,
        )
        manager.set_boundary(first["group_no"], BOUNDARY_REOPEN)
        second = manager.record_failure("EXEC-T13", "崩溃", "渲染", "持续故障")

        assert second["reopened"] is True
        assert second["group_no"] != first["group_no"]
        old_detail = manager.get_group_detail(first["group_no"])
        assert old_detail["status"] == GROUP_CLOSED
        assert all(event["status"] == "已忽略" for event in old_detail["events"])
        new_detail = manager.get_group_detail(second["group_no"])
        assert new_detail["status"] == GROUP_ACTIVE

    def test_both_boundaries_keep_one_active_entry(self, manager: AnomalyGroupManager) -> None:
        first = manager.record_failure(
            "EXEC-T14", "崩溃", "渲染", "首次", boundary=BOUNDARY_MERGE,
        )
        manager.set_boundary(first["group_no"], BOUNDARY_REOPEN)
        manager.record_failure("EXEC-T14", "崩溃", "渲染", "持续1")
        manager.record_failure("EXEC-T14", "崩溃", "渲染", "持续2")

        active = manager.list_groups(exec_no="EXEC-T14", status=GROUP_ACTIVE)
        assert len(active) == 1
        group_nos = {g["group_no"] for g in manager.list_groups(exec_no="EXEC-T14")}
        active_entry_groups = {
            e.group_no for e in store.recovery.active_entries()
            if e.group_no in group_nos
        }
        assert active_entry_groups == {active[0]["group_no"]}

    def test_boundary_switch_rejected_after_closed(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T15", "x", "y", "z")
        manager.resolve_group(result["group_no"])
        outcome = manager.set_boundary(result["group_no"], BOUNDARY_REOPEN)
        assert outcome["ok"] is False


class TestEmptyStateAndHistory:
    """处理项：处理完显示空态，历史事件仍可查询。"""

    def test_empty_state_after_all_groups_resolved(self, manager: AnomalyGroupManager) -> None:
        _fail_n_times(manager, "EXEC-T16", 3)
        other = manager.record_failure("EXEC-T17", "x", "y", "z")
        for group in manager.active_groups():
            manager.resolve_group(group["group_no"])

        assert manager.active_groups() == []
        assert len(store.recovery.active_entries()) == 0
        # 历史台账与事件仍可查询
        assert len(manager.list_events(exec_no="EXEC-T16")) == 3
        assert len(manager.ledger()) >= 2
        assert other["group_no"] in {row["group_no"] for row in manager.ledger()}

    def test_query_by_execution_number(self, manager: AnomalyGroupManager) -> None:
        _fail_n_times(manager, "EXEC-T18", 2)
        groups = manager.list_groups(exec_no="EXEC-T18")
        assert len(groups) == 1
        assert groups[0]["event_count"] == 2
        events = manager.list_events(exec_no="EXEC-T18")
        assert all(event["执行编号"] == "EXEC-T18" for event in events)


class TestInvariants:
    """终态核对：恢复入口只处理仍未关闭的异常组。"""

    def test_validate_invariants_clean(self, manager: AnomalyGroupManager) -> None:
        _fail_n_times(manager, "EXEC-T19", 3)
        manager.record_failure("EXEC-T20", "x", "y", "z")
        assert manager.validate_invariants() == []

    def test_reconcile_detects_violation(self, manager: AnomalyGroupManager) -> None:
        result = manager.record_failure("EXEC-T21", "x", "y", "z")
        # 人为制造冲突：给同一执行对象再插一个活动组
        manager._new_group(  # noqa: SLF001
            exec_no="EXEC-T21", bot_ref="b", error_type="x",
            step="y", boundary=BOUNDARY_MERGE,
        )
        report = manager.reconcile("EXEC-T21")
        assert report["ok"] is False
        assert any("活动异常组" in item for item in report["violations"])
