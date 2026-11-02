"""HTTP transport with hard bounds, and a replay transport for recorded fixtures.

Requests carry a timeout, responses are read up to a byte cap, and
redirects are refused so a credential header is never forwarded to another
host. Status codes map onto the envelope's closed error codes; a response
body is never copied into an error message.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from .envelope import (
    AUTH_REQUIRED,
    FORBIDDEN,
    INVALID_INPUT,
    NOT_FOUND,
    RATE_LIMITED,
    UNAVAILABLE,
    BrokerError,
)

MAX_RETRY_AFTER = 3600.0

# Variables that would let a caller pick the proxy or the CA trust for the
# broker's credentialed requests. Dropped from the process environment
# before any request (OpenSSL reads SSL_CERT_* when a context loads the
# default trust store); the broker configuration's `network` block is the
# only source for these settings.
SCRUBBED_ENV = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def scrub_environment(environ: Any = None) -> None:
    environ = os.environ if environ is None else environ
    for name in SCRUBBED_ENV:
        environ.pop(name, None)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _read_capped(handle: Any, max_bytes: int) -> bytes:
    raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise BrokerError(
            UNAVAILABLE,
            "the service response exceeded the broker's size limit; narrow the query or lower --limit",
            retryable=False,
        )
    return raw


class HttpTransport:
    """Proxy and CA trust come only from the broker configuration: the
    proxy handler is built explicitly, so `*_proxy` variables in the
    caller's environment are ignored, and `broker.execute` drops `SSL_CERT_*`
    before any TLS context exists (see `scrub_environment`)."""

    def __init__(self, *, https_proxy: Optional[str] = None, ca_file: Optional[str] = None) -> None:
        context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.proxies = {"https": https_proxy} if https_proxy else {}
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(self.proxies),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def send(self, request: Request, *, timeout: float, max_bytes: int) -> Response:
        req = urllib.request.Request(
            request.url, data=request.body, headers=dict(request.headers), method=request.method
        )
        try:
            with self._opener.open(req, timeout=timeout) as handle:
                body = _read_capped(handle, max_bytes)
                return Response(handle.status, _lower(handle.headers.items()), body)
        except urllib.error.HTTPError as exc:
            try:
                body = _read_capped(exc, min(max_bytes, 65_536))
            except (BrokerError, OSError):
                body = b""
            return Response(exc.code, _lower(exc.headers.items() if exc.headers else []), body)
        except (socket.timeout, TimeoutError):
            raise BrokerError(UNAVAILABLE, "the service did not answer within the timeout")
        except (urllib.error.URLError, OSError):
            raise BrokerError(UNAVAILABLE, "the service could not be reached")


def _lower(items: Any) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in items}


def retry_after(headers: Mapping[str, str]) -> Optional[float]:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            seconds = (when - _dt.datetime.now(when.tzinfo)).total_seconds()
        except (TypeError, ValueError):
            return None
    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def status_error(response: Response) -> Optional[BrokerError]:
    """The envelope error for a non-success status, or None for 2xx."""
    status = response.status
    if 200 <= status < 300:
        return None
    if status == 401:
        return BrokerError(AUTH_REQUIRED, "the service rejected the broker credential")
    if status == 403:
        return BrokerError(FORBIDDEN, "the broker account may not read this resource")
    if status in (404, 410):
        return BrokerError(NOT_FOUND, "the resource was not found")
    if status == 429:
        return BrokerError(RATE_LIMITED, "the service is rate limiting requests", retry_after=retry_after(response.headers))
    if status in (400, 422):
        return BrokerError(INVALID_INPUT, "the service rejected the request parameters")
    if 300 <= status < 400:
        return BrokerError(UNAVAILABLE, "the service answered with a redirect, which the broker refuses", retryable=False)
    if status >= 500:
        return BrokerError(UNAVAILABLE, "the service answered with a server error", retry_after=retry_after(response.headers))
    return BrokerError(UNAVAILABLE, "the service answered with an unexpected status", retryable=False)


def json_body(response: Response) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BrokerError(UNAVAILABLE, "the service returned a response that is not JSON", retryable=False)


class ReplayTransport:
    """Answers requests from recorded exchanges and keeps the requests it saw
    so a test can check what would have been sent. An exchange matches on
    method, decoded URL path and every query parameter it lists (others may vary);
    the host is not compared."""

    def __init__(self, exchanges: List[Mapping[str, Any]]) -> None:
        self._exchanges: List[Tuple[str, str, Dict[str, str], Response]] = []
        for item in exchanges:
            response = item["response"]
            body = response.get("body", b"")
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            req = item["request"]
            self._exchanges.append(
                (
                    req["method"].upper(),
                    req["path"],
                    {str(k): str(v) for k, v in req.get("query", {}).items()},
                    Response(int(response["status"]), _lower(response.get("headers", {}).items()), body),
                )
            )
        self.sent: List[Request] = []

    def send(self, request: Request, *, timeout: float, max_bytes: int) -> Response:
        self.sent.append(request)
        parts = urlsplit(request.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for method, path, wanted, response in self._exchanges:
            if method != request.method.upper() or path != unquote(parts.path):
                continue
            if any(query.get(k) != v for k, v in wanted.items()):
                continue
            if len(response.body) > max_bytes:
                raise BrokerError(UNAVAILABLE, "the service response exceeded the broker's size limit", retryable=False)
            return response
        raise BrokerError(UNAVAILABLE, "no recorded exchange for this request", retryable=False)
