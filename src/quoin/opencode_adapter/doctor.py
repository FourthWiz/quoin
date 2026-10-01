"""Doctor for the OpenCode adapter.

Two entry paths: `run_smoke` renders the catalog offline and checks the
result against itself — no host filesystem or environment is read, so it
works with no `opencode` binary and no network. `run_host` adds
installed-state, config, skill-discovery and PATH checks against the
running machine.

Redaction by construction: every finding message comes from the
`MESSAGES` template table, whose only substitutable fields are the ones
`make_finding` allowlists. There is no other way to build a `Finding` in
this module. Any path-like value passed to `make_finding` must already be
a display path (see `display_path`) — never a raw filesystem path pulled
from an environment variable or a config file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from quoin import __about__

from . import frontmatter, generate, install, manifest, names

SCHEMA_VERSION = 1

_SEVERITIES = ("error", "warn", "info", "ok")

# The only fields a MESSAGES template may reference. `make_finding` rejects
# any other keyword so a caller cannot accidentally interpolate raw file
# content, an exception string or an environment value into a finding.
_FIELD_NAMES = frozenset(("path", "name", "names", "count", "number", "version", "expected"))

# Read-only roles are those the generator itself denies edit/bash/task to
# (see generate.role_permissions). Read from the generator's own table so
# this stays in sync with the source of truth instead of duplicating it.
_READ_ONLY_ROLES = generate._READ_ONLY_ROLES  # noqa: SLF001 - intentional, same package


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    message: str
    path: Optional[str] = None
    remediation: Optional[str] = None


MESSAGES: Dict[str, str] = {
    "smoke-render": "the generator could not render the catalog into files",
    "smoke-roundtrip": "the bytes read back from a temporary write do not match the rendered content",
    "smoke-names": "the generated name does not meet OpenCode's naming requirements",
    "smoke-frontmatter": "the rendered frontmatter did not parse",
    "smoke-digests": "the metadata digest is not stable across a second render",
    "smoke-bundle": "the rendered file bundle does not match the manifest and catalog",
    "smoke-read-only-roles": "the read-only permission contract is not enforced",
    "smoke-task-graph": "the task-delegation graph built from the rendered agent files is invalid",
    "smoke-forbidden-output": "a forbidden-output check failed for the rendered files after a round trip",
    "smoke-script-refs": "a rendered file references a helper script that is not on the allowlist",
    "smoke-config": "the rendered OpenCode config file is invalid",
    "smoke-ok": "offline smoke checks passed for %(count)s rendered files",
    "render-failed": "the generator could not render the catalog into files",
    "manifest-drift": "the source tree has drifted from the pinned manifest (%(count)s difference(s))",
    "manifest-unreadable": "the manifest could not be checked against the source tree",
    "install-metadata-invalid": "the install ownership record is invalid",
    "install-absent": "no install ownership record was found",
    "owned-missing": "an owned file is missing",
    "owned-modified": "an owned file has been modified since install",
    "owned-stale": "an owned file is stale relative to a fresh render",
    "owned-not-installed": "a rendered file exists at this path but is not owned by this install",
    "owned-unreadable": "an owned file could not be read",
    "install-current": "the install matches a fresh render (%(count)s file(s))",
    "install-version-differs": "the installed version %(version)s differs from the current %(expected)s",
    "temp-leftover": "a temporary file left over from an interrupted install or uninstall was found",
    "rules-global-claude-md": "a global Claude Code rules file exists with no OpenCode AGENTS.md fallback",
    "rules-project-claude-md": "a project Claude Code rules file exists with no OpenCode AGENTS.md fallback",
    "rules-agents-md-present": "an OpenCode AGENTS.md file was found",
    "flags-set": "the following OpenCode compatibility flags are set: %(names)s",
    "project-config-disabled": "OPENCODE_DISABLE_PROJECT_CONFIG is set, so project config files are not read",
    "config-env-set": "the following config location variables are set: %(names)s",
    "config-unreadable": "a config file could not be read or parsed",
    "subagent-depth-raised": "a config file sets a subagent depth of %(number)s",
    "quoin-not-on-path": "the quoin command was not found on PATH",
    "opencode-binary-absent": "the opencode binary was not found on PATH",
    "opencode-version": "the opencode binary reports version %(version)s (pinned %(expected)s)",
    "opencode-version-unknown": "the opencode binary's version could not be determined",
    "census-unverified": "%(count)s SKILL.md file(s) could not have their name read with certainty and were left out of the duplicate and legacy checks",
    "skill-duplicate": "the skill '%(name)s' is defined by more than one file: %(names)s",
    "skill-duplicate-unnamed": "a skill name is defined by more than one file: %(names)s",
    "quoin-skill-outside-project": "a skill named '%(name)s' was found outside the project's own .opencode/skills directory",
    "legacy-claude-skills": "%(count)s SKILL.md file(s) under the legacy Claude Code skills location have a name OpenCode may also load",
    "skills-url-not-scanned": "%(count)s skills.urls entry(ies) are configured; this doctor does not fetch or scan them",
    "skills-path-missing": "skills.paths[%(number)s] in %(path)s names a directory that does not exist",
    "census-truncated": "the skill census stopped early after reaching its file or directory visit cap (%(count)s file(s) counted)",
    "permission-loosened": "a config file's user permission narrows the following tools, but a generated role allows them while it runs: %(names)s",
    "doctor-internal-error": "the doctor could not complete because of an unexpected host or config condition (%(name)s)",
}

_REMEDIATIONS: Dict[str, str] = {
    "smoke-render": "run `quoin install --runtime opencode --check` to see the generation error",
    "manifest-drift": "run `python -m quoin.opencode_adapter check-manifest` to see the drifted entries",
    "install-absent": "run `quoin install --runtime opencode` to install",
    "owned-missing": "run `quoin install --runtime opencode --check` then re-run install to recreate it",
    "owned-modified": "run `quoin install --runtime opencode --check` to see the diff, then re-run install to restore it",
    "owned-stale": "re-run `quoin install --runtime opencode` to update it",
    "owned-not-installed": "an unrelated file occupies a path Quoin also generates; move it aside or remove it",
    "owned-unreadable": "fix the file's permissions, then re-run install to verify it",
    "temp-leftover": "the next install or uninstall sweeps it automatically",
    "rules-global-claude-md": "set OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 to silence this, or add an OpenCode AGENTS.md",
    "rules-project-claude-md": "add a project AGENTS.md so OpenCode reads Quoin's own rules instead of CLAUDE.md",
    "project-config-disabled": "unset OPENCODE_DISABLE_PROJECT_CONFIG to have OpenCode read project config",
    "config-unreadable": "fix or remove the file so OpenCode and this doctor can read it",
    "subagent-depth-raised": "the effective value depends on which config files load and in what order; the doctor does not compute it",
    "config-env-set": "OPENCODE_CONFIG_CONTENT, when set, is not inspected",
    "quoin-not-on-path": "install quoin and confirm it is on PATH",
    "opencode-binary-absent": "install the opencode binary to run host checks against it",
    "census-unverified": "read each file by hand to see whether OpenCode is likely to load it",
    "quoin-skill-outside-project": "a skill named this may shadow or be shadowed by the project's own generated skill; move it or rename it",
    "legacy-claude-skills": "set OPENCODE_DISABLE_CLAUDE_CODE_SKILLS or OPENCODE_DISABLE_CLAUDE_CODE to stop OpenCode from scanning it; Quoin's generated roles already deny skills outside the quoin-* set",
    "skills-path-missing": "create the directory, or remove the entry from skills.paths",
    "permission-loosened": "a generated role's own permission can override a stricter user config while that role runs directly; narrow the role's own permission map instead of the user config",
}


def make_finding(id: str, severity: str, path: Optional[str] = None, remediation: Optional[str] = None, **fields):
    """The only constructor for a `Finding`. Raises `ValueError` on an
    unknown id, an unknown severity or an unknown field name."""
    if id not in MESSAGES:
        raise ValueError("unknown finding id %r" % (id,))
    if severity not in _SEVERITIES:
        raise ValueError("unknown finding severity %r" % (severity,))
    unknown = set(fields) - _FIELD_NAMES
    if unknown:
        raise ValueError("unknown finding field(s) %r for id %r" % (sorted(unknown), id))
    format_fields = dict(fields)
    if path is not None:
        format_fields.setdefault("path", path)
    template = MESSAGES[id]
    message = template % format_fields if format_fields else template
    if remediation is None:
        remediation = _REMEDIATIONS.get(id)
    return Finding(id=id, severity=severity, message=message, path=path, remediation=remediation)


def display_path(p, roots: List[Tuple[str, str]]) -> str:
    """Render `p` under the longest matching root in `roots`.

    `roots` is a list of `(prefix, rendered_name)` pairs. Every root and
    `p` are normalized with `os.path.abspath` (never realpath) and matched
    by path components, so a sibling that merely shares a string prefix
    (`/t/ocd-x` against root `/t/ocd`) does not match. When no root
    matches, `p` is printed absolute (never realpath-resolved).
    """
    norm_p = os.path.abspath(str(p))
    p_parts = Path(norm_p).parts
    best_parts: Optional[Tuple[str, ...]] = None
    best_name: Optional[str] = None
    for prefix, rendered_name in roots:
        norm_prefix = os.path.abspath(str(prefix))
        prefix_parts = Path(norm_prefix).parts
        if len(prefix_parts) > len(p_parts):
            continue
        if p_parts[: len(prefix_parts)] != prefix_parts:
            continue
        if best_parts is None or len(prefix_parts) > len(best_parts):
            best_parts = prefix_parts
            best_name = rendered_name
    if best_parts is None:
        return norm_p
    rest = "/".join(p_parts[len(best_parts):])
    name = best_name or ""
    if not rest:
        return name
    if name.endswith("/"):
        return name + rest
    return name + "/" + rest


def report_status(findings: List[Finding]) -> str:
    if any(f.severity == "error" for f in findings):
        return "errors"
    if any(f.severity == "warn" for f in findings):
        return "warnings"
    return "healthy"


def exit_code(status: str) -> int:
    return {"healthy": 0, "warnings": 4, "errors": 1}[status]


def _sort_key(f: Finding):
    return (f.id, f.path or "", f.message)


def render_text(findings: List[Finding], status: str) -> str:
    lines: List[str] = []
    counts = {s: 0 for s in _SEVERITIES}
    for severity in _SEVERITIES:
        group = sorted((f for f in findings if f.severity == severity), key=_sort_key)
        counts[severity] = len(group)
        for f in group:
            line = "%s %s: %s" % (severity.upper(), f.id, f.message)
            if f.path:
                line += " [%s]" % f.path
            lines.append(line)
            if f.remediation:
                lines.append("    %s" % f.remediation)
    lines.append(
        "opencode doctor: %s (errors=%d, warnings=%d, info=%d)"
        % (status, counts["error"], counts["warn"], counts["info"])
    )
    return "\n".join(lines) + "\n"


def render_json(findings: List[Finding], status: str) -> str:
    from . import categories  # noqa: PLC0415 - kept lazy; see categories.py

    finding_objs = []
    for f in sorted(findings, key=_sort_key):
        obj = {
            "id": f.id,
            "severity": f.severity,
            "message": f.message,
            "category": categories.category_for(f.id),
        }
        if f.path is not None:
            obj["path"] = f.path
        if f.remediation is not None:
            obj["remediation"] = f.remediation
        finding_objs.append(obj)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "runtime": "opencode",
        "status": status,
        "findings": finding_objs,
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def _skill_basename(relpath: str) -> str:
    # ".opencode/skills/<name>/SKILL.md" -> "<name>"
    return Path(relpath).parent.name


def _command_or_agent_basename(relpath: str) -> str:
    # ".opencode/commands/<name>.md" / ".opencode/agents/<name>.md" -> "<name>"
    return Path(relpath).stem


def _check_smoke_names(files: Dict[str, "generate.RenderedFile"]) -> List[Finding]:
    findings: List[Finding] = []
    for relpath, rf in files.items():
        if rf.kind not in ("command", "agent", "skill"):
            continue
        basename = _skill_basename(relpath) if rf.kind == "skill" else _command_or_agent_basename(relpath)
        if names.name_error(basename) is not None:
            findings.append(make_finding("smoke-names", "error", path=relpath))
    return findings


def _parse_frontmatter_all(
    files: Dict[str, "generate.RenderedFile"]
) -> Tuple[Dict[str, dict], List[Finding]]:
    parsed: Dict[str, dict] = {}
    findings: List[Finding] = []
    for relpath, rf in files.items():
        if rf.kind not in ("command", "agent", "skill"):
            continue
        try:
            fields, _body = frontmatter.parse(rf.content.decode("utf-8"))
        except frontmatter.FrontmatterError:
            findings.append(make_finding("smoke-frontmatter", "error", path=relpath))
            continue
        parsed[relpath] = fields
    return parsed, findings


def _check_smoke_digests(
    files: Dict[str, "generate.RenderedFile"], parsed: Dict[str, dict], files2: Optional[Dict[str, "generate.RenderedFile"]]
) -> List[Finding]:
    findings: List[Finding] = []
    for relpath, rf in files.items():
        if rf.kind == "skill":
            fields = parsed.get(relpath)
            metadata = fields.get("metadata") if fields else None
            digest = metadata.get("source_digest") if isinstance(metadata, dict) else None
            if digest != rf.source_digest:
                findings.append(make_finding("smoke-digests", "error", path=relpath))
                continue
        if files2 is not None:
            rf2 = files2.get(relpath)
            if rf2 is None or rf2.content != rf.content or rf2.source_digest != rf.source_digest:
                findings.append(make_finding("smoke-digests", "error", path=relpath))
    return findings


def _check_smoke_bundle(files: Dict[str, "generate.RenderedFile"]) -> List[Finding]:
    command_ids = {rf.source_id for rf in files.values() if rf.kind == "command"}
    skill_ids = {rf.source_id for rf in files.values() if rf.kind == "skill"}
    if command_ids != skill_ids or None in command_ids or not command_ids:
        return [make_finding("smoke-bundle", "error")]
    agent_roles = {rf.source_id for rf in files.values() if rf.kind == "agent"}
    if not agent_roles or None in agent_roles:
        return [make_finding("smoke-bundle", "error")]
    has_instructions = any(rf.kind == "instructions" for rf in files.values())
    has_config = any(rf.kind == "config" for rf in files.values())
    if not (has_instructions and has_config):
        return [make_finding("smoke-bundle", "error")]
    return []


def _check_smoke_read_only_roles(files: Dict[str, "generate.RenderedFile"], parsed: Dict[str, dict]) -> List[Finding]:
    findings: List[Finding] = []
    for relpath, rf in files.items():
        if rf.kind != "agent" or rf.source_id not in _READ_ONLY_ROLES:
            continue
        fields = parsed.get(relpath)
        permission = fields.get("permission") if fields else None
        if not isinstance(permission, dict) or not permission:
            findings.append(make_finding("smoke-read-only-roles", "error", path=relpath))
            continue
        first_key = next(iter(permission))
        if first_key != "*" or permission[first_key] != "deny":
            findings.append(make_finding("smoke-read-only-roles", "error", path=relpath))
            continue
        for forbidden in ("edit", "bash", "task"):
            action = permission.get(forbidden)
            if action == "allow":
                findings.append(make_finding("smoke-read-only-roles", "error", path=relpath))
                break
            if isinstance(action, dict) and any(v == "allow" for v in action.values()):
                findings.append(make_finding("smoke-read-only-roles", "error", path=relpath))
                break
    return findings


def _check_smoke_task_graph(files: Dict[str, "generate.RenderedFile"], parsed: Dict[str, dict]) -> List[Finding]:
    """Rebuild the delegation graph purely from the rendered agent
    frontmatter (mode + permission.task), independent of the generator's
    own in-memory role tables, and validate it with the same rules
    `generate.check_task_graph` documents: depth never exceeds 1, and only
    a `primary`-mode role may hold an allowed task edge."""
    agent_defs: Dict[str, dict] = {}
    for relpath, rf in files.items():
        if rf.kind != "agent":
            continue
        fields = parsed.get(relpath)
        if fields is None:
            continue
        agent_defs[rf.source_id] = fields

    def targets(fields: dict) -> List[str]:
        task_rule = fields.get("permission", {}).get("task") if isinstance(fields.get("permission"), dict) else None
        if not isinstance(task_rule, dict):
            return []
        return sorted(agent for agent, action in task_rule.items() if agent != "*" and action == "allow")

    role_agent = {role: names.role_agent_name(role) for role in agent_defs}
    agent_to_role = {agent: role for role, agent in role_agent.items()}

    for role, fields in agent_defs.items():
        for target_agent in targets(fields):
            target_role = agent_to_role.get(target_agent)
            if target_role is None:
                return [make_finding("smoke-task-graph", "error")]
            if fields.get("mode") != "primary":
                return [make_finding("smoke-task-graph", "error")]
            target_fields = agent_defs[target_role]
            if target_fields.get("mode") not in ("subagent", "all"):
                return [make_finding("smoke-task-graph", "error")]
            if targets(target_fields):
                return [make_finding("smoke-task-graph", "error")]
    return []


def _check_smoke_script_refs(files: Dict[str, "generate.RenderedFile"]) -> List[Finding]:
    from . import scripts  # local import: keeps a narrow dependency surface for the smoke path

    findings: List[Finding] = []
    for relpath, rf in files.items():
        text = rf.content.decode("utf-8")
        for name in scripts.referenced_scripts(text):
            if name not in scripts.ALLOWED_SCRIPTS:
                findings.append(make_finding("smoke-script-refs", "error", path=relpath))
                break
    return findings


def _check_smoke_config(files: Dict[str, "generate.RenderedFile"]) -> List[Finding]:
    config_files = [rf for rf in files.values() if rf.kind == "config"]
    if len(config_files) != 1:
        return [make_finding("smoke-config", "error")]
    rf = config_files[0]
    try:
        text = rf.content.decode("utf-8")
    except UnicodeDecodeError:
        return [make_finding("smoke-config", "error")]
    # Strip // line comments the same tolerant way the config is meant to
    # be read (the generator's own template never emits block comments or
    # trailing commas in the config it ships, so that much is enough here).
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [make_finding("smoke-config", "error")]
    if not isinstance(payload, dict) or set(payload) != {"$schema", "instructions"}:
        return [make_finding("smoke-config", "error")]
    if generate.INSTRUCTIONS_PATH not in str(payload.get("instructions", "")):
        return [make_finding("smoke-config", "error")]
    return []


def run_smoke(source_dir) -> List[Finding]:
    """Offline smoke checks: no filesystem or environment outside
    `source_dir` and a temporary directory is read, and no network call is
    made. Safe to run with no `opencode` binary installed."""
    try:
        inputs = generate.load_inputs(source_dir)
        files = generate.render(inputs)
    except (generate.GenerationError, manifest.ManifestLoadError):
        return [make_finding("smoke-render", "error")]

    findings: List[Finding] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for relpath, rf in files.items():
            dest = tmp_path / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(rf.content)
        back: Dict[str, bytes] = {}
        for relpath in files:
            back[relpath] = (tmp_path / relpath).read_bytes()

    for relpath, rf in files.items():
        if back[relpath] != rf.content:
            findings.append(make_finding("smoke-roundtrip", "error", path=relpath))

    findings.extend(_check_smoke_names(files))

    parsed, frontmatter_findings = _parse_frontmatter_all(files)
    findings.extend(frontmatter_findings)

    try:
        files2 = generate.render(generate.load_inputs(source_dir))
    except (generate.GenerationError, manifest.ManifestLoadError):
        files2 = None
    findings.extend(_check_smoke_digests(files, parsed, files2))

    findings.extend(_check_smoke_bundle(files))
    findings.extend(_check_smoke_read_only_roles(files, parsed))
    findings.extend(_check_smoke_task_graph(files, parsed))

    back_as_rendered = {
        relpath: generate.RenderedFile(
            relpath=relpath,
            content=back[relpath],
            kind=rf.kind,
            source_id=rf.source_id,
            source_digest=rf.source_digest,
        )
        for relpath, rf in files.items()
    }
    if generate.check_rendered(back_as_rendered):
        findings.append(make_finding("smoke-forbidden-output", "error"))

    findings.extend(_check_smoke_script_refs(files))
    findings.extend(_check_smoke_config(files))

    if not findings:
        findings.append(make_finding("smoke-ok", "ok", count=len(files)))
    return findings


def run_doctor(
    project_root,
    source_dir,
    smoke: bool,
    as_json: bool,
    out,
    err,
    env=None,
    home=None,
    which=None,
    version_runner=None,
) -> int:
    """Run the doctor and print a report to `out`. `env`, `home`, `which`
    and `version_runner` default at call time (not at definition time), so
    monkeypatching `os.environ`/`Path.home`/`shutil.which` reaches a run
    started through `quoin.cli.main`."""
    root = Path(project_root)
    if not root.is_dir():
        print("opencode doctor: project root %s is not a directory" % project_root, file=err)
        return 2

    try:
        if smoke:
            findings = run_smoke(source_dir)
        else:
            findings = run_host(
                root,
                source_dir,
                os.environ if env is None else env,
                Path.home() if home is None else home,
                __import__("shutil").which if which is None else which,
                version_runner,
            )
    except (ValueError, OSError, RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Last-resort boundary: nothing here may reach `out`/`err` except
        # the exception's class name (never `str(exc)`, which can carry a
        # raw path or config value) — see the module docstring's redaction
        # contract. Individual channels (config parsing, host `is_dir`
        # probes) already degrade to a specific finding where practical;
        # this only catches what those miss.
        findings = [make_finding("doctor-internal-error", "error", name=type(exc).__name__)]

    status = report_status(findings)
    text = render_json(findings, status) if as_json else render_text(findings, status)
    print(text, end="", file=out)
    return exit_code(status)


class _ConfigUnreadable(Exception):
    """Internal sentinel: a config file exists but could not be read or
    parsed as a JSONC object. Never escapes this module."""


_LEGACY_TRUTHY = frozenset(("true", "1"))
_RUNTIME_TRUTHY = frozenset(("true", "yes", "on", "1", "y"))

# Runtime flags use Effect's Config.boolean; the legacy flag uses the
# stricter Flag.truthy. Any other value is treated as unset (errs toward
# showing the fallback warning rather than claiming OpenCode ignores it).
_RUNTIME_FLAGS = (
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
)
_LEGACY_FLAGS = ("OPENCODE_DISABLE_PROJECT_CONFIG",)
_CONFIG_LOCATION_VARS = ("OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT")

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _safe_version(value) -> str:
    """A metadata-sourced version string, exactly as it will be printed in
    a finding message. `install.py` only checks `isinstance(str)` before
    writing it to the install-ownership record, so a doctor run must not
    trust it to be a well-formed version on its own — an operator-editable
    (or corrupted) metadata file could carry an arbitrary string, including
    an ANSI escape or embedded secret-looking text."""
    if isinstance(value, str) and _VERSION_RE.fullmatch(value):
        return value
    return "unknown"


def _flag_set(env, name: str) -> bool:
    value = env.get(name)
    if value is None:
        return False
    truthy = _LEGACY_TRUTHY if name in _LEGACY_FLAGS else _RUNTIME_TRUTHY
    return value.lower() in truthy


def _xdg_config_dir(env, home) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    return Path(base) / "opencode"


def _worktree_root(root) -> Path:
    """Nearest ancestor of `root` holding `.git` (file or dir); the
    filesystem root if none is found, matching OpenCode's non-git worktree
    fallback (project/project.ts L217)."""
    current = Path(os.path.abspath(str(root)))
    while True:
        # `os.path.lexists` (unlike `Path.exists()`) swallows any `OSError`
        # — including `EACCES` on Python 3.10-3.13, where `Path.exists()`
        # re-raises a permission error instead of treating it as absent.
        if os.path.lexists(str(current / ".git")):
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


def _dir_chain(worktree: Path, root: Path) -> List[Path]:
    """`root`, its parent, ... up to and including `worktree` (nearest
    first). `root` must be `worktree` or one of its descendants."""
    chain: List[Path] = []
    current = Path(root)
    worktree = Path(worktree)
    while True:
        chain.append(current)
        if current == worktree:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return chain


def _build_roots(root, env, home) -> List[Tuple[str, str]]:
    roots: List[Tuple[str, str]] = [(str(root), ".")]
    config_dir = env.get("OPENCODE_CONFIG_DIR")
    if config_dir:
        roots.append((config_dir, "$OPENCODE_CONFIG_DIR"))
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        roots.append((xdg, "$XDG_CONFIG_HOME"))
    roots.append((str(home), "~"))
    config_file = env.get("OPENCODE_CONFIG")
    if config_file:
        roots.append((config_file, "$OPENCODE_CONFIG"))
    return roots


def _install_state_findings(root: Path, rendered, source_dir, roots) -> List[Finding]:
    findings: List[Finding] = []
    try:
        meta = install.load_metadata(root)
    except install.InstallError:
        meta_path = root / install.METADATA_RELPATH
        return [make_finding("install-metadata-invalid", "error", path=display_path(meta_path, roots))]
    if meta is None:
        return [make_finding("install-absent", "warn")]

    owned = meta.owned
    rendered_set = set(rendered)
    owned_set = set(owned)
    unchanged = 0
    bad_ids = {"owned-missing", "owned-modified", "owned-stale", "owned-not-installed", "owned-unreadable"}

    for relpath in sorted(rendered_set | owned_set):
        display = display_path(root / relpath, roots)
        try:
            state = install.inspect_path(root, relpath)
        except install.InstallError:
            findings.append(make_finding("owned-unreadable", "warn", path=display))
            continue

        if relpath in owned_set and relpath not in rendered_set:
            if state.state == "regular":
                findings.append(make_finding("owned-stale", "warn", path=display))
            continue
        if relpath not in owned_set:
            if state.state == "regular":
                findings.append(make_finding("owned-not-installed", "warn", path=display))
            continue

        record = owned[relpath]
        if state.state != "regular":
            findings.append(make_finding("owned-missing", "warn", path=display))
            continue
        sha = hashlib.sha256(state.data or b"").hexdigest()
        if sha != record["sha256"]:
            findings.append(make_finding("owned-modified", "warn", path=display))
            continue
        rf = rendered.get(relpath)
        if rf is not None and record["source_digest"] != rf.source_digest:
            findings.append(make_finding("owned-stale", "warn", path=display))
            continue
        unchanged += 1

    if unchanged and not any(f.id in bad_ids for f in findings):
        findings.append(make_finding("install-current", "ok", count=unchanged))

    try:
        pinned = manifest.read_pinned_version(source_dir)
    except manifest.ManifestLoadError:
        pinned = None
    if meta.quoin_version != __about__.__version__ or (pinned is not None and meta.opencode_version != pinned):
        safe_quoin_version = _safe_version(meta.quoin_version)
        safe_opencode_version = _safe_version(meta.opencode_version)
        findings.append(
            make_finding(
                "install-version-differs",
                "info",
                version="quoin %s, opencode %s" % (safe_quoin_version, safe_opencode_version),
                expected="quoin %s, opencode %s" % (__about__.__version__, pinned or safe_opencode_version),
            )
        )

    for created_dir in meta.created_dirs:
        dir_path = root / created_dir
        try:
            entries = os.listdir(dir_path)
        except OSError:
            continue
        for entry in sorted(entries):
            if install.TEMP_NAME_RE.fullmatch(entry):
                findings.append(make_finding("temp-leftover", "info", path=display_path(dir_path / entry, roots)))

    return findings


def _rules_findings(root: Path, worktree: Path, env, home, roots) -> List[Finding]:
    findings: List[Finding] = []
    prompt_off = _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT") or _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE")

    config_dir_env = env.get("OPENCODE_CONFIG_DIR")
    g_rules = Path(config_dir_env) if config_dir_env else _xdg_config_dir(env, home)
    global_agents = g_rules / "AGENTS.md"
    global_claude_md = Path(home) / ".claude" / "CLAUDE.md"
    if not global_agents.is_file() and not prompt_off and global_claude_md.is_file():
        findings.append(make_finding("rules-global-claude-md", "warn", path=display_path(global_claude_md, roots)))

    if not _flag_set(env, "OPENCODE_DISABLE_PROJECT_CONFIG"):
        for d in _dir_chain(worktree, root):
            agents = d / "AGENTS.md"
            if agents.is_file():
                findings.append(make_finding("rules-agents-md-present", "info", path=display_path(agents, roots)))
                break
            claude_md = d / "CLAUDE.md"
            if not prompt_off and claude_md.is_file():
                findings.append(make_finding("rules-project-claude-md", "warn", path=display_path(claude_md, roots)))
                break
            if (d / "CONTEXT.md").is_file():
                break
    return findings


def _flag_findings(env) -> List[Finding]:
    findings: List[Finding] = []
    set_names = sorted(n for n in _RUNTIME_FLAGS + _LEGACY_FLAGS if _flag_set(env, n))
    if set_names:
        findings.append(make_finding("flags-set", "info", names=", ".join(set_names)))
    if _flag_set(env, "OPENCODE_DISABLE_PROJECT_CONFIG"):
        findings.append(make_finding("project-config-disabled", "error"))
    location_names = sorted(n for n in _CONFIG_LOCATION_VARS if env.get(n) is not None)
    if location_names:
        findings.append(make_finding("config-env-set", "info", names=", ".join(location_names)))
    return findings


def _path_findings(which) -> List[Finding]:
    if which("quoin") is None:
        return [make_finding("quoin-not-on-path", "warn")]
    return []


def _default_version_runner(path: str) -> Optional[str]:
    probe_env = dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            probe_env[var] = tmp
        try:
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
                env=probe_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _binary_findings(which, version_runner, source_dir) -> List[Finding]:
    path = which("opencode")
    if path is None:
        return [make_finding("opencode-binary-absent", "info")]
    runner = version_runner or _default_version_runner
    output = runner(path)
    match = _VERSION_RE.search(output) if output else None
    if match is None:
        return [make_finding("opencode-version-unknown", "info")]
    try:
        pinned = manifest.read_pinned_version(source_dir)
    except manifest.ManifestLoadError:
        pinned = "unknown"
    return [make_finding("opencode-version", "info", version=match.group(0), expected=pinned)]


def _strip_jsonc(text: str) -> str:
    """Strip `//` and `/* */` comments outside string literals, then strip
    a trailing comma before a closing `}` or `]`."""
    out: List[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    stripped = "".join(out)

    # Strip a trailing comma before a closing `}` or `]`, outside string
    # literals only: the naive whole-text regex this replaced would also
    # strip a `,}`/`,]` substring sitting inside a JSON string *value*
    # (comments are already gone above, so this second string-aware pass
    # only has to track quoting, not comments too).
    result: List[str] = []
    in_string = False
    escape = False
    j = 0
    m = len(stripped)
    while j < m:
        c = stripped[j]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            j += 1
            continue
        if c == '"':
            in_string = True
            result.append(c)
            j += 1
            continue
        if c == ",":
            k = j + 1
            while k < m and stripped[k] in " \t\r\n":
                k += 1
            if k < m and stripped[k] in "}]":
                j += 1
                continue  # drop the trailing comma; the closing brace/bracket is appended on its own turn
        result.append(c)
        j += 1
    return "".join(result)


def _load_config_file(path: Path) -> Optional[dict]:
    """`None` when the file does not exist. Raises `_ConfigUnreadable` when
    it exists but cannot be read or parsed as a JSON object."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError:
        raise _ConfigUnreadable()
    if not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        raise _ConfigUnreadable()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise _ConfigUnreadable()
    try:
        payload = json.loads(_strip_jsonc(text))
    except (json.JSONDecodeError, ValueError, RecursionError):
        # `ValueError` also covers a bare int-string-conversion limit error
        # (a huge integer literal, e.g. `subagent_depth` with thousands of
        # digits) that `json.loads` can raise outside `JSONDecodeError` —
        # not a parse error by class, but this config file is equally
        # unreadable either way.
        raise _ConfigUnreadable()
    if not isinstance(payload, dict):
        raise _ConfigUnreadable()
    return payload


def _project_config_dirs(worktree: Path, root: Path) -> List[Path]:
    """Worktree first, project root last: later files override earlier
    ones, so the directory nearest `root` wins."""
    return list(reversed(_dir_chain(worktree, root)))


def _read_config_files(root: Path, worktree: Path, env, home, roots) -> Tuple[List[Tuple[Path, dict]], List[Finding]]:
    docs: List[Tuple[Path, dict]] = []
    findings: List[Finding] = []

    def _try(path: Path) -> None:
        try:
            payload = _load_config_file(path)
        except _ConfigUnreadable:
            findings.append(make_finding("config-unreadable", "warn", path=display_path(path, roots)))
            return
        if payload is not None:
            docs.append((path, payload))

    g_xdg = _xdg_config_dir(env, home)
    for name in ("config.json", "opencode.json", "opencode.jsonc"):
        _try(g_xdg / name)

    config_file_env = env.get("OPENCODE_CONFIG")
    if config_file_env:
        _try(Path(config_file_env))

    project_disabled = _flag_set(env, "OPENCODE_DISABLE_PROJECT_CONFIG")
    project_dirs: List[Path] = []
    if not project_disabled:
        project_dirs = _project_config_dirs(worktree, root)
        for d in project_dirs:
            _try(d / "opencode.json")
            _try(d / "opencode.jsonc")

    # `project_dirs` are worktree-chain directories that each need their own
    # `.opencode` subdir appended. `home_opencode` and `config_dir_env`, by
    # contrast, already name a `.opencode`-suffixed (or override) directory
    # and must be read directly — appending another `.opencode` under them
    # reads a directory OpenCode never does (`config/paths.ts` L23-39: the
    # walked-up home entry stops at `home`, so it is exactly `home/.opencode`;
    # `config/config.ts` L438-448 reads `opencode.json(c)` directly from any
    # directory in that list whose name already ends in `.opencode`, or that
    # equals `OPENCODE_CONFIG_DIR`, with no extra subdir appended).
    dotted_dirs: List[Path] = list(project_dirs)  # each dir's own `.opencode` subdir, appended below
    already_dotted: set = set()
    home_opencode = Path(home) / ".opencode"
    # `os.path.isdir` (unlike `Path.is_dir()`) swallows any `OSError` —
    # including `EACCES` on Python 3.10-3.13 for an unreadable parent dir.
    if os.path.isdir(str(home_opencode)):
        dotted_dirs.append(home_opencode)
        already_dotted.add(os.path.abspath(str(home_opencode)))
    config_dir_env = env.get("OPENCODE_CONFIG_DIR")
    if config_dir_env:
        dotted_dirs.append(Path(config_dir_env))
        already_dotted.add(os.path.abspath(str(config_dir_env)))

    seen: set = set()
    for d in dotted_dirs:
        candidate = d if os.path.abspath(str(d)) in already_dotted else d / ".opencode"
        norm = os.path.abspath(str(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        _try(candidate / "opencode.json")
        _try(candidate / "opencode.jsonc")

    return docs, findings


def _subagent_depth_findings(docs: List[Tuple[Path, dict]], roots) -> List[Finding]:
    findings: List[Finding] = []
    for path, payload in docs:
        raised: Optional[int] = None
        top = payload.get("subagent_depth")
        if isinstance(top, int) and not isinstance(top, bool) and top > 1:
            raised = top
        else:
            experimental = payload.get("experimental")
            if isinstance(experimental, dict):
                inner = experimental.get("subagent_depth")
                if isinstance(inner, int) and not isinstance(inner, bool) and inner > 1:
                    raised = inner
        if raised is not None:
            findings.append(
                make_finding("subagent-depth-raised", "warn", path=display_path(path, roots), number=raised)
            )
    return findings


def run_host(project_root, source_dir, env, home, which, version_runner) -> List[Finding]:
    """Host-environment checks: install state, manifest drift, config,
    rules fallback, skill-discovery census, flags and PATH/binary."""
    root = Path(project_root)
    findings: List[Finding] = []

    try:
        rendered = generate.render(generate.load_inputs(source_dir))
    except (generate.GenerationError, manifest.ManifestLoadError):
        findings.append(make_finding("render-failed", "error"))
        rendered = None

    try:
        drift = manifest.check_source_dir(source_dir)
    except (manifest.ManifestLoadError, RecursionError, OSError):
        findings.append(make_finding("manifest-unreadable", "error"))
    else:
        if drift:
            findings.append(make_finding("manifest-drift", "error", count=len(drift)))

    roots = _build_roots(root, env, home)

    if rendered is not None:
        findings.extend(_install_state_findings(root, rendered, source_dir, roots))

    worktree = _worktree_root(root)
    docs, config_findings = _read_config_files(root, worktree, env, home, roots)
    findings.extend(config_findings)
    findings.extend(_subagent_depth_findings(docs, roots))
    findings.extend(_permission_loosened_findings(docs))
    findings.extend(_census_findings(root, worktree, env, home, docs, rendered, roots))
    findings.extend(_rules_findings(root, worktree, env, home, roots))
    findings.extend(_flag_findings(env))
    findings.extend(_path_findings(which))
    findings.extend(_binary_findings(which, version_runner, source_dir))

    return findings


# --- Skill discovery census --------------------------------------------

_SKILL_MD_NAME = "SKILL.md"
_CENSUS_FILE_CAP = 5000
_CENSUS_DIR_CAP = 50000

_SCALAR_BAD_PREFIXES = (">", "|", "[", "{", "&", "*", "!", "%", "@", "`")
_YAML_LITERALS = frozenset(("true", "false", "yes", "no", "on", "off", "null", "~"))
_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_NAME_LINE_RE = re.compile(r"^name[ \t]*:")
_QUOTED_NAME_LINE_RE = re.compile(r"""^["']name["'][ \t]*:""")
_FLOW_MAPPING_LINE_RE = re.compile(r"^\{.*\}[ \t]*(#.*)?$")
_SINGLE_QUOTED_RE = re.compile(r"^'((?:[^']|'')*)'\s*(#.*)?$")


def _json_string_end(s: str) -> Optional[int]:
    """`s[0]` is a `"`. Returns the index of the matching unescaped closing
    quote, or None if there isn't one on this line."""
    i = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i
        i += 1
    return None


def read_skill_name(text: str) -> Tuple[str, Optional[str]]:
    """Tolerantly read the top-level `name:` value from a SKILL.md's
    frontmatter block. Used only by the skill census — never a
    replacement for `frontmatter.parse`, which is used for smoke checks
    only and rejects every real, unquoted Claude skill header.

    Returns `("named", name)`, `("absent", None)` or `("unverified",
    None)`. `absent` means OpenCode reads no string name from the file
    (no opening fence, or no top-level `name:` line) and skips it
    silently. `unverified` means the reader cannot say with certainty
    what OpenCode would read; such files are counted, never grouped.
    """
    if text.startswith("﻿"):
        text = text[1:]
    raw_lines = text.split("\n")
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in raw_lines]
    if not lines or lines[0].strip() != "---":
        return ("absent", None)
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return ("unverified", None)
    body = lines[1:close_idx]
    # A quoted `name` key (`"name":`/`'name':`) or a flow-mapping frontmatter
    # block (`{name: plan}`) may or may not define `name` the way OpenCode's
    # own YAML parser sees it — this reader only tolerates the plain
    # unquoted-key, block-mapping form `_NAME_LINE_RE` matches. Either shape
    # must read `unverified` (cannot say with certainty), never `absent`
    # (certain OpenCode reads no name), which is the reader's contract.
    if any(_FLOW_MAPPING_LINE_RE.match(ln) for ln in body):
        return ("unverified", None)
    if any(_QUOTED_NAME_LINE_RE.match(ln) for ln in body):
        return ("unverified", None)
    name_idxs = [i for i, ln in enumerate(body) if _NAME_LINE_RE.match(ln)]
    if not name_idxs:
        return ("absent", None)
    if len(name_idxs) > 1:
        return ("unverified", None)
    line = body[name_idxs[0]]
    value_part = line.split(":", 1)[1].strip()
    if not value_part:
        return ("unverified", None)

    if value_part[0] == "'":
        m = _SINGLE_QUOTED_RE.match(value_part)
        if not m:
            return ("unverified", None)
        name = m.group(1).replace("''", "'")
        return ("named", name) if name else ("unverified", None)

    if value_part[0] == '"':
        end = _json_string_end(value_part)
        if end is None:
            return ("unverified", None)
        rest = value_part[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            return ("unverified", None)
        try:
            name = json.loads(value_part[: end + 1])
        except json.JSONDecodeError:
            return ("unverified", None)
        if not isinstance(name, str) or not name:
            return ("unverified", None)
        return ("named", name)

    comment = re.search(r"\s+#", value_part)
    plain = (value_part[: comment.start()] if comment else value_part).strip()
    if not plain:
        return ("unverified", None)
    if plain[0] in _SCALAR_BAD_PREFIXES:
        return ("unverified", None)
    if plain.lower() in _YAML_LITERALS:
        return ("unverified", None)
    if _NUMBER_RE.match(plain):
        return ("unverified", None)
    return ("named", plain)


class _CensusBudget:
    __slots__ = ("files", "dirs", "truncated")

    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.truncated = False


def _walk_for_skill_md(start: Path, want_dot: bool, budget: "_CensusBudget"):
    """Yield absolute `SKILL.md` paths under `start`, sorted per directory
    (deterministic), following symlinked directories, stopping only on a
    cycle (a directory whose realpath is already on the current descent
    chain), honoring the census file/directory caps."""
    yield from _walk_for_skill_md_inner(start, want_dot, budget, [])


def _walk_for_skill_md_inner(start: Path, want_dot: bool, budget: "_CensusBudget", chain: List[str]):
    if budget.truncated:
        return
    try:
        real = os.path.realpath(str(start))
    except OSError:
        return
    if real in chain:
        return
    if budget.dirs >= _CENSUS_DIR_CAP:
        budget.truncated = True
        return
    try:
        entry_names = sorted(os.listdir(str(start)))
    except OSError:
        return
    budget.dirs += 1
    next_chain = chain + [real]
    for entry in entry_names:
        if not want_dot and entry.startswith("."):
            continue
        full = start / entry
        try:
            st = os.lstat(str(full))
        except OSError:
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        if not is_dir and stat.S_ISLNK(st.st_mode):
            try:
                is_dir = stat.S_ISDIR(os.stat(str(full)).st_mode)
            except OSError:
                is_dir = False
        if is_dir:
            yield from _walk_for_skill_md_inner(full, want_dot, budget, next_chain)
            if budget.truncated:
                return
        elif entry == _SKILL_MD_NAME:
            if budget.files >= _CENSUS_FILE_CAP:
                budget.truncated = True
                return
            budget.files += 1
            yield full


def _census_ext_roots(root: Path, worktree: Path, env, home) -> List[Tuple[Path, bool]]:
    """`(dir, want_dot=True)` pairs for the external `.claude`/`.agents`
    skill roots, home first, then project root..worktree nearest first."""
    if _flag_set(env, "OPENCODE_DISABLE_EXTERNAL_SKILLS"):
        return []
    ext_names = []
    if not _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS") and not _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE"):
        ext_names.append(".claude")
    ext_names.append(".agents")
    roots: List[Tuple[Path, bool]] = []
    for d in ext_names:
        roots.append((Path(home) / d / "skills", True))
        for dirp in _dir_chain(worktree, root):
            roots.append((dirp / d / "skills", True))
    return roots


def _census_config_dirs(root: Path, worktree: Path, env, home) -> List[Path]:
    """Config dirs that also serve as skill roots, `G_xdg` first, then
    project `.opencode` root..worktree nearest first, then `~/.opencode`
    and `OPENCODE_CONFIG_DIR`."""
    dirs: List[Path] = [_xdg_config_dir(env, home)]
    if not _flag_set(env, "OPENCODE_DISABLE_PROJECT_CONFIG"):
        for dirp in _dir_chain(worktree, root):
            dirs.append(dirp / ".opencode")
    home_opencode = Path(home) / ".opencode"
    if os.path.isdir(str(home_opencode)):
        dirs.append(home_opencode)
    config_dir_env = env.get("OPENCODE_CONFIG_DIR")
    if config_dir_env:
        dirs.append(Path(config_dir_env))
    return dirs


def _last_skills_field(docs: List[Tuple[Path, dict]], field: str):
    """The value of `skills.<field>` from the last config file, in load
    order, whose `skills` map sets it (config merging replaces arrays
    whole, not element-wise), and the path of that file. `(None, None)`
    when no file sets it."""
    value = None
    value_path = None
    for path, payload in docs:
        skills = payload.get("skills")
        if isinstance(skills, dict) and field in skills:
            value = skills[field]
            value_path = path
    return value, value_path


def _is_under(path, root) -> bool:
    try:
        Path(os.path.abspath(str(path))).relative_to(os.path.abspath(str(root)))
        return True
    except ValueError:
        return False


def census(project_root, worktree, env, home, docs) -> Tuple[Dict[str, List[Path]], List[Finding], int, Dict[str, str]]:
    """The skill-discovery census: walks every root OpenCode scans for
    `SKILL.md` files, in its own scan order, and groups them by name and
    by `SKILL.md` realpath, so a symlink alias of one file is one skill
    with several locations, not a duplicate.

    Returns `(name -> [absolute path per distinct unit, in scan order],
    findings, unverified_count, unit -> "claude"|"other" category for the
    first-found location of each named unit)`. `findings` here covers only
    `skills-path-missing` and `census-truncated`; duplicate, legacy and
    outside-project findings are built by `_census_findings` once the
    caller also has the rendered catalog's names.
    """
    root = Path(project_root)
    budget = _CensusBudget()
    findings: List[Finding] = []
    seen_roots: set = set()
    seen_matches: set = set()
    ordered_matches: List[Tuple[Path, str]] = []

    def _scan(start: Path, want_dot: bool, category: str) -> None:
        norm_root = os.path.abspath(str(start))
        if norm_root in seen_roots:
            return
        seen_roots.add(norm_root)
        if not os.path.isdir(str(start)):
            return
        for match in _walk_for_skill_md(start, want_dot, budget):
            norm_match = os.path.abspath(str(match))
            if norm_match in seen_matches:
                continue
            seen_matches.add(norm_match)
            ordered_matches.append((match, category))

    for start, want_dot in _census_ext_roots(root, worktree, env, home):
        _scan(start, want_dot, "claude")

    for cfg_dir in _census_config_dirs(root, worktree, env, home):
        _scan(cfg_dir / "skill", False, "config")
        _scan(cfg_dir / "skills", False, "config")

    skills_paths, skills_paths_doc = _last_skills_field(docs, "paths")
    base_roots = _build_roots(root, env, home)
    if isinstance(skills_paths, list):
        for i, entry in enumerate(skills_paths):
            if not isinstance(entry, str):
                continue
            expanded = os.path.expanduser(entry)
            p = Path(expanded) if os.path.isabs(expanded) else root / expanded
            if not p.is_dir():
                cfg_display = display_path(skills_paths_doc, base_roots) if skills_paths_doc else ""
                findings.append(make_finding("skills-path-missing", "info", path=cfg_display, number=i))
                continue
            _scan(p, False, "skills-path")

    skills_urls, _skills_urls_doc = _last_skills_field(docs, "urls")
    if isinstance(skills_urls, list) and skills_urls:
        findings.append(make_finding("skills-url-not-scanned", "info", count=len(skills_urls)))

    if budget.truncated:
        findings.append(make_finding("census-truncated", "info", count=budget.files))

    unit_first_path: Dict[str, Path] = {}
    unit_first_category: Dict[str, str] = {}
    scan_order_units: List[str] = []
    for match, category in ordered_matches:
        try:
            unit = os.path.realpath(str(match))
        except OSError:
            unit = str(match)
        if unit in unit_first_path:
            continue
        unit_first_path[unit] = match
        unit_first_category[unit] = category
        scan_order_units.append(unit)

    census_map: Dict[str, List[Path]] = {}
    unverified_count = 0
    for unit in scan_order_units:
        path = unit_first_path[unit]
        try:
            raw = path.read_bytes()
        except OSError:
            unverified_count += 1
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            unverified_count += 1
            continue
        kind, name = read_skill_name(text)
        if kind == "absent":
            continue
        if kind == "unverified":
            unverified_count += 1
            continue
        census_map.setdefault(name, []).append(path)

    if unverified_count:
        findings.append(make_finding("census-unverified", "info", count=unverified_count))

    return census_map, findings, unverified_count, unit_first_category


_SKILL_DUP_REMEDIATION = (
    "OpenCode loads only one of these copies, and which one is not fixed; "
    "keep one location and remove or rename the others."
)
_SKILL_DUP_URL_SENTENCE = "A skill fetched from a URL in skills.urls can also take this name."


def _legacy_claude_unit_count(root: Path, worktree: Path, home, catalog_ids) -> int:
    """Count distinct `SKILL.md` realpaths under `~/.claude/skills` (or a
    walked-up project `.claude/skills`) whose name equals a catalog id.

    This is deliberately independent of `OPENCODE_DISABLE_CLAUDE_CODE(_SKILLS)`
    / `OPENCODE_DISABLE_EXTERNAL_SKILLS`: the finding still fires (at `info`)
    when a flag already stops OpenCode from scanning `.claude`, so the
    severity downgrade in `_census_findings` has something to downgrade.
    """
    if not catalog_ids:
        return 0
    budget = _CensusBudget()
    seen_roots: set = set()
    seen_units: set = set()
    count = 0
    starts = [Path(home) / ".claude" / "skills"]
    for dirp in _dir_chain(worktree, root):
        starts.append(dirp / ".claude" / "skills")
    for start in starts:
        norm = os.path.abspath(str(start))
        if norm in seen_roots or not os.path.isdir(str(start)):
            seen_roots.add(norm)
            continue
        seen_roots.add(norm)
        for match in _walk_for_skill_md(start, True, budget):
            try:
                unit = os.path.realpath(str(match))
            except OSError:
                continue
            if unit in seen_units:
                continue
            seen_units.add(unit)
            try:
                text = match.read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            kind, name = read_skill_name(text)
            if kind == "named" and name in catalog_ids:
                count += 1
    return count


def _census_findings(project_root, worktree, env, home, docs, rendered, roots) -> List[Finding]:
    """Wires `census()` into `run_host`: duplicate, legacy-discovery and
    outside-project findings, plus the `skills-path-missing` /
    `census-truncated` findings `census()` itself returns."""
    census_map, base_findings, _unverified_count, _unit_first_category = census(
        project_root, worktree, env, home, docs
    )
    findings: List[Finding] = list(base_findings)

    # `roots` alone never covers a `skills.paths` entry outside every known
    # root (project root, config-location env vars, home): without this, a
    # unit found under such an entry prints as a raw absolute path in the
    # duplicate/outside-project findings below. Mirror census()'s own entry
    # resolution exactly so the root matches the path census actually walked.
    display_roots = list(roots)
    skills_paths, skills_paths_doc = _last_skills_field(docs, "paths")
    if isinstance(skills_paths, list):
        cfg_display = display_path(skills_paths_doc, roots) if skills_paths_doc else ""
        for i, entry in enumerate(skills_paths):
            if not isinstance(entry, str):
                continue
            expanded = os.path.expanduser(entry)
            resolved = expanded if os.path.isabs(expanded) else str(Path(project_root) / expanded)
            display_roots.append((resolved, "skills.paths[%d] of %s" % (i, cfg_display)))

    catalog_ids = frozenset(
        rf.source_id for rf in rendered.values() if rf.kind == "skill"
    ) if rendered else frozenset()
    quoin_names = frozenset(names.normalize(cid) for cid in catalog_ids)

    skills_urls, _skills_urls_doc = _last_skills_field(docs, "urls")
    urls_nonempty = isinstance(skills_urls, list) and bool(skills_urls)

    project_skills_dir = Path(project_root) / ".opencode" / "skills"
    legacy_units = 0
    for name in sorted(census_map):
        locations = census_map[name]
        if len(locations) >= 2:
            display_paths = [display_path(loc, display_roots) for loc in locations]
            remediation = _SKILL_DUP_REMEDIATION
            if urls_nonempty:
                remediation = _SKILL_DUP_REMEDIATION + " " + _SKILL_DUP_URL_SENTENCE
            if name in catalog_ids or name in quoin_names:
                findings.append(
                    make_finding(
                        "skill-duplicate", "warn", name=name,
                        names=", ".join(display_paths), remediation=remediation,
                    )
                )
            else:
                findings.append(
                    make_finding(
                        "skill-duplicate-unnamed", "warn",
                        names=", ".join(display_paths), remediation=remediation,
                    )
                )
        if name in quoin_names:
            for loc in locations:
                if not _is_under(loc, project_skills_dir):
                    findings.append(
                        make_finding(
                            "quoin-skill-outside-project", "warn",
                            name=name, path=display_path(loc, display_roots),
                        )
                    )
    legacy_units = _legacy_claude_unit_count(Path(project_root), Path(worktree), home, catalog_ids)
    if legacy_units:
        legacy_off = (
            _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS")
            or _flag_set(env, "OPENCODE_DISABLE_CLAUDE_CODE")
            or _flag_set(env, "OPENCODE_DISABLE_EXTERNAL_SKILLS")
        )
        findings.append(
            make_finding(
                "legacy-claude-skills",
                "info" if legacy_off else "warn",
                count=legacy_units,
            )
        )

    return findings


def _tool_has_allow(value) -> bool:
    if value == "allow":
        return True
    if isinstance(value, dict):
        return any(v == "allow" for v in value.values())
    return False


_PERMISSION_TOOL_KEYS = ("bash", "edit", "glob", "grep", "lsp", "read", "skill", "task")


def _compute_tools_some_role_allows() -> "frozenset":
    allowed = set()
    for role in generate.ROLES:
        perms = generate.role_permissions(role)
        for key in _PERMISSION_TOOL_KEYS:
            if key in perms and _tool_has_allow(perms[key]):
                allowed.add(key)
    return frozenset(allowed)


_TOOLS_SOME_ROLE_ALLOWS = _compute_tools_some_role_allows()


def _permission_loosened_findings(docs: List[Tuple[Path, dict]]) -> List[Finding]:
    """A generated role's own allow can override a stricter user-level
    `permission` rule while that role runs directly (though not when it
    runs as a subagent, where the parent session's deny rules re-apply —
    see compatibility.md), so a tool some role allows is reported once,
    naming only tools from the generated set — never a raw key or value
    read from the config file."""
    loosened: set = set()
    for _path, payload in docs:
        perm = payload.get("permission")
        if isinstance(perm, str):
            if perm in ("deny", "ask"):
                loosened |= _TOOLS_SOME_ROLE_ALLOWS
            continue
        if isinstance(perm, dict):
            for key, value in perm.items():
                if key not in _TOOLS_SOME_ROLE_ALLOWS:
                    continue
                narrows = value in ("deny", "ask") or (
                    isinstance(value, dict) and any(v in ("deny", "ask") for v in value.values())
                )
                if narrows:
                    loosened.add(key)
    if not loosened:
        return []
    return [make_finding("permission-loosened", "info", names=", ".join(sorted(loosened)))]
