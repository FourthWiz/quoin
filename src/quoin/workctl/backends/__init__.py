"""Backend interface and registry.

A backend turns one normalized operation into normalized records. It gets
the service configuration, the limits, a transport, the resolved credential
(if the backend uses one) and a clock, and returns a `Result`. Backends
own service-specific escaping and paging; they never print, never log
retrieved content and never put response text into an error.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple
from urllib.parse import urlsplit

from ...opencode_adapter.secrets import SecretValue
from ..config import Limits, ServiceConfig
from ..envelope import POLICY_DENIED, BrokerError

# service -> backend names the configuration may select
BACKENDS_FOR_SERVICE: Dict[str, Tuple[str, ...]] = {
    "jira": ("jira-cloud", "command"),
    "slack": ("slack-web", "command"),
    "mail": ("gmail", "msgraph", "command"),
}

# backend name -> module under quoin.workctl.backends exposing BACKEND
_MODULES = {
    "jira-cloud": "jira_cloud",
    "slack-web": "slack_web",
    "gmail": "gmail",
    "msgraph": "msgraph",
    "command": "command",
}

# Backends that call an HTTP API with a bearer or basic credential.
CREDENTIAL_BACKENDS = ("jira-cloud", "slack-web", "gmail", "msgraph")


@dataclass
class Context:
    """`send` is the only way a backend reaches the network: it applies the
    configured timeout and response cap and refuses any URL whose origin is
    not the service's configured (or default) https origin, so a credential
    is never sent to a host taken from response data or a cursor. Cursors
    are opaque to the agent; a backend must never turn one into a host."""

    service: ServiceConfig
    limits: Limits
    transport: Any
    credential: Optional[SecretValue]
    fetched_at: str
    run_command: Any = None
    origin: Optional[Tuple[str, str]] = None

    def send(self, request: Any) -> Any:
        parts = urlsplit(request.url)
        if self.origin is None or parts.scheme != "https" or (parts.scheme, parts.netloc) != self.origin:
            raise BrokerError(POLICY_DENIED, "the backend tried to reach a host outside the configured service")
        return self.transport.send(
            request, timeout=self.limits.timeout_seconds, max_bytes=self.limits.max_response_bytes
        )


def origin_of(url: Optional[str]) -> Optional[Tuple[str, str]]:
    if not url:
        return None
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc) if parts.scheme == "https" and parts.netloc else None


@dataclass
class Result:
    """`data` is the normalized payload. `trim_key` names the list inside
    `data` the broker may shorten from the end to respect the byte bound.
    `cursor` is the service's next-page cursor, None on the last page."""

    data: Dict[str, Any]
    url: Optional[str] = None
    cursor: Optional[str] = None
    trim_key: Optional[str] = None
    extra_pagination: Dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    name: str
    default_base: Optional[str]

    def run(self, op_name: str, params: Mapping[str, Any], ctx: Context) -> Result: ...


def load(name: str) -> Backend:
    module = importlib.import_module("%s.%s" % (__name__, _MODULES[name]))
    return module.BACKEND
