"""Thread-safe in-memory store with idempotency records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from .models import Invoice, Plan, Subscription, UsageEvent


@dataclass
class IdempotencyRecord:
    key: str
    request_hash: str
    response: object


@dataclass
class Store:
    lock: threading.RLock = field(default_factory=threading.RLock)
    plans: dict[str, Plan] = field(default_factory=dict)
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    usage_events: dict[str, UsageEvent] = field(default_factory=dict)  # by event_id
    # (subscription_id, period_start) -> aggregated units
    usage_by_period: dict[tuple[str, datetime], float] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    # (subscription_id, period_start) -> invoice_id, one invoice per period
    invoice_by_period: dict[tuple[str, datetime], str] = field(default_factory=dict)
    closed_periods: set[tuple[str, datetime]] = field(default_factory=set)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)
