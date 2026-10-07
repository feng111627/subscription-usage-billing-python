"""计价规则：超额线性、阶梯切片、边界与金额取整。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from billing.models import Plan, PricingMode, Tier
from billing.pricing import charge

B = "计价模型"


def overage(included=100, price="0.50", base="10.00"):
    return Plan(
        id="ov", name="超额", pricing_mode=PricingMode.OVERAGE,
        base_fee=Decimal(base), included_units=included,
        overage_price=Decimal(price), tiers=(),
    )


def tiered(tiers, base="0.00"):
    return Plan(
        id="ti", name="阶梯", pricing_mode=PricingMode.TIERED,
        base_fee=Decimal(base), included_units=0,
        overage_price=Decimal("0"),
        tiers=tuple(Tier(u, Decimal(p)) for u, p in tiers),
    )


@pytest.mark.business(f"{B}/超额-包含量内不收用量费")
@pytest.mark.parametrize("qty", ["0", "50", "100"])
def test_overage_within_included_free(qty):
    assert charge(overage(), Decimal(qty)) == Decimal("0")


@pytest.mark.business(f"{B}/超额-超出部分线性计价")
def test_overage_beyond_included_linear():
    assert charge(overage(), Decimal("120")) == Decimal("10.00")
    assert charge(overage(), Decimal("101")) == Decimal("0.50")
    # 小数用量精确，不提前取整
    assert charge(overage(price="0.33"), Decimal("101")) == Decimal("0.33")


@pytest.mark.business(f"{B}/阶梯-各区间切片计价")
def test_tiered_slice_pricing():
    plan = tiered([(100, "1.00"), (None, "0.50")])
    assert charge(plan, Decimal("50")) == Decimal("50.00")
    assert charge(plan, Decimal("100")) == Decimal("100.00")
    # 前100按1.0，后50按0.5
    assert charge(plan, Decimal("150")) == Decimal("125.00")


@pytest.mark.business(f"{B}/阶梯-三档与边界点")
def test_tiered_three_bands_boundaries():
    plan = tiered([(10, "2.00"), (100, "1.00"), (None, "0.10")])
    assert charge(plan, Decimal("10")) == Decimal("20.00")
    assert charge(plan, Decimal("11")) == Decimal("21.00")
    assert charge(plan, Decimal("100")) == Decimal("110.00")
    assert charge(plan, Decimal("200")) == Decimal("120.00")


@pytest.mark.business(f"{B}/阶梯-小数用量精确保真")
def test_tiered_fractional_quantity():
    plan = tiered([(1, "10.00"), (None, "1.00")])
    assert charge(plan, Decimal("1.5")) == Decimal("10.5")


@pytest.mark.business(f"{B}/非法-负用量拒绝")
def test_negative_quantity_rejected():
    with pytest.raises(Exception):
        charge(overage(), Decimal("-1"))
