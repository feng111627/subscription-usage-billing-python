"""计费周期计算：锚定日、周期键、用量归属。

周期为自然月周期：以订阅的 anchor_day（1..28）为每月起点，
区间为左闭右开 [start, end)。周期键统一使用周期起始日 ``YYYY-MM-DD``
（天然 URL 安全、无歧义，例如锚定日 15 的 2026-09 周期键为 ``2026-09-15``）。

归属规则（稳定且确定）：
- 用量按 ``occurred_at`` 落入的周期归属，与上报时间无关；
- 周期起点当天归入新周期（左闭）；
- 周期终点当天归入下一周期（右开）。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from datetime import datetime, timezone

UTC = timezone.utc
MIN_ANCHOR_DAY = 1
MAX_ANCHOR_DAY = 28  # 限制 28，避免短月锚定日漂移导致归属不稳定


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime
    end: datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """把时间统一为带时区的 UTC；朴素时间视为 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def validate_anchor_day(anchor_day: int) -> None:
    from billing.errors import BillingError, ErrorCode

    if not isinstance(anchor_day, int) or isinstance(anchor_day, bool):
        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"anchor_day 必须为整数，收到 {anchor_day!r}",
            status_code=422,
        )
    if not (MIN_ANCHOR_DAY <= anchor_day <= MAX_ANCHOR_DAY):
        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"anchor_day 必须在 [{MIN_ANCHOR_DAY}, {MAX_ANCHOR_DAY}] 之间，"
            f"收到 {anchor_day}",
            status_code=422,
        )


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _make_period(year: int, month: int, anchor_day: int) -> Period:
    start = datetime(year, month, anchor_day, tzinfo=UTC)
    ny, nm = _shift_month(year, month, 1)
    end = datetime(ny, nm, anchor_day, tzinfo=UTC)
    key = start.strftime("%Y-%m-%d")
    return Period(key=key, start=start, end=end)


def period_for(anchor_day: int, at: datetime) -> Period:
    """返回某时刻所属的计费周期（含起点，不含终点）。"""
    validate_anchor_day(anchor_day)
    at = ensure_utc(at)
    if at.day >= anchor_day:
        return _make_period(at.year, at.month, anchor_day)
    py, pm = _shift_month(at.year, at.month, -1)
    return _make_period(py, pm, anchor_day)


def period_key(anchor_day: int, at: datetime) -> str:
    """返回时刻所属周期的周期键。"""
    return period_for(anchor_day, at).key


def period_from_key(key: str) -> Period:
    """按周期键（周期起始日 YYYY-MM-DD）反解周期；键非法时抛出 INVALID_PARAMETER。"""
    from billing.errors import BillingError, ErrorCode

    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key or ""):
            raise ValueError
        start = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=UTC)
        validate_anchor_day(start.day)
    except (ValueError, BillingError):
        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            f"非法周期键 {key!r}，应为周期起始日 YYYY-MM-DD（日为 1..28）",
            status_code=422,
        )
    return _make_period(start.year, start.month, start.day)


def next_period(period: Period) -> Period:
    """返回紧邻的下一周期。"""
    start = period.end
    anchor_day = start.day
    return _make_period(start.year, start.month, anchor_day)


def periods_between(anchor_day: int, start: datetime, end: datetime) -> list[Period]:
    """枚举 ``[start, end)`` 覆盖到的全部周期（按时间升序）。"""
    start = ensure_utc(start)
    end = ensure_utc(end)
    if end < start:
        from billing.errors import BillingError, ErrorCode

        raise BillingError(
            ErrorCode.INVALID_PARAMETER,
            "periods_between 的 end 早于 start",
            status_code=422,
        )
    if end == start:
        return []
    first = period_for(anchor_day, start)
    result = [first]
    current = first
    while current.end < end:
        current = next_period(current)
        result.append(current)
    return result
