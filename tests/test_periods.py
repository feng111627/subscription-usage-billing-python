"""周期边界与跨周期归属：锚定日、左闭右开、跨年、非法参数。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billing.errors import BillingError
from billing.periods import (
    next_period,
    period_for,
    period_from_key,
    period_key,
    periods_between,
)

UTC = timezone.utc
B = "计费周期/周期边界与归属"


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


@pytest.mark.business(f"{B}/锚定日1号-自然月左闭右开")
def test_anchor_day_1_natural_month_boundaries():
    # 周期起点当天 00:00 归入新周期（左闭）
    assert period_for(1, dt(2026, 10, 1, 0, 0)).key == "2026-10-01"
    # 前一刻归上一周期
    assert period_for(1, dt(2026, 9, 30, 23, 59)).key == "2026-09-01"
    # 周期终点当天 00:00 归入下一周期（右开）
    assert period_for(1, dt(2026, 11, 1, 0, 0)).key == "2026-11-01"
    p = period_for(1, dt(2026, 10, 15))
    assert (p.start, p.end) == (dt(2026, 10, 1), dt(2026, 11, 1))


@pytest.mark.business(f"{B}/锚定日15-跨月归属稳定")
def test_anchor_day_15_cross_month_attribution():
    assert period_for(15, dt(2026, 9, 14, 23, 59)).key == "2026-08-15"
    assert period_for(15, dt(2026, 9, 15, 0, 0)).key == "2026-09-15"
    assert period_for(15, dt(2026, 10, 14, 23, 59)).key == "2026-09-15"
    assert period_for(15, dt(2026, 10, 15, 0, 0)).key == "2026-10-15"


@pytest.mark.business(f"{B}/跨年周期")
def test_year_boundary_period():
    p = period_for(15, dt(2026, 12, 20))
    assert p.key == "2026-12-15"
    assert p.start == dt(2026, 12, 15)
    assert p.end == dt(2027, 1, 15)
    assert period_for(1, dt(2027, 1, 1)).key == "2027-01-01"


@pytest.mark.business(f"{B}/朴素时间按UTC处理")
def test_naive_datetime_treated_as_utc():
    assert period_for(1, datetime(2026, 10, 1)) == period_for(
        1, dt(2026, 10, 1)
    )


@pytest.mark.business(f"{B}/非UTC时间换算后归属")
def test_offset_datetime_converted():
    # 北京时间 10-01 07:00 == UTC 09-30 23:00，应归 9 月
    tz8 = timezone(__import__("datetime").timedelta(hours=8))
    assert period_for(1, datetime(2026, 10, 1, 7, 0, tzinfo=tz8)).key == "2026-09-01"


@pytest.mark.business(f"{B}/周期键与反解一致")
def test_period_key_roundtrip():
    for anchor, at in ((1, dt(2026, 10, 2)), (15, dt(2026, 10, 2)), (28, dt(2026, 2, 27))):
        p = period_for(anchor, at)
        assert period_key(anchor, at) == p.key
        back = period_from_key(p.key)
        assert back == p


@pytest.mark.business(f"{B}/区间枚举覆盖的周期")
def test_periods_between_enumeration():
    ps = periods_between(15, dt(2026, 9, 20), dt(2026, 11, 10))
    assert [x.key for x in ps] == ["2026-09-15", "2026-10-15"]
    assert periods_between(1, dt(2026, 10, 1), dt(2026, 10, 1)) == []
    ps = periods_between(1, dt(2026, 10, 31), dt(2026, 11, 1))
    assert [x.key for x in ps] == ["2026-10-01"]


@pytest.mark.business(f"{B}/非法锚定日拒绝")
@pytest.mark.parametrize("bad", [0, 29, 31, -1])
def test_invalid_anchor_day_rejected(bad):
    with pytest.raises(BillingError) as ei:
        period_for(bad, dt(2026, 10, 1))
    assert ei.value.code == "INVALID_PARAMETER"


@pytest.mark.business(f"{B}/非法周期键拒绝")
@pytest.mark.parametrize("bad", ["2026-10", "2026-13-01", "abcd", "2026-10-29", ""])
def test_invalid_period_key_rejected(bad):
    with pytest.raises(BillingError) as ei:
        period_from_key(bad)
    assert ei.value.code == "INVALID_PARAMETER"


@pytest.mark.business(f"{B}/next_period 连续性")
def test_next_period_is_contiguous():
    p = period_for(15, dt(2026, 9, 20))
    n = next_period(p)
    assert n.start == p.end
    assert n.key == "2026-10-15"
