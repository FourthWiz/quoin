"""Execute one normalized operation and return its envelope.

`execute` is the only entry point transports use. It loads the broker
configuration, applies the authorization policy, validates parameters,
resolves the read-only credential from the keychain, consults the
profile-bound cache, calls the backend and bounds the result. Diagnostics
name the operation, outcome, counts and timing only; they never contain
retrieved content or credentials.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, TextIO

from ..opencode_adapter import paths as oa_paths
from ..opencode_adapter import profile_dirs
from ..opencode_adapter.install import PROFILE_RE
from ..opencode_adapter.launch_env import Redactor
from ..opencode_adapter.secrets import (
    CredentialResolver,
    MacKeychainBackend,
    SecretResolutionError,
)
from . import backends as backend_registry
from . import cache as cache_mod
from . import config as config_mod
from . import limits as limits_mod
from . import operations, policy
from .envelope import AUTH_REQUIRED, POLICY_DENIED, UNAVAILABLE, BrokerError, failure, success
from .transport import HttpTransport

ENVELOPE_OVERHEAD = 4096
CACHE_SUBDIR = "workctl"


def launcher_root(env: Mapping[str, str]) -> Optional[Path]:
    """The stamped profile root the launcher exported, if any."""
    value = env.get(oa_paths.ENV_STATE_DIR)
    if not value or not os.path.isabs(value):
        return None
    root = Path(value)
    if not PROFILE_RE.fullmatch(root.name):
        return None
    return root if profile_dirs.stamped_classification(root) is not None else None


def _cache_root(env: Mapping[str, str], home: Path, cfg: config_mod.BrokerConfig) -> Optional[Path]:
    root = launcher_root(env)
    if root is None and cfg.profile is not None:
        candidate = profile_dirs.profile_root(profile_dirs.adapter_root(env, home), cfg.profile)
        if profile_dirs.stamped_classification(candidate) is not None:
            root = candidate
    return None if root is None else profile_dirs.ProfileDirs.at(root).cache / CACHE_SUBDIR


def default_resolver(platform: str) -> CredentialResolver:
    # Keychain only: there is deliberately no environment backend, so a
    # broker credential can never be read from the agent's environment.
    return CredentialResolver({"keychain": MacKeychainBackend(platform=platform)})


def _redact_tree(value: Any, redact: Redactor) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_tree(v, redact) for v in value]
    if isinstance(value, dict):
        return {(redact(k) if isinstance(k, str) else k): _redact_tree(v, redact) for k, v in value.items()}
    return value


def _now_iso(clock: Callable[[], float]) -> str:
    return _dt.datetime.fromtimestamp(clock(), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _diag(stream: Optional[TextIO], redact: Redactor, **fields: Any) -> None:
    if stream is None:
        return
    text = " ".join("%s=%s" % (k, v) for k, v in fields.items() if v is not None)
    try:
        stream.write("workctl: %s\n" % redact(text))
        stream.flush()
    except (OSError, ValueError):
        pass


def execute(
    op_name: str,
    raw_params: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    home: Optional[Path] = None,
    config_path: Optional[Path] = None,
    transport: Any = None,
    resolver: Optional[CredentialResolver] = None,
    backends: Optional[Mapping[str, Any]] = None,
    run_command: Any = None,
    clock: Callable[[], float] = time.time,
    use_cache: bool = True,
    platform: str = sys.platform,
    stderr: Optional[TextIO] = None,
) -> Dict[str, Any]:
    request_id = uuid.uuid4().hex
    redact = Redactor()
    started = time.monotonic()
    provenance: Optional[Dict[str, Any]] = None
    try:
        home = Path(home) if home is not None else config_mod.real_home()
        op = operations.get(op_name)
        cfg = config_mod.load(
            Path(config_path) if config_path is not None else config_mod.default_path(home),
            backend_registry.BACKENDS_FOR_SERVICE,
        )
        root = launcher_root(env)
        svc = policy.authorize(op, cfg, launcher_profile=root.name if root is not None else None)
        provenance = {"service": op.service, "tenant": svc.tenant}
        params = operations.normalize(op, raw_params, max_items=cfg.limits.max_items)
        backend = (backends or {}).get(svc.backend) or backend_registry.load(svc.backend)

        cache = None
        entry_key = None
        cache_dir = _cache_root(env, home, cfg) if use_cache else None
        if cache_dir is not None and cfg.limits.cache_ttl_seconds > 0:
            cache = cache_mod.Cache(cache_dir, cfg.limits.cache_ttl_seconds, clock=clock)
            identity = {
                "params": params,
                "backend": svc.backend,
                "base_url": svc.base_url,
                "account": svc.account,
                "argv": list(svc.argv),
                "limits": [cfg.limits.max_items, cfg.limits.max_bytes, cfg.limits.max_text_chars],
            }
            entry_key = cache_mod.key(cache_dir.parent.parent.name, op.service, svc.tenant, op.name, identity)
            hit = cache.get(entry_key)
            if hit is not None:
                envelope = dict(hit, request_id=request_id)
                envelope["provenance"] = dict(hit.get("provenance") or {}, cached=True)
                _diag(stderr, redact, request_id=request_id, op=op.name, outcome="ok", cached="yes")
                return envelope

        credential = None
        if svc.backend in backend_registry.CREDENTIAL_BACKENDS:
            if svc.credential is None:
                raise BrokerError(POLICY_DENIED, "%s needs a keychain credential; %s" % (op.service, config_mod.GUIDANCE))
            try:
                credential = (resolver or default_resolver(platform)).resolve(svc.credential)
            except SecretResolutionError as exc:
                raise BrokerError(AUTH_REQUIRED, "the broker credential could not be resolved (%s)" % exc.code)
            redact.add(credential.reveal())

        ctx = backend_registry.Context(
            service=svc,
            limits=cfg.limits,
            transport=transport
            if transport is not None
            else HttpTransport(https_proxy=cfg.network.https_proxy, ca_file=cfg.network.ca_file),
            credential=credential,
            fetched_at=_now_iso(clock),
            run_command=run_command,
            origin=backend_registry.origin_of(svc.base_url or getattr(backend, "default_base", None)),
        )
        result = backend.run(op.name, params, ctx)
        data, meta = limits_mod.bound(
            _redact_tree(result.data, redact),
            trim_key=result.trim_key,
            max_text_chars=cfg.limits.max_text_chars,
            max_bytes=cfg.limits.max_bytes,
            overhead=ENVELOPE_OVERHEAD,
        )
        if meta.pop("over_budget", False):
            raise BrokerError(UNAVAILABLE, "the result cannot fit within the broker's byte limit", retryable=False)
        reasons = []
        if result.cursor is not None:
            reasons.append("more_results")
        if meta.get("omitted_items"):
            reasons.append("bytes")
        if meta.get("text_truncated"):
            reasons.append("text")
        pagination: Dict[str, Any] = {
            "cursor": result.cursor,
            "truncated": bool(reasons),
            "truncation_reasons": reasons,
        }
        if "limit" in params:
            pagination["limit"] = params["limit"]
        if result.trim_key and isinstance(data.get(result.trim_key), list):
            pagination["returned"] = len(data[result.trim_key])
        pagination.update(meta)
        pagination.update(result.extra_pagination)
        provenance = {
            "service": op.service,
            "backend": svc.backend,
            "tenant": svc.tenant,
            "account": svc.account,
            "resource_id": operations.resource_id(op, params),
            "fetched_at": ctx.fetched_at,
            "url": result.url,
            "cached": False,
            "content_trust": "untrusted",
        }
        envelope = success(data, pagination=pagination, provenance=provenance, request_id=request_id)
        if cache is not None and entry_key is not None:
            cache.put(entry_key, envelope)
        _diag(
            stderr,
            redact,
            request_id=request_id,
            op=op.name,
            outcome="ok",
            returned=pagination.get("returned"),
            ms=int((time.monotonic() - started) * 1000),
        )
        return envelope
    except BrokerError as exc:
        exc.message = redact(exc.message)
        _diag(stderr, redact, request_id=request_id, op=op_name, outcome=exc.code)
        return failure(exc, request_id=request_id, provenance=provenance)
    except Exception as exc:  # noqa: BLE001 - every failure must still be one envelope
        _diag(stderr, redact, request_id=request_id, op=op_name, outcome="internal", type=type(exc).__name__)
        err = BrokerError(UNAVAILABLE, "internal broker error", retryable=False)
        return failure(err, request_id=request_id, provenance=provenance)
