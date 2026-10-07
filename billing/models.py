"""领域模型：计划、订阅、用量事件、周期汇总与账单。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class PricingMode(str, Enum):
    TIERED = "tiered"
    OVERAGE = "overage"


class BillStatus(str, Enum):
    OPEN = "open"
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class Tier:
    up_to: int | None
    unit_price: Decimal


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    pricing_mode: PricingMode
    base_fee: Decimal
    included_units: int
    overage_price: Decimal
    tiers: tuple[Tier, ...]
    currency: str = "CNY"


@dataclass
class Subscription:
    id: str
    plan_id: str
    customer_id: str
    anchor_day: int
    started_at: datetime
    canceled_at: datetime | None = None


@dataclass
class UsageEvent:
    event_id: str
    subscription_id: str
    quantity: Decimal
    occurred_at: datetime
    recorded_at: datetime
    period_key: str


@dataclass
class PeriodAggregate:
    subscription_id: str
    period_key: str
    period_start: datetime
    period_end: datetime
    total_quantity: Decimal = Decimal("0")
    event_count: int = 0
    closed: bool = False


@dataclass
class Bill:
    id: str
    subscription_id: str
    plan_id: str
    period_key: str
    period_start: datetime
    period_end: datetime
    status: BillStatus
    base_fee: Decimal
    usage_amount: Decimal
    total_amount: Decimal
    quantity: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime
    version: int = 0
    history: list[tuple[str, datetime, str]] = field(default_factory=list)
