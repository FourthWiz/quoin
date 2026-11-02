"""Immutable action proposals: the local draft of one external write.

A proposal records everything a write will do: the operation, the service,
backend, tenant and account it is bound to, the exact target, the full
content, attachments, when it was made, when it expires and an idempotency
key. `content_hash` is the SHA-256 of the canonical JSON of every other
field, and an approval signs that hash, so changing the content, the
destination or any binding after approval makes the approval useless.

Preparing a proposal is the only write step an agent may take. It reads
the broker configuration to bind the tenant and account, but touches no
credential and no network. Whether or not writes are enabled, the proposal
file is a complete local draft the user can submit by hand.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets as _stdlib_secrets
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from ..opencode_adapter import jsonio
from . import backends as backend_registry
from . import config as config_mod
from . import operations, policy
from .envelope import INVALID_INPUT, POLICY_DENIED, UNAVAILABLE, BrokerError, failure, success

SCHEMA = "workctl.proposal/1"
MAX_PROPOSAL_BYTES = 256 * 1024
FIELDS = (
    "schema",
    "proposal_id",
    "operation",
    "service",
    "backend",
    "tenant",
    "account",
    "params",
    "attachments",
    "created_at",
    "expires_at",
    "idempotency_key",
    "content_hash",
)


def iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Any) -> float:
    if not isinstance(text, str):
        raise BrokerError(POLICY_DENIED, "timestamp is missing")
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        raise BrokerError(POLICY_DENIED, "timestamp is not valid")


def canonical(obj: Any) -> bytes:
    return jsonio.dump_canonical(obj)


def content_hash(proposal: Mapping[str, Any]) -> str:
    body = {k: v for k, v in proposal.items() if k != "content_hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def build(
    op: operations.Operation,
    params: Mapping[str, Any],
    service: config_mod.ServiceConfig,
    *,
    now: float,
    ttl_seconds: int,
) -> Dict[str, Any]:
    proposal: Dict[str, Any] = {
        "schema": SCHEMA,
        "proposal_id": uuid.uuid4().hex,
        "operation": op.name,
        "service": op.service,
        "backend": service.backend,
        "tenant": service.tenant,
        "account": service.account,
        "params": dict(params),
        "attachments": [],
        "created_at": iso(now),
        "expires_at": iso(now + ttl_seconds),
        "idempotency_key": _stdlib_secrets.token_hex(16),
    }
    proposal["content_hash"] = content_hash(proposal)
    return proposal


def _no_duplicates(pairs):
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def load(path: Any) -> Dict[str, Any]:
    """Read a proposal and verify it is intact. A proposal edited after it
    was made no longer matches its hash and is refused, whoever edited it."""
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
        raise BrokerError(INVALID_INPUT, "give the proposal file")
    got = jsonio.read_regular_bytes(path, max_bytes=MAX_PROPOSAL_BYTES)
    if got is None:
        raise BrokerError(INVALID_INPUT, "the proposal file is missing, too large, a symlink or not a regular file")
    try:
        data = json.loads(got[0].decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError):
        raise BrokerError(INVALID_INPUT, "the proposal file is not valid JSON")
    if not isinstance(data, dict) or set(data) != set(FIELDS) or data.get("schema") != SCHEMA:
        raise BrokerError(POLICY_DENIED, "the proposal file does not have the proposal shape")
    if not isinstance(data["content_hash"], str) or data["content_hash"] != content_hash(data):
        raise BrokerError(POLICY_DENIED, "the proposal was changed after it was made; prepare and approve a new one")
    if data["operation"] not in operations.WRITE_OPERATIONS:
        raise BrokerError(POLICY_DENIED, "the proposal names an operation that is not a write")
    op = operations.OPERATIONS[data["operation"]]
    if data["service"] != op.service or not isinstance(data["params"], dict):
        raise BrokerError(POLICY_DENIED, "the proposal's service does not match its operation")
    if data["params"] != operations.normalize_write(op, data["params"]):
        raise BrokerError(POLICY_DENIED, "the proposal's parameters are not in normalized form")
    if data["attachments"] != []:
        raise BrokerError(POLICY_DENIED, "attachments are not supported by the writer")
    parse_iso(data["created_at"])
    parse_iso(data["expires_at"])
    return data


def save(path: Any, proposal: Mapping[str, Any]) -> Path:
    """Write a new proposal file; an existing file is never replaced."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise BrokerError(INVALID_INPUT, "--out must name a new file")
    target = Path(path)
    data = json.dumps(proposal, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise BrokerError(INVALID_INPUT, "--out names an existing file; proposals are never overwritten")
    except OSError:
        raise BrokerError(INVALID_INPUT, "--out cannot be created")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return target


def describe(proposal: Mapping[str, Any]) -> str:
    """A plain-text rendering for the person approving. Invisible and
    control characters are shown escaped, every body line is prefixed so
    the body cannot imitate the frame, and the destination and hash are
    repeated after the body so a long body cannot scroll them away."""
    params = proposal["params"]
    head = [
        "operation:   %s" % proposal["operation"],
        "service:     %s (backend %s, tenant %s, account %s)"
        % (proposal["service"], proposal["backend"], proposal["tenant"], proposal["account"] or "-"),
    ]
    for key in sorted(k for k in params if k != "body"):
        value = params[key]
        if value is None or value == []:
            continue
        head.append("%-12s %s" % (key + ":", _visible(", ".join(value) if isinstance(value, list) else str(value))))
    head.append("expires:     %s" % proposal["expires_at"])
    head.append("hash:        %s" % proposal["content_hash"])
    body = params["body"]
    lines = head + ["body (%d characters, %d lines):" % (len(body), body.count("\n") + 1)]
    lines += ["| " + _visible(line) for line in body.split("\n")]
    lines += ["end of body; this approves:"] + head
    return "\n".join(lines)


_HIDDEN_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))


def _visible(text: str) -> str:
    return "".join(
        "\\u%04x" % ord(ch) if ch != "\t" and unicodedata.category(ch) in _HIDDEN_CATEGORIES else ch for ch in text
    )


def propose(
    op_name: str,
    raw_params: Mapping[str, Any],
    *,
    out: Any,
    env: Mapping[str, str],
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Prepare one proposal and save it as a local draft. Returns the
    envelope; `data.writes_enabled` tells the agent whether the user can
    approve it for the writer or must submit it by hand."""
    from .broker import launcher_root  # local import: broker imports backends

    request_id = uuid.uuid4().hex
    try:
        home = Path(home) if home is not None else config_mod.real_home()
        op = operations.get(op_name)
        cfg = config_mod.load(
            Path(config_path) if config_path is not None else config_mod.default_path(home),
            backend_registry.BACKENDS_FOR_SERVICE,
        )
        root = launcher_root(env)
        svc = policy.authorize_proposal(op, cfg, launcher_profile=root.name if root is not None else None)
        params = operations.normalize_write(op, raw_params)
        proposal = build(op, params, svc, now=clock(), ttl_seconds=cfg.writes.proposal_ttl_seconds)
        path = save(out, proposal)
        enabled = cfg.writes.enabled and op.name in cfg.writes.operations
        next_step = (
            "ask the user to review and approve it outside this session with `workctl-write approve %s`" % path
            if enabled
            else policy.WRITES_DISABLED
        )
        return success(
            {
                "proposal_file": str(path),
                "proposal_id": proposal["proposal_id"],
                "operation": op.name,
                "content_hash": proposal["content_hash"],
                "expires_at": proposal["expires_at"],
                "writes_enabled": enabled,
                "next_step": next_step,
            },
            pagination=None,
            provenance={
                "service": op.service,
                "tenant": svc.tenant,
                "account": svc.account,
                "resource_id": operations.resource_id(op, params),
            },
            request_id=request_id,
        )
    except BrokerError as exc:
        return failure(exc, request_id=request_id)
    except Exception:  # noqa: BLE001 - every failure must still be one envelope
        return failure(BrokerError(UNAVAILABLE, "internal broker error", retryable=False), request_id=request_id)
