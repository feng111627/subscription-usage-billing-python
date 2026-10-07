"""线程安全的内存仓储。

所有读写都在同一把可重入锁下完成；应用服务在需要“读-改-写”原子性
（上报用量、出账、状态流转）时也通过 ``lock`` 上下文把多个操作
绑定为一个临界区，从而保证并发下不丢计、不重复。
"""
from __future__ import annotations

import threading
from typing import Iterator

from billing.models import Bill, PeriodAggregate, Plan, Subscription, UsageEvent

AggKey = tuple[str, str]


class Repository:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._plans: dict[str, Plan] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._events: dict[str, UsageEvent] = {}
        self._aggregates: dict[AggKey, PeriodAggregate] = {}
        self._bills: dict[str, Bill] = {}
        self._bill_index: dict[AggKey, str] = {}

    # ---- 计划 ----
    def save_plan(self, plan: Plan) -> None:
        with self.lock:
            self._plans[plan.id] = plan

    def get_plan(self, plan_id: str) -> Plan | None:
        with self.lock:
            return self._plans.get(plan_id)

    def list_plans(self) -> list[Plan]:
        with self.lock:
            return list(self._plans.values())

    # ---- 订阅 ----
    def save_subscription(self, sub: Subscription) -> None:
        with self.lock:
            self._subscriptions[sub.id] = sub

    def get_subscription(self, sub_id: str) -> Subscription | None:
        with self.lock:
            return self._subscriptions.get(sub_id)

    def list_subscriptions(self) -> list[Subscription]:
        with self.lock:
            return list(self._subscriptions.values())

    # ---- 用量事件 ----
    def insert_event_if_absent(self, event: UsageEvent) -> bool:
        """事件 ID 不存在则插入，返回 True；已存在返回 False。"""
        with self.lock:
            if event.event_id in self._events:
                return False
            self._events[event.event_id] = event
            return True

    def get_event(self, event_id: str) -> UsageEvent | None:
        with self.lock:
            return self._events.get(event_id)

    def iter_events(
        self, subscription_id: str, period_key: str
    ) -> Iterator[UsageEvent]:
        with self.lock:
            events = [
                e
                for e in self._events.values()
                if e.subscription_id == subscription_id
                and e.period_key == period_key
            ]
        yield from events

    # ---- 周期汇总 ----
    def get_aggregate(
        self, subscription_id: str, period_key: str
    ) -> PeriodAggregate | None:
        with self.lock:
            return self._aggregates.get((subscription_id, period_key))

    def save_aggregate(self, agg: PeriodAggregate) -> None:
        with self.lock:
            self._aggregates[(agg.subscription_id, agg.period_key)] = agg

    def list_aggregates(self, subscription_id: str) -> list[PeriodAggregate]:
        with self.lock:
            return [
                a
                for (sid, _pk), a in self._aggregates.items()
                if sid == subscription_id
            ]

    # ---- 账单 ----
    def save_bill(self, bill: Bill) -> None:
        with self.lock:
            self._bills[bill.id] = bill
            self._bill_index[(bill.subscription_id, bill.period_key)] = bill.id

    def get_bill(self, bill_id: str) -> Bill | None:
        with self.lock:
            return self._bills.get(bill_id)

    def find_bill(self, subscription_id: str, period_key: str) -> Bill | None:
        with self.lock:
            bill_id = self._bill_index.get((subscription_id, period_key))
            return self._bills.get(bill_id) if bill_id else None

    def list_bills(self, subscription_id: str | None = None) -> list[Bill]:
        with self.lock:
            bills = list(self._bills.values())
        if subscription_id is not None:
            bills = [b for b in bills if b.subscription_id == subscription_id]
        bills.sort(key=lambda b: (b.subscription_id, b.period_key))
        return bills
