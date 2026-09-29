"""Merge the loaded configuration layers into one effective configuration.

Layers, weakest to strongest for preferences: built-in defaults, the
personal profile, the project file and an API-only run override. The managed
policy is not a preference layer: it only constrains. Security settings
follow their own algebra: allow lists intersect, deny lists union, enumerated
settings take the most restrictive value, limits may only narrow.

Merging is a pure function of the loaded layers. It never touches the file
system, the environment, the network or a credential resolver. Hard defects
(a project widening an allow list, a personal profile used for work, a limit
above its ceiling) raise `ConfigErrors`. A missing project classification is
a blocking finding instead, so a later explain step can still render the
configuration; `ensure_compilable` turns it into the error.

Nothing in the result carries a base URL or a credential reference outside
the two accessor functions, so `canonical` (the input to the compile digest)
and every `repr` are free of them by construction.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

from . import config
from .errors import (
    OVERRIDE_LABEL,
    ConfigError,
    ConfigErrors,
    Finding,
    dedupe_by_path,
    make_error,
    make_finding,
)
from .generate import ROLES
from .install import PROFILE_RE

LAYER_ORDER = ("default", "profile", "project", "override", "managed")
EFFORTS = ("low", "medium", "high", "max")
LIMIT_NAMES = (
    "max_run_seconds",
    "max_tool_calls",
    "max_context_tokens",
    "max_output_tokens",
    "max_transient_retries",
)

# Most restrictive first.
_RESTRICTIVENESS = {
    "sharing": ("deny", "manual"),
    "external_writes": ("deny", "approval-required"),
    "isolation": ("managed", "convenience"),
}
_ENUM_DEFAULTS = {"sharing": "deny", "external_writes": "deny", "isolation": "convenience"}
_FIELD_TOKEN = {
    "sharing": "sharing",
    "external_writes": "external_writes",
    "isolation": "isolation",
}
_MODE_ORDER = ("read-only", "approved-write")


# ------------------------------------------------------------------ types


@dataclass(frozen=True)
class Value:
    value: Any
    origin: Tuple[str, ...]


@dataclass(frozen=True)
class RoleOverride:
    model: Optional[str] = None
    effort: Optional[str] = None


Overrides = Mapping[str, RoleOverride]


@dataclass(frozen=True, kw_only=True)
class ProviderView:
    id: str
    kind: str
    endpoint_family: str
    host_key: str
    credential_env: str
    name: Optional[str] = None
    ca_file: Optional[str] = None
    use_env_proxy: bool = False
    base_url: str = field(default="", repr=False, compare=False)
    credential_ref: str = field(default="", repr=False, compare=False)


def provider_base_url(view: ProviderView) -> str:
    return view.base_url


def provider_credential_ref(view: ProviderView) -> str:
    return view.credential_ref


@dataclass(frozen=True)
class ModelView:
    name: str
    provider: str
    model_id: str
    qualification_ref: str


@dataclass(frozen=True)
class EffectiveConfig:
    profile_name: str
    profile_classification: str
    project_state: str
    classification: str
    managed_present: bool
    providers: Mapping[str, ProviderView]
    models: Mapping[str, ModelView]
    effective_providers: Tuple[str, ...]
    excluded_providers: Mapping[str, str]
    values: Mapping[str, Value]
    findings: Tuple[Finding, ...]


# ---------------------------------------------------------------- helpers


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _provider_host_key(base_url: str) -> str:
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        host = ""
    return config.host_key(host)


def _keyed(entries: Any) -> Set[str]:
    return {config.host_key(item) for item in entries if isinstance(item, str)}


def _build_views(profile: Dict[str, Any]) -> Tuple[Dict[str, ProviderView], Dict[str, ModelView]]:
    providers: Dict[str, ProviderView] = {}
    for pid in sorted(_dict(profile.get("providers"))):
        entry = _dict(profile["providers"][pid])
        base_url = entry.get("base_url", "")
        providers[pid] = ProviderView(
            id=pid,
            kind=entry.get("kind", ""),
            endpoint_family=entry.get("endpoint_family", ""),
            host_key=_provider_host_key(base_url),
            credential_env=config.provider_env_name(pid),
            name=entry.get("name"),
            ca_file=entry.get("ca_file"),
            use_env_proxy=bool(entry.get("use_env_proxy", False)),
            base_url=base_url,
            credential_ref=entry.get("credential_ref", ""),
        )
    models: Dict[str, ModelView] = {}
    for name in sorted(_dict(profile.get("models"))):
        entry = _dict(profile["models"][name])
        models[name] = ModelView(
            name=name,
            provider=entry.get("provider", ""),
            model_id=entry.get("model_id", ""),
            qualification_ref=entry.get("qualification_ref", ""),
        )
    return providers, models


# -------------------------------------------------------------- overrides


def _validate_overrides(
    overrides: Overrides, models: Mapping[str, ModelView]
) -> List[ConfigError]:
    errs: List[ConfigError] = []
    for role in sorted(overrides, key=str):
        entry = overrides[role]
        if not isinstance(role, str) or role not in ROLES:
            errs.append(
                make_error(
                    "unknown-role", OVERRIDE_LABEL, ("roles", role), "unknown-override-role",
                    allowed=ROLES,
                )
            )
            continue
        model = entry.model
        if model is not None and (
            not isinstance(model, str) or not PROFILE_RE.fullmatch(model) or model not in models
        ):
            errs.append(
                make_error(
                    "dangling-reference", OVERRIDE_LABEL, ("roles", role, "model"),
                    "override-dangling-model",
                )
            )
        effort = entry.effort
        if effort is not None and effort not in EFFORTS:
            errs.append(
                make_error(
                    "invalid-effort", OVERRIDE_LABEL, ("roles", role, "effort"), "invalid-effort",
                    allowed=EFFORTS,
                )
            )
    return errs


# ------------------------------------------------------------ preferences


def _merge_preferences(
    layers: Mapping[str, Dict[str, Any]],
    overrides: Overrides,
    values: Dict[str, Value],
) -> None:
    profile = layers["profile"]
    for name in ("default_model", "auxiliary_model"):
        if isinstance(profile.get(name), str):
            values[name] = Value(profile[name], ("profile",))
    for role in ROLES:
        for attr in ("model", "effort"):
            winner: Optional[Tuple[str, Any]] = None
            for layer in ("profile", "project"):
                data = layers.get(layer)
                if data is None:
                    continue
                item = _dict(_dict(data.get("roles")).get(role)).get(attr)
                if item is not None:
                    winner = (layer, item)
            override = overrides.get(role)
            if override is not None and getattr(override, attr) is not None:
                winner = ("override", getattr(override, attr))
            if winner is not None:
                values["roles.%s.%s" % (role, attr)] = Value(winner[1], (winner[0],))


# ----------------------------------------------------------------- limits


def _merge_limits(
    layers: Mapping[str, Dict[str, Any]],
    labels: Mapping[str, str],
    values: Dict[str, Value],
    errs: List[ConfigError],
) -> None:
    def limit(layer: str, name: str) -> Optional[int]:
        data = layers.get(layer)
        if data is None:
            return None
        item = _dict(data.get("limits")).get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) else None

    for name in LIMIT_NAMES:
        path = ("limits", name)
        prof, proj, ceiling = limit("profile", name), limit("project", name), limit("managed", name)
        if prof is not None and ceiling is not None and prof > ceiling:
            errs.append(make_error("limit-above-ceiling", labels["profile"], path, "limit-above-ceiling"))
        if proj is not None:
            if ceiling is not None and proj > ceiling:
                errs.append(
                    make_error("limit-above-ceiling", labels["project"], path, "limit-above-ceiling")
                )
            elif prof is not None and proj > prof:
                errs.append(
                    make_error("limit-above-ceiling", labels["project"], path, "limit-above-profile")
                )
        if proj is not None:
            values["limits." + name] = Value(proj, ("project",))
        elif prof is not None:
            values["limits." + name] = Value(prof, ("profile",))
        elif ceiling is not None:
            values["limits." + name] = Value(ceiling, ("managed",))


# ------------------------------------------------------- security algebra


def _merge_security(
    layers: Mapping[str, Dict[str, Any]],
    labels: Mapping[str, str],
    providers: Mapping[str, ProviderView],
    values: Dict[str, Value],
    errs: List[ConfigError],
) -> Tuple[Tuple[str, ...], Dict[str, str]]:
    """Procedure for allow and deny lists. Presence of a key, never its
    truthiness, decides whether a layer sets it: an empty list allows
    nothing."""
    ids = sorted(providers)
    policy = {name: _dict(data.get("policy")) for name, data in layers.items()}
    prof, proj, man = policy["profile"], policy.get("project", {}), policy.get("managed", {})
    excluded: Dict[str, str] = {}

    def exclude(pid: str, reason: str) -> None:
        excluded.setdefault(pid, reason)

    allowed: Set[str] = set(prof["allowed_providers"]) if "allowed_providers" in prof else set(ids)
    if "allowed_providers" in man:
        managed_set = set(man["allowed_providers"])
        for pid in sorted(allowed - managed_set):
            exclude(pid, "managed-not-allowed")
        allowed &= managed_set
    if "allowed_providers" in proj:
        entries = proj["allowed_providers"]
        for index, pid in enumerate(entries):
            if pid not in allowed:
                errs.append(
                    make_error(
                        "allowlist-broadening", labels["project"],
                        ("policy", "allowed_providers", index), "allowlist-broadening",
                    )
                )
        allowed &= set(entries)
    for pid in ids:
        if pid not in allowed:
            exclude(pid, "not-allowed")
    denied: Set[str] = set()
    for data in (man, prof, proj):
        denied |= set(data.get("denied_providers", ()))
    for pid in sorted(allowed & denied):
        exclude(pid, "denied")
    allowed -= denied

    if "allowed_hosts" in prof:
        hosts = _keyed(prof["allowed_hosts"])
    else:
        hosts = {view.host_key for view in providers.values()}
    if "allowed_hosts" in man:
        hosts &= _keyed(man["allowed_hosts"])
    if "allowed_hosts" in proj:
        for index, item in enumerate(proj["allowed_hosts"]):
            if config.host_key(item) not in hosts:
                errs.append(
                    make_error(
                        "allowlist-broadening", labels["project"],
                        ("policy", "allowed_hosts", index), "allowlist-broadening-host",
                    )
                )
        hosts &= _keyed(proj["allowed_hosts"])
    denied_hosts: Set[str] = set()
    for data in (man, prof, proj):
        denied_hosts |= _keyed(data.get("denied_hosts", ()))
    for pid in sorted(allowed):
        key = providers[pid].host_key
        if key in denied_hosts:
            exclude(pid, "host-denied")
        elif key not in hosts:
            exclude(pid, "host-not-allowed")

    effective = tuple(sorted(pid for pid in ids if pid not in excluded))

    def origin_of(key: str) -> Tuple[str, ...]:
        names = tuple(name for name in ("profile", "project", "managed") if key in policy.get(name, {}))
        return names or ("default",)

    values["policy.allowed_providers"] = Value(effective, origin_of("allowed_providers"))
    values["policy.allowed_hosts"] = Value(
        tuple(sorted(hosts - denied_hosts)), origin_of("allowed_hosts")
    )
    values["policy.denied_providers"] = Value(tuple(sorted(denied)), origin_of("denied_providers"))
    values["policy.denied_hosts"] = Value(tuple(sorted(denied_hosts)), origin_of("denied_hosts"))
    return effective, excluded


def _merge_enumerated(
    layers: Mapping[str, Dict[str, Any]],
    values: Dict[str, Value],
    findings: List[Finding],
) -> None:
    for name, order in _RESTRICTIVENESS.items():
        rank = {item: index for index, item in enumerate(order)}
        set_by: Dict[str, str] = {}
        for layer in ("profile", "project", "managed"):
            item = _dict(_dict(layers.get(layer)).get("policy")).get(name) if layer in layers else None
            if item in rank:
                set_by[layer] = item
        # The project file is untrusted: it may narrow the baseline (the
        # profile value, or the built-in default when the profile is silent)
        # but never move away from it.
        floor = rank[set_by["profile"]] if "profile" in set_by else rank[_ENUM_DEFAULTS[name]]
        if "project" in set_by and rank[set_by["project"]] > floor:
            del set_by["project"]
            findings.append(
                make_finding("less-restrictive-ignored", False, _FIELD_TOKEN[name], "project")
            )
        if set_by:
            best = min(rank[item] for item in set_by.values())
            effective = order[best]
            origin = tuple(layer for layer in LAYER_ORDER if set_by.get(layer) == effective)
        else:
            effective, best, origin = _ENUM_DEFAULTS[name], rank[_ENUM_DEFAULTS[name]], ("default",)
        values["policy." + name] = Value(effective, origin)
        for layer in ("profile", "project", "managed"):
            if layer in set_by and rank[set_by[layer]] > best:
                findings.append(
                    make_finding("less-restrictive-ignored", False, _FIELD_TOKEN[name], layer)
                )
        if name == "isolation" and effective == "managed":
            findings.append(make_finding("isolation-unverified", False))
    set_layers = tuple(
        layer
        for layer in ("profile", "project", "managed")
        if layer in layers and "cross_profile_fallback" in _dict(layers[layer].get("policy"))
    )
    values["policy.cross_profile_fallback"] = Value(False, set_layers or ("default",))


def _merge_integrations(
    layers: Mapping[str, Dict[str, Any]],
    values: Dict[str, Value],
    findings: List[Finding],
) -> None:
    """Integrations are schema-only for now; the merge is restrictive so a
    repo-editable file can never widen what the profile allows. When the
    profile is silent the baseline is the most restrictive value."""
    profile = _dict(layers["profile"].get("integrations"))
    project = _dict(layers["project"].get("integrations")) if "project" in layers else {}

    if "backend" in profile:
        values["integrations.backend"] = Value(profile["backend"], ("profile",))
    if "backend" in project and project["backend"] != profile.get("backend"):
        findings.append(make_finding("integrations-value-ignored", False, "integrations-backend", "project"))

    if "enabled" in profile or "enabled" in project:
        base = set(profile.get("enabled", ()))
        result = base & set(project["enabled"]) if "enabled" in project else base
        if "enabled" in project and set(project["enabled"]) - result:
            findings.append(make_finding("integrations-value-ignored", False, "integrations-enabled", "project"))
        if "enabled" in project and "enabled" in profile:
            origin = ("profile", "project") if result != set(profile["enabled"]) else ("profile",)
        elif "enabled" in profile:
            origin = ("profile",)
        else:
            origin = ("default",) if not result else ("project",)
        values["integrations.enabled"] = Value(tuple(sorted(result)), origin)

    if "mode" in profile or "mode" in project:
        base_mode = profile.get("mode", "read-only")
        mode = base_mode
        if "mode" in project:
            if _MODE_ORDER.index(project["mode"]) < _MODE_ORDER.index(mode):
                mode = project["mode"]
            elif project["mode"] != mode:
                findings.append(make_finding("integrations-value-ignored", False, "integrations-mode", "project"))
        if "mode" in profile and "mode" in project and mode == project["mode"] == base_mode:
            origin = ("profile", "project")
        elif "mode" in project and mode == project["mode"]:
            origin = ("project",)
        elif "mode" in profile:
            origin = ("profile",)
        else:
            origin = ("default",)
        values["integrations.mode"] = Value(mode, origin)


# --------------------------------------------------------- classification


def _classify(
    loaded: config.LoadedConfig,
    profile: Dict[str, Any],
    providers: Mapping[str, ProviderView],
    labels: Mapping[str, str],
    errs: List[ConfigError],
    findings: List[Finding],
) -> Tuple[str, str, str]:
    project = loaded.project
    state = project.classification_state if project is not None else "absent"
    profile_class = profile["classification"]
    project_label = project.file if project is not None else config.PROJECT_LABEL
    if state in ("absent", "missing", "unknown"):
        message_id, path = {
            "absent": ("no-project-file", "$"),
            "missing": ("missing-classification", "$"),
            "unknown": ("unknown-classification", "$.classification"),
        }[state]
        error = make_error("missing-classification", project_label, path, message_id)
        findings.append(make_finding("missing-classification", True, error=error))
    effective = "work" if (profile_class == "work" or state == "work") else "personal"
    if state == "work" and profile_class == "personal":
        errs.append(
            make_error(
                "personal-profile-for-work", labels["project"], ("classification",),
                "personal-profile-for-work",
            )
        )
    elif effective == "work":
        for pid in sorted(providers):
            if providers[pid].kind == "openrouter":
                errs.append(
                    make_error(
                        "personal-provider-kind-for-work", labels["profile"],
                        ("providers", pid, "kind"), "personal-provider-kind-for-work",
                    )
                )
    return state, profile_class, effective


# ------------------------------------------------------------------ merge


def merge(
    loaded: config.LoadedConfig, *, overrides: Optional[Overrides] = None
) -> EffectiveConfig:
    """Merge the loaded layers. Raises `ConfigErrors` for hard defects, all
    collected first. Reads `Layer.data` only through deep copies."""
    overrides = dict(overrides or {})
    layers: Dict[str, Dict[str, Any]] = {"profile": copy.deepcopy(loaded.profile.data)}
    labels = {"profile": loaded.profile.file, "project": config.PROJECT_LABEL,
              "managed": config.MANAGED_LABEL}
    if loaded.project is not None:
        layers["project"] = copy.deepcopy(loaded.project.data)
        labels["project"] = loaded.project.file
    if loaded.managed is not None:
        layers["managed"] = copy.deepcopy(loaded.managed.data)
        labels["managed"] = loaded.managed.file
    profile = layers["profile"]

    providers, models = _build_views(profile)
    errs: List[ConfigError] = _validate_overrides(overrides, models)
    findings: List[Finding] = []
    values: Dict[str, Value] = {}

    project_state, profile_class, effective_class = _classify(
        loaded, profile, providers, labels, errs, findings
    )
    _merge_preferences(layers, overrides, values)
    _merge_limits(layers, labels, values, errs)
    effective_ids, excluded = _merge_security(layers, labels, providers, values, errs)
    if errs:
        raise ConfigErrors(dedupe_by_path(errs))

    for pid in sorted(excluded):
        findings.append(make_finding("provider-excluded", False, pid, excluded[pid]))
    _merge_enumerated(layers, values, findings)
    _merge_integrations(layers, values, findings)
    if loaded.managed is None:
        findings.append(make_finding("no-managed-policy", False))
    else:
        managed_policy = _dict(layers["managed"].get("policy"))
        if "allowed_providers" in managed_policy or "denied_providers" in managed_policy:
            findings.append(make_finding("provider-ids-are-labels", False))

    return EffectiveConfig(
        profile_name=profile["profile"],
        profile_classification=profile_class,
        project_state=project_state,
        classification=effective_class,
        managed_present=loaded.managed is not None,
        providers=MappingProxyType(dict(providers)),
        models=MappingProxyType(dict(models)),
        effective_providers=effective_ids,
        excluded_providers=MappingProxyType(dict(sorted(excluded.items()))),
        values=MappingProxyType(dict(sorted(values.items()))),
        findings=tuple(findings),
    )


# ------------------------------------------------------ compile boundary


def compile_blockers(effective: EffectiveConfig) -> Tuple[ConfigError, ...]:
    return tuple(
        finding.error
        for finding in effective.findings
        if finding.blocking and finding.error is not None
    )


def ensure_compilable(effective: EffectiveConfig) -> None:
    blockers = compile_blockers(effective)
    if blockers:
        raise ConfigErrors(blockers)


# -------------------------------------------------------------- canonical


def _plain(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical_provider(view: ProviderView) -> Dict[str, Any]:
    return {
        "id": view.id,
        "kind": view.kind,
        "endpoint_family": view.endpoint_family,
        "host_key": view.host_key,
        "credential_env": view.credential_env,
        "name": view.name,
        "ca_file": view.ca_file,
        "use_env_proxy": view.use_env_proxy,
    }


def _canonical_model(view: ModelView) -> Dict[str, Any]:
    return {
        "name": view.name,
        "provider": view.provider,
        "model_id": view.model_id,
        "qualification_ref": view.qualification_ref,
    }


def canonical(effective: EffectiveConfig) -> Dict[str, Any]:
    """Plain JSON-compatible dict of everything except provider base URLs and
    credential references. Built from explicit per-type field lists."""
    return {
        "profile_name": effective.profile_name,
        "profile_classification": effective.profile_classification,
        "project_state": effective.project_state,
        "classification": effective.classification,
        "managed_present": effective.managed_present,
        "providers": {pid: _canonical_provider(v) for pid, v in sorted(effective.providers.items())},
        "models": {name: _canonical_model(v) for name, v in sorted(effective.models.items())},
        "effective_providers": list(effective.effective_providers),
        "excluded_providers": dict(sorted(effective.excluded_providers.items())),
        "values": {
            key: {"value": _plain(v.value), "origin": list(v.origin)}
            for key, v in sorted(effective.values.items())
        },
        "findings": [
            {"code": f.code, "blocking": f.blocking, "subject": list(f.subject)}
            for f in effective.findings
        ],
    }
