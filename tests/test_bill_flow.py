"""账单状态机、账目核对、非法转移/越权/参数错误的可区分拒绝原因。"""
from __future__ import annotations

import pytest

from billing.errors import BillingError

B = "账单状态流转"
V = "参数校验与越权拒绝"


def _issue(svc, admin, cust, qty="120", period="2026-09-01", at="2026-09-10T00:00:00Z"):
    if qty:
        svc.report_usage(
            {"event_id": "e1", "subscription_id": "S1", "quantity": qty,
             "occurred_at": at}, cust)
    return svc.issue_bill("S1", period, admin)


# ---------------- 状态机 ----------------
@pytest.mark.business(f"{B}/合法链路-出账-支付-退款")
def test_happy_path_issue_pay_refund(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    assert b["status"] == "issued"
    assert b["history"][0]["action"] == "issued"
    paid = svc.mark_paid(b["id"], admin)
    assert paid["status"] == "paid"
    refunded = svc.refund_bill(b["id"], admin)
    assert refunded["status"] == "refunded"
    actions = [h["action"] for h in refunded["history"]]
    assert actions == ["issued", "paid", "refund"]


@pytest.mark.business(f"{B}/合法链路-出账-作废")
def test_happy_path_issue_void(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    v = svc.void_bill(b["id"], admin)
    assert v["status"] == "void"
    # 作废后可重新出账
    b2 = svc.issue_bill("S1", "2026-09-01", admin)
    assert b2["status"] == "issued"
    assert b2["id"] != b["id"]


@pytest.mark.business(f"{B}/非法转移-已支付不能作废")
def test_paid_cannot_void(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    svc.mark_paid(b["id"], admin)
    with pytest.raises(BillingError) as ei:
        svc.void_bill(b["id"], admin)
    assert ei.value.code == "INVALID_BILL_TRANSITION"
    assert svc.get_bill(b["id"], admin)["status"] == "paid"


@pytest.mark.business(f"{B}/非法转移-未支付不能退款")
def test_issued_cannot_refund(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    with pytest.raises(BillingError) as ei:
        svc.refund_bill(b["id"], admin)
    assert ei.value.code == "INVALID_BILL_TRANSITION"


@pytest.mark.business(f"{B}/非法转移-已退款为终态")
def test_refunded_is_terminal(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    svc.mark_paid(b["id"], admin)
    svc.refund_bill(b["id"], admin)
    # 跨动作（再支付/再作废）一律拒绝
    for fn in (svc.mark_paid, svc.void_bill):
        with pytest.raises(BillingError) as ei:
            fn(b["id"], admin)
        assert ei.value.code == "INVALID_BILL_TRANSITION"
    # 对同一终态动作的重复提交是幂等重放，不再次退款
    again = svc.refund_bill(b["id"], admin)
    assert again["status"] == "refunded"
    assert again["replayed"] is True


@pytest.mark.business(f"{B}/非法转移-已作废不能支付或退款")
def test_void_is_terminal(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust)
    svc.void_bill(b["id"], admin)
    for fn in (svc.mark_paid, svc.refund_bill):
        with pytest.raises(BillingError) as ei:
            fn(b["id"], admin)
        assert ei.value.code == "INVALID_BILL_TRANSITION"


@pytest.mark.business(f"{B}/账目核对-账单金额与周期汇总对得上")
def test_bill_amount_matches_aggregate(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    svc.report_usage(
        {"event_id": "e1", "subscription_id": "S1", "quantity": "50",
         "occurred_at": "2026-09-10T00:00:00Z"}, cust)
    svc.report_usage(
        {"event_id": "e2", "subscription_id": "S1", "quantity": "70",
         "occurred_at": "2026-09-20T00:00:00Z"}, cust)
    agg = svc.get_aggregate("S1", "2026-09-01", cust)
    bill = svc.issue_bill("S1", "2026-09-01", admin)
    assert bill["quantity"] == agg["total_quantity"] == "120"
    # 基础费10 + (120-100)*0.5
    assert bill["base_fee"] == "10.00"
    assert bill["usage_amount"] == "10.00"
    assert bill["total_amount"] == "20.00"
    # 作废/重出金额仍一致
    svc.void_bill(bill["id"], admin)
    bill2 = svc.issue_bill("S1", "2026-09-01", admin)
    assert bill2["total_amount"] == "20.00"


@pytest.mark.business(f"{B}/阶梯计价出账")
def test_tiered_bill_amounts(make_plan_sub):
    svc, admin, cust = make_plan_sub(
        pricing_mode="tiered", base_fee="5.00",
        tiers=[{"up_to": 100, "unit_price": "1.00"},
               {"up_to": None, "unit_price": "0.50"}])
    svc.report_usage(
        {"event_id": "e1", "subscription_id": "S1", "quantity": "150",
         "occurred_at": "2026-09-10T00:00:00Z"}, cust)
    bill = svc.issue_bill("S1", "2026-09-01", admin)
    assert bill["usage_amount"] == "125.00"
    assert bill["total_amount"] == "130.00"


@pytest.mark.business(f"{B}/无用量周期出账只收基础费")
def test_zero_usage_period_bills_base_fee(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    bill = _issue(svc, admin, cust, qty=None)
    assert bill["quantity"] == "0"
    assert bill["usage_amount"] == "0.00"
    assert bill["total_amount"] == "10.00"


# ---------------- 拒绝原因 ----------------
@pytest.mark.business(f"{V}/周期未结束拒绝出账")
def test_period_not_ended_rejected(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    # 固定时钟为 2026-11-02：9/10 月已结束，11 月周期尚未结束
    assert svc.issue_bill("S1", "2026-09-01", admin)["status"] == "issued"
    with pytest.raises(BillingError) as ei:
        svc.issue_bill("S1", "2026-11-01", admin)
    assert ei.value.code == "PERIOD_NOT_ENDED"


@pytest.mark.business(f"{V}/出账后补报-周期已锁定")
def test_report_after_close_rejected(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    _issue(svc, admin, cust, qty=None)
    with pytest.raises(BillingError) as ei:
        svc.report_usage(
            {"event_id": "eX", "subscription_id": "S1", "quantity": "1",
             "occurred_at": "2026-09-10T00:00:00Z"}, cust)
    assert ei.value.code == "PERIOD_CLOSED"


@pytest.mark.business(f"{V}/客户越权出账/作废/退款被拒")
def test_customer_cannot_administer_bills(make_plan_sub):
    svc, admin, cust = make_plan_sub()
    b = _issue(svc, admin, cust, qty=None)
    for fn in (
        lambda: svc.issue_bill("S1", "2026-09-01", cust),
        lambda: svc.void_bill(b["id"], cust),
        lambda: svc.mark_paid(b["id"], cust),
        lambda: svc.refund_bill(b["id"], cust),
    ):
        with pytest.raises(BillingError) as ei:
            fn()
        assert ei.value.code == "FORBIDDEN"
        assert ei.value.status_code == 403


@pytest.mark.business(f"{V}/客户越权建计划被拒")
def test_customer_cannot_create_plan(svc, customer):
    with pytest.raises(BillingError) as ei:
        svc.create_plan(
            {"id": "P", "name": "n", "pricing_mode": "overage"}, customer)
    assert ei.value.code == "FORBIDDEN"


@pytest.mark.business(f"{V}/他人订阅资源越权拒绝")
def test_other_customer_forbidden(make_plan_sub, other_customer):
    svc, admin, cust = make_plan_sub()
    with pytest.raises(BillingError) as ei:
        svc.report_usage(
            {"event_id": "e1", "subscription_id": "S1", "quantity": "1",
             "occurred_at": "2026-09-10T00:00:00Z"}, other_customer)
    assert ei.value.code == "FORBIDDEN"
    with pytest.raises(BillingError) as ei:
        svc.get_aggregate("S1", "2026-09-01", other_customer)
    assert ei.value.code == "FORBIDDEN"


@pytest.mark.business(f"{V}/缺失与非法参数的可区分原因")
@pytest.mark.parametrize(
    "payload,code",
    [
        ({"subscription_id": "S1", "quantity": "1"}, "MISSING_PARAMETER"),
        ({"event_id": "e", "subscription_id": "S1"}, "MISSING_PARAMETER"),
        ({"event_id": "e", "subscription_id": "S1", "quantity": "0"},
         "INVALID_PARAMETER"),
        ({"event_id": "e", "subscription_id": "S1", "quantity": "-3"},
         "INVALID_PARAMETER"),
        ({"event_id": "e", "subscription_id": "S1", "quantity": "abc"},
         "INVALID_PARAMETER"),
        ({"event_id": "e", "subscription_id": "S1", "quantity": "1",
          "occurred_at": "not-a-date"}, "INVALID_PARAMETER"),
    ],
)
def test_invalid_usage_params(make_plan_sub, payload, code):
    svc, admin, cust = make_plan_sub()
    with pytest.raises(BillingError) as ei:
        svc.report_usage(payload, cust)
    assert ei.value.code == code


@pytest.mark.business(f"{V}/不存在资源返回不同404原因")
def test_not_found_distinct_codes(svc, admin):
    with pytest.raises(BillingError) as ei:
        svc.issue_bill("NOPE", "2026-09-01", admin)
    assert ei.value.code == "SUBSCRIPTION_NOT_FOUND"
    with pytest.raises(BillingError) as ei:
        svc.get_bill("bill-nope", admin)
    assert ei.value.code == "BILL_NOT_FOUND"


@pytest.mark.business(f"{V}/计划配置非法拒绝")
@pytest.mark.parametrize(
    "plan,code",
    [
        ({"id": "P", "name": "n", "pricing_mode": "bad"}, "INVALID_PARAMETER"),
        ({"id": "P", "name": "n", "pricing_mode": "tiered"}, "MISSING_PARAMETER"),
        ({"id": "P", "name": "n", "pricing_mode": "overage",
          "base_fee": "-1"}, "INVALID_PARAMETER"),
        ({"id": "P", "name": "n", "pricing_mode": "overage",
          "included_units": -2}, "INVALID_PARAMETER"),
        ({"id": "P", "name": "n", "pricing_mode": "overage",
          "tiers": [{"up_to": 1, "unit_price": "1"}]}, "INVALID_PARAMETER"),
        ({"id": "P", "name": "n", "pricing_mode": "tiered",
          "tiers": [{"up_to": 10, "unit_price": "1"},
                    {"up_to": 5, "unit_price": "2"},
                    {"up_to": None, "unit_price": "3"}]}, "INVALID_PARAMETER"),
    ],
)
def test_invalid_plan_config(svc, admin, plan, code):
    with pytest.raises(BillingError) as ei:
        svc.create_plan(plan, admin)
    assert ei.value.code == code


@pytest.mark.business(f"{V}/非法锚定日建订阅拒绝")
def test_bad_anchor_day_on_subscription(svc, admin):
    svc.create_plan({"id": "P", "name": "n", "pricing_mode": "overage"}, admin)
    with pytest.raises(BillingError) as ei:
        svc.create_subscription(
            {"id": "S", "plan_id": "P", "customer_id": "admin-1",
             "anchor_day": 31}, admin)
    assert ei.value.code == "INVALID_PARAMETER"
