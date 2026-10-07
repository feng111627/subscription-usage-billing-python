"""Domain models: plans, subscriptions, usage events, invoices."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PricingModel(str, Enum):
    TIERED = "TIERED"      # graduated tiers, each unit priced by its bracket
    OVERAGE = "OVERAGE"    # included quota free, overage billed at flat rate


class InvoiceStatus(str, Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PAID = "PAID"
    VOID = "VOID"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True)
class Tier:
    """A graduated pricing bracket. up_to=None means unbounded."""

    up_to: float | None
    unit_price_cents: int


@dataclass(frozen=True)
class Plan:
    plan_id: str
    name: str
    period_days: int
    pricing_model: PricingModel
    tiers: tuple[Tier, ...] = ()
    included_units: float = 0.0
    overage_unit_price_cents: int = 0


@dataclass
class Subscription:
    subscription_id: str
    plan_id: str
    customer_id: str
    anchor: datetime  # period boundaries derive from this anchor
    current_period_start: datetime
    current_period_end: datetime


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    subscription_id: str
    quantity: float
    occurred_at: datetime


@dataclass
class InvoiceLine:
    description: str
    quantity: float
    unit_price_cents: int
    amount_cents: int


@dataclass
class Invoice:
    invoice_id: str
    subscription_id: str
    period_start: datetime
    period_end: datetime
    status: InvoiceStatus = InvoiceStatus.DRAFT
    lines: list[InvoiceLine] = field(default_factory=list)
    total_cents: int = 0
    version: int = 0
