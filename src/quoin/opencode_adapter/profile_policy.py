"""Profile isolation policy model: closed order tables, classification
defaults and the pure merge helpers behind the layered policy algebra.

Everything here is a pure function over plain values. File labels, error
objects and finding objects are built by the caller (`merge`) from the closed
tokens these helpers return, so this module depends on nothing from the
adapter.

Layer tokens, strongest authority first for approvals:

* ``profile``          the personal profile file (may approve);
* ``trusted-managed``  the root-owned managed policy file (may approve);
* ``managed``          the policy file named by the environment, which the
                       user can edit or unset, so it only ever narrows;
* ``project``          the repository file, untrusted, which only narrows.

Classification defaults carry the origin ``default`` and are never counted as
approved, so a personal default such as ``web_tools: allow`` is a default, not
a grant. Consumers decide allow or deny from the value and ask
``ProfilePolicy.approved(field)`` only where an explicit approval is required
(grant lists and restriction lists).
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

# ------------------------------------------------- cited built-in id sets

# The built-in language server ids at the pinned release: 38 literals, one of
# which contains a space. Rests on the compatibility key `lsp-builtin-ids`.
LSP_BUILTIN_IDS: Tuple[str, ...] = (
    "astro", "bash", "biome", "clangd", "clojure-lsp", "csharp", "dart", "deno",
    "dockerfile", "elixir-ls", "eslint", "fsharp", "gleam", "gopls",
    "haskell-language-server", "jdtls", "julials", "kotlin-ls", "lua-ls", "nixd",
    "ocaml-lsp", "oxlint", "php intelephense", "prisma", "pyright", "razor",
    "ruby-lsp", "rust", "sourcekit-lsp", "svelte", "terraform", "texlab",
    "tinymist", "ty", "typescript", "vue", "yaml-ls", "zls",
)

# The built-in formatter ids at the pinned release (registered names, which are
# what a disable entry removes): 26. Rests on `formatter-builtin-ids`.
FORMATTER_BUILTIN_IDS: Tuple[str, ...] = (
    "air", "biome", "clang-format", "cljfmt", "dart", "dfmt", "gleam", "gofmt",
    "htmlbeautifier", "ktlint", "latexindent", "mix", "nixfmt", "ocamlformat",
    "ormolu", "oxfmt", "pint", "prettier", "rubocop", "ruff", "rustfmt", "shfmt",
    "standardrb", "terraform", "uv", "zig",
)

# Whether the id sets above are cited as complete. Tied to the status of the
# `lsp-builtin-ids` and `formatter-builtin-ids` rows by a test; an unverified
# row would empty the tuple and clear the flag.
LSP_ALLOWLIST_VERIFIED = True
FORMATTER_ALLOWLIST_VERIFIED = True

# Formatters that OpenCode disables together (`formatter-ruff-uv-pair`): a
# disable entry for either removes both, so an allow list must name both or
# neither.
FORMATTER_PAIRED_IDS: Tuple[Tuple[str, ...], ...] = (("ruff", "uv"),)

# ------------------------------------------------------------ order tables

# Most restrictive first.
ENUM_ORDER: Dict[str, Tuple[str, ...]] = {
    "fallback": ("stop", "same-profile"),
    "web_tools": ("deny", "allow"),
    "package_install": ("deny", "allow"),
    "mcp.local": ("deny", "allow-listed", "any"),
    "mcp.remote": ("deny", "allow-listed", "any"),
    "lsp.mode": ("deny", "allow-listed", "builtin"),
    "formatter.mode": ("deny", "allow-listed", "builtin"),
    "network.identity_check": ("required", "off"),
    "logging.content": ("metadata", "summary"),
}
GRANT_LISTS = ("mcp.allowed", "lsp.allowed", "formatter.allowed")
RESTRICTION_LISTS = (
    "network.allowed_addresses",
    "network.pins",
    "repositories.allowed_roots",
)
RETENTION_KINDS = ("runs_days", "cache_days", "logs_days", "crash_days", "exports_days")
RETENTION_FIELDS = tuple("retention." + kind for kind in RETENTION_KINDS)
POLICY_FIELDS: Tuple[str, ...] = (
    tuple(ENUM_ORDER) + GRANT_LISTS + RESTRICTION_LISTS + RETENTION_FIELDS
)

# The pre-existing policy enums, most restrictive first.
LEGACY_ENUM_ORDER: Dict[str, Tuple[str, ...]] = {
    "sharing": ("deny", "manual"),
    "external_writes": ("deny", "approval-required"),
    "isolation": ("managed", "convenience"),
}
LEGACY_ALLOW_LISTS = ("allowed_providers", "allowed_hosts")
LEGACY_DENY_LISTS = ("denied_providers", "denied_hosts")
LEGACY_POLICY_KEYS = (
    LEGACY_ALLOW_LISTS + LEGACY_DENY_LISTS + tuple(LEGACY_ENUM_ORDER) + ("cross_profile_fallback",)
)
LIMIT_KEYS = (
    "max_run_seconds",
    "max_tool_calls",
    "max_context_tokens",
    "max_output_tokens",
    "max_transient_retries",
)

LAYER_ORDER: Tuple[str, ...] = ("profile", "trusted-managed", "managed", "project")
APPROVING_LAYERS = frozenset({"profile", "trusted-managed"})

# ------------------------------------------------------ classification defaults

_WORK_RETENTION = {field_name: 30 for field_name in RETENTION_FIELDS}
_PERSONAL_RETENTION: Dict[str, Optional[int]] = {field_name: None for field_name in RETENTION_FIELDS}

CLASSIFICATION_DEFAULTS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "work": MappingProxyType(
            {
                "fallback": "stop",
                "web_tools": "deny",
                "package_install": "deny",
                "mcp.local": "deny",
                "mcp.remote": "deny",
                "mcp.allowed": (),
                "lsp.mode": "deny",
                "lsp.allowed": (),
                "formatter.mode": "deny",
                "formatter.allowed": (),
                "network.identity_check": "required",
                "network.allowed_addresses": None,
                "network.pins": None,
                "logging.content": "metadata",
                "repositories.allowed_roots": None,
                **_WORK_RETENTION,
            }
        ),
        "personal": MappingProxyType(
            {
                "fallback": "same-profile",
                "web_tools": "allow",
                "package_install": "allow",
                "mcp.local": "any",
                "mcp.remote": "any",
                "mcp.allowed": (),
                "lsp.mode": "deny",
                "lsp.allowed": (),
                "formatter.mode": "deny",
                "formatter.allowed": (),
                "network.identity_check": "off",
                "network.allowed_addresses": None,
                "network.pins": None,
                "logging.content": "summary",
                "repositories.allowed_roots": None,
                **_PERSONAL_RETENTION,
            }
        ),
    }
)


def defaults_for(classification: str) -> Mapping[str, Any]:
    """Defaults for an effective classification; anything but ``work`` gets
    the personal table only when it is exactly ``personal``."""
    return CLASSIFICATION_DEFAULTS["work" if classification != "personal" else "personal"]


# ------------------------------------------------------------ normalisation


def normalise_address(value: str) -> str:
    """Canonical text of a network in CIDR form (``with_prefixlen``)."""
    return ip_network_text(value)


def ip_network_text(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=True))


def normalise_members(field_name: str, items: Iterable[str]) -> Tuple[str, ...]:
    """Normalised, de-duplicated, sorted members of one list field."""
    out: List[str] = []
    for item in items:
        if field_name == "network.allowed_addresses":
            try:
                item = ip_network_text(item)
            except ValueError:
                pass
        elif field_name == "network.pins":
            item = item.lower()
        out.append(item)
    return tuple(sorted(set(out)))


# ----------------------------------------------------------- flat extraction


def flatten_policy(policy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Flat ``field -> value`` view of the new policy fields set in one layer.

    Unset fields are absent. Lists become sorted tuples of normalised members.
    Malformed shapes are skipped (validation already ran on every layer).
    """
    out: Dict[str, Any] = {}
    if not isinstance(policy, Mapping):
        return out
    for name in ("fallback", "web_tools", "package_install"):
        if name in policy:
            out[name] = policy[name]
    for group, keys in (
        ("mcp", ("local", "remote")),
        ("lsp", ("mode",)),
        ("formatter", ("mode",)),
        ("network", ("identity_check",)),
        ("logging", ("content",)),
    ):
        sub = policy.get(group)
        if isinstance(sub, Mapping):
            for key in keys:
                if key in sub:
                    out["%s.%s" % (group, key)] = sub[key]
    for name in GRANT_LISTS + RESTRICTION_LISTS:
        group, key = name.split(".")
        sub = policy.get(group)
        if isinstance(sub, Mapping) and isinstance(sub.get(key), (list, tuple)):
            out[name] = normalise_members(name, [x for x in sub[key] if isinstance(x, str)])
    retention = policy.get("retention")
    if isinstance(retention, Mapping):
        for kind in RETENTION_KINDS:
            item = retention.get(kind)
            if isinstance(item, int) and not isinstance(item, bool):
                out["retention." + kind] = item
    return out


# ---------------------------------------------------------------- helpers


def most_restrictive(field_name: str, first: str, second: str) -> str:
    order = ENUM_ORDER[field_name]
    return first if order.index(first) <= order.index(second) else second


def _origin(layers: Mapping[str, Any], *, exclude: Sequence[str] = ()) -> Tuple[str, ...]:
    names = tuple(
        name for name in LAYER_ORDER if layers.get(name) is not None and name not in exclude
    )
    return names or ("default",)


# ------------------------------------------------------------- merge helpers
#
# Each helper returns ``(value, origin, findings_spec, errors_spec)``.
# ``findings_spec`` is a tuple of layer tokens whose setting was ignored;
# ``errors_spec`` is a tuple of project list indices that broaden a stricter
# set. ``merge`` turns both into findings and errors.


def merge_enum(
    field_name: str, layers: Mapping[str, Optional[str]], default: str
) -> Tuple[str, Tuple[str, ...], Tuple[str, ...], Tuple[int, ...]]:
    order = ENUM_ORDER[field_name]
    rank = {item: index for index, item in enumerate(order)}
    set_layers: Dict[str, str] = {
        name: item for name, item in layers.items() if isinstance(item, str) and item in rank
    }
    if "profile" in set_layers:
        baseline = set_layers["profile"]
    elif "trusted-managed" in set_layers:
        baseline = set_layers["trusted-managed"]
    else:
        baseline = default
    value = baseline
    for ceiling in ("trusted-managed", "managed"):
        if ceiling in set_layers:
            value = most_restrictive(field_name, value, set_layers[ceiling])
    ignored: List[str] = []
    dropped = None
    if "project" in set_layers:
        if rank[set_layers["project"]] > rank[baseline]:
            dropped = set_layers.pop("project")
        else:
            value = most_restrictive(field_name, value, set_layers["project"])
    if dropped is not None and dropped != value:
        ignored.append("project")
    for name in LAYER_ORDER:
        if name in set_layers and rank[set_layers[name]] > rank[value]:
            ignored.append(name)
    origin = tuple(name for name in LAYER_ORDER if set_layers.get(name) == value) or ("default",)
    return value, origin, tuple(ignored), ()


def merge_grant_list(
    layers: Mapping[str, Optional[Sequence[str]]], default: Sequence[str] = ()
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[int, ...]]:
    profile = layers.get("profile")
    trusted = layers.get("trusted-managed")
    env = layers.get("managed")
    project = layers.get("project")
    if profile is not None:
        base = set(profile)
    elif trusted is not None:
        base = set(trusted)
    else:
        base = set(default)
    if profile is not None and trusted is not None:
        base &= set(trusted)
    if env is not None:
        base &= set(env)
    errors: Tuple[int, ...] = ()
    if project is not None:
        errors = tuple(index for index, item in enumerate(project) if item not in base)
        base &= set(project)
    return tuple(sorted(base)), _origin(layers), (), errors


def merge_restriction_list(
    layers: Mapping[str, Optional[Sequence[str]]]
) -> Tuple[Optional[Tuple[str, ...]], Tuple[str, ...], Tuple[str, ...], Tuple[int, ...]]:
    current: Optional[set] = None
    for name in ("profile", "trusted-managed", "managed"):
        item = layers.get(name)
        if item is not None:
            current = set(item) if current is None else current & set(item)
    project = layers.get("project")
    errors: Tuple[int, ...] = ()
    if project is not None:
        if current is not None:
            errors = tuple(index for index, item in enumerate(project) if item not in current)
            current &= set(project)
        else:
            current = set(project)
    value = None if current is None else tuple(sorted(current))
    return value, _origin(layers), (), errors


def merge_retention(
    layers: Mapping[str, Optional[int]], default: Optional[int]
) -> Tuple[
    Tuple[Optional[int], Optional[int]],
    Tuple[Tuple[str, ...], Tuple[str, ...]],
    Tuple[str, ...],
    Tuple[int, ...],
]:
    profile = layers.get("profile")
    trusted = layers.get("trusted-managed")
    env = layers.get("managed")
    project = layers.get("project")
    if profile is not None:
        baseline = profile
    elif trusted is not None:
        baseline = trusted
    else:
        baseline = default
    candidates = [item for item in (baseline, trusted, env) if item is not None]
    wide = min(candidates) if candidates else None
    ignored: Tuple[str, ...] = ()
    if project is None:
        scoped = wide
    elif wide is None:
        scoped = project
    elif project > wide:
        scoped = wide
        ignored = ("project",)
    else:
        scoped = project
    wide_origin = (
        tuple(
            name
            for name in ("profile", "trusted-managed", "managed")
            if wide is not None and layers.get(name) == wide
        )
        or ("default",)
    )
    if project is not None and scoped == project:
        scoped_origin: Tuple[str, ...] = ("project",)
    else:
        scoped_origin = wide_origin
    return (wide, scoped), (wide_origin, scoped_origin), ignored, ()


# ---------------------------------------------------- managed combination


def _combine_lists(first: Sequence[str], second: Sequence[str], key: Callable[[str], str]) -> List[str]:
    wanted = {key(item) for item in second}
    return [item for item in first if key(item) in wanted]


def _union(first: Sequence[str], second: Sequence[str]) -> List[str]:
    out: List[str] = list(first)
    for item in second:
        if item not in out:
            out.append(item)
    return out


def combine_managed(
    env_policy: Optional[Mapping[str, Any]],
    trusted_policy: Optional[Mapping[str, Any]],
    host_key: Callable[[str], str] = lambda item: item,
) -> Dict[str, Any]:
    """Combine the environment-pointed and trusted managed policies for the
    pre-existing policy keys only: allow lists intersect when both set, deny
    lists union, enumerated settings take the most restrictive value and
    ``cross_profile_fallback`` is copied."""
    first = dict(env_policy) if isinstance(env_policy, Mapping) else {}
    second = dict(trusted_policy) if isinstance(trusted_policy, Mapping) else {}
    out: Dict[str, Any] = {}
    for name in LEGACY_ALLOW_LISTS:
        if name in first and name in second:
            key = host_key if name == "allowed_hosts" else (lambda item: item)
            out[name] = _combine_lists(first[name], second[name], key)
        elif name in first or name in second:
            out[name] = list((first if name in first else second)[name])
    for name in LEGACY_DENY_LISTS:
        if name in first or name in second:
            out[name] = _union(first.get(name, ()), second.get(name, ()))
    for name, order in LEGACY_ENUM_ORDER.items():
        values = [d[name] for d in (first, second) if d.get(name) in order]
        if values:
            out[name] = min(values, key=order.index)
    if "cross_profile_fallback" in first or "cross_profile_fallback" in second:
        out["cross_profile_fallback"] = first.get(
            "cross_profile_fallback", second.get("cross_profile_fallback")
        )
    return out


def combine_limits(
    first: Optional[Mapping[str, Any]], second: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Combine two limit ceilings by taking the minimum of each limit."""
    one = dict(first) if isinstance(first, Mapping) else {}
    two = dict(second) if isinstance(second, Mapping) else {}
    out: Dict[str, Any] = {}
    for name in LIMIT_KEYS:
        values = [d[name] for d in (one, two) if isinstance(d.get(name), int)]
        if values:
            out[name] = min(values)
    return out


def combine_flat(first: Mapping[str, Any], second: Mapping[str, Any]) -> Dict[str, Any]:
    """Combine two flat new-field views as ceilings: most restrictive enum,
    intersected lists, minimum retention. A field set on one side only is
    copied."""
    out: Dict[str, Any] = {}
    for name in sorted(set(first) | set(second)):
        if name not in first or name not in second:
            out[name] = first[name] if name in first else second[name]
        elif name in ENUM_ORDER:
            out[name] = most_restrictive(name, first[name], second[name])
        elif name in RETENTION_FIELDS:
            out[name] = min(first[name], second[name])
        else:
            out[name] = tuple(sorted(set(first[name]) & set(second[name])))
    return out


# ------------------------------------------------------------------ view


class _EffectiveLike(Protocol):
    values: Mapping[str, Any]
    classification: str


@dataclass(frozen=True)
class ProfilePolicy:
    """Typed read of the effective isolation policy."""

    classification: str
    fallback: str
    web_tools: str
    package_install: str
    mcp_local: str
    mcp_remote: str
    mcp_allowed: Tuple[str, ...]
    lsp_mode: str
    lsp_allowed: Tuple[str, ...]
    formatter_mode: str
    formatter_allowed: Tuple[str, ...]
    identity_check: str
    allowed_addresses: Optional[Tuple[str, ...]]
    pins: Optional[Tuple[str, ...]]
    logging_content: str
    retention: Mapping[str, Optional[int]]
    project_retention: Mapping[str, Optional[int]]
    repository_roots: Optional[Tuple[str, ...]]
    origins: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def approved(self, key: str) -> bool:
        """True when the field's value was set by the profile or the trusted
        managed policy. Defaults are never approved."""
        return bool(set(self.origins.get(key, ("default",))) & APPROVING_LAYERS)


def view(effective: _EffectiveLike) -> ProfilePolicy:
    defaults = defaults_for(effective.classification)
    origins: Dict[str, Tuple[str, ...]] = {}

    def read(name: str, key: Optional[str] = None) -> Any:
        entry = effective.values.get("policy." + (key or name))
        if entry is None:
            origins[name] = ("default",)
            return defaults.get(name)
        origins[name] = tuple(entry.origin)
        return entry.value

    retention: Dict[str, Optional[int]] = {}
    project_retention: Dict[str, Optional[int]] = {}
    for kind in RETENTION_KINDS:
        retention[kind] = read("retention." + kind)
        entry = effective.values.get("policy.retention.project." + kind)
        project_retention[kind] = entry.value if entry is not None else retention[kind]
    return ProfilePolicy(
        classification=effective.classification,
        fallback=read("fallback"),
        web_tools=read("web_tools"),
        package_install=read("package_install"),
        mcp_local=read("mcp.local"),
        mcp_remote=read("mcp.remote"),
        mcp_allowed=read("mcp.allowed"),
        lsp_mode=read("lsp.mode"),
        lsp_allowed=read("lsp.allowed"),
        formatter_mode=read("formatter.mode"),
        formatter_allowed=read("formatter.allowed"),
        identity_check=read("network.identity_check"),
        allowed_addresses=read("network.allowed_addresses"),
        pins=read("network.pins"),
        logging_content=read("logging.content"),
        retention=MappingProxyType(retention),
        project_retention=MappingProxyType(project_retention),
        repository_roots=read("repositories.allowed_roots"),
        origins=MappingProxyType(dict(origins)),
    )
