"""Doctor for the OpenCode adapter.

Two entry paths (D-01): `run_smoke` renders the catalog offline and checks
the result against itself — no host filesystem or environment is read, so
it works with no `opencode` binary and no network. `run_host` (T-06/T-07)
adds installed-state, config, census and PATH checks against the running
machine.

Redaction by construction (D-02): every finding message comes from the
`MESSAGES` template table, whose only substitutable fields are the ones
`make_finding` allowlists. There is no other way to build a `Finding` in
this module. Any path-like value passed to `make_finding` must already be
a display path (see `display_path`) — never a raw filesystem path pulled
from an environment variable or a config file.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import frontmatter, generate, manifest, names

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
}

_REMEDIATIONS: Dict[str, str] = {
    "smoke-render": "run `quoin install --runtime opencode --check` to see the generation error",
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
    finding_objs = []
    for f in sorted(findings, key=_sort_key):
        obj = {"id": f.id, "severity": f.severity, "message": f.message}
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
    """Offline smoke checks (D-01): no filesystem or environment outside
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

    status = report_status(findings)
    text = render_json(findings, status) if as_json else render_text(findings, status)
    print(text, end="", file=out)
    return exit_code(status)


def run_host(project_root, source_dir, env, home, which, version_runner) -> List[Finding]:
    """Host-environment checks: install state, manifest drift, config,
    census, rules fallback, flags, PATH and binary. Implemented in T-06/T-07."""
    raise NotImplementedError("run_host lands in T-06")
