"""Judge whether a model may be used, from the capability record the gateway
probe wrote for it.

A record qualifies a model only for the exact endpoint, model id and runtime
version it was measured against, and only while it is fresh. Every other
situation is a named state with a closed reason token; record content never
reaches a message or a finding. The record file is treated as untrusted: it
is read through one descriptor that does not follow symlinks, and a record
that another user owns or that anyone can write is not believed.

Nothing here runs a probe, resolves a credential or touches the network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional

from . import config, jsonio, manifest, paths
from .errors import ConfigErrors
from .merge import EffectiveConfig, ModelView, ProviderView, provider_base_url

RECORD_SCHEMA = "quoin.opencode.capability-record"
RECORD_SCHEMA_VERSION = 1
QUALIFICATION_MAX_AGE = timedelta(days=30)
CLOCK_SKEW = timedelta(minutes=5)

STATES = ("qualified", "missing", "failed", "stale", "mismatched", "malformed")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_RECORD_LABEL = "qualification record"
_LOCAL_PREFIX = "local:"


@dataclass(frozen=True)
class QualificationResult:
    model: str
    state: str
    reason: Optional[str] = None
    reasoning_supported: bool = False
    probed_at: Optional[str] = None


def pinned_version() -> str:
    """The runtime version the adapter is pinned to."""
    data_dir = paths.adapter_data_dir()
    if data_dir is None:
        raise paths.AdapterDataMissing("adapter data directory not found")
    try:
        return manifest.read_pinned_version(data_dir.parent.parent)
    except manifest.ManifestLoadError:
        raise paths.AdapterDataMissing("pinned runtime version not found") from None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _reasoning_supported(record: dict) -> bool:
    # A type-checked lookup: any missing key or non-dict level is "not
    # supported"; capability data is not part of qualification identity.
    status = _dict(_dict(_dict(record.get("capabilities")).get("reasoning_parameters"))).get("status")
    return status == "supported"


def _unsafe(info: os.stat_result) -> bool:
    if os.name != "posix":
        return False
    if info.st_mode & 0o002:
        return True
    getuid = getattr(os, "getuid", None)
    return getuid is not None and info.st_uid != getuid()


def evaluate(
    model: ModelView,
    provider: ProviderView,
    *,
    env: Mapping[str, str],
    home,
    now: datetime,
    pinned_version: str,
) -> QualificationResult:
    """Qualification state of `model` served through `provider`.

    Check order, first failure wins: absent record, unsafe directory or
    file, unreadable, wrong schema, wrong shape, timestamp in the future,
    failed probe result, model, endpoint and runtime identity, age.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    def result(state: str, reason: Optional[str] = None, **extra: Any) -> QualificationResult:
        return QualificationResult(model.name, state, reason, **extra)

    ref = model.qualification_ref
    if not isinstance(ref, str) or not ref.startswith(_LOCAL_PREFIX):
        return result("malformed", "bad-shape")
    try:
        path = paths.qualification_path(ref[len(_LOCAL_PREFIX):], env, home)
    except ValueError:
        return result("malformed", "bad-shape")
    if not os.path.lexists(path):
        return result("missing")
    if os.name == "posix":
        try:
            directory = os.stat(path.parent)
        except OSError:
            return result("malformed", "unreadable")
        if directory.st_mode & 0o002:
            return result("malformed", "unsafe-permissions")
    try:
        record, info = jsonio.load_strict_with_stat(path, file_label=_RECORD_LABEL)
    except ConfigErrors:
        return result("malformed", "unreadable")
    if _unsafe(info):
        return result("malformed", "unsafe-permissions")

    if not isinstance(record, dict):
        return result("malformed", "bad-shape")
    version = record.get("schema_version")
    if record.get("schema") != RECORD_SCHEMA or type(version) is not int or version != RECORD_SCHEMA_VERSION:
        return result("malformed", "bad-schema")
    key = record.get("key")
    runtime = _dict(key).get("runtime")
    verdict = _dict(record.get("verdict")).get("status")
    stamp = record.get("probed_at")
    if not (
        isinstance(key, dict)
        and isinstance(runtime, dict)
        and isinstance(verdict, str)
        and isinstance(stamp, str)
    ):
        return result("malformed", "bad-shape")
    try:
        probed = datetime.strptime(stamp, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return result("malformed", "bad-shape")
    if probed > now + CLOCK_SKEW:
        return result("malformed", "future-timestamp", probed_at=stamp)
    if verdict == "not_qualified":
        return result("failed", "not-qualified", probed_at=stamp)
    if verdict == "could_not_run":
        return result("failed", "could-not-run", probed_at=stamp)
    if verdict != "qualified":
        return result("malformed", "bad-shape", probed_at=stamp)
    if key.get("model_id") != model.model_id:
        return result("mismatched", "model-mismatch", probed_at=stamp)
    try:
        endpoint = config.endpoint_identity(provider_base_url(provider))
    except ValueError:
        endpoint = None
    if endpoint is None or key.get("endpoint") != endpoint:
        return result("mismatched", "endpoint-mismatch", probed_at=stamp)
    if runtime.get("name") != "opencode" or runtime.get("version") != pinned_version:
        return result("mismatched", "runtime-mismatch", probed_at=stamp)
    if now - probed > QUALIFICATION_MAX_AGE:
        return result("stale", "too-old", probed_at=stamp)
    return result(
        "qualified", reasoning_supported=_reasoning_supported(record), probed_at=stamp
    )


def evaluate_all(
    effective: EffectiveConfig,
    *,
    env: Mapping[str, str],
    home,
    now: datetime,
    pinned_version: str,
) -> Mapping[str, QualificationResult]:
    """Qualification of every model the profile declares, sorted by name."""
    out = {}
    for name in sorted(effective.models):
        model = effective.models[name]
        out[name] = evaluate(
            model,
            effective.providers[model.provider],
            env=env,
            home=home,
            now=now,
            pinned_version=pinned_version,
        )
    return MappingProxyType(out)
