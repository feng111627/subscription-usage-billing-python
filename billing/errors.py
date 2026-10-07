"""Domain errors with distinguishable reasons."""
from __future__ import annotations

from enum import Enum


class ErrorReason(str, Enum):
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    USAGE_OUT_OF_PERIOD = "USAGE_OUT_OF_PERIOD"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    FORBIDDEN = "FORBIDDEN"


class BillingError(Exception):
    """Domain error carrying a machine-readable reason."""

    def __init__(self, reason: ErrorReason, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason.value)
