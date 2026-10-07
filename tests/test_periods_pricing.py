"""周期边界与计价：归属规则、阶梯/超额计价、非法参数拒绝。"""
from __future__ import annotations

import pytest

from billing.errors import BillingError, ErrorReason
from billing.models import Plan, PricingModel, Tier
from billing.pricing import compute_lines, period_for
from tests.conftest import ANCHOR, day


class TestPeriodAttribution:
    def test_first_period_bounds(self, tiered_plan):
        start, end = period_for(tiered_plan, ANCHOR, day(0))
        assert (start, end) == (day(0), day(30))

    def test_event_on_boundary_belongs_to_new_period(self, tiered_plan):
        # 恰好落在边界的事件归入以该边界为起点的新周期（左闭右开）
        start, end = period_for(tiered_plan, ANCHOR, day(30))
        assert (start, end) == (day(30), day(60))

    def test_event_just_before_boundary_stays_in_old_period(self, tiered_plan):
        start, _ = period_for(tiered_plan, ANCHOR, day(29.999))
        assert start == day(0)

    def test_late_periods_resolve_deterministically(self, tiered_plan):
        start, end = period_for(tiered_plan, ANCHOR, day(95))
        assert (start, end) == (day(90), day(120))

    def test_usage_before_anchor_rejected(self, tiered_plan):
        with pytest.raises(BillingError) as exc:
            period_for(tiered_plan, ANCHOR, day(-1))
        assert exc.value.reason == ErrorReason.USAGE_OUT_OF_PERIOD

    def test_cross_period_usage_attributed_separately(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 10, day(1), "e-a")
        service.report_usage(sub.subscription_id, 20, day(31), "e-b")
        assert service.store.usage_by_period[(sub.subscription_id, day(0))] == 10
        assert service.store.usage_by_period[(sub.subscription_id, day(30))] == 20


class TestTieredPricing:
    def test_within_first_tier(self, tiered_plan):
        lines = compute_lines(tiered_plan, 80)
        assert [l.amount_cents for l in lines] == [800]

    def test_spans_tiers_graduated(self, tiered_plan):
        lines = compute_lines(tiered_plan, 250)
        assert [(l.quantity, l.amount_cents) for l in lines] == [(100, 1000), (150, 3000)]

    def test_zero_usage_no_lines(self, tiered_plan):
        assert compute_lines(tiered_plan, 0) == []

    def test_negative_usage_rejected(self, tiered_plan):
        with pytest.raises(BillingError) as exc:
            compute_lines(tiered_plan, -1)
        assert exc.value.reason == ErrorReason.VALIDATION_FAILED


class TestOveragePricing:
    def test_within_included_quota_free(self, overage_plan):
        assert compute_lines(overage_plan, 100) == []

    def test_overage_billed_flat(self, overage_plan):
        lines = compute_lines(overage_plan, 130)
        assert [(l.quantity, l.unit_price_cents, l.amount_cents) for l in lines] == [
            (30, 5, 150)
        ]


class TestInvalidPlanParams:
    @pytest.mark.parametrize(
        "plan",
        [
            Plan("x", "x", 0, PricingModel.OVERAGE, included_units=1, overage_unit_price_cents=1),
            Plan("x", "x", 30, PricingModel.TIERED, tiers=()),  # 缺阶梯
            Plan("x", "x", 30, PricingModel.TIERED, tiers=(Tier(100, 10),)),  # 末档有界
            Plan(
                "x", "x", 30, PricingModel.TIERED,
                tiers=(Tier(None, 10), Tier(100, 20)),  # 无界档不在末尾
            ),
            Plan(
                "x", "x", 30, PricingModel.TIERED,
                tiers=(Tier(100, 10), Tier(50, 20), Tier(None, 30)),  # 档位非递增
            ),
            Plan("x", "x", 30, PricingModel.OVERAGE, included_units=-1, overage_unit_price_cents=1),
            Plan("x", "x", 30, PricingModel.OVERAGE, included_units=0, overage_unit_price_cents=-5),
        ],
        ids=[
            "period_days非正",
            "阶梯缺失",
            "末档必须有界外无界",
            "无界档不在末尾",
            "档位非递增",
            "included_units为负",
            "超额单价为负",
        ],
    )
    def test_invalid_plan_rejected_with_validation_reason(self, service, plan):
        with pytest.raises(BillingError) as exc:
            service.create_plan(plan)
        assert exc.value.reason == ErrorReason.VALIDATION_FAILED

    def test_subscribe_unknown_plan_rejected(self, service):
        with pytest.raises(BillingError) as exc:
            service.subscribe("nope", "cust", ANCHOR)
        assert exc.value.reason == ErrorReason.PLAN_NOT_FOUND
