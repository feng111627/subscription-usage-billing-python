"""pytest 公共夹具与业务分组日志。

测试通过 @pytest.mark.business("业务名/子场景") 标注所属业务；
运行时终端与项目根目录 ``test-run.log`` 会输出每个业务覆盖了哪些
用例及其结果，例如::

    ■ 用量归集/周期边界（3 例）
        tests/test_periods.py::test_anchor_day_left_closed ...  ✓
"""
from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from billing.api import create_app
from billing.service import BillingService

LOG_PATH = Path(__file__).resolve().parent.parent / "test-run.log"


def pytest_configure(config):
    config.addinivalue_line("markers", "business(name): 用例所属业务/子场景")
    config.pluginmanager.register(BusinessLogPlugin(), "business-log")


@pytest.fixture
def clock():
    current = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)

    def get():
        return current

    def advance(seconds: int):
        nonlocal current
        current += timedelta(seconds=seconds)

    get.advance = advance
    return get


@pytest.fixture
def svc(clock) -> BillingService:
    return BillingService(clock=clock)


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(create_app(svc))


@pytest.fixture
def admin():
    return {"id": "admin-1", "role": "admin"}


@pytest.fixture
def customer():
    return {"id": "user-1", "role": "customer"}


@pytest.fixture
def other_customer():
    return {"id": "user-2", "role": "customer"}


@pytest.fixture
def admin_headers():
    return {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}


@pytest.fixture
def customer_headers():
    return {"X-Actor-Id": "user-1", "X-Actor-Role": "customer"}


@pytest.fixture
def other_headers():
    return {"X-Actor-Id": "user-2", "X-Actor-Role": "customer"}


@pytest.fixture
def make_plan_sub(svc, admin, customer):
    """创建计划+订阅，默认 overage：基础费10、含100单位、超额0.5。"""

    def _make(
        pricing_mode: str = "overage",
        base_fee: str = "10.00",
        included_units: int = 100,
        overage_price: str = "0.50",
        tiers=None,
        anchor_day: int = 1,
        started_at: str = "2026-09-01T00:00:00Z",
        plan_id: str = "P1",
        sub_id: str = "S1",
        customer_id: str = "user-1",
        create_plan: bool = True,
    ):
        if create_plan:
            svc.create_plan(
                {
                    "id": plan_id,
                    "name": "标准计划",
                    "pricing_mode": pricing_mode,
                    "base_fee": base_fee,
                    "included_units": included_units,
                    "overage_price": overage_price,
                    "tiers": tiers or [],
                },
                admin,
            )
        svc.create_subscription(
            {
                "id": sub_id,
                "plan_id": plan_id,
                "customer_id": customer_id,
                "anchor_day": anchor_day,
                "started_at": started_at,
            },
            admin if customer_id != customer["id"] else customer,
        )
        return svc, admin, customer

    return _make


class BusinessLogPlugin:
    """按 @business 标记分组，打印/落盘用例与结果。"""

    def __init__(self):
        self.groups: OrderedDict[str, list] = OrderedDict()
        self.items_by_nodeid: dict[str, pytest.Item] = {}
        self.t0 = 0.0

    def pytest_configure(self, config):
        self.t0 = time.perf_counter()

    def pytest_collection_modifyitems(self, items):
        self.items_by_nodeid = {item.nodeid: item for item in items}

    @staticmethod
    def _mark(report) -> str | None:
        if report.when == "call":
            if report.passed:
                return "✓"
            if report.failed:
                return "✗ 失败"
            if report.skipped:
                return "⊙ 跳过"
        if report.when == "setup" and report.outcome == "skipped":
            return "⊙ 跳过"
        return None

    def pytest_runtest_logreport(self, report):
        mark = self._mark(report)
        if mark is None:
            return
        item = self.items_by_nodeid.get(report.nodeid)
        group = "未分组"
        if item is not None:
            marker = item.get_closest_marker("business")
            if marker:
                group = marker.args[0]
        self.groups.setdefault(group, []).append((report.nodeid, mark))

    def pytest_sessionfinish(self, session, exitstatus):
        elapsed = time.perf_counter() - self.t0
        lines = ["", "================ 业务覆盖日志 ================"]
        total = passed = failed = skipped = 0
        for group, cases in self.groups.items():
            lines.append(f"■ {group}（{len(cases)} 例）")
            for nodeid, mark in cases:
                lines.append(f"    {nodeid}  {mark}")
                total += 1
                if "失败" in mark:
                    failed += 1
                elif "跳过" in mark:
                    skipped += 1
                else:
                    passed += 1
        lines.append("-" * 48)
        lines.append(
            f"合计 {total} 例：成功 {passed}，失败 {failed}，跳过 {skipped}，"
            f"耗时 {elapsed:.2f}s"
        )
        lines.append("=" * 48)
        text = "\n".join(lines)
        print(text)
        header = f"# 测试运行 {datetime.now(timezone.utc).isoformat()}\n"
        LOG_PATH.write_text(header + text + "\n", encoding="utf-8")


def pytest_plugin_registered(plugin, manager):
    pass
