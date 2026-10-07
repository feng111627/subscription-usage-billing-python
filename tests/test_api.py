"""HTTP 端到端：鉴权头、统一错误结构、幂等键头、状态流转、并发请求。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


def _create_plan(client, h):
    return client.post(
        "/plans",
        json={
            "id": "P1", "name": "标准", "pricing_mode": "overage",
            "base_fee": "10.00", "included_units": 100, "overage_price": "0.50",
        },
        headers=h,
    )


def _create_sub(client, h):
    return client.post(
        "/subscriptions",
        json={
            "id": "S1", "plan_id": "P1", "customer_id": "user-1",
            "anchor_day": 1, "started_at": "2026-09-01T00:00:00Z",
        },
        headers=h,
    )


B = "HTTP接口"


@pytest.mark.business(f"{B}/健康检查")
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.business(f"{B}/未认证拒绝且结构统一")
def test_unauthenticated(client):
    r = client.get("/plans")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.business(f"{B}/完整链路-计划订阅上报出账支付退款")
def test_full_happy_path_http(client, admin_headers, customer_headers):
    assert _create_plan(client, admin_headers).status_code == 201
    assert _create_sub(client, customer_headers).status_code == 201
    r = client.post(
        "/usage",
        json={"event_id": "e1", "subscription_id": "S1", "quantity": "120",
              "occurred_at": "2026-09-10T00:00:00Z"},
        headers=customer_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["period_key"] == "2026-09-01"

    r = client.post("/subscriptions/S1/bills/2026-09-01/issue",
                    headers=admin_headers)
    assert r.status_code == 201, r.text
    bill = r.json()
    assert bill["total_amount"] == "20.00"

    bid = bill["id"]
    assert client.post(f"/bills/{bid}/pay", headers=admin_headers).status_code == 200
    r = client.post(f"/bills/{bid}/refund", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "refunded"
    g = client.get(f"/bills/{bid}", headers=customer_headers)
    assert g.status_code == 200
    assert g.json()["total_amount"] == "20.00"


@pytest.mark.business(f"{B}/HTTP幂等键-重放不重复计费")
def test_idempotency_key_header(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    headers = dict(customer_headers, **{"Idempotency-Key": "report-once"})
    payload = {"event_id": "e1", "subscription_id": "S1", "quantity": "10",
               "occurred_at": "2026-09-10T00:00:00Z"}
    r1 = client.post("/usage", json=payload, headers=headers)
    r2 = client.post("/usage", json=payload, headers=headers)
    assert r1.status_code == r2.status_code == 201
    agg = client.get(
        "/subscriptions/S1/aggregates/2026-09-01", headers=customer_headers
    ).json()
    assert agg["total_quantity"] == "10"
    assert agg["event_count"] == 1
    # 同键不同体 -> 409 冲突
    conflict = client.post(
        "/usage", json=dict(payload, quantity="11"), headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.business(f"{B}/越权-客户不能出账")
def test_forbidden_issue_http(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    r = client.post("/subscriptions/S1/bills/2026-09-01/issue",
                    headers=customer_headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.business(f"{B}/越权-不能访问他人账单")
def test_forbidden_other_bill_http(
    client, admin_headers, customer_headers, other_headers
):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    bill = client.post(
        "/subscriptions/S1/bills/2026-09-01/issue", headers=admin_headers
    ).json()
    r = client.get(f"/bills/{bill['id']}", headers=other_headers)
    assert r.status_code == 403


@pytest.mark.business(f"{B}/非法参数-422统一错误结构")
def test_validation_error_shape(client, admin_headers):
    r = client.post(
        "/plans", json={"id": "P", "pricing_mode": "overage"}, headers=admin_headers
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "details" in body["error"]


@pytest.mark.business(f"{B}/业务错误-数量非法给出可区分原因")
def test_invalid_quantity_http(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    r = client.post(
        "/usage",
        json={"event_id": "e1", "subscription_id": "S1", "quantity": "0",
              "occurred_at": "2026-09-10T00:00:00Z"},
        headers=customer_headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


@pytest.mark.business(f"{B}/非法状态流转-HTTP 409")
def test_invalid_transition_http(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    bill = client.post(
        "/subscriptions/S1/bills/2026-09-01/issue", headers=admin_headers
    ).json()
    # 未支付直接退款
    r = client.post(f"/bills/{bill['id']}/refund", headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INVALID_BILL_TRANSITION"


@pytest.mark.business(f"{B}/并发-HTTP 重复出账只出一张")
def test_concurrent_issue_http(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    client.post(
        "/usage",
        json={"event_id": "e1", "subscription_id": "S1", "quantity": "120",
              "occurred_at": "2026-09-10T00:00:00Z"},
        headers=customer_headers,
    )

    def issue(_):
        return client.post(
            "/subscriptions/S1/bills/2026-09-01/issue", headers=admin_headers
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(issue, range(10)))
    assert all(r.status_code in (201, 200) for r in responses)
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1
    assert all(r.json()["total_amount"] == "20.00" for r in responses)


@pytest.mark.business(f"{B}/并发-HTTP 相同事件并发上报只计一次")
def test_concurrent_usage_http(client, admin_headers, customer_headers):
    _create_plan(client, admin_headers)
    _create_sub(client, customer_headers)
    payload = {"event_id": "dup-1", "subscription_id": "S1", "quantity": "7",
               "occurred_at": "2026-09-10T00:00:00Z"}

    def post(_):
        return client.post("/usage", json=payload, headers=customer_headers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(10)))
    assert all(r.status_code == 201 for r in responses)
    agg = client.get(
        "/subscriptions/S1/aggregates/2026-09-01", headers=customer_headers
    ).json()
    assert agg["total_quantity"] == "7"
    assert agg["event_count"] == 1
