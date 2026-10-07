"""Pricing: period resolution and charge computation."""
from __future__ import annotations

from datetime import datetime

from .models import InvoiceLine, Plan


def period_for(plan: Plan, anchor: datetime, at: datetime) -> tuple[datetime, datetime]:
    """Return the [start, end) billing period containing `at`.

    Boundaries are derived from the subscription anchor so that usage is
    attributed to a stable, deterministic period.
    """
    raise NotImplementedError


def compute_lines(plan: Plan, total_units: float) -> list[InvoiceLine]:
    """Compute invoice lines for `total_units` under the plan's pricing model."""
    raise NotImplementedError
