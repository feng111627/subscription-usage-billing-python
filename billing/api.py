"""FastAPI 装配：路由、鉴权、统一错误响应、幂等键处理。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from billing.errors import BillingError, ErrorCode
from billing.service import BillingService


class TierIn(BaseModel):
    up_to: int | None = None
    unit_price: str


class PlanIn(BaseModel):
    id: str
    name: str
    pricing_mode: str
    base_fee: str = "0"
    included_units: int = 0
    overage_price: str = "0"
    tiers: list[TierIn] = []
    currency: str = "CNY"


class SubscriptionIn(BaseModel):
    id: str
    plan_id: str
    customer_id: str
    anchor_day: int = 1
    started_at: str | None = None


class UsageIn(BaseModel):
    event_id: str
    subscription_id: str
    quantity: str
    occurred_at: str | None = None


def _plan_dict(p) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "pricing_mode": p.pricing_mode.value,
        "base_fee": str(p.base_fee),
        "included_units": p.included_units,
        "overage_price": str(p.overage_price),
        "tiers": [
            {"up_to": t.up_to, "unit_price": str(t.unit_price)} for t in p.tiers
        ],
        "currency": p.currency,
    }


def _sub_dict(svc: BillingService, s) -> dict[str, Any]:
    return {
        "id": s.id,
        "plan_id": s.plan_id,
        "customer_id": s.customer_id,
        "anchor_day": s.anchor_day,
        "started_at": svc._iso(s.started_at),
        "canceled_at": svc._iso(s.canceled_at) if s.canceled_at else None,
    }


def create_app(service: BillingService | None = None) -> FastAPI:
    app = FastAPI(title="subscription-usage-billing")
    svc = service or BillingService()
    app.state.service = svc

    @app.exception_handler(BillingError)
    async def _billing_error_handler(_: Request, exc: BillingError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"field": ".".join(str(x) for x in e.get("loc", []) if x != "body"),
             "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.VALIDATION_ERROR,
                    "message": "请求体校验失败",
                    "details": details,
                }
            },
        )

    def _actor(x_actor_id: str | None, x_actor_role: str | None) -> dict[str, str]:
        if not x_actor_id:
            raise BillingError(
                ErrorCode.UNAUTHENTICATED, "缺少 X-Actor-Id", status_code=401
            )
        return {"id": x_actor_id, "role": x_actor_role or "customer"}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/plans", status_code=201)
    def create_plan(
        body: PlanIn,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        return svc.create_plan(body.model_dump(), _actor(x_actor_id, x_actor_role))

    @app.get("/plans")
    def list_plans(
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        _actor(x_actor_id, x_actor_role)
        return {"plans": [_plan_dict(p) for p in svc.repo.list_plans()]}

    @app.post("/subscriptions", status_code=201)
    def create_subscription(
        body: SubscriptionIn,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        return svc.create_subscription(
            body.model_dump(), _actor(x_actor_id, x_actor_role)
        )

    @app.get("/subscriptions")
    def list_subscriptions(
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        actor = _actor(x_actor_id, x_actor_role)
        subs = svc.repo.list_subscriptions()
        if actor.get("role") != "admin":
            subs = [s for s in subs if s.customer_id == actor["id"]]
        return {"subscriptions": [_sub_dict(svc, s) for s in subs]}

    @app.post("/subscriptions/{sub_id}/cancel")
    def cancel_subscription(
        sub_id: str,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        return svc.cancel_subscription(sub_id, _actor(x_actor_id, x_actor_role))

    @app.post("/usage", status_code=201)
    def report_usage(
        body: UsageIn,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        return svc.report_usage(
            body.model_dump(),
            _actor(x_actor_id, x_actor_role),
            idempotency_key=idempotency_key,
        )

    @app.get("/subscriptions/{sub_id}/aggregates/{period_key}")
    def get_aggregate(
        sub_id: str,
        period_key: str,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        return svc.get_aggregate(
            sub_id, period_key, _actor(x_actor_id, x_actor_role)
        )

    @app.post("/subscriptions/{sub_id}/bills/{period_key}/issue", status_code=201)
    def issue_bill(
        sub_id: str,
        period_key: str,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        return svc.issue_bill(
            sub_id,
            period_key,
            _actor(x_actor_id, x_actor_role),
            idempotency_key=idempotency_key,
        )

    def _bill_action(svc_method):
        def _handler(
            bill_id: str,
            x_actor_id: str | None = Header(None),
            x_actor_role: str | None = Header(None),
            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        ) -> dict:
            return svc_method(
                bill_id,
                _actor(x_actor_id, x_actor_role),
                idempotency_key=idempotency_key,
            )

        return _handler

    app.add_api_route(
        "/bills/{bill_id}/pay", _bill_action(svc.mark_paid), methods=["POST"]
    )
    app.add_api_route(
        "/bills/{bill_id}/void", _bill_action(svc.void_bill), methods=["POST"]
    )
    app.add_api_route(
        "/bills/{bill_id}/refund", _bill_action(svc.refund_bill), methods=["POST"]
    )

    @app.get("/bills/{bill_id}")
    def get_bill(
        bill_id: str,
        x_actor_id: str | None = Header(None),
        x_actor_role: str | None = Header(None),
    ) -> dict:
        return svc.get_bill(bill_id, _actor(x_actor_id, x_actor_role))

    return app


app = create_app()
