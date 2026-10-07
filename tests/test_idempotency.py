"""幂等与重放：用量事件去重、出账幂等键、每周期唯一账单。"""
from __future__ import annotations

import pytest

from billing.errors import BillingError, ErrorReason
from tests.conftest import day


class TestUsageIdempotency:
    def test_replay_same_event_does_not_double_count(self, subscribed):
        service, sub = subscribed
        first = service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        second = service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        assert second.event_id == first.event_id
        assert service.store.usage_by_period[(sub.subscription_id, day(0))] == 10
        assert len(service.store.usage_events) == 1

    def test_same_event_id_different_payload_conflicts(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        with pytest.raises(BillingError) as exc:
            service.report_usage(sub.subscription_id, 99, day(1), "evt-1")
        assert exc.value.reason == ErrorReason.IDEMPOTENCY_CONFLICT

    def test_replay_after_period_closed_still_returns_original(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        service.close_period(sub.subscription_id, "k1")
        # 重放已入账周期内的事件：返回原记录，而不是报"周期已关闭"
        replayed = service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        assert replayed.event_id == "evt-1"

    @pytest.mark.parametrize(
        "quantity", [0, -5, float("nan")], ids=["零", "负数", "NaN"]
    )
    def test_illegal_quantity_rejected(self, subscribed, quantity):
        service, sub = subscribed
        with pytest.raises(BillingError) as exc:
            service.report_usage(sub.subscription_id, quantity, day(1), "evt-x")
        assert exc.value.reason == ErrorReason.VALIDATION_FAILED

    def test_unknown_subscription_rejected(self, service):
        with pytest.raises(BillingError) as exc:
            service.report_usage("no-such-sub", 1, day(1), "evt-x")
        assert exc.value.reason == ErrorReason.SUBSCRIPTION_NOT_FOUND


class TestClosePeriodIdempotency:
    def test_same_key_replay_returns_same_invoice(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 150, day(1), "evt-1")
        inv1 = service.close_period(sub.subscription_id, "close-key")
        inv2 = service.close_period(sub.subscription_id, "close-key")
        assert inv1.invoice_id == inv2.invoice_id
        assert len(service.store.invoices) == 1
        assert inv2.total_cents == 100 * 10 + 50 * 20

    def test_key_reuse_with_different_request_conflicts(self, subscribed):
        service, sub = subscribed
        service.close_period(sub.subscription_id, "shared-key")
        other = service.subscribe(sub.plan_id, "cust-2", day(0))
        with pytest.raises(BillingError) as exc:
            service.close_period(other.subscription_id, "shared-key")
        assert exc.value.reason == ErrorReason.IDEMPOTENCY_CONFLICT

    def test_one_invoice_per_period_even_with_different_keys(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 10, day(1), "evt-1")
        inv = service.close_period(sub.subscription_id, "k1")
        # 周期已推进，再次出账针对的是新的空周期，不会重复计上一周期
        inv2 = service.close_period(sub.subscription_id, "k2")
        assert inv2.invoice_id != inv.invoice_id
        assert inv2.total_cents == 0
        assert service.store.invoice_by_period[(sub.subscription_id, day(0))] == inv.invoice_id
        assert len(service.store.invoices) == 2

    def test_missing_idempotency_key_rejected(self, subscribed):
        service, sub = subscribed
        with pytest.raises(BillingError) as exc:
            service.close_period(sub.subscription_id, "")
        assert exc.value.reason == ErrorReason.VALIDATION_FAILED

    def test_usage_into_closed_period_rejected(self, subscribed):
        service, sub = subscribed
        service.close_period(sub.subscription_id, "k1")
        with pytest.raises(BillingError) as exc:
            service.report_usage(sub.subscription_id, 5, day(2), "evt-late")
        assert exc.value.reason == ErrorReason.USAGE_OUT_OF_PERIOD

    def test_close_unknown_subscription_rejected(self, service):
        with pytest.raises(BillingError) as exc:
            service.close_period("no-such-sub", "k")
        assert exc.value.reason == ErrorReason.SUBSCRIPTION_NOT_FOUND
