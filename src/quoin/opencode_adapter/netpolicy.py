"""Destination approval and identity for a launch.

Three pure steps over plain inputs, run before a credential is resolved:

* `census` lists every network destination the launch could reach: provider
  endpoints, proxy routes, remote MCP servers, URL instructions, web tools and
  the package registry. Each row records which layer approved its host.
* `approve` refuses a work launch whose destinations a profile or the trusted
  managed policy did not approve. A provider id never approves a host: only
  the host name does.
* `identity_check` (when the policy requires it) connects to each provider
  endpoint and checks that the certificate names the approved host, through a
  proxy tunnel if the launch uses one.

`enforcement_table` and `network_section` describe what is and is not
enforced, for the effective-configuration record. Nothing here claims more
than it did: a control that could not run is recorded as not enforced.

No function in this module raises for a malformed input: a destination it
cannot read becomes a row of kind `unparseable`, which `approve` and
`identity_check` refuse where the policy requires the control.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from . import config, effective_record, isolation

# ------------------------------------------------------------------ closed sets

REFUSAL_TEXT: Dict[str, str] = {
    "allowed-hosts-unset": "a work profile must list the hosts it may reach; the profile sets no allowed hosts",
    "destination-not-approved": "a destination this launch could reach is not approved by the profile or the trusted managed policy",
    "web-tools-denied": "web tools are not denied in the compiled configuration, or are not approved by the profile",
    "mcp-remote-denied": "a remote MCP server names a host that the profile does not approve",
    "proxy-route-not-approved": "the proxy this launch would route through is not an approved host",
    "destination-identity-mismatch": "a provider endpoint did not present the identity the profile approved",
    "destination-unverifiable": "the identity of a provider endpoint could not be checked",
    "destination-plaintext": "a provider endpoint uses plain http outside this machine while the profile requires an identity check",
}

IDENTITY_REASONS = (
    "reserved-name", "host-unparseable", "dns-failed", "dns-timeout", "connect-failed", "proxy-refused",
    "proxy-unreachable", "ca-file-unreadable", "tls-failed", "check-error", "name-mismatch",
    "untrusted-certificate", "pin-mismatch", "address-outside",
)

POLICY_EVALUATION_FAILED = "policy-evaluation-failed"
MAX_ADDRESSES = 64
MAX_PROXY_HEADER = 16 * 1024
_PROXY_NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_DEFAULT_REGISTRY = "registry.npmjs.org"

# --------------------------------------------------------------------- data


@dataclass(frozen=True)
class NetRefusal:
    code: str
    detail: Optional[str] = None


@dataclass(frozen=True)
class ProxyRoute:
    variable: str
    host_key: str
    scheme: str
    port: Optional[int]
    approved_by: str


@dataclass(frozen=True)
class Destination:
    role: str
    host_key: str
    scheme: Optional[str]
    port: Optional[int]
    kind: str  # loopback | tls | plaintext | any-host | disabled | unparseable | host-less
    approved_by: str  # loopback | trusted-managed | profile | none
    provider_id: Optional[str] = None
    ca_file: Optional[str] = None
    via_proxy: Optional[ProxyRoute] = None


@dataclass(frozen=True)
class WebTools:
    mode: str
    posture_ok: bool
    approved_by: str


@dataclass(frozen=True)
class Census:
    destinations: Tuple[Destination, ...] = ()
    proxies: Tuple[ProxyRoute, ...] = ()
    web_tools: Optional[WebTools] = None
    package_registry: Tuple[Destination, ...] = ()
    registry_cause: Optional[str] = None  # set only when the npmrc read failed


EMPTY_CENSUS = Census()


# ----------------------------------------------------------------- helpers


def approved_by_origin(origin: Iterable[str]) -> str:
    names = set(origin or ())
    if names & {"trusted-managed", "managed"}:
        return "trusted-managed"
    if "profile" in names:
        return "profile"
    return "none"


def hosts_approved(effective: Any) -> bool:
    """True when the profile or the trusted managed policy set allowed_hosts."""
    try:
        entry = effective.values.get("policy.allowed_hosts")
        return entry is not None and approved_by_origin(entry.origin) != "none"
    except Exception:  # noqa: BLE001 - an unreadable origin is not an approval
        return False


def network_controls_required(policy: Any, isolation: Any) -> bool:
    """Whether the profile requires a network control this module enforces:
    work, or an identity check, or managed isolation. A requirement that
    cannot be computed counts as required."""
    try:
        return bool(
            policy.classification == "work"
            or policy.identity_check == "required"
            or isolation == "managed"
        )
    except Exception:  # noqa: BLE001
        return True


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_endpoint(url: Any) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """``(host_key, scheme, port)`` or ``(None, None, None)`` when the URL is
    not a plain http(s) endpoint with a well-formed host."""
    try:
        if not isinstance(url, str) or not config.url_is_unambiguous(url):
            return None, None, None
        parts = urlsplit(url)
        host = parts.hostname
        scheme = parts.scheme.lower()
        port = parts.port
        if scheme not in ("http", "https") or not config.host_is_well_formed(host):
            return None, None, None
        return config.host_key(host), scheme, port if port is not None else (443 if scheme == "https" else 80)
    except (ValueError, UnicodeError, TypeError):
        return None, None, None


def _no_proxy_matches(host: str, ambient: Mapping[str, str]) -> bool:
    raw = ambient.get("NO_PROXY") or ambient.get("no_proxy") or ""
    for item in (part.strip().lower() for part in raw.split(",")):
        if not item:
            continue
        if item == "*" or item == host:
            return True
        if item.startswith(".") and host.endswith(item):
            return True
    return False


def _scheme_transport(scheme: object) -> str:
    """Transport label of a remote destination's URL scheme: `http` and `ws`
    are plaintext, `https` and `wss` are tls, anything else is unparseable."""
    value = scheme.lower() if isinstance(scheme, str) else ""
    if value in ("http", "ws"):
        return "plaintext"
    if value in ("https", "wss"):
        return "tls"
    return "unparseable"


def _read_registry_hosts(files: Sequence[Path]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Registry hosts named by npmrc files, as `(hosts, None)`; `(None, cause)`
    when a file cannot be read (`registry-unreadable`) or a file or value
    cannot be parsed (`registry-unparseable`). The registry is then unknown
    and refused."""
    hosts: List[str] = []
    for path in files:
        try:
            if not os.path.lexists(path):
                continue
            raw = isolation._read_bounded(path)
            if raw is None or len(raw) >= isolation.MAX_READ_BYTES:
                return None, "registry-unreadable"
            text = raw.decode("utf-8")
        except UnicodeError:
            return None, "registry-unparseable"
        except (OSError, ValueError):
            return None, "registry-unreadable"
        for line in text.splitlines():
            match = re.match(r"^\s*(?:@[^:=\s]+:)?registry\s*=\s*(.+?)\s*$", line)
            if match:
                host, _scheme, _port = _parse_endpoint(match.group(1))
                if host is None:
                    return None, "registry-unparseable"
                hosts.append(host)
    return hosts, None


# ------------------------------------------------------------------ census


def evaluation_refusal(effective: Any) -> Optional[NetRefusal]:
    """The refusals the evaluation alone decides, so a label-approved provider
    whose host is excluded refuses with the destination code rather than
    `compile-blocked`. Work only: any other classification returns at once."""
    if effective.classification != "work":
        return None
    if any(f.code == "allowed-hosts-unset" for f in effective.findings):
        return NetRefusal("allowed-hosts-unset")
    for provider_id in sorted(effective.excluded_providers):
        reason = effective.excluded_providers[provider_id]
        if reason in ("host-not-allowed", "host-denied"):
            return NetRefusal("destination-not-approved", reason)
    return None


def _provider_rows(
    effective: Any, compiled_doc: Mapping[str, Any], ambient: Mapping[str, str],
    allowed: frozenset, origin: str, proxies: Mapping[str, ProxyRoute],
) -> List[Destination]:
    from . import compiler

    views = {}
    for view in effective.providers.values():
        try:
            views[compiler.native_provider_id(view)] = view
        except Exception:  # noqa: BLE001
            continue
    rows: List[Destination] = []
    providers = compiled_doc.get("provider") if isinstance(compiled_doc, Mapping) else None
    for native_id in sorted(providers if isinstance(providers, Mapping) else ()):
        view = views.get(native_id)
        use_proxy = bool(getattr(view, "use_env_proxy", False))
        ca_file = getattr(view, "ca_file", None)
        try:
            host, scheme, port = _parse_endpoint(providers[native_id]["options"]["baseURL"])
        except (KeyError, TypeError):
            host, scheme, port = None, None, None
        if host is None:
            rows.append(Destination("provider", "unknown", None, None, "unparseable", "none", native_id, ca_file))
            continue
        if _is_loopback(host):
            kind, approved = "loopback", "loopback"
        else:
            kind = "plaintext" if scheme == "http" else "tls"
            approved = origin if host in allowed else "none"
        via = None
        if use_proxy and not _no_proxy_matches(host, ambient):
            names = ("HTTPS_PROXY", "https_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy")
            via = next((proxies[n] for n in names if n in proxies), None)
        rows.append(Destination("provider", host, scheme, port, kind, approved, native_id, ca_file, via))
    return rows


def _proxy_routes(ambient: Mapping[str, str], allowed: frozenset, origin: str) -> List[ProxyRoute]:
    routes: List[ProxyRoute] = []
    for name in _PROXY_NAMES:
        value = ambient.get(name)
        if not value:
            continue
        host, scheme, port = _parse_endpoint(value)
        if host is None:
            routes.append(ProxyRoute(name, "unknown", "unknown", None, "none"))
        else:
            routes.append(ProxyRoute(name, host, scheme, port, origin if host in allowed else "none"))
    return routes


def _web_tools(effective: Any, compiled_doc: Mapping[str, Any], policy: Any) -> WebTools:
    posture = False
    try:
        from . import compiler, names

        agents = compiled_doc.get("agent")
        posture = compiled_doc.get("permission") == {"webfetch": "deny"} and isinstance(agents, Mapping) and all(
            isinstance(agents.get(names.role_agent_name(role)), Mapping)
            and agents[names.role_agent_name(role)].get("permission") == {"webfetch": "deny", "websearch": "deny"}
            for role in compiler.ROLES
        )
    except Exception:  # noqa: BLE001
        posture = False
    entry = effective.values.get("policy.web_tools")
    approved = approved_by_origin(entry.origin) if entry is not None else "none"
    return WebTools(policy.web_tools, bool(posture), approved)


def census(
    *, effective: Any, compiled_doc: Mapping[str, Any], inventory: Any, ambient: Mapping[str, str],
    policy: Any, npm_userconfig: Any, fetch_dirs: Sequence[Tuple[Any, str]],
) -> Census:
    """Every destination the launch could reach. Never raises: a row it cannot
    read is recorded with kind `unparseable`."""
    entry = effective.values.get("policy.allowed_hosts")
    allowed = frozenset(entry.value) if entry is not None and entry.value else frozenset()
    origin = approved_by_origin(entry.origin) if entry is not None else "none"
    work = effective.classification == "work"
    providers_proxy = any(
        getattr(v, "use_env_proxy", False) for v in effective.providers.values()
    )
    proxies = _proxy_routes(ambient, allowed, origin) if providers_proxy else []
    by_name = {route.variable: route for route in proxies}
    rows = _provider_rows(effective, compiled_doc, ambient, allowed, origin, by_name)
    for item in getattr(inventory, "entries", ()):
        facts = item.facts
        if item.kind == "mcp-remote":
            raw = facts.get("url_host")
            host = None
            if config.host_is_well_formed(str(raw) if raw is not None else None):
                host = config.host_key(str(raw))
            if host is None:
                rows.append(Destination("mcp-remote", "unknown", facts.get("url_scheme"), None, "unparseable", "none"))
            elif _is_loopback(host):
                rows.append(Destination("mcp-remote", host, facts.get("url_scheme"), None, "loopback", "loopback"))
            else:
                rows.append(Destination(
                    "mcp-remote", host, facts.get("url_scheme"), None,
                    _scheme_transport(facts.get("url_scheme")),
                    origin if host in allowed else "none",
                ))
        elif item.kind in ("instruction", "skill") and facts.get("url"):
            rows.append(Destination(item.kind, "*", facts.get("url_scheme"), None, "host-less", "none"))
        elif item.kind in ("remote-config", "account-config"):
            rows.append(Destination(item.kind, "*", None, None, "host-less", "none"))
    tools = _web_tools(effective, compiled_doc, policy)
    rows.append(Destination(
        "web-tools", "*", None, None, "any-host" if tools.mode == "allow" else "disabled", tools.approved_by,
    ))
    rows.append(Destination("models-catalogue", "*", None, None, "disabled", "none"))
    registry: List[Destination] = []
    registry_cause: Optional[str] = None
    if work and policy.package_install == "allow":
        files = [Path(npm_userconfig)] if npm_userconfig else []
        files += [Path(d) / ".npmrc" for d, _layer in fetch_dirs]
        found, registry_cause = _read_registry_hosts(files)
        if found is None:
            registry.append(Destination("npm-registry", "unknown", None, None, "unparseable", "none"))
        else:
            for host in sorted(set(found) or {_DEFAULT_REGISTRY}):
                registry.append(Destination(
                    "npm-registry", host, "https", 443, "tls", origin if host in allowed else "none",
                ))
    rows.extend(registry)
    rows.sort(key=lambda r: (r.role, effective_record.digest_token(r.host_key), r.provider_id or ""))
    return Census(tuple(rows), tuple(proxies), tools, tuple(registry), registry_cause)


def approve(census_: Census, policy: Any, classification: str) -> Optional[NetRefusal]:
    """Refuse a work launch whose destinations are not approved. Every check
    here enforces a control the enforcement table marks enforced only under
    work, so classification is exactly the requirement for this function; the
    one control a personal profile can require, the identity check, is
    enforced by `identity_check`, never skipped here."""
    if classification != "work":
        return None
    for route in census_.proxies:
        if route.approved_by == "none":
            return NetRefusal("proxy-route-not-approved", "host-unparseable" if route.host_key == "unknown" else "host-not-allowed")
    for row in census_.destinations:
        if row.role == "mcp-remote" and row.approved_by == "none":
            return NetRefusal("mcp-remote-denied", "host-unparseable" if row.kind == "unparseable" else "host-not-allowed")
    tools = census_.web_tools
    if tools is not None:
        if tools.mode == "deny" and not tools.posture_ok:
            return NetRefusal("web-tools-denied", "posture-missing")
        if tools.mode == "allow" and tools.approved_by == "none":
            return NetRefusal("web-tools-denied", "not-approved")
    for row in census_.destinations:
        if row.role == "provider" and row.kind == "unparseable":
            return NetRefusal("destination-not-approved", "host-unparseable")
        if row.role == "provider" and row.approved_by == "none":
            return NetRefusal("destination-not-approved", "host-not-allowed")
        if row.role == "npm-registry" and row.approved_by == "none":
            return NetRefusal(
                "destination-not-approved", (census_.registry_cause or "registry-unreadable") if row.kind == "unparseable" else "registry-not-approved",
            )
    return None


# -------------------------------------------------------- identity check


@dataclass(frozen=True)
class IdentityResult:
    outcome: str  # ok | mismatch | unverifiable | plaintext
    kind: str
    reason: Optional[str] = None
    addresses_count: Optional[int] = None
    cert_sha256_prefix: Optional[str] = None
    pinned: bool = False


def _result(outcome: str, kind: str, reason: Optional[str] = None, **extra: Any) -> IdentityResult:
    return IdentityResult(outcome, kind, reason, **extra)


def _fold(address: str) -> str:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def _pin_set(pins: Optional[Sequence[str]]) -> frozenset:
    out = set()
    for pin in pins or ():
        text = str(pin).lower()
        if text.startswith("sha256:"):
            text = text[len("sha256:"):]
        out.add(text.replace(":", ""))
    return frozenset(out)


class IdentityChecker(Protocol):
    def check(self, destination: Destination, *, policy: Any, proxy: Optional[ProxyRoute] = None) -> IdentityResult:
        ...


class SystemIdentityChecker:
    """Connects to a provider endpoint and checks its certificate.

    Resolution runs in a daemon thread joined with a timeout; the connection
    goes to a checked address (never a second lookup); the TLS handshake
    verifies the approved host name; optional pins compare the leaf
    certificate's SHA-256. Through an approved proxy the name is checked end
    to end over a CONNECT tunnel, and address ranges are not checked (the
    proxy resolves). Names under the reserved `.invalid` domain are refused
    without any lookup."""

    def __init__(
        self, resolver: Callable[..., Any] = socket.getaddrinfo,
        connect: Callable[..., Any] = socket.create_connection,
        context_factory: Callable[..., Any] = ssl.create_default_context, timeout_s: float = 10.0,
    ) -> None:
        self._resolver = resolver
        self._connect = connect
        self._context_factory = context_factory
        self._timeout = timeout_s

    def check(self, destination: Destination, *, policy: Any, proxy: Optional[ProxyRoute] = None) -> IdentityResult:
        host = destination.host_key
        if destination.kind == "unparseable" or (proxy is not None and proxy.host_key == "unknown"):
            return _result("unverifiable", "tls", "host-unparseable")
        if host.endswith(".invalid"):
            return _result("unverifiable", "tls", "reserved-name")
        if destination.kind == "loopback":
            return _result("ok", "loopback")
        if destination.kind == "plaintext":
            return _result("plaintext", "plaintext")
        sockets: List[Any] = []
        try:
            return self._check(destination, policy, proxy, sockets)
        finally:
            for item in reversed(sockets):
                try:
                    item.close()
                except Exception:  # noqa: BLE001
                    pass

    def _resolve(self, host: str, port: int) -> Tuple[Optional[List[str]], Optional[str]]:
        box: Dict[str, Any] = {}

        def work() -> None:
            try:
                box["addresses"] = self._resolver(host, port, type=socket.SOCK_STREAM)
            except BaseException as exc:  # noqa: BLE001 - reported by class below
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        try:
            thread.start()
        except RuntimeError:
            return None, "dns-failed"
        thread.join(self._timeout)
        if thread.is_alive():
            return None, "dns-timeout"
        if "error" in box or not box.get("addresses"):
            return None, "dns-failed"
        found: List[str] = []
        for item in box["addresses"]:
            try:
                address = _fold(str(item[4][0]))
            except (IndexError, TypeError):
                continue
            if address not in found:
                found.append(address)
            if len(found) >= MAX_ADDRESSES:
                break
        return (found, None) if found else (None, "dns-failed")

    def _tunnel(self, proxy: ProxyRoute, host: str, port: int, sockets: List[Any]) -> Tuple[Any, Optional[str]]:
        try:
            raw = self._connect((proxy.host_key, proxy.port or 80), self._timeout)
        except (OSError, ValueError):
            return None, "proxy-unreachable"
        sockets.append(raw)
        try:
            raw.settimeout(self._timeout)
            raw.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode("ascii"))
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = raw.recv(1024)
                if not chunk:
                    return None, "proxy-unreachable"
                buffer += chunk
                if len(buffer) > MAX_PROXY_HEADER:
                    return None, "proxy-refused"
        except (OSError, ValueError, UnicodeError):
            return None, "proxy-unreachable"
        status = buffer.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            return None, "proxy-refused"
        return raw, None

    def _check(self, destination: Destination, policy: Any, proxy: Optional[ProxyRoute], sockets: List[Any]) -> IdentityResult:
        host = destination.host_key
        port = destination.port or 443
        count: Optional[int] = None
        if proxy is not None:
            raw, reason = self._tunnel(proxy, host, port, sockets)
            if raw is None:
                return _result("unverifiable", "via-proxy", reason)
            kind = "via-proxy"
        else:
            addresses, reason = self._resolve(host, port)
            if addresses is None:
                return _result("unverifiable", "tls", reason)
            count = len(addresses)
            if policy.allowed_addresses:
                try:
                    networks = [ipaddress.ip_network(n, strict=False) for n in policy.allowed_addresses]
                    inside = all(
                        any(ipaddress.ip_address(a) in net for net in networks) for a in addresses
                    )
                except ValueError:
                    inside = False
                if not inside:
                    return _result("mismatch", "tls", "address-outside", addresses_count=count)
            try:
                raw = self._connect((addresses[0], port), self._timeout)
            except (OSError, ValueError):
                return _result("unverifiable", "tls", "connect-failed", addresses_count=count)
            sockets.append(raw)
            kind = "tls"
        try:
            context = self._context_factory(cafile=destination.ca_file)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        except (OSError, ssl.SSLError, ValueError, TypeError):
            return _result("unverifiable", kind, "ca-file-unreadable", addresses_count=count)
        try:
            secure = context.wrap_socket(raw, server_hostname=host)
            sockets.append(secure)
        except ssl.SSLCertVerificationError as exc:
            mismatch = getattr(exc, "verify_code", None) == 62 or "mismatch" in str(getattr(exc, "verify_message", "")).lower()
            return _result("mismatch", kind, "name-mismatch" if mismatch else "untrusted-certificate", addresses_count=count)
        except (ssl.SSLError, OSError, UnicodeError, ValueError):
            return _result("unverifiable", kind, "tls-failed", addresses_count=count)
        try:
            fingerprint = hashlib.sha256(secure.getpeercert(binary_form=True) or b"").hexdigest()
        except (OSError, ValueError, ssl.SSLError):
            return _result("unverifiable", kind, "tls-failed", addresses_count=count)
        pins = _pin_set(policy.pins)
        if pins and fingerprint not in pins:
            return _result("mismatch", kind, "pin-mismatch", addresses_count=count, cert_sha256_prefix=fingerprint[:16], pinned=True)
        return _result("ok", kind, None, addresses_count=count, cert_sha256_prefix=fingerprint[:16], pinned=bool(pins))


def identity_check(
    census_: Census, policy: Any, checker: Any,
) -> Tuple[List[Dict[str, Any]], Optional[NetRefusal]]:
    """Check every provider endpoint when the policy requires it, whatever the
    classification. Returns the redacted observations and the first refusal."""
    if policy.identity_check != "required":
        return [], None
    observations: List[Dict[str, Any]] = []
    refusal: Optional[NetRefusal] = None
    rows = sorted(
        (r for r in census_.destinations if r.role == "provider"),
        key=lambda r: (effective_record.digest_token(r.host_key), r.provider_id or ""),
    )
    for row in rows:
        if row.kind == "loopback":
            result = IdentityResult("ok", "loopback")
        elif row.kind == "plaintext":
            result = IdentityResult("plaintext", "plaintext")
        elif row.kind == "unparseable" or (row.via_proxy is not None and row.via_proxy.host_key == "unknown"):
            result = IdentityResult("unverifiable", "tls", "host-unparseable")
        else:
            try:
                result = checker.check(row, policy=policy, proxy=row.via_proxy)
            except Exception:  # noqa: BLE001 - a failing check refuses, never crashes
                result = IdentityResult("unverifiable", "tls", "check-error")
        observations.append({
            "host": "loopback" if row.kind == "loopback" else effective_record.digest_token(row.host_key),
            "kind": result.kind, "outcome": result.outcome, "reason": result.reason,
            "addresses_count": result.addresses_count, "cert_sha256_prefix": result.cert_sha256_prefix,
            "pinned": bool(result.pinned),
        })
        if refusal is None and result.outcome != "ok":
            if result.outcome == "mismatch":
                refusal = NetRefusal("destination-identity-mismatch", result.reason)
            elif result.outcome == "plaintext":
                refusal = NetRefusal("destination-plaintext")
            else:
                refusal = NetRefusal("destination-unverifiable", result.reason)
    return observations, refusal


# ------------------------------------------------- enforcement and record

NETWORK_STEP_CONTROLS = ("provider-destinations", "destination-identity", "proxy-route", "remote-mcp")

_BOUNDARY_ROWS = (
    "shell-egress", "redirects", "dns-after-launch", "opposite-profile-files", "profile-config-edits",
    "path-executables", "keychain-access", "child-trust-store", "npm-global-config",
)


def enforcement_table(
    policy: Any, classification: str, isolation_verdict: Any, census_: Census, *,
    isolation: str = "convenience", allowed_hosts_approved: bool = False, network_status: str = "planned",
) -> List[Dict[str, str]]:
    """The closed enforcement rows, sorted by control. With
    `network_status="unavailable"` the network-step rows read `not-enforced`
    whatever the policy says, so no row claims a check that did not run."""
    work = classification == "work"
    unavailable = network_status == "unavailable"

    def config_row(control: str, layer: str, enforced: bool, network_step: bool = False) -> Dict[str, str]:
        on = enforced and not (unavailable and network_step)
        return {"control": control, "layer": layer, "status": "enforced" if on else "not-enforced"}

    rows = [
        config_row("provider-destinations", "quoin-config-refusal", work or allowed_hosts_approved, True),
        config_row("destination-identity", "quoin-launch-identity", policy.identity_check == "required", True),
        config_row("proxy-route", "quoin-config-refusal", work, True),
        config_row("web-tools", "quoin-config-refusal", policy.web_tools == "deny"),
        config_row("remote-mcp", "quoin-config-refusal", policy.mcp_remote != "any", True),
        config_row("remote-config-and-instructions", "quoin-config-refusal", work),
        {"control": "launcher-proxy", "layer": "launcher-proxy", "status": "not-provided"},
        {"control": "brokered-registry", "layer": "launcher-proxy", "status": "not-provided"},
    ]
    if getattr(isolation_verdict, "status", None) == "verified":
        managed = "delegated-verified"
    elif isolation == "managed":
        managed = "delegated-unverified"
    else:
        managed = "not-enforced"
    for control in _BOUNDARY_ROWS:
        if control == "npm-global-config" and not (work and policy.package_install == "allow"):
            continue
        rows.append({"control": control, "layer": "managed-boundary", "status": managed})
    return sorted(rows, key=lambda r: r["control"])


def network_section(
    census_: Census, policy: Any, *, status: str = "planned", egress_hosts: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """The record's network section: hosts only as digests (or `loopback`,
    `*`), never names, URLs or credentials."""
    section: Dict[str, Any] = {
        "status": status, "identity_check": policy.identity_check, "destinations": [], "proxies": [],
    }
    if status == "unavailable":
        return section
    required = policy.identity_check == "required"
    for row in census_.destinations:
        if row.kind == "loopback":
            host = "loopback"
        elif row.host_key in ("*", "unknown") or row.kind in ("host-less", "any-host", "disabled"):
            host = "*"
        else:
            host = effective_record.digest_token(row.host_key)
        if row.role != "provider":
            identity = "not-checked" if required else "not-required"
        elif row.kind == "loopback":
            identity = "loopback"
        elif not required:
            identity = "not-required"
        else:
            identity = "via-proxy" if row.via_proxy is not None else "tls-required"
        if egress_hosts is None or host in ("loopback", "*"):
            egress = "not-applicable"
        else:
            egress = "listed" if row.host_key in egress_hosts else "not-listed"
        section["destinations"].append({
            "role": row.role, "host": host, "kind": row.kind, "approved_by": row.approved_by,
            "identity": identity, "pinned": bool(policy.pins) and row.role == "provider",
            "addresses_restricted": bool(policy.allowed_addresses) and row.role == "provider",
            "managed_egress": egress,
        })
    for route in census_.proxies:
        section["proxies"].append({
            "variable": route.variable,
            "host": "*" if route.host_key == "unknown" else effective_record.digest_token(route.host_key),
            "approved_by": route.approved_by,
        })
    return section
