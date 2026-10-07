"""Pricing: period resolution and charge computation."""
from __future__ import annotations

from datetime import datetime, timedelta

from .errors import BillingError, ErrorReason
from .models import InvoiceLine, Plan, PricingModel


def period_for(plan: Plan, anchor: datetime, at: datetime) -> tuple[datetime, datetime]:
    """Return the half-open [start, end) billing period containing `at`.

    Boundaries are derived from the subscription anchor, so every instant
    belongs to exactly one deterministic period: an event exactly on a
    boundary belongs to the period that starts at that boundary.
    """
    if at < anchor:
        raise BillingError(
            ErrorReason.USAGE_OUT_OF_PERIOD,
            f"usage at {at.isoformat()} precedes subscription anchor {anchor.isoformat()}",
        )
    length = timedelta(days=plan.period_days)
    elapsed = at - anchor
    k = elapsed // length  # floor division on timedelta
    start = anchor + k * length
    return start, start + length


def validate_plan(plan: Plan) -> None:
    """Reject plans whose pricing parameters are missing or illegal."""
    if plan.period_days <= 0:
        raise BillingError(ErrorReason.VALIDATION_FAILED, "period_days must be positive")
    if plan.pricing_model is PricingModel.TIERED:
        if not plan.tiers:
            raise BillingError(ErrorReason.VALIDATION_FAILED, "TIERED plan requires tiers")
        prev = 0.0
        for i, tier in enumerate(plan.tiers):
            if tier.unit_price_cents < 0:
                raise BillingError(ErrorReason.VALIDATION_FAILED, "tier price must be >= 0")
            if tier.up_to is None:
                if i != len(plan.tiers) - 1:
                    raise BillingError(
                        ErrorReason.VALIDATION_FAILED,
                        "only the last tier may be unbounded",
                    )
            elif tier.up_to <= prev:
                raise BillingError(
                    ErrorReason.VALIDATION_FAILED, "tier bounds must be strictly increasing"
                )
            else:
                prev = tier.up_to
        if plan.tiers[-1].up_to is not None:
            raise BillingError(
                ErrorReason.VALIDATION_FAILED, "last tier must be unbounded (up_to=null)"
            )
    elif plan.pricing_model is PricingModel.OVERAGE:
        if plan.included_units < 0 or plan.overage_unit_price_cents < 0:
            raise BillingError(
                ErrorReason.VALIDATION_FAILED,
                "included_units and overage price must be >= 0",
            )


def compute_lines(plan: Plan, total_units: float) -> list[InvoiceLine]:
    """Compute invoice lines for `total_units` under the plan's pricing model."""
    if total_units < 0:
        raise BillingError(ErrorReason.VALIDATION_FAILED, "total_units must be >= 0")
    if plan.pricing_model is PricingModel.TIERED:
        return _tiered_lines(plan, total_units)
    return _overage_lines(plan, total_units)


def _tiered_lines(plan: Plan, total_units: float) -> list[InvoiceLine]:
    lines: list[InvoiceLine] = []
    lower = 0.0
    remaining = total_units
    for i, tier in enumerate(plan.tiers):
        if remaining <= 0:
            break
        if tier.up_to is None:
            qty = remaining
        else:
            qty = min(remaining, tier.up_to - lower)
            lower = tier.up_to
        if qty <= 0:
            continue
        amount = round(qty * tier.unit_price_cents)
        lines.append(
            InvoiceLine(
                description=f"tier {i + 1}",
                quantity=qty,
                unit_price_cents=tier.unit_price_cents,
                amount_cents=amount,
            )
        )
        remaining -= qty
    return lines


def _overage_lines(plan: Plan, total_units: float) -> list[InvoiceLine]:
    overage = max(0.0, total_units - plan.included_units)
    if overage <= 0:
        return []
    amount = round(overage * plan.overage_unit_price_cents)
    return [
        InvoiceLine(
            description="overage",
            quantity=overage,
            unit_price_cents=plan.overage_unit_price_cents,
            amount_cents=amount,
        )
    ]
