"""Shared fixtures + coverage logging.

Each test module maps to a business area; the hooks below log, for every
case, which business flow it covers and its result, e.g.:

    [COVERAGE] 幂等与重放 :: test_usage_replay_does_not_double_count
    [RESULT]   PASSED 幂等与重放 :: test_usage_replay_does_not_double_count
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

import pytest

from billing.models import Plan, PricingModel, Tier
from billing.service import BillingService

logger = logging.getLogger("billing.tests")

BUSINESS_AREAS = {
    "test_periods_pricing": "周期边界与计价",
    "test_idempotency": "幂等与重放",
    "test_concurrency": "并发一致性",
    "test_invoice_lifecycle": "账单状态流转/作废/退款",
    "test_api": "HTTP API 端到端",
}

ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _area(item: pytest.Item) -> str:
    module = item.module.__name__.rsplit(".", 1)[-1]
    return BUSINESS_AREAS.get(module, module)


def pytest_configure(config: pytest.Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def pytest_runtest_logstart(nodeid: str, location) -> None:
    logger.info("[COVERAGE] %s", nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        logger.info("[RESULT]   %s %s", report.outcome.upper(), report.nodeid)


@pytest.fixture()
def service() -> BillingService:
    return BillingService()


@pytest.fixture()
def tiered_plan(service: BillingService) -> Plan:
    plan = Plan(
        plan_id="tiered",
        name="Tiered 30d",
        period_days=30,
        pricing_model=PricingModel.TIERED,
        tiers=(Tier(up_to=100, unit_price_cents=10), Tier(up_to=None, unit_price_cents=20)),
    )
    return service.create_plan(plan)


@pytest.fixture()
def overage_plan(service: BillingService) -> Plan:
    plan = Plan(
        plan_id="overage",
        name="Overage 30d",
        period_days=30,
        pricing_model=PricingModel.OVERAGE,
        included_units=100,
        overage_unit_price_cents=5,
    )
    return service.create_plan(plan)


@pytest.fixture()
def subscribed(service: BillingService, tiered_plan: Plan):
    sub = service.subscribe(tiered_plan.plan_id, "cust-1", ANCHOR)
    return service, sub


def day(n: float) -> datetime:
    """ANCHOR + n days, handy for period-boundary cases."""
    return ANCHOR + timedelta(days=n)
