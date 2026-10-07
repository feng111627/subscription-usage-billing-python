"""Billing service: subscriptions, usage ingestion, invoicing, payments.

Concurrency & idempotency rules:
- A single re-entrant lock serializes all mutations, so concurrent usage
  reports and concurrent period closes cannot lose or double-count usage.
- Usage events are deduplicated by ``event_id``: replaying the same event
  returns the stored record; the same id with a different payload is a
  conflict.
- ``close_period`` is idempotent per idempotency key, and additionally
  guarded per (subscription, period) so two different keys can never
  produce two invoices for the same period.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from .errors import BillingError, ErrorReason
from .models import (
    Invoice,
    InvoiceStatus,
    Plan,
    Subscription,
    UsageEvent,
)
from .pricing import compute_lines, period_for, validate_plan
from .store import IdempotencyRecord, Store

# Legal invoice status transitions.
_TRANSITIONS: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.ISSUED, InvoiceStatus.VOID}),
    InvoiceStatus.ISSUED: frozenset({InvoiceStatus.PAID, InvoiceStatus.VOID}),
    InvoiceStatus.PAID: frozenset({InvoiceStatus.REFUNDED}),
    InvoiceStatus.VOID: frozenset(),
    InvoiceStatus.REFUNDED: frozenset(),
}

_ADMIN = "admin"


def _hash_payload(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


class BillingService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # --- plan & subscription -------------------------------------------------
    def create_plan(self, plan: Plan) -> Plan:
        validate_plan(plan)
        with self.store.lock:
            self.store.plans[plan.plan_id] = plan
        return plan

    def subscribe(self, plan_id: str, customer_id: str, anchor: datetime) -> Subscription:
        with self.store.lock:
            plan = self.store.plans.get(plan_id)
            if plan is None:
                raise BillingError(ErrorReason.PLAN_NOT_FOUND, f"plan {plan_id!r} not found")
            if not customer_id:
                raise BillingError(ErrorReason.VALIDATION_FAILED, "customer_id is required")
            start, end = period_for(plan, anchor, anchor)
            sub = Subscription(
                subscription_id=uuid.uuid4().hex,
                plan_id=plan_id,
                customer_id=customer_id,
                anchor=anchor,
                current_period_start=start,
                current_period_end=end,
            )
            self.store.subscriptions[sub.subscription_id] = sub
            return sub

    # --- usage ---------------------------------------------------------------
    def report_usage(
        self,
        subscription_id: str,
        quantity: float,
        occurred_at: datetime,
        event_id: str,
    ) -> UsageEvent:
        """Ingest a usage event; duplicate event_ids are replayed, not re-billed."""
        if not event_id:
            raise BillingError(ErrorReason.VALIDATION_FAILED, "event_id is required")
        if not isinstance(quantity, (int, float)) or quantity != quantity or quantity <= 0:
            raise BillingError(
                ErrorReason.VALIDATION_FAILED, "quantity must be a positive finite number"
            )
        with self.store.lock:
            sub = self.store.subscriptions.get(subscription_id)
            if sub is None:
                raise BillingError(
                    ErrorReason.SUBSCRIPTION_NOT_FOUND,
                    f"subscription {subscription_id!r} not found",
                )
            existing = self.store.usage_events.get(event_id)
            if existing is not None:
                same = (
                    existing.subscription_id == subscription_id
                    and existing.quantity == quantity
                    and existing.occurred_at == occurred_at
                )
                if same:
                    return existing  # idempotent replay
                raise BillingError(
                    ErrorReason.IDEMPOTENCY_CONFLICT,
                    f"event_id {event_id!r} already used with a different payload",
                )
            plan = self.store.plans[sub.plan_id]
            start, end = period_for(plan, sub.anchor, occurred_at)
            if (sub.subscription_id, start) in self.store.closed_periods:
                raise BillingError(
                    ErrorReason.USAGE_OUT_OF_PERIOD,
                    f"period starting {start.isoformat()} is already billed",
                )
            event = UsageEvent(
                event_id=event_id,
                subscription_id=subscription_id,
                quantity=quantity,
                occurred_at=occurred_at,
            )
            self.store.usage_events[event_id] = event
            key = (subscription_id, start)
            self.store.usage_by_period[key] = self.store.usage_by_period.get(key, 0.0) + quantity
            return event

    # --- invoicing -----------------------------------------------------------
    def close_period(self, subscription_id: str, idempotency_key: str) -> Invoice:
        """Close the current period and issue an invoice (idempotent)."""
        if not idempotency_key:
            raise BillingError(ErrorReason.VALIDATION_FAILED, "idempotency_key is required")
        request_hash = _hash_payload({"op": "close_period", "sub": subscription_id})
        with self.store.lock:
            record = self.store.idempotency.get(idempotency_key)
            if record is not None:
                if record.request_hash != request_hash:
                    raise BillingError(
                        ErrorReason.IDEMPOTENCY_CONFLICT,
                        f"idempotency key {idempotency_key!r} reused with a different request",
                    )
                return record.response  # type: ignore[return-value]

            sub = self.store.subscriptions.get(subscription_id)
            if sub is None:
                raise BillingError(
                    ErrorReason.SUBSCRIPTION_NOT_FOUND,
                    f"subscription {subscription_id!r} not found",
                )
            period_key = (subscription_id, sub.current_period_start)
            existing_id = self.store.invoice_by_period.get(period_key)
            if existing_id is not None:
                # Period already billed under another key: return that invoice.
                invoice = self.store.invoices[existing_id]
                self.store.idempotency[idempotency_key] = IdempotencyRecord(
                    idempotency_key, request_hash, invoice
                )
                return invoice

            plan = self.store.plans[sub.plan_id]
            total_units = self.store.usage_by_period.get(period_key, 0.0)
            lines = compute_lines(plan, total_units)
            invoice = Invoice(
                invoice_id=uuid.uuid4().hex,
                subscription_id=subscription_id,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                status=InvoiceStatus.ISSUED,
                lines=lines,
                total_cents=sum(line.amount_cents for line in lines),
            )
            self.store.invoices[invoice.invoice_id] = invoice
            self.store.invoice_by_period[period_key] = invoice.invoice_id
            self.store.closed_periods.add(period_key)
            # Advance the subscription to the next period.
            period_len = sub.current_period_end - sub.current_period_start
            sub.current_period_start = sub.current_period_end
            sub.current_period_end = sub.current_period_end + period_len
            self.store.idempotency[idempotency_key] = IdempotencyRecord(
                idempotency_key, request_hash, invoice
            )
            return invoice

    # --- invoice lifecycle ---------------------------------------------------
    def pay_invoice(self, invoice_id: str, actor: str = "customer") -> Invoice:
        return self._transition(invoice_id, InvoiceStatus.PAID, actor, require_admin=False)

    def void_invoice(self, invoice_id: str, actor: str = "admin") -> Invoice:
        return self._transition(invoice_id, InvoiceStatus.VOID, actor, require_admin=True)

    def refund_invoice(self, invoice_id: str, actor: str = "admin") -> Invoice:
        return self._transition(invoice_id, InvoiceStatus.REFUNDED, actor, require_admin=True)

    def _transition(
        self,
        invoice_id: str,
        target: InvoiceStatus,
        actor: str,
        require_admin: bool,
    ) -> Invoice:
        with self.store.lock:
            invoice = self.store.invoices.get(invoice_id)
            if invoice is None:
                raise BillingError(
                    ErrorReason.INVOICE_NOT_FOUND, f"invoice {invoice_id!r} not found"
                )
            if require_admin and actor != _ADMIN:
                raise BillingError(
                    ErrorReason.FORBIDDEN, f"{target.value} requires an admin actor"
                )
            if target not in _TRANSITIONS[invoice.status]:
                raise BillingError(
                    ErrorReason.ILLEGAL_STATE_TRANSITION,
                    f"cannot move invoice from {invoice.status.value} to {target.value}",
                )
            invoice.status = target
            invoice.version += 1
            return invoice
