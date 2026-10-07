"""HTTP API for the subscription usage billing service."""
from __future__ import annotations

from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing.errors import BillingError, ErrorReason
from billing.models import Invoice, Plan, PricingModel, Subscription, Tier, UsageEvent
from billing.service import BillingService

app = FastAPI(title="Subscription Usage Billing")
service = BillingService()

_STATUS = {
    ErrorReason.PLAN_NOT_FOUND: 404,
    ErrorReason.SUBSCRIPTION_NOT_FOUND: 404,
    ErrorReason.INVOICE_NOT_FOUND: 404,
    ErrorReason.VALIDATION_FAILED: 422,
    ErrorReason.USAGE_OUT_OF_PERIOD: 422,
    ErrorReason.DUPLICATE_EVENT: 200,
    ErrorReason.IDEMPOTENCY_CONFLICT: 409,
    ErrorReason.ILLEGAL_STATE_TRANSITION: 409,
    ErrorReason.FORBIDDEN: 403,
}


@app.exception_handler(BillingError)
async def billing_error_handler(_request: Request, exc: BillingError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS[exc.reason],
        content={"error": exc.reason.value, "detail": str(exc)},
    )


class TierIn(BaseModel):
    up_to: float | None = None
    unit_price_cents: int = Field(ge=0)


class PlanIn(BaseModel):
    plan_id: str
    name: str
    period_days: int = Field(gt=0)
    pricing_model: PricingModel
    tiers: list[TierIn] = []
    included_units: float = 0.0
    overage_unit_price_cents: int = 0


class SubscribeIn(BaseModel):
    plan_id: str
    customer_id: str
    anchor: datetime


class UsageIn(BaseModel):
    subscription_id: str
    quantity: float
    occurred_at: datetime
    event_id: str


class ClosePeriodIn(BaseModel):
    subscription_id: str
    idempotency_key: str


class ActorIn(BaseModel):
    actor: str = "customer"


@app.post("/plans", status_code=201)
def create_plan(body: PlanIn) -> Plan:
    plan = Plan(
        plan_id=body.plan_id,
        name=body.name,
        period_days=body.period_days,
        pricing_model=body.pricing_model,
        tiers=tuple(Tier(up_to=t.up_to, unit_price_cents=t.unit_price_cents) for t in body.tiers),
        included_units=body.included_units,
        overage_unit_price_cents=body.overage_unit_price_cents,
    )
    return service.create_plan(plan)


@app.post("/subscriptions", status_code=201)
def subscribe(body: SubscribeIn) -> Subscription:
    return service.subscribe(body.plan_id, body.customer_id, body.anchor)


@app.post("/usage", status_code=201)
def report_usage(body: UsageIn) -> UsageEvent:
    return service.report_usage(body.subscription_id, body.quantity, body.occurred_at, body.event_id)


@app.post("/periods/close", status_code=201)
def close_period(body: ClosePeriodIn) -> Invoice:
    return service.close_period(body.subscription_id, body.idempotency_key)


@app.post("/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, body: ActorIn) -> Invoice:
    return service.pay_invoice(invoice_id, actor=body.actor)


@app.post("/invoices/{invoice_id}/void")
def void_invoice(invoice_id: str, body: ActorIn) -> Invoice:
    return service.void_invoice(invoice_id, actor=body.actor)


@app.post("/invoices/{invoice_id}/refund")
def refund_invoice(invoice_id: str, body: ActorIn) -> Invoice:
    return service.refund_invoice(invoice_id, actor=body.actor)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> Invoice:
    inv = service.store.invoices.get(invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail=ErrorReason.INVOICE_NOT_FOUND.value)
    return inv


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
