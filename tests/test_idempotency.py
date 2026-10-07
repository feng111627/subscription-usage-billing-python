"""幂等：事件重放、幂等键重放、同键冲突、出账/状态操作重放。"""
from __future__ import annotations

import pytest

from billing.errors import BillingError

B = "幂等性"


@pytest.fixture
def world(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    return svc, admin, cust


def _usage(svc, cust, event_id, qty, at, key=None):
    return svc.report_usage(
        {"event_id": event_id, "subscription_id": "S1",
         "quantity": qty, "occurred_at": at},
        cust, idempotency_key=key,
    )


@pytest.mark.business(f"{B}/用量-同事件重放不重复归集")
def test_duplicate_event_not_double_counted(world):
    svc, admin, cust = world
    r1 = _usage(svc, cust, "e1", "10", "2026-09-10T00:00:00Z")
    r2 = _usage(svc, cust, "e1", "10", "2026-09-10T00:00:00Z")
    assert r1["replayed"] is False
    assert r2["replayed"] is True
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["total_quantity"] == "10"
    assert agg["event_count"] == 1


@pytest.mark.business(f"{B}/用量-同事件ID不同内容拒绝")
def test_same_event_id_different_payload_rejected(world):
    svc, admin, cust = world
    _usage(svc, cust, "e1", "10", "2026-09-10T00:00:00Z")
    with pytest.raises(BillingError) as ei:
        _usage(svc, cust, "e1", "11", "2026-09-10T00:00:00Z")
    assert ei.value.code == "DUPLICATE_EVENT"
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["total_quantity"] == "10"


@pytest.mark.business(f"{B}/用量-跨周期相同事件ID视为不同订阅事件仍去重")
def test_event_id_dedup_is_global(world):
    svc, admin, cust = world
    _usage(svc, cust, "e1", "10", "2026-09-10T00:00:00Z")
    # 同 event_id 即使落在另一周期，也是同一事件，内容不同则拒绝
    with pytest.raises(BillingError) as ei:
        _usage(svc, cust, "e1", "10", "2026-10-10T00:00:00Z")
    assert ei.value.code == "DUPLICATE_EVENT"


@pytest.mark.business(f"{B}/HTTP幂等键-同键重放返回首次结果")
def test_idempotency_key_replay(world):
    svc, admin, cust = world
    payload = {"event_id": "e1", "subscription_id": "S1", "quantity": "10",
               "occurred_at": "2026-09-10T00:00:00Z"}
    r1 = svc.report_usage(payload, cust, idempotency_key="key-1")
    # 换了事件ID但复用幂等键且载荷不同 -> 冲突
    payload2 = dict(payload, event_id="e2")
    with pytest.raises(BillingError) as ei:
        svc.report_usage(payload2, cust, idempotency_key="key-1")
    assert ei.value.code == "IDEMPOTENCY_CONFLICT"
    # 完全相同载荷+同键 -> 返回首次结果，不产生 e2
    r3 = svc.report_usage(payload, cust, idempotency_key="key-1")
    assert r3 == r1
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    assert agg["event_count"] == 1


@pytest.mark.business(f"{B}/HTTP幂等键-不同用户同键互不影响")
def test_idempotency_key_scoped_by_actor(make_plan_sub):
    svc, admin, cust = make_plan_sub(customer_id="user-1")
    # 第二个客户也有订阅
    make_plan_sub(sub_id="S2", customer_id="user-2", create_plan=False)
    cust2 = {"id": "user-2", "role": "customer"}
    p1 = {"event_id": "a", "subscription_id": "S1", "quantity": "1",
          "occurred_at": "2026-09-10T00:00:00Z"}
    p2 = {"event_id": "b", "subscription_id": "S2", "quantity": "2",
          "occurred_at": "2026-09-10T00:00:00Z"}
    r1 = svc.report_usage(p1, cust, idempotency_key="K")
    r2 = svc.report_usage(p2, cust2, idempotency_key="K")
    assert r1["total_quantity"] == "1"
    assert r2["total_quantity"] == "2"


@pytest.mark.business(f"{B}/出账-重复出账幂等且金额不变")
def test_issue_bill_idempotent(world):
    svc, admin, cust = world
    _usage(svc, cust, "e1", "120", "2026-09-10T00:00:00Z")
    b1 = svc.issue_bill("S1", "2026-09-01", admin)
    b2 = svc.issue_bill("S1", "2026-09-01", admin, idempotency_key="ib")
    b3 = svc.issue_bill("S1", "2026-09-01", admin, idempotency_key="ib")
    assert b1["id"] == b2["id"] == b3["id"]
    assert b2["replayed"] is True
    assert b1["total_amount"] == "20.00" == b3["total_amount"]
    assert len([b for b in svc.repo.list_bills("S1")]) == 1


@pytest.mark.business(f"{B}/状态流转-支付/作废/退款重复提交幂等")
def test_transition_replays_idempotent(world):
    svc, admin, cust = world
    _usage(svc, cust, "e1", "5", "2026-09-10T00:00:00Z")
    b = svc.issue_bill("S1", "2026-09-01", admin)
    p1 = svc.mark_paid(b["id"], admin, idempotency_key="pay")
    p2 = svc.mark_paid(b["id"], admin, idempotency_key="pay")
    p3 = svc.mark_paid(b["id"], admin)  # 无键重放同样安全
    assert p1["status"] == p2["status"] == p3["status"] == "paid"
    assert p1["version"] == 2  # 出账 v1 -> 支付 v2；重放不推进
    r1 = svc.refund_bill(b["id"], admin)
    r2 = svc.refund_bill(b["id"], admin)
    assert r1["status"] == r2["status"] == "refunded"
    assert r1["version"] == 3  # 退款 v3，重复退款不再推进版本


@pytest.mark.business(f"{B}/作废后重出账-金额一致且为新版本")
def test_void_then_reissue_same_amount(world):
    svc, admin, cust = world
    _usage(svc, cust, "e1", "120", "2026-09-10T00:00:00Z")
    b1 = svc.issue_bill("S1", "2026-09-01", admin)
    svc.void_bill(b1["id"], admin)
    b2 = svc.issue_bill("S1", "2026-09-01", admin)
    assert b2["id"] != b1["id"]
    assert b2["total_amount"] == b1["total_amount"] == "20.00"
