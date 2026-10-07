"""并发一致性：并发上报不丢不重，并发出账金额唯一确定。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from tests.conftest import day

THREADS = 16


class TestConcurrentUsage:
    def test_concurrent_distinct_events_all_counted(self, subscribed):
        service, sub = subscribed
        n = THREADS * 20

        def report(i: int) -> None:
            service.report_usage(sub.subscription_id, 1, day(1), f"evt-{i}")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            list(pool.map(report, range(n)))

        assert len(service.store.usage_events) == n
        assert service.store.usage_by_period[(sub.subscription_id, day(0))] == n

    def test_concurrent_replays_of_same_event_count_once(self, subscribed):
        service, sub = subscribed

        def report(_: int) -> None:
            service.report_usage(sub.subscription_id, 7, day(1), "evt-shared")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            list(pool.map(report, range(THREADS * 10)))

        assert len(service.store.usage_events) == 1
        assert service.store.usage_by_period[(sub.subscription_id, day(0))] == 7

    def test_concurrent_usage_and_close_is_consistent(self, subscribed):
        """上报与出账并发：每笔事件要么计入本期账单，要么计入下一周期，总量守恒。"""
        service, sub = subscribed
        n = THREADS * 10

        def report(i: int) -> None:
            # 一半事件发生在新周期，保证出账后仍有可接受的事件
            at = day(1) if i % 2 == 0 else day(31)
            try:
                service.report_usage(sub.subscription_id, 1, at, f"evt-{i}")
            except Exception:
                pass  # 落在已关闭周期的少量事件可能被拒，属合法竞争结果

        def close(_: int) -> None:
            service.close_period(sub.subscription_id, "close-key")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(report, i) for i in range(n)]
            futures.append(pool.submit(close, 0))
            for f in futures:
                f.result()

        recorded = len(service.store.usage_events)
        billed_p1 = service.store.usage_by_period.get((sub.subscription_id, day(0)), 0)
        billed_p2 = service.store.usage_by_period.get((sub.subscription_id, day(30)), 0)
        # 所有被接受的事件都必须归集到某个周期，无丢失
        assert billed_p1 + billed_p2 == recorded


class TestConcurrentClose:
    def test_concurrent_close_same_key_single_invoice(self, subscribed):
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 250, day(1), "evt-1")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            invoices = list(pool.map(lambda _: service.close_period(sub.subscription_id, "k"), range(THREADS * 4)))

        assert {inv.invoice_id for inv in invoices} == {invoices[0].invoice_id}
        assert len(service.store.invoices) == 1
        assert invoices[0].total_cents == 100 * 10 + 150 * 20

    def test_concurrent_close_mixed_keys_amounts_consistent(self, subscribed):
        """不同幂等键并发出账：每个周期至多一张账单，金额与归集用量一致。"""
        service, sub = subscribed
        service.report_usage(sub.subscription_id, 250, day(1), "evt-1")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            invoices = list(
                pool.map(lambda i: service.close_period(sub.subscription_id, f"k-{i}"), range(THREADS * 2))
            )

        period1_invoices = [inv for inv in invoices if inv.period_start == day(0)]
        assert {inv.invoice_id for inv in period1_invoices} == {
            service.store.invoice_by_period[(sub.subscription_id, day(0))]
        }
        # 每个周期恰好一张账单
        assert len(service.store.invoice_by_period) == len(service.store.invoices)
        p1_id = service.store.invoice_by_period[(sub.subscription_id, day(0))]
        assert service.store.invoices[p1_id].total_cents == 100 * 10 + 150 * 20
