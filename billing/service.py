"""应用服务：参数校验、权限、幂等、并发原子性、账单状态流转。

关键正确性约束：
- 上报用量在单个仓储锁临界区内完成“事件去重 + 汇总累加”，并发下
  不丢计、不重复计；事件 ID 与 HTTP 幂等键双重幂等。
- 出账在单临界区内完成“查既有账单 + 锁周期 + 计价 + 落账”，
  并发出账只会得到同一张账单（同 ID、同金额）。
- 金额全程 Decimal，落账时按 0.01、ROUND_HALF_UP 取整。
- 账单状态机：ISSUED -> PAID -> REFUNDED；ISSUED -> VOID；
  其余转移一律以 INVALID_BILL_TRANSITION 拒绝。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from billing.errors import BillingError, ErrorCode
from billing.idempotency import IdempotencyStore, IdempotencyRecord, fingerprint
from billing.models import (
    Bill,
    BillStatus,
    PeriodAggregate,
    Plan,
    PricingMode,
    Subscription,
    Tier,
    UsageEvent,
)
from billing.periods import UTC, ensure_utc, now_utc, period_for, period_from_key
from billing.pricing import charge
from billing.repository import Repository

ADMIN = "admin"
CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_decimal(raw: Any, field_name: str) -> Decimal:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        raise BillingError(
            ErrorCode.MISSING_PARAMETER, f"缺少参数 {field_name}", status_code=400
        )
    try:
        if isinstance(raw, bool):
            raise ValueError
        return Decimal(str(raw))
    except Exception:
        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"参数 {field_name} 不是合法数值：{raw!r}",
            status_code=422,
        )


def _parse_datetime(raw: Any, field_name: str) -> datetime:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        raise BillingError(
            ErrorCode.MISSING_PARAMETER, f"缺少参数 {field_name}", status_code=400
        )
    if isinstance(raw, datetime):
        return ensure_utc(raw)
    try:
        text = str(raw).strip().replace("Z", "+00:00")
        return ensure_utc(datetime.fromisoformat(text))
    except Exception:
        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"参数 {field_name} 不是合法 ISO-8601 时间：{raw!r}",
            status_code=422,
        )


def _require_nonempty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BillingError(
            ErrorCode.MISSING_PARAMETER
            if value is None
            else ErrorCode.INVALID_PARAMETER,
            f"参数 {field_name} 不能为空",
            status_code=400 if value is None else 422,
        )
    return value.strip()


class BillingService:
    def __init__(
        self,
        repo: Repository | None = None,
        idempotency: IdempotencyStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo or Repository()
        self.idempotency = idempotency or IdempotencyStore()
        self.clock = clock or now_utc

    # ---------- 序列化 ----------
    @staticmethod
    def _iso(dt: datetime) -> str:
        return ensure_utc(dt).isoformat().replace("+00:00", "Z")

    def _bill_dict(self, bill: Bill, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "id": bill.id,
            "subscription_id": bill.subscription_id,
            "plan_id": bill.plan_id,
            "period_key": bill.period_key,
            "period_start": self._iso(bill.period_start),
            "period_end": self._iso(bill.period_end),
            "status": bill.status.value,
            "base_fee": str(bill.base_fee),
            "usage_amount": str(bill.usage_amount),
            "total_amount": str(bill.total_amount),
            "quantity": str(bill.quantity),
            "currency": bill.currency,
            "version": bill.version,
            "created_at": self._iso(bill.created_at),
            "updated_at": self._iso(bill.updated_at),
            "history": [
                {"action": a, "at": self._iso(t), "actor": actor}
                for a, t, actor in bill.history
            ],
            "replayed": replayed,
        }

    def _agg_dict(self, agg: PeriodAggregate, *, event_id: str | None = None,
                  replayed: bool = False) -> dict[str, Any]:
        return {
            "subscription_id": agg.subscription_id,
            "period_key": agg.period_key,
            "period_start": self._iso(agg.period_start),
            "period_end": self._iso(agg.period_end),
            "total_quantity": str(agg.total_quantity),
            "event_count": agg.event_count,
            "closed": agg.closed,
            "event_id": event_id,
            "replayed": replayed,
        }

    # ---------- 鉴权 ----------
    @staticmethod
    def _require_admin(actor: dict[str, str]) -> None:
        if actor.get("role") != ADMIN:
            raise BillingError(
                ErrorCode.FORBIDDEN,
                "该操作仅允许计费管理员执行",
                status_code=403,
            )

    def _load_subscription(self, sub_id: str) -> Subscription:
        sub = self.repo.get_subscription(sub_id)
        if sub is None:
            raise BillingError(
                ErrorCode.SUBSCRIPTION_NOT_FOUND,
                f"订阅不存在：{sub_id}",
                status_code=404,
            )
        return sub

    def _require_owner_or_admin(
        self, actor: dict[str, str], sub: Subscription
    ) -> None:
        if actor.get("role") == ADMIN:
            return
        if actor.get("id") != sub.customer_id:
            raise BillingError(
                ErrorCode.FORBIDDEN,
                "无权操作他人的订阅资源",
                status_code=403,
            )

    # ---------- HTTP 幂等 ----------
    def with_idempotency(
        self,
        actor: dict[str, str],
        key: str | None,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not key:
            return fn()
        fp = fingerprint(method, path, payload)
        existing = self.idempotency.get(actor["id"], key)
        if existing is not None:
            if existing.fingerprint != fp:
                raise BillingError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同幂等键对应了不同的请求内容",
                    status_code=409,
                )
            return dict(existing.response)
        result = fn()
        self.idempotency.save(
            IdempotencyRecord(
                key=key, actor_id=actor["id"], fingerprint=fp, response=dict(result)
            )
        )
        return result

    # ---------- 计划 ----------
    def create_plan(self, payload: dict[str, Any], actor: dict[str, str]) -> dict:
        self._require_admin(actor)
        plan_id = _require_nonempty(payload.get("id"), "id")
        name = _require_nonempty(payload.get("name"), "name")
        mode_raw = _require_nonempty(payload.get("pricing_mode"), "pricing_mode")
        try:
            mode = PricingMode(mode_raw)
        except ValueError:
            raise BillingError(
                ErrorCode.INVALID_PARAMETER,
                f"pricing_mode 必须是 tiered 或 overage，收到 {mode_raw!r}",
                status_code=422,
            )
        base_fee = _parse_decimal(payload.get("base_fee", "0"), "base_fee")
        overage_price = _parse_decimal(
            payload.get("overage_price", "0"), "overage_price"
        )
        included_units = payload.get("included_units", 0)
        if (
            not isinstance(included_units, int)
            or isinstance(included_units, bool)
            or included_units < 0
        ):
            raise BillingError(
                ErrorCode.INVALID_PARAMETER,
                "included_units 必须为非负整数",
                status_code=422,
            )
        currency = payload.get("currency") or "CNY"
        if not isinstance(currency, str) or not currency.strip():
            raise BillingError(
                ErrorCode.INVALID_PARAMETER, "currency 不能为空", status_code=422
            )
        for value, label in ((base_fee, "base_fee"), (overage_price, "overage_price")):
            if value < 0:
                raise BillingError(
                    ErrorCode.INVALID_PARAMETER,
                    f"{label} 不能为负",
                    status_code=422,
                )

        tiers_raw = payload.get("tiers") or []
        if not isinstance(tiers_raw, list):
            raise BillingError(
                ErrorCode.INVALID_PARAMETER, "tiers 必须为数组", status_code=422
            )
        tiers: list[Tier] = []
        if mode is PricingMode.TIERED:
            if not tiers_raw:
                raise BillingError(
                    ErrorCode.MISSING_PARAMETER,
                    "tiered 计划必须提供至少一个阶梯 tiers",
                    status_code=400,
                )
            prev_bound = 0
            for i, t in enumerate(tiers_raw):
                price = _parse_decimal(t.get("unit_price"), f"tiers[{i}].unit_price")
                if price < 0:
                    raise BillingError(
                        ErrorCode.INVALID_PARAMETER,
                        f"tiers[{i}].unit_price 不能为负",
                        status_code=422,
                    )
                up_to = t.get("up_to")
                is_last = i == len(tiers_raw) - 1
                if up_to is None:
                    if not is_last:
                        raise BillingError(
                            ErrorCode.INVALID_PARAMETER,
                            "只有最后一个阶梯的 up_to 可以为 null（无上限）",
                            status_code=422,
                        )
                else:
                    if not isinstance(up_to, int) or isinstance(up_to, bool):
                        raise BillingError(
                            ErrorCode.INVALID_PARAMETER,
                            f"tiers[{i}].up_to 必须为整数",
                            status_code=422,
                        )
                    if up_to <= prev_bound:
                        raise BillingError(
                            ErrorCode.INVALID_PARAMETER,
                            "阶梯上界必须严格递增且为正",
                            status_code=422,
                        )
                    prev_bound = up_to
                tiers.append(Tier(up_to=up_to, unit_price=price))
        else:
            if tiers_raw:
                raise BillingError(
                    ErrorCode.INVALID_PARAMETER,
                    "overage 计划不应配置 tiers",
                    status_code=422,
                )

        with self.repo.lock:
            if self.repo.get_plan(plan_id) is not None:
                raise BillingError(
                    ErrorCode.PLAN_ALREADY_EXISTS,
                    f"计划已存在：{plan_id}",
                    status_code=409,
                )
            plan = Plan(
                id=plan_id,
                name=name,
                pricing_mode=mode,
                base_fee=_money(base_fee),
                included_units=included_units,
                overage_price=overage_price,
                tiers=tuple(tiers),
                currency=currency.strip(),
            )
            self.repo.save_plan(plan)
        return {
            "id": plan.id,
            "name": plan.name,
            "pricing_mode": plan.pricing_mode.value,
            "base_fee": str(plan.base_fee),
            "included_units": plan.included_units,
            "overage_price": str(plan.overage_price),
            "tiers": [
                {"up_to": t.up_to, "unit_price": str(t.unit_price)} for t in plan.tiers
            ],
            "currency": plan.currency,
        }

    # ---------- 订阅 ----------
    def create_subscription(
        self, payload: dict[str, Any], actor: dict[str, str]
    ) -> dict[str, Any]:
        sub_id = _require_nonempty(payload.get("id"), "id")
        plan_id = _require_nonempty(payload.get("plan_id"), "plan_id")
        customer_id = _require_nonempty(payload.get("customer_id"), "customer_id")
        if actor.get("role") != ADMIN and actor["id"] != customer_id:
            raise BillingError(
                ErrorCode.FORBIDDEN,
                "客户只能为自己创建订阅",
                status_code=403,
            )
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            raise BillingError(
                ErrorCode.PLAN_NOT_FOUND, f"计划不存在：{plan_id}", status_code=404
            )
        anchor_day = payload.get("anchor_day", 1)
        if not isinstance(anchor_day, int) or isinstance(anchor_day, bool):
            raise BillingError(
                ErrorCode.INVALID_PARAMETER,
                "anchor_day 必须为 1..28 的整数",
                status_code=422,
            )
        # 非法锚定日在这里被明确拒绝（周期归属才有稳定定义）
        from billing.periods import validate_anchor_day

        validate_anchor_day(anchor_day)
        started_at = (
            _parse_datetime(payload.get("started_at"), "started_at")
            if payload.get("started_at")
            else self.clock()
        )
        with self.repo.lock:
            if self.repo.get_subscription(sub_id) is not None:
                raise BillingError(
                    ErrorCode.SUBSCRIPTION_ALREADY_EXISTS,
                    f"订阅已存在：{sub_id}",
                    status_code=409,
                )
            sub = Subscription(
                id=sub_id,
                plan_id=plan_id,
                customer_id=customer_id,
                anchor_day=anchor_day,
                started_at=started_at,
            )
            self.repo.save_subscription(sub)
        return {
            "id": sub.id,
            "plan_id": sub.plan_id,
            "customer_id": sub.customer_id,
            "anchor_day": sub.anchor_day,
            "started_at": self._iso(sub.started_at),
            "canceled_at": None,
        }

    def cancel_subscription(self, sub_id: str, actor: dict[str, str]) -> dict[str, Any]:
        with self.repo.lock:
            sub = self._load_subscription(sub_id)
            self._require_owner_or_admin(actor, sub)
            if sub.canceled_at is not None:
                return {
                    "id": sub.id,
                    "canceled_at": self._iso(sub.canceled_at),
                    "replayed": True,
                }
            sub.canceled_at = self.clock()
            self.repo.save_subscription(sub)
        return {"id": sub.id, "canceled_at": self._iso(sub.canceled_at)}

    # ---------- 用量上报 ----------
    def report_usage(
        self,
        payload: dict[str, Any],
        actor: dict[str, str],
        *,
        idempotency_key: str | None = None,
        method: str = "POST",
        path: str = "/usage",
    ) -> dict[str, Any]:
        return self.with_idempotency(
            actor, idempotency_key, method, path, payload,
            lambda: self._report_usage(payload, actor),
        )

    def _report_usage(
        self, payload: dict[str, Any], actor: dict[str, str]
    ) -> dict[str, Any]:
        event_id = _require_nonempty(payload.get("event_id"), "event_id")
        sub_id = _require_nonempty(payload.get("subscription_id"), "subscription_id")
        quantity = _parse_decimal(payload.get("quantity"), "quantity")
        if quantity <= 0:
            raise BillingError(
                ErrorCode.INVALID_PARAMETER,
                f"quantity 必须大于 0，收到 {quantity}",
                status_code=422,
            )
        occurred_at = (
            _parse_datetime(payload.get("occurred_at"), "occurred_at")
            if payload.get("occurred_at")
            else self.clock()
        )
        with self.repo.lock:
            sub = self._load_subscription(sub_id)
            self._require_owner_or_admin(actor, sub)
            if occurred_at < sub.started_at:
                raise BillingError(
                    ErrorCode.INVALID_PARAMETER,
                    "用量时间早于订阅生效时间",
                    status_code=422,
                )
            if sub.canceled_at is not None and occurred_at >= sub.canceled_at:
                raise BillingError(
                    ErrorCode.INVALID_PARAMETER,
                    "用量时间晚于订阅取消时间",
                    status_code=422,
                )
            period = period_for(sub.anchor_day, occurred_at)
            agg = self.repo.get_aggregate(sub_id, period.key)
            if agg is not None and agg.closed:
                raise BillingError(
                    ErrorCode.PERIOD_CLOSED,
                    f"周期 {period.key} 已出账锁定，拒绝补报用量",
                    status_code=409,
                )
            event = UsageEvent(
                event_id=event_id,
                subscription_id=sub_id,
                quantity=quantity,
                occurred_at=occurred_at,
                recorded_at=self.clock(),
                period_key=period.key,
            )
            # 先去重：同一 event_id 绝不重复归集
            inserted = self.repo.insert_event_if_absent(event)
            if not inserted:
                stored = self.repo.get_event(event_id)
                assert stored is not None
                same = (
                    stored.subscription_id == sub_id
                    and stored.quantity == quantity
                    and stored.occurred_at == occurred_at
                )
                if not same:
                    raise BillingError(
                        ErrorCode.DUPLICATE_EVENT,
                        f"事件 ID {event_id} 已存在且内容不同",
                        status_code=409,
                    )
                existing_agg = self.repo.get_aggregate(sub_id, period.key)
                return self._agg_dict(
                    existing_agg, event_id=event_id, replayed=True
                )

            if agg is None:
                agg = PeriodAggregate(
                    subscription_id=sub_id,
                    period_key=period.key,
                    period_start=period.start,
                    period_end=period.end,
                )
            agg.total_quantity += quantity
            agg.event_count += 1
            self.repo.save_aggregate(agg)
            return self._agg_dict(agg, event_id=event_id)

    def get_aggregate(
        self, sub_id: str, period_key: str, actor: dict[str, str]
    ) -> dict[str, Any]:
        sub = self._load_subscription(sub_id)
        self._require_owner_or_admin(actor, sub)
        period = period_from_key(period_key)
        agg = self.repo.get_aggregate(sub_id, period.key)
        if agg is None:
            return {
                "subscription_id": sub_id,
                "period_key": period.key,
                "period_start": self._iso(period.start),
                "period_end": self._iso(period.end),
                "total_quantity": "0",
                "event_count": 0,
                "closed": False,
            }
        return self._agg_dict(agg)

    # ---------- 出账 ----------
    def issue_bill(
        self,
        sub_id: str,
        period_key: str,
        actor: dict[str, str],
        *,
        idempotency_key: str | None = None,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        path = path or f"/subscriptions/{sub_id}/bills/{period_key}/issue"
        return self.with_idempotency(
            actor,
            idempotency_key,
            method,
            path,
            {"subscription_id": sub_id, "period_key": period_key},
            lambda: self._issue_bill(sub_id, period_key, actor),
        )

    def _issue_bill(self, sub_id: str, period_key: str, actor: dict[str, str]):
        self._require_admin(actor)
        period = period_from_key(period_key)
        with self.repo.lock:
            sub = self._load_subscription(sub_id)
            if self.clock() < period.end:
                raise BillingError(
                    ErrorCode.PERIOD_NOT_ENDED,
                    f"周期 {period.key} 尚未结束，不能出账",
                    status_code=422,
                )
            plan = self.repo.get_plan(sub.plan_id)
            if plan is None:
                raise BillingError(
                    ErrorCode.PLAN_NOT_FOUND,
                    f"计划不存在：{sub.plan_id}",
                    status_code=404,
                )
            existing = self.repo.find_bill(sub_id, period.key)
            if existing is not None and existing.status is not BillStatus.VOID:
                # 幂等：同一周期重复出账直接返回既有账单，不重复计费
                return self._bill_dict(existing, replayed=True)

            agg = self.repo.get_aggregate(sub_id, period.key)
            quantity = agg.total_quantity if agg else Decimal("0")
            base_fee = _money(plan.base_fee)
            usage_amount = _money(charge(plan, quantity))
            total_amount = _money(base_fee + usage_amount)
            revision = 0
            if existing is not None:  # 上一张已作废 -> 新版本账单
                revision = int(existing.id.rsplit("__r", 1)[1]) + 1 if "__r" in existing.id else 1
            bill_id = f"bill-{sub_id}-{period.key}" + (
                f"__r{revision}" if revision else ""
            )
            now = self.clock()
            bill = Bill(
                id=bill_id,
                subscription_id=sub_id,
                plan_id=plan.id,
                period_key=period.key,
                period_start=period.start,
                period_end=period.end,
                status=BillStatus.ISSUED,
                base_fee=base_fee,
                usage_amount=usage_amount,
                total_amount=total_amount,
                quantity=quantity,
                currency=plan.currency,
                created_at=now,
                updated_at=now,
                version=1,
                history=[("issued", now, actor["id"])],
            )
            self.repo.save_bill(bill)
            if agg is None:
                agg = PeriodAggregate(
                    subscription_id=sub_id,
                    period_key=period.key,
                    period_start=period.start,
                    period_end=period.end,
                )
            agg.closed = True
            self.repo.save_aggregate(agg)
            return self._bill_dict(bill)

    # ---------- 账单状态流转 ----------
    def _load_bill_for_admin(self, bill_id: str, actor: dict[str, str]) -> Bill:
        self._require_admin(actor)
        bill = self.repo.get_bill(bill_id)
        if bill is None:
            raise BillingError(
                ErrorCode.BILL_NOT_FOUND, f"账单不存在：{bill_id}", status_code=404
            )
        return bill

    def _transition(
        self,
        bill_id: str,
        actor: dict[str, str],
        action: str,
        allowed_from: tuple[BillStatus, ...],
        target: BillStatus,
    ) -> dict[str, Any]:
        with self.repo.lock:
            bill = self._load_bill_for_admin(bill_id, actor)
            if bill.status not in allowed_from:
                raise BillingError(
                    ErrorCode.INVALID_BILL_TRANSITION,
                    f"账单状态 {bill.status.value} 不允许执行 {action}，"
                    f"仅 {[s.value for s in allowed_from]} 可执行",
                    status_code=409,
                )
            bill.status = target
            bill.version += 1
            bill.updated_at = self.clock()
            bill.history.append((action, bill.updated_at, actor["id"]))
            self.repo.save_bill(bill)
            return self._bill_dict(bill)

    def mark_paid(
        self,
        bill_id: str,
        actor: dict[str, str],
        *,
        idempotency_key: str | None = None,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        path = path or f"/bills/{bill_id}/pay"

        def _do() -> dict[str, Any]:
            with self.repo.lock:
                bill = self.repo.get_bill(bill_id)
                # 重放仅针对“已经是目标状态”的账单；其余状态交给状态机判定
                if bill is not None and bill.status is BillStatus.PAID:
                    return self._bill_dict(bill, replayed=True)
            return self._transition(
                bill_id, actor, "paid", (BillStatus.ISSUED,), BillStatus.PAID
            )

        return self.with_idempotency(
            actor, idempotency_key, method, path, {"bill_id": bill_id}, _do
        )

    def void_bill(
        self,
        bill_id: str,
        actor: dict[str, str],
        *,
        idempotency_key: str | None = None,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        path = path or f"/bills/{bill_id}/void"

        def _do() -> dict[str, Any]:
            with self.repo.lock:
                bill = self.repo.get_bill(bill_id)
                if bill is not None and bill.status is BillStatus.VOID:
                    return self._bill_dict(bill, replayed=True)
            return self._transition(
                bill_id, actor, "void", (BillStatus.ISSUED,), BillStatus.VOID
            )

        return self.with_idempotency(
            actor, idempotency_key, method, path, {"bill_id": bill_id}, _do
        )

    def refund_bill(
        self,
        bill_id: str,
        actor: dict[str, str],
        *,
        idempotency_key: str | None = None,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        path = path or f"/bills/{bill_id}/refund"

        def _do() -> dict[str, Any]:
            with self.repo.lock:
                bill = self.repo.get_bill(bill_id)
                if bill is not None and bill.status is BillStatus.REFUNDED:
                    return self._bill_dict(bill, replayed=True)
            return self._transition(
                bill_id, actor, "refund", (BillStatus.PAID,), BillStatus.REFUNDED
            )

        return self.with_idempotency(
            actor, idempotency_key, method, path, {"bill_id": bill_id}, _do
        )

    def get_bill(self, bill_id: str, actor: dict[str, str]) -> dict[str, Any]:
        bill = self.repo.get_bill(bill_id)
        if bill is None:
            raise BillingError(
                ErrorCode.BILL_NOT_FOUND, f"账单不存在：{bill_id}", status_code=404
            )
        sub = self._load_subscription(bill.subscription_id)
        self._require_owner_or_admin(actor, sub)
        return self._bill_dict(bill)
