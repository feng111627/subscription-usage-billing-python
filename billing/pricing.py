"""计价：阶梯价 / 超额价。

两种定价模式（金额单位：元，全程 Decimal 精确计算）：

- ``overage`` 超额模式：包含量 included_units 以内不另收用量费，
  超出部分按 overage_price/单位 线性计价；基础费 base_fee 另计。
- ``tiered`` 阶梯模式：累计用量按阶梯切片，每片落在各自单价区间；
  阶梯用 (up_to, unit_price) 表示，up_to 为该阶梯包含的累计上限
  （None 表示无上限），最后一个阶梯必须无上限。

本模块只计算用量部分费用，不含基础费；账单总价由应用服务组装。
"""
from __future__ import annotations

from decimal import Decimal

from billing.models import Plan, PricingMode

ZERO = Decimal("0")


def _overage_charge(
    quantity: Decimal, included_units: int, overage_price: Decimal
) -> Decimal:
    extra = quantity - Decimal(included_units)
    if extra <= 0:
        return ZERO
    return extra * overage_price


def _tiered_charge(quantity: Decimal, tiers: tuple) -> Decimal:
    amount = ZERO
    remaining = quantity
    prev_bound = 0
    for tier in tiers:
        if remaining <= 0:
            break
        if tier.up_to is None:
            slice_qty = remaining
        else:
            capacity = Decimal(tier.up_to - prev_bound)
            slice_qty = min(remaining, capacity)
            prev_bound = tier.up_to
        amount += slice_qty * tier.unit_price
        remaining -= slice_qty
    return amount


def charge(plan: Plan, quantity: Decimal) -> Decimal:
    """按计划的计价模式对周期累计用量计价（不含基础费）。"""
    if quantity < 0:
        from billing.errors import BillingError, ErrorCode

        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"计价用量不能为负：{quantity}",
            status_code=422,
        )
    if plan.pricing_mode is PricingMode.OVERAGE:
        return _overage_charge(
            quantity, plan.included_units, plan.overage_price
        )
    return _tiered_charge(quantity, plan.tiers)
