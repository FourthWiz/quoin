"""The approved-write path: human approval, a journaled writer, reconciliation.

Three steps, run outside the agent's session:

* `approve` shows a proposal in full on the controlling terminal and, once
  the person types back a one-time code shown only there, signs the
  proposal's hash with the approval key (HMAC-SHA256) into a single-use
  approval that expires.
* `execute` loads the proposal and the approval, verifies the proposal is
  intact, the approval's signature, binding and expiry, that the operation
  is an enabled write and that the approval was not used before. It then
  records the intent in its journal before it sends anything, sends once
  with the write credential, and records the service's receipt.
* `reconcile` resolves a write whose outcome is unknown by asking the
  service for the write's idempotency marker. It never sends again.

After a timeout, a server error or a crash between recording intent and
recording the result, the writer does not know whether the service acted.
It looks the write up; if the service does not confirm it, the journal
records OUTCOME_UNKNOWN and nothing is resent until a person either finds
the write or confirms it was not sent. Remote delivery is therefore at most
once as far as the journal can tell, not exactly once: a service that
accepted a write but cannot find it by its marker stays unknown.

None of this is a security boundary on its own. The approval key and the
write credentials must be out of the agent's reach: the configuration's
`writes.approval_boundary` declares how the deployment provides that, and
the refusal to run inside a launcher session and the terminal code only
prevent accidents: a process that can read the approval key can approve.
Journal records carry a MAC derived from the approval key, so a record
edited by hand is refused rather than trusted.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets as _stdlib_secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

from ..opencode_adapter import jsonio
from ..opencode_adapter import paths as oa_paths
from ..opencode_adapter.launch_env import Redactor
from ..opencode_adapter.secrets import CredentialResolver, SecretResolutionError
from . import backends as backend_registry
from . import config as config_mod
from . import operations, policy
from . import proposal as proposal_mod
from .envelope import (
    AUTH_REQUIRED,
    INVALID_INPUT,
    OUTCOME_UNKNOWN,
    POLICY_DENIED,
    UNAVAILABLE,
    BrokerError,
    failure,
    success,
)
from .transport import HttpTransport, scrub_environment

APPROVAL_SCHEMA = "workctl.approval/1"
APPROVAL_FIELDS = (
    "schema",
    "proposal_id",
    "content_hash",
    "idempotency_key",
    "nonce",
    "approved_at",
    "expires_at",
    "mac",
)
MIN_KEY_BYTES = 32
MAX_APPROVAL_BYTES = 4096
CHALLENGE_BYTES = 3
CLOCK_SKEW_SECONDS = 300
LOOKUP_MARGIN_SECONDS = 300

PENDING, DONE, FAILED, UNKNOWN = "PENDING", "DONE", "FAILED", "UNKNOWN"
IDEMPOTENCY_RE = re.compile(r"[0-9a-f]{32}")


# --- approval tokens ---------------------------------------------------------


def _mac(key: bytes, token: Mapping[str, Any]) -> str:
    body = {k: v for k, v in token.items() if k != "mac"}
    return hmac.new(key, proposal_mod.canonical(body), hashlib.sha256).hexdigest()


def mint(proposal: Mapping[str, Any], key: bytes, *, now: float, ttl_seconds: int) -> Dict[str, Any]:
    expires = min(now + ttl_seconds, proposal_mod.parse_iso(proposal["expires_at"]))
    token: Dict[str, Any] = {
        "schema": APPROVAL_SCHEMA,
        "proposal_id": proposal["proposal_id"],
        "content_hash": proposal["content_hash"],
        "idempotency_key": proposal["idempotency_key"],
        "nonce": _stdlib_secrets.token_hex(16),
        "approved_at": proposal_mod.iso(now),
        "expires_at": proposal_mod.iso(expires),
    }
    token["mac"] = _mac(key, token)
    return token


def load_approval(path: Any) -> Dict[str, Any]:
    if not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path):
        raise BrokerError(INVALID_INPUT, "give the approval file")
    got = jsonio.read_regular_bytes(path, max_bytes=MAX_APPROVAL_BYTES)
    if got is None:
        raise BrokerError(INVALID_INPUT, "the approval file is missing, too large, a symlink or not a regular file")
    try:
        data = json.loads(got[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BrokerError(INVALID_INPUT, "the approval file is not valid JSON")
    if (
        not isinstance(data, dict)
        or set(data) != set(APPROVAL_FIELDS)
        or data.get("schema") != APPROVAL_SCHEMA
        or not all(isinstance(data[k], str) for k in APPROVAL_FIELDS)
    ):
        raise BrokerError(POLICY_DENIED, "the approval file does not have the approval shape")
    return data


def verify(token: Mapping[str, Any], proposal: Mapping[str, Any], key: bytes, *, now: float) -> None:
    """Refuse an approval that was not signed with the approval key, that
    belongs to other content, or that has expired."""
    if not hmac.compare_digest(_mac(key, token), token["mac"]):
        raise BrokerError(POLICY_DENIED, "the approval's signature is not valid")
    for name in ("proposal_id", "content_hash", "idempotency_key"):
        if not hmac.compare_digest(str(token[name]), str(proposal[name])):
            raise BrokerError(POLICY_DENIED, "the approval was given for different content or a different target")
    if now >= proposal_mod.parse_iso(token["expires_at"]):
        raise BrokerError(POLICY_DENIED, "the approval has expired; approve the proposal again")


# --- journal -----------------------------------------------------------------


class Journal:
    """One private record per idempotency key, rewritten atomically at
    every state change, plus a lock file so two writers never act on the
    same proposal at once."""

    def __init__(self, directory: Path, approval_key: bytes) -> None:
        self.directory = Path(directory)
        self._key = hmac.new(approval_key, b"workctl-journal/1", hashlib.sha256).digest()
        jsonio.ensure_private_directory(self.directory)

    def _mac(self, record: Mapping[str, Any]) -> str:
        return _mac(self._key, record)

    def _path(self, key: str, suffix: str) -> Path:
        if not IDEMPOTENCY_RE.fullmatch(key):
            raise BrokerError(INVALID_INPUT, "not a valid idempotency key")
        return self.directory / (key + suffix)

    @contextlib.contextmanager
    def lock(self, key: str) -> Iterator[None]:
        path = self._path(key, ".lock")
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BrokerError(POLICY_DENIED, "another writer is working on this proposal", retryable=True)
            yield
        finally:
            os.close(fd)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        got = jsonio.read_regular_bytes(self._path(key, ".json"), max_bytes=proposal_mod.MAX_PROPOSAL_BYTES * 2)
        if got is None:
            return None
        if got[1].st_uid != os.getuid():
            raise BrokerError(POLICY_DENIED, "the journal record is not owned by the current user")
        try:
            record = json.loads(got[0].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BrokerError(OUTCOME_UNKNOWN, "the journal record is unreadable; reconcile by hand")
        if (
            not isinstance(record, dict)
            or record.get("idempotency_key") != key
            or not isinstance(record.get("mac"), str)
            or not hmac.compare_digest(self._mac(record), record["mac"])
        ):
            raise BrokerError(OUTCOME_UNKNOWN, "the journal record is damaged or was edited; reconcile by hand")
        record.pop("mac")
        return record

    def put(self, record: Mapping[str, Any]) -> None:
        signed = dict(record)
        signed.pop("mac", None)
        signed["mac"] = self._mac(signed)
        data = json.dumps(signed, sort_keys=True, ensure_ascii=False).encode("utf-8")
        jsonio.write_private_atomic(self._path(record["idempotency_key"], ".json"), data)


def journal_dir(home: Path) -> Path:
    """Fixed under the writer account's home, so no setting can point the
    writer at an empty journal and make a spent approval look fresh."""
    return Path(home) / ".local" / "state" / "quoin" / "workctl" / "journal"


# --- shared helpers ----------------------------------------------------------


def refuse_agent_session(env: Mapping[str, str]) -> None:
    """The approver and writer run outside the agent. A launcher session
    marks its environment; refusing there prevents accidental use but is
    not the boundary itself."""
    if env.get(oa_paths.ENV_STATE_DIR):
        raise BrokerError(
            POLICY_DENIED,
            "the approved-write commands do not run inside an agent session; run them in your own terminal",
        )


def _resolve(resolver: CredentialResolver, ref: Any, what: str) -> str:
    try:
        return resolver.resolve(ref).reveal()
    except SecretResolutionError as exc:
        raise BrokerError(AUTH_REQUIRED, "the %s could not be resolved (%s)" % (what, exc.code))


def _approval_key(cfg: config_mod.BrokerConfig, resolver: CredentialResolver, redact: Redactor) -> bytes:
    value = _resolve(resolver, cfg.writes.approval_key, "approval key")
    redact.add(value)
    key = value.encode("utf-8")
    if len(key) < MIN_KEY_BYTES:
        raise BrokerError(AUTH_REQUIRED, "the approval key must be at least %d bytes" % MIN_KEY_BYTES)
    return key


def _check_binding(
    proposal: Mapping[str, Any], svc: config_mod.ServiceConfig, cfg: config_mod.BrokerConfig, now: float
) -> None:
    if (proposal["backend"], proposal["tenant"], proposal["account"]) != (svc.backend, svc.tenant, svc.account):
        raise BrokerError(POLICY_DENIED, "the proposal is bound to a different backend, tenant or account")
    created = proposal_mod.parse_iso(proposal["created_at"])
    expires = proposal_mod.parse_iso(proposal["expires_at"])
    # A proposal file is plain JSON, so its times are only trusted within
    # the configured lifetime and the writer's own clock.
    if created > now + CLOCK_SKEW_SECONDS or not created < expires <= created + cfg.writes.proposal_ttl_seconds:
        raise BrokerError(POLICY_DENIED, "the proposal's times are outside the configured proposal lifetime")
    if now >= expires:
        raise BrokerError(POLICY_DENIED, "the proposal has expired; prepare a new one")


def _load(
    proposal_path: Any, *, home: Optional[Path], config_path: Optional[Path]
) -> "tuple[Path, config_mod.BrokerConfig, Dict[str, Any], operations.Operation, config_mod.ServiceConfig]":
    home = Path(home) if home is not None else config_mod.real_home()
    cfg = config_mod.load(
        Path(config_path) if config_path is not None else config_mod.default_path(home),
        backend_registry.BACKENDS_FOR_SERVICE,
    )
    prop = proposal_mod.load(proposal_path)
    op = operations.OPERATIONS[prop["operation"]]
    svc = policy.authorize_write(op, cfg)
    return home, cfg, prop, op, svc


def _default_resolver(resolver: Optional[CredentialResolver], platform: str) -> CredentialResolver:
    if resolver is not None:
        return resolver
    from .broker import default_resolver

    return default_resolver(platform)


def _now_iso(clock: Callable[[], float]) -> str:
    return proposal_mod.iso(clock())


def _envelope_error(exc: BrokerError, redact: Redactor, request_id: str, provenance=None) -> Dict[str, Any]:
    exc.message = redact(exc.message)
    return failure(exc, request_id=request_id, provenance=provenance)


def _save_new(path: Path, data: Mapping[str, Any]) -> None:
    raw = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise BrokerError(INVALID_INPUT, "the approval file already exists; approvals are never overwritten")
    except OSError:
        raise BrokerError(INVALID_INPUT, "the approval file cannot be created")
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)


def _open_terminal() -> Any:
    """The controlling terminal, never stdin or stdout, so redirected
    output stays one envelope and a pipe cannot answer the prompt."""
    try:
        return open("/dev/tty", "r+", encoding="utf-8", errors="replace")
    except OSError:
        raise BrokerError(POLICY_DENIED, "this step needs a person at an interactive terminal")


def _ask(terminal: Any, text: str) -> str:
    terminal.write(text)
    terminal.flush()
    answer = terminal.readline()
    return answer.strip() if isinstance(answer, str) else ""


def _receipt_json(receipt: Any) -> Dict[str, Any]:
    return {"remote_id": receipt.remote_id, "url": receipt.url, "extra": dict(receipt.extra or {})}


def _public(record: Mapping[str, Any]) -> Dict[str, Any]:
    """A journal record without the proposal content."""
    return {k: v for k, v in record.items() if k not in ("proposal", "nonces")}


# --- approve -----------------------------------------------------------------


def approve(
    proposal_path: Any,
    *,
    out: Optional[str] = None,
    env: Mapping[str, str],
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    resolver: Optional[CredentialResolver] = None,
    clock: Callable[[], float] = time.time,
    platform: str = sys.platform,
    terminal: Any = None,
) -> Dict[str, Any]:
    request_id = uuid.uuid4().hex
    redact = Redactor()
    opened = None
    try:
        refuse_agent_session(env)
        _, cfg, prop, op, svc = _load(proposal_path, home=home, config_path=config_path)
        now = clock()
        _check_binding(prop, svc, cfg, now)
        if terminal is None:
            terminal = opened = _open_terminal()
        challenge = _stdlib_secrets.token_hex(CHALLENGE_BYTES)
        answer = _ask(
            terminal,
            proposal_mod.describe(prop) + "\n\nTo approve exactly this write, type %s: " % challenge,
        )
        if not hmac.compare_digest(answer.lower(), challenge):
            raise BrokerError(POLICY_DENIED, "the write was not approved")
        key = _approval_key(cfg, _default_resolver(resolver, platform), redact)
        token = mint(prop, key, now=now, ttl_seconds=cfg.writes.approval_ttl_seconds)
        target = Path(out) if out else Path(str(proposal_path) + ".approval.json")
        _save_new(target, token)
        return success(
            {
                "approval_file": str(target),
                "proposal_id": prop["proposal_id"],
                "operation": op.name,
                "expires_at": token["expires_at"],
            },
            pagination=None,
            provenance={"service": op.service, "tenant": svc.tenant, "account": svc.account},
            request_id=request_id,
        )
    except BrokerError as exc:
        return _envelope_error(exc, redact, request_id)
    except Exception:  # noqa: BLE001
        return failure(BrokerError(UNAVAILABLE, "internal writer error", retryable=False), request_id=request_id)
    finally:
        if opened is not None:
            opened.close()


# --- execute and reconcile ---------------------------------------------------


def _context(
    cfg: config_mod.BrokerConfig,
    svc: config_mod.ServiceConfig,
    backend: Any,
    *,
    resolver: CredentialResolver,
    transport: Any,
    clock: Callable[[], float],
    redact: Redactor,
) -> backend_registry.Context:
    credential = None
    if svc.backend in backend_registry.CREDENTIAL_BACKENDS:
        ref = cfg.writes.credentials.get(svc.service)
        if ref is None:
            raise BrokerError(POLICY_DENIED, "writes.credentials has no write credential for %s" % svc.service)
        try:
            credential = resolver.resolve(ref)
        except SecretResolutionError as exc:
            raise BrokerError(AUTH_REQUIRED, "the write credential could not be resolved (%s)" % exc.code)
        redact.add(credential.reveal())
    return backend_registry.Context(
        service=svc,
        limits=cfg.limits,
        transport=transport
        if transport is not None
        else HttpTransport(https_proxy=cfg.network.https_proxy, ca_file=cfg.network.ca_file),
        credential=credential,
        fetched_at=_now_iso(clock),
        origin=backend_registry.origin_of(svc.base_url or getattr(backend, "default_base", None)),
    )


def _writer_backend(svc: config_mod.ServiceConfig, backends: Optional[Mapping[str, Any]]) -> Any:
    backend = (backends or {}).get(svc.backend) or backend_registry.load(svc.backend)
    if not callable(getattr(backend, "write", None)):
        raise BrokerError(POLICY_DENIED, "the %s backend does not support writes" % svc.backend)
    return backend


def _provenance(op: operations.Operation, svc: config_mod.ServiceConfig, params: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "service": op.service,
        "backend": svc.backend,
        "tenant": svc.tenant,
        "account": svc.account,
        "resource_id": operations.resource_id(op, params),
    }


def _done(record: Dict[str, Any], receipt: Any, journal: Journal, clock, request_id, prov, *, reconciled: bool):
    record.update(state=DONE, receipt=_receipt_json(receipt), error=None, updated_at=_now_iso(clock))
    try:
        journal.put(record)
    except Exception:  # noqa: BLE001 - the write happened; say so even if the journal cannot
        err = BrokerError(
            OUTCOME_UNKNOWN,
            "the service accepted the write (receipt %s) but the journal could not record it; do not resend"
            % receipt.remote_id,
        )
        return failure(err, request_id=request_id, provenance=dict(prov, url=receipt.url))
    data = dict(_public(record), reconciled=reconciled)
    return success(data, pagination=None, provenance=dict(prov, url=receipt.url), request_id=request_id)


def _look_up(
    record: Dict[str, Any],
    op: operations.Operation,
    backend: Any,
    ctx: backend_registry.Context,
    journal: Journal,
    clock: Callable[[], float],
    request_id: str,
    prov: Dict[str, Any],
) -> Dict[str, Any]:
    """Ask the service whether the write happened. Found: record the
    receipt. Otherwise the outcome stays unknown; nothing is resent."""
    record["lookups"] = int(record.get("lookups", 0)) + 1
    found = None
    lookup = getattr(backend, "lookup", None)
    if callable(lookup):
        try:
            found = lookup(
                op.name,
                record["proposal"]["params"],
                ctx,
                marker=record["idempotency_key"],
                since=proposal_mod.parse_iso(record["started_at"]) - LOOKUP_MARGIN_SECONDS,
            )
        except BrokerError as exc:
            record["last_lookup_error"] = exc.code
        except Exception:  # noqa: BLE001 - an unreadable answer is not a confirmation
            record["last_lookup_error"] = UNAVAILABLE
    if found is not None:
        return _done(record, found, journal, clock, request_id, prov, reconciled=True)
    record.update(state=UNKNOWN, updated_at=_now_iso(clock))
    journal.put(record)
    err = BrokerError(
        OUTCOME_UNKNOWN,
        "the service did not confirm the write and it will not be resent; check the service, then run "
        "`workctl-write reconcile %s` (add --confirm-not-sent only if you verified it was not sent)"
        % record["idempotency_key"],
    )
    return failure(err, request_id=request_id, provenance=prov)


def execute(
    proposal_path: Any,
    approval_path: Any,
    *,
    env: Mapping[str, str],
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    transport: Any = None,
    resolver: Optional[CredentialResolver] = None,
    backends: Optional[Mapping[str, Any]] = None,
    clock: Callable[[], float] = time.time,
    platform: str = sys.platform,
) -> Dict[str, Any]:
    request_id = uuid.uuid4().hex
    scrub_environment()
    redact = Redactor()
    prov = None
    try:
        refuse_agent_session(env)
        home, cfg, prop, op, svc = _load(proposal_path, home=home, config_path=config_path)
        prov = _provenance(op, svc, prop["params"])
        now = clock()
        _check_binding(prop, svc, cfg, now)
        resolver = _default_resolver(resolver, platform)
        token = load_approval(approval_path)
        approval_key = _approval_key(cfg, resolver, redact)
        verify(token, prop, approval_key, now=now)
        backend = _writer_backend(svc, backends)
        journal = Journal(journal_dir(home), approval_key)
        key = prop["idempotency_key"]
        with journal.lock(key):
            record = journal.get(key)
            if record is not None and record.get("content_hash") != prop["content_hash"]:
                raise BrokerError(POLICY_DENIED, "the journal holds different content under this idempotency key")
            state = record.get("state") if record else None
            if state in (PENDING, UNKNOWN):
                # An earlier attempt may have reached the service: look it up, never resend.
                ctx = _context(cfg, svc, backend, resolver=resolver, transport=transport, clock=clock, redact=redact)
                return _look_up(record, op, backend, ctx, journal, clock, request_id, prov)
            if state == DONE:
                raise BrokerError(
                    POLICY_DENIED,
                    "this proposal was already executed (receipt %s); the approval cannot be used again"
                    % (record.get("receipt") or {}).get("remote_id"),
                )
            if record is not None and token["nonce"] in record.get("nonces", []):
                raise BrokerError(POLICY_DENIED, "this approval was already used; approve the proposal again")
            ctx = _context(cfg, svc, backend, resolver=resolver, transport=transport, clock=clock, redact=redact)
            record = record or {
                "idempotency_key": key,
                "proposal_id": prop["proposal_id"],
                "content_hash": prop["content_hash"],
                "operation": op.name,
                "proposal": prop,
                "nonces": [],
                "attempts": 0,
                "lookups": 0,
                "receipt": None,
                "error": None,
            }
            record["nonces"] = list(record.get("nonces", [])) + [token["nonce"]]
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record.update(state=PENDING, error=None, started_at=_now_iso(clock), updated_at=_now_iso(clock))
            journal.put(record)  # intent is durable before anything is sent
            try:
                receipt = backend.write(op.name, prop["params"], ctx, marker=key)
            except BrokerError as exc:
                if exc.code in (UNAVAILABLE, OUTCOME_UNKNOWN):
                    record["last_send_error"] = exc.code
                    return _look_up(record, op, backend, ctx, journal, clock, request_id, prov)
                record.update(state=FAILED, error=exc.code, updated_at=_now_iso(clock))
                journal.put(record)
                raise
            except Exception:  # noqa: BLE001 - the request may have left; treat as unknown
                record["last_send_error"] = "internal"
                return _look_up(record, op, backend, ctx, journal, clock, request_id, prov)
            return _done(record, receipt, journal, clock, request_id, prov, reconciled=False)
    except BrokerError as exc:
        return _envelope_error(exc, redact, request_id, prov)
    except jsonio.UnsafeDirectoryError:
        return failure(BrokerError(POLICY_DENIED, "the writer journal directory is not private"), request_id=request_id)
    except Exception:  # noqa: BLE001
        return failure(BrokerError(UNAVAILABLE, "internal writer error", retryable=False), request_id=request_id)


def reconcile(
    key: str,
    *,
    env: Mapping[str, str],
    confirm_not_sent: bool = False,
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    transport: Any = None,
    resolver: Optional[CredentialResolver] = None,
    backends: Optional[Mapping[str, Any]] = None,
    clock: Callable[[], float] = time.time,
    platform: str = sys.platform,
    terminal: Any = None,
) -> Dict[str, Any]:
    """Resolve an unknown outcome. Without `confirm_not_sent` this only
    looks the write up. With it, a person who checked the service records
    that the write did not happen, which lets a fresh approval send it;
    that is allowed only after a lookup has failed to find the write."""
    request_id = uuid.uuid4().hex
    scrub_environment()
    redact = Redactor()
    prov = None
    opened = None
    try:
        refuse_agent_session(env)
        home = Path(home) if home is not None else config_mod.real_home()
        cfg = config_mod.load(
            Path(config_path) if config_path is not None else config_mod.default_path(home),
            backend_registry.BACKENDS_FOR_SERVICE,
        )
        if cfg is None or cfg.writes.approval_key is None:
            raise BrokerError(POLICY_DENIED, policy.WRITES_DISABLED)
        resolver = _default_resolver(resolver, platform)
        journal = Journal(journal_dir(home), _approval_key(cfg, resolver, redact))
        with journal.lock(key):
            record = journal.get(key)
            if record is None:
                raise BrokerError(INVALID_INPUT, "the journal has no write with this idempotency key")
            op = operations.OPERATIONS[record["operation"]]
            if record.get("state") not in (PENDING, UNKNOWN):
                return success(_public(record), pagination=None, provenance=None, request_id=request_id)
            if confirm_not_sent:
                if record.get("state") != UNKNOWN or int(record.get("lookups", 0)) < 1:
                    raise BrokerError(
                        POLICY_DENIED, "look the write up first (run reconcile without --confirm-not-sent)"
                    )
                if terminal is None:
                    terminal = opened = _open_terminal()
                answer = _ask(terminal, "Type 'not sent' to record that this write did not reach the service: ")
                if answer.lower() != "not sent":
                    raise BrokerError(POLICY_DENIED, "nothing was recorded")
                record.update(state=FAILED, error="CONFIRMED_NOT_SENT", updated_at=_now_iso(clock))
                journal.put(record)
                return success(_public(record), pagination=None, provenance=None, request_id=request_id)
            svc = policy.authorize_write(op, cfg)
            prov = _provenance(op, svc, record["proposal"]["params"])
            backend = _writer_backend(svc, backends)
            ctx = _context(cfg, svc, backend, resolver=resolver, transport=transport, clock=clock, redact=redact)
            return _look_up(record, op, backend, ctx, journal, clock, request_id, prov)
    except BrokerError as exc:
        return _envelope_error(exc, redact, request_id, prov)
    except jsonio.UnsafeDirectoryError:
        return failure(BrokerError(POLICY_DENIED, "the writer journal directory is not private"), request_id=request_id)
    except Exception:  # noqa: BLE001
        return failure(BrokerError(UNAVAILABLE, "internal writer error", retryable=False), request_id=request_id)
    finally:
        if opened is not None:
            opened.close()


def status(
    key: str,
    *,
    env: Mapping[str, str],
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    resolver: Optional[CredentialResolver] = None,
    platform: str = sys.platform,
) -> Dict[str, Any]:
    request_id = uuid.uuid4().hex
    redact = Redactor()
    try:
        refuse_agent_session(env)
        home = Path(home) if home is not None else config_mod.real_home()
        cfg = config_mod.load(
            Path(config_path) if config_path is not None else config_mod.default_path(home),
            backend_registry.BACKENDS_FOR_SERVICE,
        )
        if cfg is None or cfg.writes.approval_key is None:
            raise BrokerError(POLICY_DENIED, policy.WRITES_DISABLED)
        key_bytes = _approval_key(cfg, _default_resolver(resolver, platform), redact)
        record = Journal(journal_dir(home), key_bytes).get(key)
        if record is None:
            raise BrokerError(INVALID_INPUT, "the journal has no write with this idempotency key")
        return success(_public(record), pagination=None, provenance=None, request_id=request_id)
    except BrokerError as exc:
        return _envelope_error(exc, redact, request_id)
    except jsonio.UnsafeDirectoryError:
        return failure(BrokerError(POLICY_DENIED, "the writer journal directory is not private"), request_id=request_id)
    except Exception:  # noqa: BLE001
        return failure(BrokerError(UNAVAILABLE, "internal writer error", retryable=False), request_id=request_id)
