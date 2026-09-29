"""Compile the layered runtime configuration into a native OpenCode config.

The compiler is a pure function of loaded configuration, qualification
records and the injected clock, environment and home directory. It never
resolves a credential, never opens a network connection and never reads the
real environment. The compiled document refers to credentials only through
the `{env:NAME}` placeholder form OpenCode substitutes at launch.

Three stages:

* evaluation: load, merge, qualify and resolve roles, then add the findings
  that only matter for compilation (missing agent files, provider id
  conflicts, model references that cannot be written);
* building: turn a blocker-free evaluation into the native document, then
  pass it through post-build gates that re-check the security properties on
  the finished document rather than trusting the builder;
* output: a digest over everything that shaped the result, a metadata
  sidecar for the launcher, and a private write outside the project.

Provider ids are namespaced: an OpenAI-compatible provider is written as
`quoin-` plus its own id, because a config provider whose id equals a
built-in OpenCode provider id would inherit that provider's loader, stored
key and wire protocol. Only the OpenRouter kind keeps the built-in id.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import config, jsonio, merge, names, paths, qualification, roles, schema_check
from .errors import Finding, make_finding
from .generate import ROLES

NATIVE_ID_PREFIX = "quoin-"
VARIANT_PREFIX = "quoin-"
CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"
ANY = "*"
DENY_ALL_RESOURCE = "*"
POLICY_ACTION = "provider.use"
NATIVE_FILE = "opencode.json"
SIDECAR_FILE = "quoin-compile.json"
MAX_OUTPUT_BYTES = 4_194_304

# Package per (provider kind, endpoint family).
PACKAGES: Mapping[Tuple[str, str], str] = {
    ("openai-compatible", "chat-completions"): "@ai-sdk/openai-compatible",
    ("openai-compatible", "responses"): "@ai-sdk/openai",
    ("openrouter", ANY): "@openrouter/ai-sdk-provider",
}

# The default model factory of the responses-family package reaches the
# Responses endpoint on the pinned runtime (see the compatibility notes). If
# that ever stops holding, flipping this blocks every referenced
# responses-family provider instead of compiling something unverified.
RESPONSES_FAMILY_VERIFIED = True

AUXILIARY_EMITTED = ("title", "compaction")
_BAD_MODEL_ID_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_PLACEHOLDER_MARKERS = ("{env:", "{file:", "${", "REPLACE_WITH_")


# ---------------------------------------------------------------- errors

GATE_MESSAGES: Mapping[str, str] = {
    "schema": "the compiled document does not match the vendored native schema",
    "placeholder": "the compiled document contains a placeholder that is not a generated credential reference",
    "allowlist": "the provider allowlist of the compiled document is inconsistent with the effective configuration",
    "reference": "a model or variant reference in the compiled document does not resolve",
    "constants": "a fixed setting of the compiled document has an unexpected value",
}


class CompileGateError(Exception):
    """A post-build gate failed. The message is fixed text; nothing from the
    document is ever included."""

    def __init__(self, gate: str):
        if gate not in GATE_MESSAGES:
            raise ValueError("unknown gate")
        self.gate = gate
        super().__init__(GATE_MESSAGES[gate])


class CompileBlocked(Exception):
    """Compilation is refused because of blocking findings."""

    def __init__(self, findings: Sequence[Finding]):
        self.findings: Tuple[Finding, ...] = tuple(findings)
        codes = sorted({f.code for f in self.findings})
        super().__init__("compilation is blocked: " + ", ".join(codes))


OUTPUT_MESSAGES: Mapping[str, Tuple[str, str]] = {
    "not-a-directory": (
        "the output location exists and is not a directory",
        "pass --output a directory; the compiled files are written inside it",
    ),
    "names-a-file": (
        "--output names a file, not a directory",
        "--output names a directory; the compiled files are written inside it",
    ),
    "inside-project": (
        "the output location is inside the project or its git checkout",
        "choose a location outside the project with --output, or point XDG_STATE_HOME outside it",
    ),
}


class OutputRefused(Exception):
    """The output location is not acceptable. Carries a closed code and a
    fixed message; the path is never printed."""

    def __init__(self, code: str):
        if code not in OUTPUT_MESSAGES:
            raise ValueError("unknown refusal code")
        self.code = code
        self.fix = OUTPUT_MESSAGES[code][1]
        super().__init__(OUTPUT_MESSAGES[code][0])


# ------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Evaluation:
    effective: merge.EffectiveConfig
    qualifications: Mapping[str, qualification.QualificationResult]
    resolutions: roles.Resolutions
    findings: Tuple[Finding, ...]
    profile: str
    project_key: str
    pinned_version: str
    project_root: Path = field(repr=False, compare=False)
    allow_unqualified: bool = False


def native_provider_id(view: merge.ProviderView) -> str:
    return _native_id(view.kind, view.id)


def _native_id(kind: str, provider_id: str) -> str:
    return "openrouter" if kind == "openrouter" else NATIVE_ID_PREFIX + provider_id


def native_model_ref(res: roles.RoleResolution) -> str:
    return _native_id(res.provider_kind, res.provider_id) + "/" + res.model_id


def package_for(view: merge.ProviderView) -> str:
    key = (view.kind, view.endpoint_family)
    if key in PACKAGES:
        return PACKAGES[key]
    return PACKAGES[(view.kind, ANY)]


def _emitted_resolutions(res: roles.Resolutions) -> List[roles.RoleResolution]:
    return list(res.roles) + [a for a in res.auxiliary if a.role in AUXILIARY_EMITTED]


def _referenced(res: roles.Resolutions) -> List[roles.RoleResolution]:
    return [r for r in _emitted_resolutions(res) if r.status != "blocked"]


def _compile_findings(
    effective: merge.EffectiveConfig, resolutions: roles.Resolutions, project_root: Path
) -> Tuple[Finding, ...]:
    out: List[Finding] = []
    for res in resolutions.roles:
        agent_file = Path(project_root) / ".opencode" / "agents" / (names.role_agent_name(res.role) + ".md")
        if not agent_file.is_file():
            out.append(make_finding("agent-file-missing", True, res.role))
    referenced = _referenced(resolutions)
    openrouter_ids = sorted({r.provider_id for r in referenced if r.provider_kind == "openrouter"})
    if len(openrouter_ids) > 1:
        for pid in openrouter_ids:
            out.append(make_finding("native-id-conflict", True, pid))
    seen_models = set()
    for res in referenced:
        if res.model in seen_models:
            continue
        seen_models.add(res.model)
        if not res.model_id or _BAD_MODEL_ID_RE.search(res.model_id):
            out.append(make_finding("native-ref-invalid", True, res.model))
    if not RESPONSES_FAMILY_VERIFIED:
        for pid in sorted({r.provider_id for r in referenced if r.endpoint_family == "responses"}):
            out.append(make_finding("endpoint-family-unverified", True, pid))
    out.append(make_finding("later-layers-can-override", False))
    out.append(make_finding("policies-supplementary", False))
    return tuple(out)


def evaluate(
    *,
    project_root,
    profile: Optional[str],
    env: Mapping[str, str],
    home,
    now,
    allow_unqualified: bool = False,
    overrides: Optional[merge.Overrides] = None,
    pinned_version: Optional[str] = None,
) -> Evaluation:
    """Load, merge, qualify and resolve. `ConfigErrors`, `AdapterDataMissing`
    and `roles.AllowUnqualifiedRefused` propagate to the caller."""
    root = Path(project_root)
    loaded = config.load_all(project_root=root, profile=profile, env=env, home=home)
    effective = merge.merge(loaded, overrides=overrides)
    pinned = pinned_version if pinned_version is not None else qualification.pinned_version()
    quals = qualification.evaluate_all(
        effective, env=env, home=home, now=now, pinned_version=pinned
    )
    resolutions = roles.resolve_all(effective, quals, allow_unqualified=allow_unqualified)
    return Evaluation(
        effective=effective,
        qualifications=quals,
        resolutions=resolutions,
        findings=_compile_findings(effective, resolutions, root),
        profile=effective.profile_name,
        project_key=paths.project_key(root),
        pinned_version=pinned,
        project_root=root,
        allow_unqualified=allow_unqualified,
    )


def all_findings(ev: Evaluation) -> Tuple[Finding, ...]:
    return tuple(ev.effective.findings) + tuple(ev.resolutions.findings) + tuple(ev.findings)


def compile_blockers(ev: Evaluation) -> Tuple[Finding, ...]:
    """Blocking findings that stop compilation. An unqualified role never
    blocks: accepting `allow_unqualified` already decided that."""
    return tuple(f for f in all_findings(ev) if f.blocking and f.code != "role-unqualified")


def launchable(ev: Evaluation) -> bool:
    return ev.resolutions.launchable and not compile_blockers(ev)


# ---------------------------------------------------------- native document


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _variant_name(effort: str) -> str:
    return VARIANT_PREFIX + effort


def _emitted_views(ev: Evaluation) -> Dict[str, merge.ProviderView]:
    """Native provider id -> the profile provider it stands for, over every
    emitted resolution."""
    out: Dict[str, merge.ProviderView] = {}
    for res in _emitted_resolutions(ev.resolutions):
        view = ev.effective.providers[res.provider_id]
        out.setdefault(native_provider_id(view), view)
    return out


def _agent_entry(res: roles.RoleResolution) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"model": native_model_ref(res)}
    if res.effort_options is not None and res.effort is not None:
        entry["variant"] = _variant_name(res.effort)
    return entry


def build_document(ev: Evaluation) -> Dict[str, Any]:
    """The native configuration. Only valid for an evaluation without
    compile blockers."""
    if compile_blockers(ev):
        raise ValueError("cannot build a document while blocking findings exist")
    by_role = {r.role: r for r in ev.resolutions.roles}
    aux = {a.role: a for a in ev.resolutions.auxiliary}
    emitted = _emitted_resolutions(ev.resolutions)

    agent: Dict[str, Any] = {}
    for role in ROLES:
        agent[names.role_agent_name(role)] = _agent_entry(by_role[role])
    for name in AUXILIARY_EMITTED:
        agent[name] = {"model": native_model_ref(aux[name])}

    views = _emitted_views(ev)
    provider: Dict[str, Any] = {}
    for native_id in sorted(views):
        view = views[native_id]
        on_provider = [r for r in emitted if native_provider_id(ev.effective.providers[r.provider_id]) == native_id]
        model_ids = sorted({r.model_id for r in on_provider})
        models: Dict[str, Any] = {}
        for model_id in model_ids:
            sharing = [r for r in on_provider if r.model_id == model_id]
            entry: Dict[str, Any] = {"name": sorted(r.model for r in sharing)[0]}
            variants: Dict[str, Any] = {}
            for res in sorted(sharing, key=lambda r: r.effort or ""):
                if res.effort_options is not None and res.effort is not None:
                    variants[_variant_name(res.effort)] = _plain(res.effort_options)
            if variants:
                entry["variants"] = {k: variants[k] for k in sorted(variants)}
            models[model_id] = entry
        provider[native_id] = {
            "npm": package_for(view),
            "name": view.name or view.id,
            "options": {
                "baseURL": merge.provider_base_url(view),
                "apiKey": "{env:" + view.credential_env + "}",
            },
            "whitelist": model_ids,
            "models": models,
        }

    policies = [{"action": POLICY_ACTION, "effect": "deny", "resource": DENY_ALL_RESOURCE}]
    policies += [
        {"action": POLICY_ACTION, "effect": "allow", "resource": native_id}
        for native_id in sorted(provider)
    ]
    return {
        "$schema": CONFIG_SCHEMA_URL,
        "model": native_model_ref(by_role["coordinator"]),
        "small_model": native_model_ref(aux["title"]),
        "agent": agent,
        "share": "disabled",
        "autoupdate": False,
        "enabled_providers": sorted(provider),
        "provider": provider,
        "experimental": {"policies": policies},
    }


# ------------------------------------------------------------------ gates


@functools.lru_cache(maxsize=4)
def _native_schema(path: str) -> Dict[str, Any]:
    return jsonio.load_strict(path, file_label="native configuration schema")


def _walk_strings(node: Any, where: Tuple[str, ...]):
    """Yield (path, kind, text) for every dict key and string value."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield where + (str(key),), "key", str(key)
            yield from _walk_strings(value, where + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, where + (str(index),))
    elif isinstance(node, str):
        yield where, "value", node


def _gate_placeholder(doc: Dict[str, Any], ev: Evaluation) -> None:
    allowed: Dict[Tuple[str, ...], str] = {}
    for native_id, view in _emitted_views(ev).items():
        allowed[("provider", native_id, "options", "apiKey")] = "{env:" + view.credential_env + "}"
    for where, kind, text in _walk_strings(doc, ()):
        if not any(marker in text for marker in _PLACEHOLDER_MARKERS):
            continue
        if kind == "key":
            raise CompileGateError("placeholder")
        if allowed.get(where) != text:
            raise CompileGateError("placeholder")


def _gate_allowlist(doc: Dict[str, Any], ev: Evaluation) -> None:
    permitted = {
        native_provider_id(ev.effective.providers[pid]) for pid in ev.effective.effective_providers
    }
    provider = doc.get("provider")
    enabled = doc.get("enabled_providers")
    policies = doc.get("experimental", {}).get("policies")
    if not isinstance(provider, dict) or not isinstance(enabled, list) or not isinstance(policies, list):
        raise CompileGateError("allowlist")
    if not set(provider) <= permitted or not set(enabled) <= permitted:
        raise CompileGateError("allowlist")
    if enabled != sorted(provider):
        raise CompileGateError("allowlist")
    if not policies or policies[0] != {
        "action": POLICY_ACTION, "effect": "deny", "resource": DENY_ALL_RESOURCE
    }:
        raise CompileGateError("allowlist")
    rest = policies[1:]
    if any(p.get("effect") != "allow" or p.get("action") != POLICY_ACTION for p in rest):
        raise CompileGateError("allowlist")
    if [p.get("resource") for p in rest] != sorted(provider):
        raise CompileGateError("allowlist")


def _split_ref(ref: Any) -> Tuple[str, str]:
    if not isinstance(ref, str) or "/" not in ref:
        raise CompileGateError("reference")
    head, _, tail = ref.partition("/")
    return head, tail


def _gate_references(doc: Dict[str, Any]) -> None:
    provider = doc["provider"]
    for entry in provider.values():
        for model in entry["models"].values():
            variants = model.get("variants")
            if variants is not None and (not variants or any(not v for v in variants.values())):
                raise CompileGateError("reference")
    refs: List[Tuple[Any, Optional[str]]] = [(doc["model"], None), (doc["small_model"], None)]
    for entry in doc["agent"].values():
        refs.append((entry.get("model"), entry.get("variant")))
    for ref, variant in refs:
        head, tail = _split_ref(ref)
        if head not in provider:
            raise CompileGateError("reference")
        entry = provider[head]
        if tail not in entry["whitelist"] or tail not in entry["models"]:
            raise CompileGateError("reference")
        if variant is not None and variant not in entry["models"][tail].get("variants", {}):
            raise CompileGateError("reference")


def _gate_constants(doc: Dict[str, Any]) -> None:
    if doc.get("share") != "disabled" or doc.get("autoupdate") is not False:
        raise CompileGateError("constants")
    if doc.get("$schema") != CONFIG_SCHEMA_URL:
        raise CompileGateError("constants")


def check_gates(doc: Dict[str, Any], ev: Evaluation) -> None:
    """Re-check the finished document. The first failing gate raises."""
    schema = _native_schema(str(paths.native_schema_path()))
    if schema_check.validate(doc, schema, "config"):
        raise CompileGateError("schema")
    _gate_placeholder(doc, ev)
    _gate_allowlist(doc, ev)
    _gate_references(doc)
    _gate_constants(doc)


# ----------------------------------------------------------------- digest


def canonical_resolution(ev: Evaluation, res: roles.RoleResolution) -> Dict[str, Any]:
    native = _native_id(res.provider_kind, res.provider_id)
    return {
        "role": res.role,
        "auxiliary": res.auxiliary,
        "agent": res.role if res.auxiliary else names.role_agent_name(res.role),
        "model": res.model,
        "provider": res.provider_id,
        "native_provider": native,
        "native_model": native + "/" + res.model_id,
        "status": res.status,
        "reason": res.reason,
        "block_reason": res.block_reason,
        "origin": res.origin,
        "qualification_state": res.qualification_state,
        "effort": res.effort,
        "effort_origin": res.effort_origin,
        "variant": _variant_name(res.effort) if res.effort_options is not None and res.effort else None,
        "effort_diagnostic": res.effort_diagnostic,
        "emitted": res.role != "summary",
    }


def _endpoint(view: merge.ProviderView) -> str:
    try:
        return config.endpoint_identity(merge.provider_base_url(view))
    except ValueError:
        return ""


def digest_input(ev: Evaluation, doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "digest_format": 1,
        "effective": merge.canonical(ev.effective),
        "endpoints": {
            pid: {
                "endpoint": _endpoint(view),
                "credential_ref": merge.provider_credential_ref(view),
            }
            for pid, view in sorted(ev.effective.providers.items())
        },
        "native": doc,
        "resolutions": [
            canonical_resolution(ev, r) for r in tuple(ev.resolutions.roles) + tuple(ev.resolutions.auxiliary)
        ],
        "qualification": {
            name: {"state": q.state, "reason": q.reason, "probed_at": q.probed_at}
            for name, q in sorted(ev.qualifications.items())
        },
        "compile_findings": [
            {"code": f.code, "blocking": f.blocking, "subject": list(f.subject)} for f in ev.findings
        ],
        "launchable": launchable(ev),
        "unqualified_models": list(ev.resolutions.unqualified_models),
        "pinned_version": ev.pinned_version,
    }


def digest(ev: Evaluation, doc: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(jsonio.dump_canonical(digest_input(ev, doc))).hexdigest()


# ------------------------------------------------------------- serialization


def native_bytes(doc: Dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


PROTECTED_KEYS = (
    "$schema", "model", "small_model", "agent", "share", "autoupdate",
    "enabled_providers", "provider", "experimental.policies",
)


def sidecar(ev: Evaluation, doc: Dict[str, Any], digest_value: str) -> Dict[str, Any]:
    views = _emitted_views(ev)
    return {
        "sidecar_format": 1,
        "digest": digest_value,
        "native_sha256": hashlib.sha256(native_bytes(doc)).hexdigest(),
        "launchable": launchable(ev),
        "unqualified_models": list(ev.resolutions.unqualified_models),
        "pinned_version": ev.pinned_version,
        "profile": ev.profile,
        "project_key": ev.project_key,
        "classification": ev.effective.classification,
        "role_resolutions": [canonical_resolution(ev, r) for r in ev.resolutions.roles],
        "auxiliary_resolutions": [canonical_resolution(ev, r) for r in ev.resolutions.auxiliary],
        "effort_diagnostics": [
            {"role": r.role, "diagnostic": r.effort_diagnostic}
            for r in ev.resolutions.roles
            if r.effort_diagnostic is not None
        ],
        "credential_env": {view.credential_env: view.id for _, view in sorted(views.items())},
        "native_provider": {view.id: native_id for native_id, view in sorted(views.items())},
        "launch_requirements": {
            "config_path_env": "OPENCODE_CONFIG",
            "protected_keys": list(PROTECTED_KEYS),
            "credential_env_required": True,
        },
    }


def sidecar_bytes(data: Dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class CompileResult:
    document: Dict[str, Any] = field(repr=False)
    native_bytes: bytes = field(repr=False)
    sidecar: Dict[str, Any] = field(repr=False)
    sidecar_bytes: bytes = field(repr=False)
    digest: str
    launchable: bool


def build(ev: Evaluation) -> CompileResult:
    blockers = compile_blockers(ev)
    if blockers:
        raise CompileBlocked(blockers)
    doc = build_document(ev)
    check_gates(doc, ev)
    value = digest(ev, doc)
    side = sidecar(ev, doc, value)
    return CompileResult(
        document=doc,
        native_bytes=native_bytes(doc),
        sidecar=side,
        sidecar_bytes=sidecar_bytes(side),
        digest=value,
        launchable=launchable(ev),
    )


# ------------------------------------------------------------------ output


def _root_stats(project_root: Path, home) -> List[os.stat_result]:
    roots = [Path(project_root)]
    worktree = paths.git_worktree_root(Path(project_root), home)
    if worktree is not None:
        roots.append(worktree)
    stats = []
    for root in roots:
        try:
            stats.append(os.stat(root))
        except OSError:
            continue
    return stats


def refuse_inside_project(target, project_root, *, home=None) -> None:
    """Refuse a target that is, or lies below, the project root or its git
    checkout. Identity is decided by `stat`, so symlink aliases and
    differently-cased spellings of the same directory cannot slip through."""
    stats = _root_stats(Path(project_root), home)
    probe = os.path.realpath(str(target))
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    while True:
        try:
            info = os.stat(probe)
        except OSError:
            info = None
        if info is not None and any(os.path.samestat(info, root) for root in stats):
            raise OutputRefused("inside-project")
        parent = os.path.dirname(probe)
        if parent == probe:
            return
        probe = parent


def resolve_output_dir(ev: Evaluation, *, output, env: Mapping[str, str], home) -> Path:
    if output is not None:
        target = Path(os.path.abspath(str(output)))
        if target.exists():
            if not target.is_dir():
                raise OutputRefused("not-a-directory")
        elif target.name.endswith(".json"):
            raise OutputRefused("names-a-file")
    else:
        target = paths.compiled_output_dir(ev.profile, ev.project_root, env, home)
    refuse_inside_project(target, ev.project_root, home=home)
    return target


def write(result: CompileResult, directory) -> Path:
    """Write the pair privately. The sidecar goes last, so an interrupted
    write leaves a pair that `check` reports as stale."""
    directory = Path(directory)
    jsonio.write_private_atomic(directory / NATIVE_FILE, result.native_bytes, private_parent=True)
    jsonio.write_private_atomic(directory / SIDECAR_FILE, result.sidecar_bytes, private_parent=True)
    return directory / NATIVE_FILE


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reasons: Tuple[str, ...]


CHECK_REASONS = ("missing", "not-private", "flag-mismatch", "stale")


def check(ev: Evaluation, directory) -> CheckResult:
    """Compare the files in `directory` with a fresh in-memory build. Never
    writes. `CompileBlocked` and `CompileGateError` from the rebuild
    propagate."""
    directory = Path(directory)
    native_path, side_path = directory / NATIVE_FILE, directory / SIDECAR_FILE
    stored_native = jsonio.read_regular_bytes(native_path, max_bytes=MAX_OUTPUT_BYTES)
    stored_side = jsonio.read_regular_bytes(side_path, max_bytes=MAX_OUTPUT_BYTES)
    if stored_native is None or stored_side is None:
        return CheckResult(False, ("missing",))
    reasons: List[str] = []
    try:
        dir_mode = os.stat(directory).st_mode
    except OSError:
        return CheckResult(False, ("missing",))
    if (
        stored_native[1].st_mode & 0o077
        or stored_side[1].st_mode & 0o077
        or dir_mode & 0o077
    ):
        reasons.append("not-private")
    try:
        stored = json.loads(stored_side[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return CheckResult(False, tuple(reasons + ["stale"]))
    if not isinstance(stored, dict):
        return CheckResult(False, tuple(reasons + ["stale"]))
    # A file compiled with unqualified models needs the same flag to be
    # rebuilt. Without it, the rebuild is refused only while some model still
    # lacks a valid qualification; once every model is qualified the files are
    # simply stale.
    fresh = None
    unqualified = stored.get("unqualified_models")
    if unqualified and not ev.allow_unqualified:
        try:
            fresh = build(ev)
        except CompileBlocked as exc:
            if any(
                f.code == "role-blocked"
                and len(f.subject) > 1
                and f.subject[1].startswith("qualification-")
                for f in exc.findings
            ):
                return CheckResult(False, tuple(reasons + ["flag-mismatch"]))
            raise
    if fresh is None:
        fresh = build(ev)
    if fresh.native_bytes != stored_native[0] or fresh.sidecar_bytes != stored_side[0]:
        reasons.append("stale")
    return CheckResult(not reasons, tuple(reasons))
