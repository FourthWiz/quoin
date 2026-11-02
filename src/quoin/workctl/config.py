"""The broker's own configuration file.

The file lives at a fixed place under the account's real home directory
(`~/.config/quoin/workctl.json`, resolved from the password database, not
from `$HOME` or `$XDG_CONFIG_HOME`), so an agent cannot point the broker at
a configuration of its choosing through its environment or a flag. It must
be a regular file owned by the current user and closed to group and other
writes. Credentials appear only as keychain references under the
`quoin-workctl` service prefix, which keeps the broker from being steered
to any other keychain item, such as the agent's provider key.

Shape::

    {
      "schema_version": 1,
      "profile": "work",
      "limits": {"max_items": 50, "max_bytes": 262144, ...},
      "network": {"https_proxy": "http://proxy.corp:8080", "ca_file": "/etc/ssl/corp.pem"},
      "services": {
        "jira":  {"backend": "jira-cloud", "tenant": "acme",
                  "base_url": "https://acme.atlassian.net",
                  "account": "reader@acme.example",
                  "credential": "keychain:quoin-workctl-jira/reader@acme.example"},
        "slack": {"backend": "slack-web", "tenant": "T0123", "credential": "..."},
        "mail":  {"backend": "command", "tenant": "acme",
                  "argv": ["/usr/local/bin/org-mail", "--json"]}
      },
      "writes": {
        "approval_boundary": "separate-user",
        "approval_key": "keychain:quoin-workctl-approver/acme",
        "operations": ["slack.post"],
        "credentials": {"slack": "keychain:quoin-workctl-writer/T0123"}
      }
    }

The `writes` section is optional and writes stay disabled without it. It
declares which trusted approval boundary the deployment provides, the
keychain item holding the approval key, the write operations allowed and
the write credentials. Approval keys and write credentials use their own
keychain prefixes, which read credentials may not use, so the read broker
can never be pointed at a write credential.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from ..opencode_adapter import secrets
from ..opencode_adapter.install import PROFILE_RE
from .envelope import POLICY_DENIED, BrokerError
from .operations import OPERATIONS, SERVICES

CONFIG_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
KEYCHAIN_SERVICE_PREFIX = "quoin-workctl"
APPROVER_KEYCHAIN_PREFIX = "quoin-workctl-approver"
WRITER_KEYCHAIN_PREFIX = "quoin-workctl-writer"
_RESERVED_PREFIXES = (APPROVER_KEYCHAIN_PREFIX, WRITER_KEYCHAIN_PREFIX)

# The boundary that keeps approval and write credentials away from the
# agent: a platform approval mechanism, a separate OS account, or a managed
# container the agent runs in without access to the writer's keychain.
APPROVAL_BOUNDARIES = ("platform", "separate-user", "managed-container")

# write operation -> backends that implement it (all use a write credential)
WRITE_BACKENDS: Dict[str, Tuple[str, ...]] = {
    "jira.comment": ("jira-cloud",),
    "slack.post": ("slack-web",),
    "mail.send": ("gmail",),
}

# name: (default, minimum, maximum)
WRITE_TTL_SPECS: Dict[str, Tuple[int, int, int]] = {
    "proposal_ttl_seconds": (3_600, 60, 604_800),
    "approval_ttl_seconds": (900, 60, 86_400),
}

TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9._@+-]{1,256}")

# name: (default, hard maximum)
LIMIT_SPECS: Dict[str, Tuple[int, int]] = {
    "max_items": (50, 100),
    "max_bytes": (262_144, 1_048_576),
    "max_text_chars": (8_000, 64_000),
    "max_response_bytes": (4_194_304, 16_777_216),
    "timeout_seconds": (20, 60),
    "cache_ttl_seconds": (300, 3_600),
}

GUIDANCE = (
    "configure the service in ~/.config/quoin/workctl.json (see the workctl section of "
    "the OpenCode adapter README); the broker reads nothing from the agent environment"
)


@dataclass(frozen=True)
class Limits:
    max_items: int = LIMIT_SPECS["max_items"][0]
    max_bytes: int = LIMIT_SPECS["max_bytes"][0]
    max_text_chars: int = LIMIT_SPECS["max_text_chars"][0]
    max_response_bytes: int = LIMIT_SPECS["max_response_bytes"][0]
    timeout_seconds: int = LIMIT_SPECS["timeout_seconds"][0]
    cache_ttl_seconds: int = LIMIT_SPECS["cache_ttl_seconds"][0]


@dataclass(frozen=True)
class ServiceConfig:
    service: str
    backend: str
    tenant: str
    account: Optional[str] = None
    base_url: Optional[str] = None
    web_url: Optional[str] = None
    credential: Optional[secrets.CredentialRef] = None
    argv: Tuple[str, ...] = ()
    operations: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class Network:
    https_proxy: Optional[str] = None
    ca_file: Optional[str] = None


@dataclass(frozen=True)
class Writes:
    approval_boundary: Optional[str] = None
    approval_key: Optional[secrets.CredentialRef] = None
    operations: Tuple[str, ...] = ()
    credentials: Dict[str, secrets.CredentialRef] = field(default_factory=dict)
    proposal_ttl_seconds: int = WRITE_TTL_SPECS["proposal_ttl_seconds"][0]
    approval_ttl_seconds: int = WRITE_TTL_SPECS["approval_ttl_seconds"][0]

    @property
    def enabled(self) -> bool:
        return self.approval_boundary is not None and self.approval_key is not None and bool(self.operations)


@dataclass(frozen=True)
class BrokerConfig:
    profile: Optional[str]
    limits: Limits
    services: Dict[str, ServiceConfig] = field(default_factory=dict)
    network: Network = field(default_factory=Network)
    writes: Writes = field(default_factory=Writes)


def _deny(message: str) -> BrokerError:
    return BrokerError(POLICY_DENIED, message)


def real_home() -> Path:
    """The account's home from the password database, ignoring `$HOME`."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_path(home: Optional[Path] = None) -> Path:
    return Path(home if home is not None else real_home()) / ".config" / "quoin" / "workctl.json"


def _no_duplicates(pairs):
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise _deny("broker configuration is unreadable or a symlink")
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _deny("broker configuration must be a regular file")
            if info.st_uid != os.getuid():
                raise _deny("broker configuration must be owned by the current user")
            if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise _deny("broker configuration must not be writable by group or others")
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise _deny("broker configuration is unreadable")
    if len(raw) > MAX_CONFIG_BYTES:
        raise _deny("broker configuration is too large")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError):
        raise _deny("broker configuration is not valid JSON")
    if not isinstance(data, dict):
        raise _deny("broker configuration must be a JSON object")
    return data


def _keys(obj: Any, allowed: set, where: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise _deny("%s must be an object" % where)
    extra = set(obj) - allowed
    if extra:
        raise _deny("%s has an unknown key: %s" % (where, sorted(extra)[0]))
    return obj


def _limits(obj: Any) -> Limits:
    obj = _keys(obj if obj is not None else {}, set(LIMIT_SPECS), "limits")
    values = {}
    for name, (default, maximum) in LIMIT_SPECS.items():
        value = obj.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise _deny("limits.%s must be an integer from 0 to %d" % (name, maximum))
        if value == 0 and name != "cache_ttl_seconds":
            raise _deny("limits.%s must be positive" % name)
        values[name] = value
    return Limits(**values)


def _https_url(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise _deny("%s must be an https URL" % where)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _deny("%s must be an https URL without embedded credentials" % where)
    if parts.query or parts.fragment:
        raise _deny("%s must not carry a query or fragment" % where)
    return value.rstrip("/")


def _has_prefix(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + "-")


def _keychain_ref(value: Any, where: str, prefix: str) -> secrets.CredentialRef:
    try:
        ref = secrets.parse(value)
    except ValueError:
        raise _deny("%s must be keychain:SERVICE/ACCOUNT" % where)
    if ref.scheme != "keychain":
        raise _deny(
            "%s must be a keychain reference: broker credentials never come from the agent environment" % where
        )
    if not _has_prefix(ref.service or "", prefix):
        raise _deny("%s keychain service must start with %s" % (where, prefix))
    return ref


def _credential(value: Any, service: str) -> Optional[secrets.CredentialRef]:
    if value is None:
        return None
    where = "services.%s.credential" % service
    ref = _keychain_ref(value, where, KEYCHAIN_SERVICE_PREFIX)
    if any(_has_prefix(ref.service or "", p) for p in _RESERVED_PREFIXES):
        raise _deny("%s must not name an approval key or write credential" % where)
    return ref


def _writes(obj: Any, services: Mapping[str, ServiceConfig]) -> Writes:
    if obj is None:
        return Writes()
    obj = _keys(
        obj,
        {"approval_boundary", "approval_key", "operations", "credentials"} | set(WRITE_TTL_SPECS),
        "writes",
    )
    boundary = obj.get("approval_boundary")
    if boundary is not None and boundary not in APPROVAL_BOUNDARIES:
        raise _deny("writes.approval_boundary must be one of: %s" % ", ".join(APPROVAL_BOUNDARIES))
    key = obj.get("approval_key")
    key_ref = None if key is None else _keychain_ref(key, "writes.approval_key", APPROVER_KEYCHAIN_PREFIX)
    if boundary is not None and key_ref is None:
        raise _deny("writes.approval_key is required when writes.approval_boundary is set")
    ops = obj.get("operations", [])
    if not isinstance(ops, list) or not all(
        isinstance(o, str) and o in OPERATIONS and not OPERATIONS[o].read_only for o in ops
    ):
        raise _deny("writes.operations must list write operations only")
    if ops and boundary is None:
        raise _deny("writes.operations needs writes.approval_boundary and writes.approval_key")
    creds_obj = _keys(obj.get("credentials", {}), set(SERVICES), "writes.credentials")
    credentials = {
        name: _keychain_ref(value, "writes.credentials.%s" % name, WRITER_KEYCHAIN_PREFIX)
        for name, value in creds_obj.items()
    }
    for name in ops:
        service = OPERATIONS[name].service
        svc = services.get(service)
        if svc is None:
            raise _deny("writes.operations lists %s, whose service is not configured" % name)
        if svc.backend not in WRITE_BACKENDS[name]:
            raise _deny("the %s backend does not support %s" % (svc.backend, name))
        if service not in credentials:
            raise _deny("writes.credentials needs a write credential for %s" % service)
    ttls = {}
    for name, (default, minimum, maximum) in WRITE_TTL_SPECS.items():
        value = obj.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise _deny("writes.%s must be an integer from %d to %d" % (name, minimum, maximum))
        ttls[name] = value
    return Writes(
        approval_boundary=boundary,
        approval_key=key_ref,
        operations=tuple(ops),
        credentials=credentials,
        **ttls,
    )


def _network(obj: Any) -> Network:
    obj = _keys(obj if obj is not None else {}, {"https_proxy", "ca_file"}, "network")
    proxy = obj.get("https_proxy")
    if proxy is not None:
        parts = urlsplit(proxy) if isinstance(proxy, str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
            raise _deny("network.https_proxy must be an http(s)://host:port URL")
    ca_file = obj.get("ca_file")
    if ca_file is not None and (not isinstance(ca_file, str) or not os.path.isabs(ca_file) or "\x00" in ca_file):
        raise _deny("network.ca_file must be an absolute path")
    return Network(https_proxy=proxy, ca_file=ca_file)


def _argv(value: Any, service: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 32
        or not all(isinstance(a, str) and a and "\x00" not in a and len(a) <= 1024 for a in value)
    ):
        raise _deny("services.%s.argv must be a non-empty list of strings" % service)
    if not os.path.isabs(value[0]):
        raise _deny("services.%s.argv[0] must be an absolute path" % service)
    return tuple(value)


def _service(name: str, obj: Any, backends: Mapping[str, Tuple[str, ...]]) -> ServiceConfig:
    obj = _keys(
        obj,
        {"backend", "tenant", "account", "base_url", "web_url", "credential", "argv", "operations"},
        "services.%s" % name,
    )
    backend = obj.get("backend")
    if backend not in backends.get(name, ()):
        raise _deny(
            "services.%s.backend must be one of: %s" % (name, ", ".join(backends.get(name, ())) or "none")
        )
    tenant = obj.get("tenant")
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        raise _deny("services.%s.tenant is required and must be a short identifier" % name)
    account = obj.get("account")
    if account is not None and (not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account)):
        raise _deny("services.%s.account is not a valid account identifier" % name)
    ops = obj.get("operations")
    if ops is not None:
        if not isinstance(ops, list) or not all(
            isinstance(o, str) and o in OPERATIONS and OPERATIONS[o].read_only and OPERATIONS[o].service == name
            for o in ops
        ):
            raise _deny(
                "services.%s.operations lists an unknown operation (write operations go in writes.operations)" % name
            )
        ops = tuple(ops)
    return ServiceConfig(
        service=name,
        backend=backend,
        tenant=tenant,
        account=account,
        base_url=_https_url(obj.get("base_url"), "services.%s.base_url" % name),
        web_url=_https_url(obj.get("web_url"), "services.%s.web_url" % name),
        credential=_credential(obj.get("credential"), name),
        argv=_argv(obj.get("argv"), name),
        operations=ops,
    )


def parse(data: Mapping[str, Any], backends: Mapping[str, Tuple[str, ...]]) -> BrokerConfig:
    data = _keys(data, {"schema_version", "profile", "limits", "services", "network", "writes"}, "configuration")
    if data.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise _deny("broker configuration schema_version must be %d" % CONFIG_SCHEMA_VERSION)
    profile = data.get("profile")
    if profile is not None and (not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile)):
        raise _deny("profile must be a profile name")
    services_obj = _keys(data.get("services", {}), set(SERVICES), "services")
    services = {name: _service(name, obj, backends) for name, obj in services_obj.items()}
    return BrokerConfig(
        profile=profile,
        limits=_limits(data.get("limits")),
        services=services,
        network=_network(data.get("network")),
        writes=_writes(data.get("writes"), services),
    )


def load(path: Path, backends: Mapping[str, Tuple[str, ...]]) -> Optional[BrokerConfig]:
    """The parsed configuration, or None when no file exists."""
    data = _read(path)
    if data is None:
        return None
    return parse(data, backends)
