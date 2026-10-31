"""The effective-configuration record of a launch.

A launch's record states which configuration sources were read, what each
entry is and how it was judged, which environment names the child receives,
the credentials by kind, and the policy the launch ran under. It is built
from the inventory and the policy view only, so it holds no timestamp, run
id, absolute path or secret: two launches of the same world produce the same
digest. Names that could carry a secret are replaced by a digest (the
"token or digest" rule), and the serialised bytes pass through the launch
redactor as a last guard.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from . import errors, inventory, jsonio, merge, profile_policy
from .launch_env import Redactor

RECORD_FORMAT = 1
MAX_RECORD_BYTES = 262144
PENDING_SECTIONS = ["enforcement", "isolation", "network"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def digest_token(text: str) -> str:
    return "h-" + _sha(text)[:32]


def token_or_digest(value: Any, redactor: Optional[Callable[[Any], str]] = None) -> str:
    """The value itself when it is a plain safe name the redactor leaves
    alone, else an ``h-`` digest."""
    text = value if isinstance(value, str) else str(value)
    if (
        errors.is_plain_token(text)
        and (redactor is None or redactor(text) == text)
    ):
        return text
    return digest_token(text)


def _tokens(items: Optional[Iterable[Any]]) -> List[str]:
    return sorted({token_or_digest(item) for item in (items or ())})


def _digests(items: Optional[Iterable[Any]]) -> List[str]:
    return sorted({digest_token(str(item)) for item in (items or ())})


def _restriction(items: Optional[Iterable[Any]], realpath: bool = False) -> Optional[List[str]]:
    if items is None:
        return None
    return sorted({digest_token(os.path.realpath(str(i)) if realpath else str(i)) for i in items})


def _value(effective: Any, key: str, default: Any) -> Any:
    entry = effective.values.get("policy." + key)
    return default if entry is None else entry.value


def policy_section(effective: Any) -> Dict[str, Any]:
    """The policy a launch ran under, safe to persist."""
    view = profile_policy.view(effective)
    defaults = getattr(merge, "_ENUM_DEFAULTS", {})
    out: Dict[str, Any] = {
        "classification": view.classification,
        "sharing": _value(effective, "sharing", defaults.get("sharing", "deny")),
        "external_writes": _value(effective, "external_writes", defaults.get("external_writes", "deny")),
        "isolation": _value(effective, "isolation", defaults.get("isolation", "managed")),
        "cross_profile_fallback": bool(_value(effective, "cross_profile_fallback", False)),
        "allowed_providers": _tokens(_value(effective, "allowed_providers", ())),
        "denied_providers": _tokens(_value(effective, "denied_providers", ())),
        "allowed_hosts": _digests(_value(effective, "allowed_hosts", ())),
        "denied_hosts": _digests(_value(effective, "denied_hosts", ())),
        "fallback": view.fallback,
        "web_tools": view.web_tools,
        "package_install": view.package_install,
        "mcp.local": view.mcp_local,
        "mcp.remote": view.mcp_remote,
        "mcp.allowed": _tokens(view.mcp_allowed),
        "lsp.mode": view.lsp_mode,
        "lsp.allowed": _tokens(view.lsp_allowed),
        "formatter.mode": view.formatter_mode,
        "formatter.allowed": _tokens(view.formatter_allowed),
        "network.identity_check": view.identity_check,
        "network.allowed_addresses": _restriction(view.allowed_addresses),
        "network.pins": _restriction(view.pins),
        "repositories.allowed_roots": _restriction(view.repository_roots, realpath=True),
        "logging.content": view.logging_content,
    }
    for kind in profile_policy.RETENTION_KINDS:
        out["retention." + kind] = view.retention.get(kind)
    return out


def entries_for(
    inv: inventory.Inventory, verdicts: Sequence[inventory.Verdict],
    redactor: Optional[Callable[[Any], str]] = None,
) -> List[Dict[str, Any]]:
    judged = {v.entry_index: v for v in verdicts if v.entry_index >= 0}
    out: List[Dict[str, Any]] = []
    for position, entry in enumerate(inv.entries):
        verdict = judged.get(position)
        out.append({
            "kind": entry.kind,
            "layer": entry.layer,
            "name": token_or_digest(entry.name, redactor),
            "value_sha256": entry.value_sha256,
            "owned": bool(entry.owned),
            "verdict": verdict.verdict if verdict is not None else "recorded",
            "code": verdict.code if verdict is not None else None,
        })
    return out


def _shown(display: str) -> str:
    """A location as ``~/...``, ``./...`` or a label; an absolute path the
    display roots did not cover becomes a digest."""
    return digest_token(display) if os.path.isabs(display) else display


def _sources_for(inv: inventory.Inventory) -> List[Dict[str, Any]]:
    return [
        {"kind": s.kind, "layer": s.layer, "display": _shown(s.display), "sha256": s.sha256, "state": s.state}
        for s in inv.sources
    ]


def digest(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "digest"}
    return "sha256:" + hashlib.sha256(jsonio.dump_canonical(body)).hexdigest()


def build(
    *,
    inventory: "inventory.Inventory",  # noqa: F811 - the parameter name is the documented keyword
    verdicts: Sequence["inventory.Verdict"],
    profile: str,
    classification: str,
    opencode_version: str,
    compiled_digest: str,
    native_sha256: str,
    env_names: Iterable[str],
    launcher_names: Iterable[str],
    stripped_names: Iterable[str],
    dirs_display: Mapping[str, str],
    credentials: Sequence[Mapping[str, str]],
    policy: Mapping[str, Any],
    stripped_other_count: int = 0,
    redactor: Optional[Callable[[Any], str]] = None,
    isolation: Optional[Mapping[str, Any]] = None,
    network: Optional[Mapping[str, Any]] = None,
    enforcement: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """The record. When all of `isolation`, `network` and `enforcement` are
    given the three sections are filled and none is pending; otherwise each
    missing section keeps its pending placeholder."""
    inv = inventory
    entries = entries_for(inv, verdicts, redactor)
    sources = _sources_for(inv)
    record: Dict[str, Any] = {
        "record_format": RECORD_FORMAT,
        "opencode_version": opencode_version,
        "profile": profile,
        "classification": classification,
        "sources": sources,
        "entries": entries,
        "entries_sha256": hashlib.sha256(jsonio.dump_canonical(entries)).hexdigest(),
        "compiled": {"digest": compiled_digest, "native_sha256": native_sha256},
        "env": {
            "names": sorted(set(env_names)),
            "launcher_set": sorted(set(launcher_names)),
            "stripped": sorted(set(stripped_names)),
            "stripped_other_count": int(stripped_other_count),
        },
        "dirs": dict(dirs_display),
        "isolation": dict(isolation) if isolation is not None else {"status": "pending", "evidence": [], "missing": []},
        "network": (
            dict(network) if network is not None
            else {"status": "pending", "destinations": [], "identity_check": policy.get("network.identity_check")}
        ),
        "enforcement": [dict(r) for r in enforcement] if enforcement is not None else [],
        "pending_sections": [
            name for name, given in (("enforcement", enforcement), ("isolation", isolation), ("network", network))
            if given is None
        ],
        "credentials": [dict(c) for c in credentials],
        "static_key_in_use": any(c.get("kind") == "static-api-key" for c in credentials),
        "logging": {"content": policy.get("logging.content")},
        "retention": {k[len("retention."):]: policy.get(k) for k in policy if k.startswith("retention.")},
        "policy": dict(policy),
    }
    if len(jsonio.dump_canonical(record)) > MAX_RECORD_BYTES:
        by_kind: Dict[str, int] = {}
        for item in entries:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        record["entries"] = {"count": len(entries), "by_kind": dict(sorted(by_kind.items())), "truncated": True}
        record["sources"] = {"count": len(sources), "truncated": True}
    record["digest"] = digest(record)
    return record


def serialise(record: Mapping[str, Any], redactor: Optional[Callable[[Any], str]] = None) -> bytes:
    """Canonical bytes passed through the redactor. For a record built by
    ``build`` the redactor changes nothing (the token rule already removed
    secret shapes), so the stored digest stays valid."""
    raw = jsonio.dump_canonical(record).decode("utf-8")
    scrubbed = (redactor if redactor is not None else Redactor())(raw)
    return scrubbed.encode("utf-8")


# ---------------------------------------------------------------- widening

_ENUMS: Dict[str, Sequence[str]] = {**profile_policy.ENUM_ORDER, **profile_policy.LEGACY_ENUM_ORDER}
_GRANT_FIELDS = ("allowed_providers", "allowed_hosts") + profile_policy.GRANT_LISTS
_DENY_FIELDS = ("denied_providers", "denied_hosts")
_RESTRICTION_FIELDS = profile_policy.RESTRICTION_LISTS
_RETENTION_FIELDS = profile_policy.RETENTION_FIELDS


def widened(recorded: Mapping[str, Any], requested: Mapping[str, Any]) -> List[str]:
    """Field names where the requested policy is wider than the recorded one.
    A field missing from the recorded policy cannot be compared and is
    skipped. Only the policy section is compared: every network policy field
    (allowed hosts, allowed providers, the identity check, address ranges,
    pins) is in it, destination approval is a pure function of those, and the
    isolation verdict is an observation of the machine re-evaluated at every
    launch, where a managed requirement already refuses when it is
    unverified. Comparing the observed sections would add instability and no
    protection."""
    out: List[str] = []
    for field_name, order in _ENUMS.items():
        if field_name not in recorded or field_name not in requested:
            continue
        before, after = recorded[field_name], requested[field_name]
        if before == after:
            continue
        if before in order and after in order:
            if order.index(after) > order.index(before):
                out.append(field_name)
        else:
            out.append(field_name)
    if "cross_profile_fallback" in recorded and "cross_profile_fallback" in requested:
        if bool(requested["cross_profile_fallback"]) and not bool(recorded["cross_profile_fallback"]):
            out.append("cross_profile_fallback")
    for field_name in _GRANT_FIELDS:
        if field_name in recorded and field_name in requested:
            if set(requested[field_name] or ()) - set(recorded[field_name] or ()):
                out.append(field_name)
    for field_name in _DENY_FIELDS:
        if field_name in recorded and field_name in requested:
            if set(recorded[field_name] or ()) - set(requested[field_name] or ()):
                out.append(field_name)
    for field_name in _RESTRICTION_FIELDS:
        if field_name in recorded and field_name in requested:
            before, after = recorded[field_name], requested[field_name]
            if before is not None and (after is None or set(after) - set(before)):
                out.append(field_name)
    for field_name in _RETENTION_FIELDS:
        if field_name in recorded and field_name in requested:
            before, after = recorded[field_name], requested[field_name]
            if before is not None and (after is None or after > before):
                out.append(field_name)
    if recorded.get("classification") == "work" and requested.get("classification") == "personal":
        out.append("classification")
    return out
