"""Thread-safe in-memory store with idempotency records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

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
    usage_by_period: dict[tuple[str, int], float] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)
