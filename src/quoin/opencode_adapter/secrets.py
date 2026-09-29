"""Credential references: how configuration names a secret without holding it.

A reference is either `env:NAME` (an environment variable) or
`keychain:SERVICE/ACCOUNT`. This module only parses the grammar; resolver
backends arrive later. Where the standard library's secrets module is
needed, import it as `_stdlib_secrets` to keep the two apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_ENV_NAME = r"[A-Z_][A-Z0-9_]{0,127}"
_SERVICE = r"[A-Za-z0-9._-]{1,128}"
_ACCOUNT = r"[A-Za-z0-9._@/+-]{1,256}"

# The anchored string form; the runtime schema's credential_ref pattern
# must equal it.
CREDENTIAL_REF_PATTERN = r"^(?:env:%s|keychain:%s/%s)$" % (_ENV_NAME, _SERVICE, _ACCOUNT)

_ENV_RE = re.compile(_ENV_NAME)
_SERVICE_RE = re.compile(_SERVICE)
_ACCOUNT_RE = re.compile(_ACCOUNT)

_INVALID = "credential reference must be env:NAME or keychain:SERVICE/ACCOUNT"


@dataclass(frozen=True)
class CredentialRef:
    scheme: str
    name: Optional[str] = None
    service: Optional[str] = None
    account: Optional[str] = None

    def masked(self) -> str:
        if self.scheme == "env":
            return "env:%s" % self.name
        return "keychain:%s/***" % self.service

    def __repr__(self) -> str:
        return "CredentialRef(%s)" % self.masked()

    __str__ = __repr__


def parse(text: str) -> CredentialRef:
    """Parse a reference. The error message never includes `text`."""
    if not isinstance(text, str):
        raise ValueError(_INVALID)
    if text.startswith("env:"):
        name = text[len("env:"):]
        if _ENV_RE.fullmatch(name):
            return CredentialRef("env", name=name)
    elif text.startswith("keychain:"):
        service, sep, account = text[len("keychain:"):].partition("/")
        if sep and _SERVICE_RE.fullmatch(service) and _ACCOUNT_RE.fullmatch(account):
            return CredentialRef("keychain", service=service, account=account)
    raise ValueError(_INVALID)
