"""账单状态流转：合法转移、非法转移拒绝、越权拒绝、作废/退款后账目不变。"""
from __future__ import annotations

import pytest

from billing.errors import BillingError, ErrorReason
from billing.models import InvoiceStatus
from tests.conftest import day


@pytest.fixture()
def issued_invoice(subscribed):
    service, sub = subscribed
    service.report_usage(sub.subscription_id, 150, day(1), "evt-1")
    invoice = service.close_period(sub.subscription_id, "k1")
    return service, invoice


class TestLegalTransitions:
    def test_issue_to_paid(self, issued_invoice):
        service, inv = issued_invoice
        before = inv.version
        paid = service.pay_invoice(inv.invoice_id, actor="customer")
        assert paid.status == InvoiceStatus.PAID
        assert paid.version == before + 1

    def test_issue_to_void(self, issued_invoice):
        service, inv = issued_invoice
        voided = service.void_invoice(inv.invoice_id, actor="admin")
        assert voided.status == InvoiceStatus.VOID

    def test_paid_to_refunded(self, issued_invoice):
        service, inv = issued_invoice
        service.pay_invoice(inv.invoice_id)
        refunded = service.refund_invoice(inv.invoice_id, actor="admin")
        assert refunded.status == InvoiceStatus.REFUNDED

    def test_amounts_unchanged_across_transitions(self, issued_invoice):
        """出账、作废、退款前后金额必须对得上。"""
        service, inv = issued_invoice
        expected = 100 * 10 + 50 * 20
        assert inv.total_cents == expected
        service.pay_invoice(inv.invoice_id)
        assert service.store.invoices[inv.invoice_id].total_cents == expected
        service.refund_invoice(inv.invoice_id, actor="admin")
        final = service.store.invoices[inv.invoice_id]
        assert final.total_cents == expected
        assert sum(l.amount_cents for l in final.lines) == final.total_cents


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "path",
        [
            ["void", "pay"],       # VOID 是终态
            ["void", "refund"],    # 作废单不能退款
            ["pay", "void"],     # 已支付只能退款，不能作废
            ["pay", "refund", "pay"],
            ["refund"],            # 未支付不能退款
            ["pay", "pay"],        # 重复支付
        ],
        ids=[
            "作废后支付",
            "作废后退款",
            "支付后作废",
            "退款后再支付",
            "未支付退款",
            "重复支付",
        ],
    )
    def test_illegal_path_rejected(self, issued_invoice, path):
        service, inv = issued_invoice
        ops = {
            "pay": lambda: service.pay_invoice(inv.invoice_id),
            "void": lambda: service.void_invoice(inv.invoice_id, actor="admin"),
            "refund": lambda: service.refund_invoice(inv.invoice_id, actor="admin"),
        }
        for step in path[:-1]:
            ops[step]()
        with pytest.raises(BillingError) as exc:
            ops[path[-1]]()
        assert exc.value.reason == ErrorReason.ILLEGAL_STATE_TRANSITION


class TestAuthorization:
    @pytest.mark.parametrize("op", ["void", "refund"])
    def test_non_admin_cannot_void_or_refund(self, issued_invoice, op):
        service, inv = issued_invoice
        if op == "refund":
            service.pay_invoice(inv.invoice_id)
            call = lambda: service.refund_invoice(inv.invoice_id, actor="customer")
        else:
            call = lambda: service.void_invoice(inv.invoice_id, actor="customer")
        with pytest.raises(BillingError) as exc:
            call()
        assert exc.value.reason == ErrorReason.FORBIDDEN

    def test_authorization_checked_before_state(self, issued_invoice):
        """越权优先于状态检查：即使状态允许，非 admin 也被拒。"""
        service, inv = issued_invoice
        with pytest.raises(BillingError) as exc:
            service.void_invoice(inv.invoice_id, actor="customer")
        assert exc.value.reason == ErrorReason.FORBIDDEN
        assert service.store.invoices[inv.invoice_id].status == InvoiceStatus.ISSUED

    def test_unknown_invoice_rejected(self, service):
        with pytest.raises(BillingError) as exc:
            service.pay_invoice("no-such-invoice")
        assert exc.value.reason == ErrorReason.INVOICE_NOT_FOUND
