"""Helpers shared by the HTTP backends."""
from __future__ import annotations

import base64
import html
import json
import re
from typing import Any, Dict, Mapping, Optional

from ..envelope import INVALID_INPUT, UNAVAILABLE, BrokerError
from ..operations import CURSOR_RE
from ..transport import Request, json_body, status_error

USER_AGENT = "quoin-workctl/1"


def bearer(ctx: Any) -> Dict[str, str]:
    return {"Authorization": "Bearer %s" % ctx.credential.reveal()}


def get_json(ctx: Any, url: str, headers: Optional[Mapping[str, str]] = None) -> Any:
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    response = ctx.send(Request("GET", url, request_headers))
    error = status_error(response)
    if error is not None:
        raise error
    return json_body(response)


def encode_cursor(backend: str, value: Any) -> Optional[str]:
    """Wrap a service paging token so it passes the cursor grammar and can
    only be replayed against the backend that issued it."""
    if value in (None, ""):
        return None
    raw = json.dumps({"b": backend, "v": value}, separators=(",", ":")).encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not CURSOR_RE.fullmatch(cursor):
        raise BrokerError(
            UNAVAILABLE,
            "the service returned a paging token too long to hand out; narrow the query",
            retryable=False,
        )
    return cursor


def decode_cursor(backend: str, cursor: Optional[str]) -> Any:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
    if not isinstance(obj, dict) or obj.get("b") != backend or "v" not in obj:
        raise BrokerError(INVALID_INPUT, "cursor is not valid for this service")
    return obj["v"]


_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(text: str) -> str:
    """A plain-text rendering of HTML for context, not for display: scripts
    and styles are dropped, tags removed and entities decoded."""
    out = _SCRIPT_RE.sub("", text)
    out = _BREAK_RE.sub("\n", out)
    out = _TAG_RE.sub("", out)
    out = html.unescape(out)
    out = _SPACE_RE.sub(" ", out)
    return _LINES_RE.sub("\n\n", "\n".join(line.strip() for line in out.split("\n"))).strip()


def text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
