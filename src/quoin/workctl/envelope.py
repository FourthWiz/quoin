"""The typed JSON envelope and the closed set of error codes."""
from __future__ import annotations

from typing import Any, Dict, Optional

SCHEMA_VERSION = 1

AUTH_REQUIRED = "AUTH_REQUIRED"
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
RATE_LIMITED = "RATE_LIMITED"
INVALID_INPUT = "INVALID_INPUT"
POLICY_DENIED = "POLICY_DENIED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
# Reads add one code the write-oriented list lacks: the service could not be
# reached or answered with a server error, so nothing is known to be wrong
# with the request itself.
UNAVAILABLE = "UNAVAILABLE"

ERROR_CODES = (
    AUTH_REQUIRED,
    FORBIDDEN,
    NOT_FOUND,
    RATE_LIMITED,
    INVALID_INPUT,
    POLICY_DENIED,
    OUTCOME_UNKNOWN,
    UNAVAILABLE,
)

_DEFAULT_RETRYABLE = {
    AUTH_REQUIRED: False,
    FORBIDDEN: False,
    NOT_FOUND: False,
    RATE_LIMITED: True,
    INVALID_INPUT: False,
    POLICY_DENIED: False,
    OUTCOME_UNKNOWN: False,
    UNAVAILABLE: True,
}

ENVELOPE_KEYS = ("schema_version", "ok", "data", "error", "pagination", "provenance", "request_id")


class BrokerError(Exception):
    """A failure with a stable code and a message that is safe to show: it
    never contains a credential, a response body or retrieved content."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError("unknown error code")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = _DEFAULT_RETRYABLE[code] if retryable is None else bool(retryable)
        self.retry_after = retry_after

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


def success(
    data: Any,
    *,
    pagination: Optional[Dict[str, Any]],
    provenance: Optional[Dict[str, Any]],
    request_id: str,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "data": data,
        "error": None,
        "pagination": pagination,
        "provenance": provenance,
        "request_id": request_id,
    }


def failure(
    error: BrokerError, *, request_id: str, provenance: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "data": None,
        "error": error.to_json(),
        "pagination": None,
        "provenance": provenance,
        "request_id": request_id,
    }
