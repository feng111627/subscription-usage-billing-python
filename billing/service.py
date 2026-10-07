"""Billing service: subscriptions, usage ingestion, invoicing, payments."""
from __future__ import annotations

from datetime import datetime

from .models import Invoice, Plan, Subscription, UsageEvent
from .store import Store


class BillingService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # --- plan & subscription -------------------------------------------------
    def create_plan(self, plan: Plan) -> Plan:
        raise NotImplementedError

    def subscribe(self, plan_id: str, customer_id: str, anchor: datetime) -> Subscription:
        raise NotImplementedError

    # --- usage ---------------------------------------------------------------
    def report_usage(
        self,
        subscription_id: str,
        quantity: float,
        occurred_at: datetime,
        event_id: str,
    ) -> UsageEvent:
        """Ingest a usage event; duplicate event_ids are rejected idempotently."""
        raise NotImplementedError

    # --- invoicing -----------------------------------------------------------
    def close_period(self, subscription_id: str, idempotency_key: str) -> Invoice:
        """Close the current period and issue an invoice (idempotent)."""
        raise NotImplementedError

    # --- invoice lifecycle ---------------------------------------------------
    def pay_invoice(self, invoice_id: str, actor: str = "customer") -> Invoice:
        raise NotImplementedError

    def void_invoice(self, invoice_id: str, actor: str = "admin") -> Invoice:
        raise NotImplementedError

    def refund_invoice(self, invoice_id: str, actor: str = "admin") -> Invoice:
        raise NotImplementedError
