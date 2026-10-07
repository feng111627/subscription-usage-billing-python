"""并发正确性：多线程并发上报、重复事件、并发出账/状态操作。

验证“最终用量与金额一致，不丢计、不重复扣”。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from billing.errors import BillingError

B = "并发安全"


@pytest.mark.business(f"{B}/并发上报-不同事件不丢计")
def test_concurrent_distinct_events_no_lost_update(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    n = 200

    def report(i):
        svc.report_usage(
            {"event_id": f"ev-{i}", "subscription_id": "S1", "quantity": "1",
             "occurred_at": "2026-09-10T00:00:00Z"},
            cust,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(report, range(n)))
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["event_count"] == n
    assert agg["total_quantity"] == str(n)


@pytest.mark.business(f"{B}/并发上报-含重复事件不重复计")
def test_concurrent_replayed_events_count_once(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    # 200 次提交，但只有 50 个不同事件，每个并发提交 4 次
    ids = [f"ev-{i % 50}" for i in range(200)]

    def report(event_id):
        svc.report_usage(
            {"event_id": event_id, "subscription_id": "S1", "quantity": "3",
             "occurred_at": "2026-09-10T00:00:00Z"},
            cust,
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(report, ids))
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["event_count"] == 50
    assert agg["total_quantity"] == "150"


@pytest.mark.business(f"{B}/并发上报-跨两个周期分别归集")
def test_concurrent_events_split_across_periods(make_plan_sub):
    svc, admin, cust = make_plan_sub()

    def report(i):
        at = (
            "2026-09-30T23:59:59Z"
            if i % 2 == 0
            else "2026-10-01T00:00:00Z"
        )
        svc.report_usage(
            {"event_id": f"ev-{i}", "subscription_id": "S1", "quantity": "1",
             "occurred_at": at},
            cust,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(report, range(100)))
    a9 = svc.get_aggregate("S1", "2026-09-01", cust)
    a10 = svc.get_aggregate("S1", "2026-10-01", cust)
    assert a9["event_count"] + a10["event_count"] == 100
    assert a9["event_count"] == 50
    assert a10["event_count"] == 50
    assert a9["total_quantity"] == "50"
    assert a10["total_quantity"] == "50"


@pytest.mark.business(f"{B}/并发出账-只生成一张账单且金额确定")
def test_concurrent_issue_single_bill(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    for i in range(120):
        svc.report_usage(
            {"event_id": f"ev-{i}", "subscription_id": "S1", "quantity": "1",
             "occurred_at": "2026-09-10T00:00:00Z"},
            cust,
        )
    results = []
    errors = []

    def issue():
        try:
            results.append(svc.issue_bill("S1", "2026-09-01", admin))
        except BillingError as exc:  # pragma: no cover - 不应发生
            errors.append(exc.code)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: issue(), range(20)))
    assert errors == []
    bills = svc.repo.list_bills("S1")
    assert len(bills) == 1
    assert len({b['id'] for b in results}) == 1
    assert all(
        str(b.total_amount) == results[0]["total_amount"] for b in bills
    )
    # 120 单位：10 基础费 + 20 * 0.5
    assert results[0]["total_amount"] == "20.00"


@pytest.mark.business(f"{B}/并发上报与出账-账目一致不丢不重")
def test_concurrent_reports_then_issue_consistent(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    barrier = threading.Barrier(8)

    def worker(w):
        barrier.wait()
        for i in range(25):
            svc.report_usage(
                {"event_id": f"w{w}-e{i}", "subscription_id": "S1",
                 "quantity": "2", "occurred_at": "2026-09-10T00:00:00Z"},
                cust,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["event_count"] == 200
    assert agg["total_quantity"] == "400"
    bill = svc.issue_bill("S1", "2026-09-01", admin)
    # 用量费 (400-100)*0.5=150，+基础费10
    assert bill["total_amount"] == "160.00"
    assert bill["quantity"] == "400"
    # 出账后再并发补报全部被拒，金额不被篡改
    refused = {"count": 0, "lock": threading.Lock()}

    def late_report(i):
        try:
            svc.report_usage(
                {"event_id": f"late-{i}", "subscription_id": "S1",
                 "quantity": "10", "occurred_at": "2026-09-12T00:00:00Z"},
                cust,
            )
        except BillingError as exc:
            assert exc.code == "PERIOD_CLOSED"
            with refused["lock"]:
                refused["count"] += 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(late_report, range(16)))
    assert refused["count"] == 16
    assert svc.get_aggregate("S1", "2026-09-01", cust)["total_quantity"] == "400"


@pytest.mark.business(f"{B}/并发状态流转-非法转移不破坏账目")
def test_concurrent_pay_and_void_only_one_wins(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    svc.report_usage(
        {"event_id": "e1", "subscription_id": "S1", "quantity": "5",
         "occurred_at": "2026-09-10T00:00:00Z"}, cust)
    bill = svc.issue_bill("S1", "2026-09-01", admin)
    outcomes = {"paid": 0, "void": 0, "rejected": 0}
    lock = threading.Lock()

    def pay():
        try:
            r = svc.mark_paid(bill["id"], admin)
            # 只有真正完成一次状态转移才计数；重放返回 replayed=True
            if r["status"] == "paid" and not r["replayed"]:
                with lock:
                    outcomes["paid"] += 1
        except BillingError:
            with lock:
                outcomes["rejected"] += 1

    def void():
        try:
            r = svc.void_bill(bill["id"], admin)
            if r["status"] == "void" and not r["replayed"]:
                with lock:
                    outcomes["void"] += 1
        except BillingError:
            with lock:
                outcomes["rejected"] += 1

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = []
        for _ in range(10):
            futs.append(pool.submit(pay))
            futs.append(pool.submit(void))
        for f in futs:
            f.result()
    # pay 与 void 互斥：恰有一个方向成功（且只成功一次），另一个全被拒
    assert outcomes["paid"] + outcomes["void"] == 1
    final = svc.get_bill(bill["id"], admin)
    assert final["status"] in ("paid", "void")
    # 金额始终未变
    assert final["total_amount"] == "10.00"
