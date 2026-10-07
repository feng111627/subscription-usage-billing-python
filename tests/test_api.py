"""HTTP API 端到端：建计划→订阅→上报→出账→支付/作废/退款，错误原因可区分。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from billing.service import BillingService
from main import app
from tests.conftest import ANCHOR


@pytest.fixture()
def client(monkeypatch):
    import main

    svc = BillingService()
    monkeypatch.setattr(main, "service", svc)
    return TestClient(app)


def _setup(client: TestClient) -> str:
    r = client.post("/plans", json={
        "plan_id": "tiered", "name": "t", "period_days": 30, "pricing_model": "TIERED",
        "tiers": [{"up_to": 100, "unit_price_cents": 10}, {"up_to": None, "unit_price_cents": 20}],
    })
    assert r.status_code == 201, r.text
    r = client.post("/subscriptions", json={
        "plan_id": "tiered", "customer_id": "c1", "anchor": ANCHOR.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["subscription_id"]


class TestHappyPath:
    def test_full_billing_cycle(self, client):
        sub_id = _setup(client)
        r = client.post("/usage", json={
            "subscription_id": sub_id, "quantity": 150,
            "occurred_at": "2026-01-05T00:00:00Z", "event_id": "e1",
        })
        assert r.status_code == 201, r.text

        r = client.post("/periods/close", json={"subscription_id": sub_id, "idempotency_key": "k1"})
        assert r.status_code == 201, r.text
        invoice = r.json()
        assert invoice["total_cents"] == 100 * 10 + 50 * 20
        assert invoice["status"] == "ISSUED"

        # 幂等重放出账
        r = client.post("/periods/close", json={"subscription_id": sub_id, "idempotency_key": "k1"})
        assert r.json()["invoice_id"] == invoice["invoice_id"]

        r = client.post(f"/invoices/{invoice['invoice_id']}/pay", json={"actor": "customer"})
        assert r.json()["status"] == "PAID"
        r = client.post(f"/invoices/{invoice['invoice_id']}/refund", json={"actor": "admin"})
        assert r.json()["status"] == "REFUNDED"

    def test_usage_replay_via_api(self, client):
        sub_id = _setup(client)
        body = {"subscription_id": sub_id, "quantity": 5,
                "occurred_at": "2026-01-02T00:00:00Z", "event_id": "e1"}
        assert client.post("/usage", json=body).status_code == 201
        assert client.post("/usage", json=body).status_code == 201  # 重放不重复计


class TestErrorReasons:
    def test_unknown_plan_404(self, client):
        r = client.post("/subscriptions", json={
            "plan_id": "nope", "customer_id": "c", "anchor": ANCHOR.isoformat()})
        assert r.status_code == 404 and r.json()["error"] == "PLAN_NOT_FOUND"

    def test_invalid_plan_422(self, client):
        r = client.post("/plans", json={
            "plan_id": "bad", "name": "b", "period_days": 30, "pricing_model": "TIERED",
            "tiers": []})
        assert r.status_code == 422 and r.json()["error"] == "VALIDATION_FAILED"

    def test_conflicting_event_409(self, client):
        sub_id = _setup(client)
        body = {"subscription_id": sub_id, "quantity": 5,
                "occurred_at": "2026-01-02T00:00:00Z", "event_id": "e1"}
        client.post("/usage", json=body)
        body["quantity"] = 9
        r = client.post("/usage", json=body)
        assert r.status_code == 409 and r.json()["error"] == "IDEMPOTENCY_CONFLICT"

    def test_illegal_transition_409(self, client):
        sub_id = _setup(client)
        inv = client.post("/periods/close",
                          json={"subscription_id": sub_id, "idempotency_key": "k"}).json()
        r = client.post(f"/invoices/{inv['invoice_id']}/refund", json={"actor": "admin"})
        assert r.status_code == 409 and r.json()["error"] == "ILLEGAL_STATE_TRANSITION"

    def test_forbidden_void_403(self, client):
        sub_id = _setup(client)
        inv = client.post("/periods/close",
                          json={"subscription_id": sub_id, "idempotency_key": "k"}).json()
        r = client.post(f"/invoices/{inv['invoice_id']}/void", json={"actor": "customer"})
        assert r.status_code == 403 and r.json()["error"] == "FORBIDDEN"

    def test_late_usage_422(self, client):
        sub_id = _setup(client)
        client.post("/periods/close", json={"subscription_id": sub_id, "idempotency_key": "k"})
        r = client.post("/usage", json={
            "subscription_id": sub_id, "quantity": 1,
            "occurred_at": "2026-01-02T00:00:00Z", "event_id": "late"})
        assert r.status_code == 422 and r.json()["error"] == "USAGE_OUT_OF_PERIOD"
