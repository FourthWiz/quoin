"""Load runtime configuration layers with strict, redaction-safe validation.

Three layers exist: a personal profile, a per-project file and an optional
managed policy. Each layer is loaded on its own (strict JSON, sweeps for
placeholders and secret-shaped text, schema validation, semantic checks) and
a final cross-layer step checks the project file against the selected
profile. Nothing here merges layers, resolves roles or touches the network.

Path convention for reported defects: a value-level problem is reported at
the value's own path; a key-level problem (unknown key, bad key name, a
secret-shaped key) is reported at the offending key's path; a missing
required key is reported at the parent with the key name as a schema-authored
parameter. Only one error is kept per path (see `errors.CLASS_PRIORITY`), so
two different secret-shaped keys under one parent, which both render as
`["*"]`, collapse to a single error.

No error text ever contains user-supplied values; see `errors`.
"""
from __future__ import annotations

import copy
import functools
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from . import jsonio, paths, schema_check
from . import secrets as credential_refs
from .errors import (
    SECRET_SHAPE_RE,
    ConfigError,
    ConfigErrors,
    dedupe_by_path,
    make_error,
)
from .generate import ROLES
from .install import PROFILE_RE

ENDPOINT_KEYS = ("providers", "models", "default_model", "auxiliary_model")
PROJECT_LABEL = ".quoin/runtime.json"
MANAGED_LABEL = "managed policy"

_PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "${", "{env:", "{file:")
_HOST_RE = re.compile(r"[A-Za-z0-9.:-]{1,253}")
_URL_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,250}[A-Za-z0-9])?\.?")
_QUALIFICATION_REF_RE = re.compile(r"local:[a-z0-9][a-z0-9_-]{0,62}")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_EFFORTS = ("low", "medium", "high", "max")
_LIMIT_KEYS = (
    "max_run_seconds",
    "max_tool_calls",
    "max_context_tokens",
    "max_output_tokens",
    "max_transient_retries",
)


@dataclass(frozen=True)
class Layer:
    kind: str
    file: str
    data: Dict[str, Any]
    classification_state: str
    declared_endpoint: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedConfig:
    profile: Layer
    project: Optional[Layer]
    managed: Optional[Layer]


def provider_env_name(provider_id: str) -> str:
    return "QUOIN_" + provider_id.upper().replace("-", "_") + "_API_KEY"


@functools.lru_cache(maxsize=1)
def _schema() -> Dict[str, Any]:
    with open(paths.runtime_config_schema_path(), "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------- sweeps


def _sweep(data: Any, label: str) -> List[ConfigError]:
    """Flag placeholders and secret-shaped text in every value and key."""
    out: List[ConfigError] = []

    def flag(text: str, path: Tuple[Any, ...]) -> None:
        if any(marker in text for marker in _PLACEHOLDER_MARKERS):
            out.append(make_error("unresolved-placeholder", label, path, "unresolved-placeholder"))
        if SECRET_SHAPE_RE.search(text):
            out.append(make_error("inline-credential", label, path, "secret-shaped-value"))

    def walk(node: Any, path: Tuple[Any, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str):
                    flag(key, path + (key,))
                walk(value, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + (index,))
        elif isinstance(node, str):
            flag(node, path)

    walk(data, ())
    return out


# ------------------------------------------------- schema violation mapping


def _map_violation(v: schema_check.Violation, label: str) -> ConfigError:
    path = v.path
    if v.keyword == "additionalProperties":
        return make_error("unknown-key", label, path + (v.key,), "unknown-key")
    if v.keyword == "propertyNames":
        if path == ("roles",):
            return make_error(
                "unknown-role", label, path + (v.key,), "unknown-role", allowed=ROLES
            )
        return make_error("invalid-type", label, path + (v.key,), "wrong-type")
    if v.keyword == "required":
        return make_error(
            "invalid-type", label, path, "missing-required", expected=v.key
        )
    if path == ("schema_version",):
        return make_error(
            "unsupported-schema-version", label, path, "unsupported-schema-version", expected=1
        )
    if path == ("runtime",):
        return make_error("unknown-runtime", label, path, "unknown-runtime", allowed="opencode")
    if path == ("profile",):
        return make_error("invalid-profile-name", label, path, "invalid-profile-name")
    if len(path) == 3 and path[0] == "roles" and path[2] == "effort":
        return make_error("invalid-effort", label, path, "invalid-effort", allowed=_EFFORTS)
    if len(path) == 2 and path[0] == "limits" and v.keyword in ("type", "minimum"):
        return make_error("invalid-limit", label, path, "invalid-limit")
    if (
        len(path) == 3
        and path[0] == "providers"
        and path[2] == "credential_ref"
        and v.keyword == "pattern"
    ):
        return make_error("inline-credential", label, path, "credential-not-a-reference")
    if path == ("policy", "cross_profile_fallback"):
        return make_error(
            "cross-profile-fallback-enabled", label, path, "cross-profile-fallback-enabled"
        )
    return make_error("invalid-type", label, path, "wrong-type")


# -------------------------------------------------------- URL checking


def _host_is_well_formed(host: str) -> bool:
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    return bool(_URL_HOST_RE.fullmatch(host))


def _check_base_url(url: str, path: Tuple[Any, ...], label: str) -> Optional[ConfigError]:
    if any(c.isspace() or ord(c) < 32 for c in url):
        return make_error("invalid-url", label, path, "invalid-url")
    if "?" in url or "#" in url:
        return make_error("url-credentials", label, path, "url-credentials")
    # A JavaScript URL parser reads a backslash as a path separator and
    # decodes percent-escapes in the host; refuse both, and anything that is
    # not printable ASCII, so every parser sees the same host.
    if "\\" in url or any(ord(c) > 126 for c in url):
        return make_error("invalid-url", label, path, "invalid-url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return make_error("invalid-url", label, path, "invalid-url")
    if port == 0 or "%" in parts.netloc:
        return make_error("invalid-url", label, path, "invalid-url")
    if parts.scheme not in ("http", "https"):
        return make_error("invalid-url", label, path, "invalid-url")
    if "@" in parts.netloc:
        return make_error("url-credentials", label, path, "url-credentials")
    if not host or not _host_is_well_formed(host):
        return make_error("invalid-url", label, path, "invalid-url")
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        return make_error("insecure-http", label, path, "insecure-http")
    return None


def _endpoint_identity(url: str) -> str:
    # Same normalisation as the gateway probe: host lowercased, an explicit
    # port kept, path verbatim, query/fragment/userinfo dropped.
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += ":%d" % (parts.port,)
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def endpoint_identity_tuple(value: Any) -> Optional[Tuple[str, str, str, str]]:
    """Identity of a provider entry, or None when it is malformed."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    family = value.get("endpoint_family")
    url = value.get("base_url")
    ref = value.get("credential_ref")
    if not (
        isinstance(kind, str)
        and isinstance(family, str)
        and isinstance(url, str)
        and isinstance(ref, str)
    ):
        return None
    try:
        identity = _endpoint_identity(url)
    except ValueError:
        return None
    return (kind, family, identity, ref)


# ------------------------------------------------------ semantic checks


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ident_errors(value: Any, path: Tuple[Any, ...], label: str) -> List[ConfigError]:
    if isinstance(value, str) and not PROFILE_RE.fullmatch(value):
        return [make_error("invalid-type", label, path, "wrong-type")]
    return []


def _semantic(
    data: Dict[str, Any], kind: str, label: str, expected_profile: Optional[str]
) -> List[ConfigError]:
    errs: List[ConfigError] = []
    providers = _dict(data.get("providers"))
    models = _dict(data.get("models"))

    if kind == "profile":
        if expected_profile is not None and isinstance(data.get("profile"), str):
            if data["profile"] != expected_profile:
                errs.append(
                    make_error(
                        "invalid-profile-name", label, ("profile",), "profile-name-mismatch"
                    )
                )
        for field_name in ("profile", "default_model", "auxiliary_model"):
            errs += _ident_errors(data.get(field_name), (field_name,), label)
        for pid, prov in providers.items():
            errs += _ident_errors(pid, ("providers", pid), label)
            prov = _dict(prov)
            url = prov.get("base_url")
            if isinstance(url, str):
                bad = _check_base_url(url, ("providers", pid, "base_url"), label)
                if bad:
                    errs.append(bad)
            ref = prov.get("credential_ref")
            if isinstance(ref, str) and re.search(
                credential_refs.CREDENTIAL_REF_PATTERN, ref
            ):
                try:
                    credential_refs.parse(ref)
                except ValueError:
                    errs.append(
                        make_error(
                            "inline-credential",
                            label,
                            ("providers", pid, "credential_ref"),
                            "credential-not-a-reference",
                        )
                    )
            if prov.get("kind") == "openrouter" and prov.get("endpoint_family") == "responses":
                errs.append(
                    make_error(
                        "invalid-type",
                        label,
                        ("providers", pid, "endpoint_family"),
                        "openrouter-requires-chat",
                    )
                )
        for name, model in models.items():
            errs += _ident_errors(name, ("models", name), label)
            model = _dict(model)
            qref = model.get("qualification_ref")
            if isinstance(qref, str) and not _QUALIFICATION_REF_RE.fullmatch(qref):
                errs.append(
                    make_error(
                        "invalid-type", label, ("models", name, "qualification_ref"), "wrong-type"
                    )
                )
            ref = model.get("provider")
            errs += _ident_errors(ref, ("models", name, "provider"), label)
            if isinstance(ref, str) and ref not in providers:
                errs.append(
                    make_error(
                        "dangling-reference", label, ("models", name, "provider"), "dangling-reference"
                    )
                )
        for field_name in ("default_model", "auxiliary_model"):
            ref = data.get(field_name)
            if isinstance(ref, str) and ref not in models:
                errs.append(
                    make_error("dangling-reference", label, (field_name,), "dangling-reference")
                )
        seen: Dict[str, str] = {}
        for pid in providers:
            env_name = provider_env_name(pid)
            if env_name in seen:
                errs.append(
                    make_error("env-name-collision", label, ("providers", pid), "env-name-collision")
                )
            else:
                seen[env_name] = pid

    # Checks shared by every layer.
    for role, entry in _dict(data.get("roles")).items():
        entry = _dict(entry)
        ref = entry.get("model")
        errs += _ident_errors(ref, ("roles", role, "model"), label)
        if kind == "profile" and isinstance(ref, str) and ref not in models:
            errs.append(
                make_error("dangling-reference", label, ("roles", role, "model"), "dangling-reference")
            )
    policy = _dict(data.get("policy"))
    for list_name in ("allowed_providers", "denied_providers"):
        items = policy.get(list_name)
        if isinstance(items, list):
            for index, item in enumerate(items):
                path = ("policy", list_name, index)
                errs += _ident_errors(item, path, label)
                if (
                    kind == "profile"
                    and list_name == "allowed_providers"
                    and isinstance(item, str)
                    and item not in providers
                ):
                    errs.append(make_error("dangling-reference", label, path, "dangling-reference"))
    for list_name in ("allowed_hosts", "denied_hosts"):
        items = policy.get(list_name)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, str) and not _HOST_RE.fullmatch(item):
                    errs.append(
                        make_error("invalid-type", label, ("policy", list_name, index), "wrong-type")
                    )
    errs += _ident_errors(_dict(data.get("integrations")).get("backend"), ("integrations", "backend"), label)
    return errs


def _classification_state(data: Dict[str, Any], kind: str) -> str:
    if kind == "profile":
        return data["classification"]
    if kind == "project":
        if "classification" not in data:
            return "missing"
        value = data["classification"]
        if isinstance(value, str) and value in ("work", "personal"):
            return value
        return "unknown"
    return "missing"


# ---------------------------------------------------------- layer loads


def load_layer(
    path, kind: str, *, file_label: str, expected_profile: Optional[str] = None
) -> Layer:
    data = jsonio.load_strict(path, file_label=file_label)
    if not isinstance(data, dict):
        raise ConfigErrors([make_error("invalid-type", file_label, "$", "not-an-object")])
    errs: List[ConfigError] = _sweep(data, file_label)
    declared: Dict[str, Any] = {}
    if kind == "project":
        declared = {k: copy.deepcopy(data.pop(k)) for k in ENDPOINT_KEYS if k in data}
    for violation in schema_check.validate(data, _schema(), kind):
        errs.append(_map_violation(violation, file_label))
    errs += _semantic(data, kind, file_label, expected_profile)
    kept = dedupe_by_path(errs)
    if kept:
        raise ConfigErrors(kept)
    return Layer(
        kind, file_label, copy.deepcopy(data), _classification_state(data, kind), declared
    )


def load_profile(name: str, *, env: Mapping[str, str], home: Path) -> Layer:
    if not isinstance(name, str) or not PROFILE_RE.fullmatch(name):
        raise ConfigErrors(
            [make_error("invalid-profile-name", "profiles/", "$", "invalid-profile-name")]
        )
    label = "profiles/%s.json" % name
    path = paths.profile_path(name, env, home)
    if not path.is_file():
        raise ConfigErrors([make_error("profile-not-found", label, "$", "profile-not-found")])
    return load_layer(path, "profile", file_label=label, expected_profile=name)


def load_project(project_root) -> Optional[Layer]:
    path = paths.project_runtime_path(Path(project_root))
    if not path.exists():
        return None
    return load_layer(path, "project", file_label=PROJECT_LABEL)


def load_managed(env: Mapping[str, str]) -> Optional[Layer]:
    path = paths.managed_policy_path(env)
    if path is None:
        return None
    try:
        with open(path, "rb"):
            pass
    except OSError:
        raise ConfigErrors(
            [make_error("invalid-json", MANAGED_LABEL, "$", "managed-policy-unreadable")]
        ) from None
    return load_layer(path, "managed", file_label=MANAGED_LABEL)


# ------------------------------------------------------ cross-layer step


def check_cross_layer(profile: Layer, project: Optional[Layer]) -> Tuple[ConfigError, ...]:
    if project is None:
        return ()
    label = project.file
    errs: List[ConfigError] = []
    declared = project.declared_endpoint
    profile_providers = _dict(profile.data.get("providers"))
    profile_models = _dict(profile.data.get("models"))

    if "providers" in declared:
        value = declared["providers"]
        if isinstance(value, dict) and value:
            for pid, entry in value.items():
                path = ("providers", pid)
                if pid in profile_providers:
                    mine = endpoint_identity_tuple(entry)
                    theirs = endpoint_identity_tuple(profile_providers[pid])
                    if mine is None or theirs is None or mine != theirs:
                        errs.append(
                            make_error(
                                "endpoint-identity-change", label, path, "endpoint-identity-change"
                            )
                        )
                        continue
                errs.append(
                    make_error("project-declares-endpoint", label, path, "project-declares-endpoint")
                )
        else:
            errs.append(
                make_error(
                    "project-declares-endpoint", label, ("providers",), "project-declares-endpoint"
                )
            )
    for key in ("models", "default_model", "auxiliary_model"):
        if key in declared:
            errs.append(
                make_error("project-declares-endpoint", label, (key,), "project-declares-endpoint")
            )

    for role, entry in _dict(project.data.get("roles")).items():
        ref = _dict(entry).get("model")
        if isinstance(ref, str) and ref not in profile_models:
            errs.append(
                make_error("dangling-reference", label, ("roles", role, "model"), "dangling-reference")
            )
    allowed = _dict(project.data.get("policy")).get("allowed_providers")
    if isinstance(allowed, list):
        for index, item in enumerate(allowed):
            if isinstance(item, str) and item not in profile_providers:
                errs.append(
                    make_error(
                        "dangling-reference",
                        label,
                        ("policy", "allowed_providers", index),
                        "dangling-reference",
                    )
                )
    return dedupe_by_path(errs)


def load_all(
    *,
    project_root,
    profile: Optional[str] = None,
    env: Mapping[str, str],
    home: Path,
) -> LoadedConfig:
    """Load the project, profile and managed layers.

    Raises `ConfigErrors` for defects in user data. A missing or unreadable
    packaged schema raises `AdapterDataMissing` instead, which callers must
    handle separately.
    """
    project = load_project(project_root)
    name = profile
    if not name and project is not None:
        name = project.data.get("profile")
    if not name:
        raise ConfigErrors(
            [make_error("profile-not-found", "profiles/", "$", "no-profile-selected")]
        )
    selected = load_profile(name, env=env, home=home)
    managed = load_managed(env)
    errs = check_cross_layer(selected, project)
    if errs:
        raise ConfigErrors(errs)
    return LoadedConfig(selected, project, managed)
