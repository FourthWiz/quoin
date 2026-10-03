"""The pure OpenCode generator.

Renders Quoin's OpenCode commands, skills, agents, instruction document and
config file from portable inputs (the skill catalog, the manifest, the
overlay data and the bundle contracts). Nothing in this module touches a
filesystem outside an explicit source directory it is given, and nothing
here runs a subprocess.

Filled in task by task: role permission maps and the delegation graph land
first, so the security posture is pinned by tests before anything renders
them into a file; the rest of the pipeline (`GeneratorInputs`, `load_inputs`,
`extract_sections`, `render`, `digest`, and the forbidden-output check)
follows in later tasks.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from quoin.opencode_adapter import frontmatter, manifest, names, scripts

# The directory Quoin's own workflow artifacts live under, relative to a
# project root. Templates and overlays never spell this literal; they use
# the `{{ARTIFACT_ROOT}}` placeholder, expanded from this constant, so a
# rendered file can name the user's own workflow folder without any shipped
# source under this package ever containing it.
ARTIFACT_ROOT = ".workflow_artifacts"

# Every Quoin role may load any `quoin-*` skill and nothing else.
SKILL_RULE = {"*": "deny", "quoin-*": "allow"}

# OpenCode's own built-in read posture (`packages/opencode/src/agent/agent.ts`
# L119-136). A read-only role emits this instead of a bare `allow`, so it
# never loosens the built-in `.env` handling.
READ_RULE = {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"}

# The helper scripts (`quoin/core/scripts/<name>.py`; see scripts.py) each
# role may run through `quoin opencode script <name> *`. Only the
# investigator holds a write-capable script (scripts.WRITE_CAPABLE_SCRIPTS):
# `generate_discovery_map`.
ROLE_SCRIPTS: Dict[str, tuple] = {
    "architect": ("classify_critic_issues", "path_resolve", "validate_artifact"),
    "planner": ("classify_critic_issues", "path_resolve", "validate_artifact"),
    "gate": ("path_resolve", "validate_artifact"),
    "implementer": ("path_resolve", "validate_artifact"),
    "coordinator": (
        "checkpoint_picker",
        "classify_critic_issues",
        "handoff_validate",
        "path_resolve",
        "validate_artifact",
    ),
    "investigator": ("generate_discovery_map", "path_resolve"),
    "critic": (),
    "reviewer": (),
}

# The last two keys of every shell map (last-match-wins), so a redirected,
# heredoc or process-substitution statement is asked about even when its
# command is an allowed script: OpenCode matches a shell permission pattern
# against the whole statement text, redirections included, and never
# path-checks a redirection target
# (`packages/opencode/src/tool/shell.ts` L99, L119-121).
REDIRECT_RULE = {"*>*": "ask", "*<*": "ask"}

# The Quoin helper commands each role may run directly. A helper allow covers
# every argument, so `shell_rule` follows each one with asks for the
# arguments that could redirect it at another tree, another project or a
# secrets file.
HELPER_ALLOWS: Dict[str, tuple] = {
    "gate": ("quoin opencode gate *",),
    "coordinator": (
        "quoin opencode gate *",
        "quoin opencode handoff *",
        "quoin opencode workflow next *",
    ),
    "implementer": ("quoin opencode test-run *",),
}

# Argument fragments asked about after every helper allow.
HELPER_ASK_FRAGMENTS = ("--source-dir", "--project-root", ".env")

_READ_ONLY_ROLES = ("critic", "reviewer")

# The eight Quoin roles this generator knows about. Used only for the
# unknown-role error message; nothing here iterates this tuple to build a
# role map.
ROLES = (
    "coordinator",
    "investigator",
    "architect",
    "planner",
    "implementer",
    "critic",
    "reviewer",
    "gate",
)


def artifact_edit_rule(base: str) -> Dict[str, str]:
    """The edit permission map shared by every role that may touch the
    artifact root: `base` for everything else, allow inside the artifact
    root whether it sits at the project root or nested under a sub-repo."""
    return {
        "*": base,
        "%s/*" % ARTIFACT_ROOT: "allow",
        "*/%s/*" % ARTIFACT_ROOT: "allow",
    }


def shell_rule(role: str) -> Dict[str, str]:
    """The bash permission map for `role`: ask by default, one allow per
    allowlisted helper script (argument tail included, never argument-wide),
    one per helper command followed by asks for its tree, project and
    secrets-file arguments, then the redirection ask rules, always last so they win under
    last-match evaluation."""
    rule: Dict[str, str] = {"*": "ask"}
    for name in sorted(ROLE_SCRIPTS.get(role, ())):
        rule["quoin opencode script %s *" % name] = "allow"
    helpers = HELPER_ALLOWS.get(role, ())
    for pattern in helpers:
        rule[pattern] = "allow"
    for pattern in helpers:
        prefix = pattern[:-2] if pattern.endswith(" *") else pattern
        for fragment in HELPER_ASK_FRAGMENTS:
            rule["%s *%s*" % (prefix, fragment)] = "ask"
    rule.update(REDIRECT_RULE)
    return rule


def role_permissions(role: str) -> Dict[str, object]:
    """The OpenCode agent frontmatter `permission` map for `role`.

    Key insertion order is emission order (`frontmatter.emit` never sorts).
    Every call returns fresh mappings, so callers may mutate the result
    without affecting later calls. See the stage plan's Procedures section
    for the source table this mirrors.
    """
    if role in _READ_ONLY_ROLES:
        return {
            "*": "deny",
            "read": dict(READ_RULE),
            "glob": "allow",
            "grep": "allow",
            "lsp": "allow",
            "skill": dict(SKILL_RULE),
        }
    if role == "investigator":
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    if role in ("architect", "planner"):
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": {
                "*": "deny",
                "quoin-investigator": "allow",
                "quoin-critic": "allow",
            },
            "skill": dict(SKILL_RULE),
        }
    if role == "gate":
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    if role == "coordinator":
        return {
            "edit": artifact_edit_rule("ask"),
            "bash": shell_rule(role),
            "task": {
                "*": "deny",
                "quoin-investigator": "allow",
                "quoin-critic": "allow",
                "quoin-reviewer": "allow",
            },
            "skill": dict(SKILL_RULE),
        }
    if role == "implementer":
        return {
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    raise ValueError("unknown role %r, expected one of %s" % (role, ROLES))


def task_targets(role: str) -> List[str]:
    """The sorted list of agent names `role` may Task-delegate to: the
    allowed patterns of its own `task` permission rule, excluding the
    catch-all. Empty when the role's `task` rule is a bare action (no
    delegation) or absent."""
    rule = role_permissions(role).get("task")
    if not isinstance(rule, dict):
        return []
    return sorted(name for name, action in rule.items() if name != "*" and action == "allow")


def check_task_graph(roles_by_name: Dict[str, dict]) -> List[str]:
    """Validate the delegation graph implied by every role's `task` rule.

    `roles_by_name` maps a role name to an object carrying at least a
    `"mode"` key (the manifest's own `roles` shape). Every allowed task
    target must be a role declared in `roles_by_name` whose own mode is
    `subagent` or `all` and whose own `task` rule denies further
    delegation, so depth never exceeds 1; every role with an allowed edge
    must itself be `primary`, because a role that can be a Task target
    cannot also be a source. Returns offender-naming messages, empty when
    the graph is clean.
    """
    agent_to_role = {names.role_agent_name(role): role for role in roles_by_name}
    errors: List[str] = []
    for role, role_def in roles_by_name.items():
        for target_agent in task_targets(role):
            target_role = agent_to_role.get(target_agent)
            if target_role is None:
                errors.append(
                    "role '%s' has an allowed task edge to unknown target '%s'" % (role, target_agent)
                )
                continue
            if role_def.get("mode") != "primary":
                errors.append(
                    "role '%s' (mode %r) has an allowed task edge to '%s' (role '%s'), "
                    "but only a 'primary' role may delegate"
                    % (role, role_def.get("mode"), target_agent, target_role)
                )
            target_def = roles_by_name.get(target_role, {})
            if target_def.get("mode") not in ("subagent", "all"):
                errors.append(
                    "role '%s' targets '%s' (role '%s'), whose mode is %r, expected 'subagent' or 'all'"
                    % (role, target_agent, target_role, target_def.get("mode"))
                )
            if task_targets(target_role):
                errors.append(
                    "role '%s' targets '%s' (role '%s'), which itself has an allowed task edge"
                    % (role, target_agent, target_role)
                )
    return errors


# --- Generator core: inputs, sections, rendering, digests, collisions ---
#
# GENERATOR_SCHEMA_VERSION is bumped whenever a renderer's output changes
# for unchanged `parts` — that bump is the only thing that may move every
# digest at once. Every `parts_*` dict below carries it, so a code change
# that is not accompanied by a version bump is caught by the byte-hash pin
# test.

GENERATOR_SCHEMA_VERSION = 1
INSTRUCTIONS_PATH = ".opencode/quoin/instructions.md"
CONFIG_PATH = ".opencode/opencode.jsonc"
DEFAULT_SECTIONS = ("Purpose", "When to use", "Inputs", "Output", "Behavior contract")

_TEMPLATE_NAMES = ("command", "skill", "agent", "instructions")

_REQUIRED_TEMPLATE_PLACEHOLDERS = {
    "command": frozenset(("SKILL_NAME", "TITLE", "COMMAND_NOTE")),
    "skill": frozenset(("SKILL_NAME", "COMMAND_NAME", "ROLE_AGENT", "CANONICAL_ID", "CONTRACT", "NOTES")),
    "agent": frozenset(("ROLE_PROMPT", "DELEGATION")),
    "instructions": frozenset(
        ("COMMAND_LIST", "ARTIFACT_ROOT", "ROLE_TABLE", "LIMITS", "UNAVAILABLE_LIST", "CORE_RULES")
    ),
}

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{[A-Za-z_]+\}\}")
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


class GenerationError(ValueError):
    pass


@dataclass
class GeneratorInputs:
    catalog: List[dict]
    manifest: dict
    pinned_version: str
    overlays: dict
    templates: Dict[str, str]
    contracts: Dict[str, str]
    rules: str


@dataclass(frozen=True)
class RenderedFile:
    relpath: str
    content: bytes
    kind: str
    source_id: Optional[str]
    source_digest: str


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GenerationError("cannot read %s: %s" % (path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise GenerationError("cannot decode %s as UTF-8: %s" % (path, exc)) from exc


def _read_json(path: Path):
    text = _read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenerationError("invalid JSON in %s: %s" % (path, exc)) from exc


def _expand_artifact_root(text: str) -> str:
    return text.replace("{{ARTIFACT_ROOT}}", ARTIFACT_ROOT)


def _check_placeholder_tokens(label: str, text: str) -> None:
    for token in _PLACEHOLDER_TOKEN_RE.findall(text):
        if token != "{{ARTIFACT_ROOT}}":
            raise GenerationError("%s: unknown placeholder token %s" % (label, token))


def _validate_overlays(inputs: "GeneratorInputs", supported_ids: List[str]) -> None:
    overlays = inputs.overlays
    entries = overlays.get("entries") if isinstance(overlays, dict) else None
    if not isinstance(entries, dict):
        raise GenerationError("overlays.json 'entries' must be an object")

    entry_ids = set(entries)
    expected_ids = set(supported_ids)
    missing = sorted(expected_ids - entry_ids)
    extra = sorted(entry_ids - expected_ids)
    if missing:
        raise GenerationError("overlays.json is missing entries for: %s" % ", ".join(missing))
    if extra:
        raise GenerationError("overlays.json has unknown entries: %s" % ", ".join(extra))

    for eid, entry in entries.items():
        if not isinstance(entry, dict):
            raise GenerationError("overlays entry %r must be an object" % eid)
        description = entry.get("description")
        if not isinstance(description, str) or not description:
            raise GenerationError("overlays entry %r: 'description' must be a non-empty string" % eid)
        _check_placeholder_tokens("overlays entry %r description" % eid, description)

        command_note = entry.get("command_note")
        if not isinstance(command_note, str):
            raise GenerationError("overlays entry %r: 'command_note' must be a string" % eid)
        _check_placeholder_tokens("overlays entry %r command_note" % eid, command_note)

        extra_sections = entry.get("extra_sections")
        if not isinstance(extra_sections, list) or not all(isinstance(s, str) for s in extra_sections):
            raise GenerationError("overlays entry %r: 'extra_sections' must be a list of strings" % eid)

        notes = entry.get("notes")
        if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise GenerationError("overlays entry %r: 'notes' must be a list of strings" % eid)
        for note in notes:
            _check_placeholder_tokens("overlays entry %r notes" % eid, note)

        rewrites = entry.get("rewrites")
        if not isinstance(rewrites, list):
            raise GenerationError("overlays entry %r: 'rewrites' must be a list" % eid)
        for rw in rewrites:
            if not isinstance(rw, dict) or not all(k in rw for k in ("section", "from", "to")):
                raise GenerationError("overlays entry %r has a malformed rewrite" % eid)
            if not all(isinstance(rw[k], str) for k in ("section", "from", "to")):
                raise GenerationError("overlays entry %r rewrite fields must be strings" % eid)
            _check_placeholder_tokens("overlays entry %r rewrite 'from'" % eid, rw["from"])
            _check_placeholder_tokens("overlays entry %r rewrite 'to'" % eid, rw["to"])

    roles = overlays.get("roles")
    if not isinstance(roles, dict):
        raise GenerationError("overlays.json 'roles' must be an object")
    role_names = set(inputs.manifest.get("roles") or {})
    role_keys = set(roles)
    missing_roles = sorted(role_names - role_keys)
    extra_roles = sorted(role_keys - role_names)
    if missing_roles:
        raise GenerationError("overlays.json is missing roles for: %s" % ", ".join(missing_roles))
    if extra_roles:
        raise GenerationError("overlays.json has unknown roles: %s" % ", ".join(extra_roles))

    for rname, rdef in roles.items():
        if not isinstance(rdef, dict):
            raise GenerationError("overlays role %r must be an object" % rname)
        description = rdef.get("description")
        if not isinstance(description, str) or not description:
            raise GenerationError("overlays role %r: 'description' must be a non-empty string" % rname)
        _check_placeholder_tokens("overlays role %r description" % rname, description)
        prompt = rdef.get("prompt")
        if not isinstance(prompt, list) or not prompt or not all(isinstance(p, str) for p in prompt):
            raise GenerationError("overlays role %r: 'prompt' must be a non-empty list of strings" % rname)
        for para in prompt:
            _check_placeholder_tokens("overlays role %r prompt" % rname, para)


def _validate_templates(templates: Dict[str, str]) -> None:
    for kind, required in _REQUIRED_TEMPLATE_PLACEHOLDERS.items():
        text = templates.get(kind)
        if text is None:
            raise GenerationError("templates is missing the %r template" % kind)
        present = set(_TEMPLATE_PLACEHOLDER_RE.findall(text))
        missing = sorted(required - present)
        unknown = sorted(present - required)
        if missing:
            raise GenerationError(
                "template %r is missing required placeholder(s): %s" % (kind, ", ".join(missing))
            )
        if unknown:
            raise GenerationError("template %r has unknown placeholder(s): %s" % (kind, ", ".join(unknown)))


def load_inputs(source_dir) -> GeneratorInputs:
    """Read every portable input the generator needs from `source_dir`.

    Runs `manifest.check_manifest` and raises `GenerationError` listing
    every finding when it is non-empty, so generation never proceeds on an
    unclassified or drifting manifest. Validates the overlay shape (entry
    ids equal the supported catalog ids, role keys equal the manifest
    roles, field types) and the template placeholder sets before
    returning.
    """
    source_dir = Path(source_dir)

    try:
        catalog = manifest.load_catalog(source_dir)
        manifest_data = manifest.load_manifest(source_dir)
        pinned_version = manifest.read_pinned_version(source_dir)
    except manifest.ManifestLoadError as exc:
        raise GenerationError(str(exc)) from exc

    try:
        findings = manifest.check_manifest(manifest_data, catalog, pinned_version)
    except RecursionError as exc:
        raise GenerationError("manifest is nested too deeply to check") from exc
    if findings:
        raise GenerationError("manifest drift: " + "; ".join(findings))

    overlays_dir = source_dir / "adapters" / "opencode"
    overlays = _read_json(overlays_dir / "overlays.json")
    templates = {kind: _read_text(overlays_dir / "templates" / ("%s.md" % kind)) for kind in _TEMPLATE_NAMES}

    supported_ids = sorted(
        row["id"]
        for row in manifest_data.get("catalog_entries", [])
        if isinstance(row, dict) and row.get("status") == "supported" and isinstance(row.get("id"), str)
    )
    contracts = {}
    for cid in supported_ids:
        contracts[cid] = _read_text(source_dir / "core" / "skills" / ("%s.md" % cid))

    rules = _read_text(source_dir / "core" / "workflow" / "rules.md")

    inputs = GeneratorInputs(
        catalog=catalog,
        manifest=manifest_data,
        pinned_version=pinned_version,
        overlays=overlays,
        templates=templates,
        contracts=contracts,
        rules=rules,
    )
    _validate_overlays(inputs, supported_ids)
    _validate_templates(templates)
    return inputs


def extract_sections(markdown: str) -> List[Tuple[str, str]]:
    """Split `markdown` on `## ` headings outside fenced code blocks.

    A line starting with three backticks or three tildes toggles the
    fence. `###` subsections stay inside their parent's body. Returns
    `(heading, body)` pairs in document order; text before the first `##`
    heading is discarded (the H1 and any lead-in prose).
    """
    lines = markdown.split("\n")
    sections: List[Tuple[str, str]] = []
    heading: Optional[str] = None
    body: List[str] = []
    in_fence = False
    fence_marker = ""
    for line in lines:
        stripped = line.strip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence_marker = stripped[:3]
            body.append(line)
            continue
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            body.append(line)
            continue
        if line.startswith("## ") and not line.startswith("### "):
            if heading is not None:
                sections.append((heading, "\n".join(body)))
            heading = line[3:].strip()
            body = []
        else:
            body.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(body)))
    return sections


def _demote_headings_outside_fences(text: str) -> str:
    """Drop the H1 and add one `#` to every other heading outside fences."""
    lines = text.split("\n")
    out: List[str] = []
    in_fence = False
    fence_marker = ""
    seen_h1 = False
    for line in lines:
        stripped = line.strip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence_marker = stripped[:3]
            out.append(line)
            continue
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            out.append(line)
            continue
        if line.startswith("#"):
            if line.startswith("# ") and not line.startswith("## ") and not seen_h1:
                seen_h1 = True
                continue
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def _slash_translate_pattern(catalog_ids) -> "re.Pattern":
    ids_sorted = sorted(catalog_ids, key=len, reverse=True)
    alternation = "|".join(re.escape(cid) for cid in ids_sorted)
    return re.compile(r"(?<![\w./-])/(%s)(?![\w/-]|\.\w)" % alternation)


def _translate_slashes(text: str, catalog_id_set, supported_id_set) -> Tuple[str, List[str]]:
    """Translate `/id` references to `/quoin-<name>` for supported ids and
    drop the leading slash for every other catalog id. Returns the
    translated text and the sorted list of non-bundle ids whose slash was
    dropped."""
    pattern = _slash_translate_pattern(catalog_id_set)
    dropped = set()

    def _sub(m: "re.Match") -> str:
        cid = m.group(1)
        if cid in supported_id_set:
            return "/" + names.normalize(cid)
        dropped.add(cid)
        return cid

    translated = pattern.sub(_sub, text)
    return translated, sorted(dropped)


def _render_notes(notes: List[str], dropped: List[str]) -> str:
    all_notes = list(notes)
    if dropped:
        plural = len(dropped) > 1
        all_notes.append(
            "This step also references %s, which %s not available as %s in OpenCode."
            % (
                ", ".join(dropped),
                "are" if plural else "is",
                "Quoin workflow steps" if plural else "a Quoin workflow step",
            )
        )
    if not all_notes:
        return "- (none)"
    return "\n".join("- %s" % note for note in all_notes)


def _assemble_contract(
    entry_id: str, contract_text: str, overlay_entry: dict, catalog_id_set, supported_id_set
) -> Tuple[str, List[str]]:
    wanted = set(DEFAULT_SECTIONS) | set(overlay_entry["extra_sections"])
    sections = extract_sections(contract_text)
    bodies = {}
    for heading, body in sections:
        bodies.setdefault(heading, body)

    missing = sorted(h for h in wanted if h not in bodies)
    if missing:
        raise GenerationError(
            "contract %r is missing required section(s): %s" % (entry_id, ", ".join(missing))
        )

    for rw in overlay_entry["rewrites"]:
        section = rw["section"]
        if section not in bodies:
            raise GenerationError("entry %r rewrite section %r not found in its contract" % (entry_id, section))
        frm = _expand_artifact_root(rw["from"])
        to = _expand_artifact_root(rw["to"])
        body = bodies[section]
        count = body.count(frm)
        if count != 1:
            raise GenerationError(
                "entry %r rewrite in section %r: 'from' text starting %r must occur exactly once in "
                "the source contract, found %d" % (entry_id, section, frm[:60], count)
            )
        bodies[section] = body.replace(frm, to, 1)

    ordered_headings = [h for h, _ in sections if h in wanted]
    joined = "\n".join("## %s\n%s" % (h, bodies[h]) for h in ordered_headings)
    contract_text_assembled = joined.strip("\n") + "\n"

    translated, dropped = _translate_slashes(contract_text_assembled, catalog_id_set, supported_id_set)
    return translated, dropped


def _finalize_text(text: str) -> str:
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def _substitute(template_text: str, mapping: Dict[str, str]) -> str:
    result = template_text
    for key, value in mapping.items():
        result = result.replace("{{%s}}" % key, value)
    return result


def _digest(parts: dict) -> str:
    encoded = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def render_command(parts: dict) -> str:
    body = _substitute(
        parts["template_command"],
        {"SKILL_NAME": parts["name"], "TITLE": parts["title"], "COMMAND_NOTE": parts["command_note"]},
    )
    fm = frontmatter.emit({"description": parts["description"], "agent": parts["agent"]})
    return _finalize_text(fm + body)


def render_skill(parts: dict, digest_value: str) -> str:
    body = _substitute(
        parts["template_skill"],
        {
            "SKILL_NAME": parts["name"],
            "COMMAND_NAME": parts["command"],
            "ROLE_AGENT": parts["agent"],
            "CANONICAL_ID": parts["id"],
            "CONTRACT": parts["contract_text"],
            "NOTES": parts["notes"],
        },
    )
    fm = frontmatter.emit(
        {
            "name": parts["name"],
            "description": parts["description"],
            "metadata": {
                "canonical_id": parts["id"],
                "source_digest": digest_value,
                "generator": "quoin",
            },
        }
    )
    return _finalize_text(fm + body)


def render_agent(parts: dict) -> str:
    targets = parts["targets"]
    delegation = (
        "This role may delegate to: %s." % ", ".join(targets)
        if targets
        else "This role may not delegate to any other role."
    )
    body = _substitute(parts["template_agent"], {"ROLE_PROMPT": parts["prompt"], "DELEGATION": delegation})
    fm = frontmatter.emit(
        {"description": parts["description"], "mode": parts["mode"], "permission": parts["permission"]}
    )
    return _finalize_text(fm + body)


def render_instructions(parts: dict) -> str:
    body = _substitute(
        parts["template_instructions"],
        {
            "COMMAND_LIST": parts["command_list"],
            "ARTIFACT_ROOT": parts["root"],
            "ROLE_TABLE": parts["role_table"],
            "LIMITS": parts["limits"],
            "UNAVAILABLE_LIST": parts["unavailable_list"],
            "CORE_RULES": parts["core_rules"],
        },
    )
    return _finalize_text(body)


def render_config(parts: dict) -> str:
    header = (
        "// Quoin owns this file, remove it with `quoin opencode uninstall`.\n"
        "// Keep your own settings in `.opencode/opencode.json` or `opencode.json`.\n"
    )
    obj = {"$schema": "https://opencode.ai/config.json", "instructions": [parts["instructions_path"]]}
    body = json.dumps(obj, indent=2) + "\n"
    return _finalize_text(header + body)


_FRONTMATTER_ALLOWED_KEYS: Dict[str, frozenset] = {
    "command": frozenset(("description", "agent")),
    "skill": frozenset(("name", "description", "metadata")),
    "agent": frozenset(("description", "mode", "permission")),
}
_FRONTMATTER_KINDS = frozenset(_FRONTMATTER_ALLOWED_KEYS)

_AGENT_MODES = frozenset(("subagent", "primary", "all"))
_PERMISSION_ACTIONS = frozenset(("ask", "allow", "deny"))
_ACTION_ONLY_PERMISSION_KEYS = frozenset(("todowrite", "question", "webfetch", "websearch", "doom_loop"))

# Mirrors OpenCode's own extraction regexes so a rendered file never carries
# a pattern the runtime would execute, substitute or resolve at command
# time (`packages/opencode/src/config/markdown.ts`
# `SHELL_REGEX = /!\`([^\`]+)\`/g`, `FILE_REGEX = /(?<![\w\`])@(...)/g`).
_SHELL_EXPANSION_RE = re.compile(r"!`[^`]+`")
_FILE_REFERENCE_RE = re.compile(r"(?<![\w`])@\S")
_DOLLAR_DIGIT_RE = re.compile(r"\$\d")

# Built from an escape, never the raw glyph, so a section-sign-plus-digit
# pattern here never itself matches the stage-1 source sweep.
_SECTION_SIGN = "§"
_SECTION_DIGIT_RE = re.compile(_SECTION_SIGN + r"\d")

_CLAUDE_TIER_WORDS = ("haiku", "sonnet", "opus")
_MODEL_LINE_RE = re.compile(
    r"^\s*model:.*\b(%s)\b" % "|".join(_CLAUDE_TIER_WORDS), re.IGNORECASE | re.MULTILINE
)

_CLAUDE_HOME_PATTERNS = ("~/.claude", "$HOME/.claude", ".claude/")

_DISPATCH_SENTINEL = "[no-redispatch]"
_ASK_USER_QUESTION = "AskUserQuestion"


def _model_id_patterns() -> List["re.Pattern"]:
    # Assembled from split literals, mirroring `test_opencode_docs.py`'s own
    # `_model_id_denylist`, so this module's own source text never contains
    # one of these ids whole (keeps the stage-1 source sweep green).
    raw = (
        "gpt" + "-4o",
        r"\bgpt-[0-9]",
        r"o[1-9]-(mini" + r"|preview)",
        r"claude-(3|opus|sonnet|" + r"haiku)",
        "qwen" + r"[0-9]",
        "gemini-" + r"[0-9]",
        r"llama-?" + r"[0-9]",
        r"deepseek-(v|r|" + r"coder)",
        r"mistral-(large|small|" + r"medium)",
        r"kimi-k" + r"[0-9]",
        r"glm-" + r"[0-9]",
    )
    return [re.compile(p, re.IGNORECASE) for p in raw]


def _check_frontmatter_keys(relpath: str, kind: str, fields: dict) -> List[str]:
    allowed = _FRONTMATTER_ALLOWED_KEYS[kind]
    extra = sorted(set(fields) - allowed)
    if extra:
        return ["%s: frontmatter has unexpected key(s): %s" % (relpath, ", ".join(extra))]
    return []


def _check_permission_value(relpath: str, perm_type: str, value: object) -> List[str]:
    findings: List[str] = []
    if isinstance(value, str):
        if value not in _PERMISSION_ACTIONS:
            findings.append(
                "%s: permission %r has action %r, expected one of %s"
                % (relpath, perm_type, value, sorted(_PERMISSION_ACTIONS))
            )
        return findings
    if isinstance(value, dict):
        if perm_type in _ACTION_ONLY_PERMISSION_KEYS:
            findings.append("%s: permission %r may not be a map (Action-only key)" % (relpath, perm_type))
            return findings
        if not value:
            findings.append("%s: permission %r is an empty map" % (relpath, perm_type))
            return findings
        for pattern, action in value.items():
            if not isinstance(pattern, str) or not isinstance(action, str) or action not in _PERMISSION_ACTIONS:
                findings.append(
                    "%s: permission %r pattern %r has invalid action %r"
                    % (relpath, perm_type, pattern, action)
                )
        return findings
    findings.append("%s: permission %r has a value of type %s, expected str or map" % (relpath, perm_type, type(value).__name__))
    return findings


def _check_agent_frontmatter_values(relpath: str, fields: dict) -> List[str]:
    findings: List[str] = []
    mode = fields.get("mode")
    if mode not in _AGENT_MODES:
        findings.append("%s: agent mode %r not in %s" % (relpath, mode, sorted(_AGENT_MODES)))
    permission = fields.get("permission")
    if not isinstance(permission, dict):
        findings.append("%s: agent 'permission' must be a map" % relpath)
    else:
        for perm_type, value in permission.items():
            findings.extend(_check_permission_value(relpath, perm_type, value))
    return findings


def _check_command_frontmatter_values(relpath: str, fields: dict) -> List[str]:
    findings: List[str] = []
    if not isinstance(fields.get("description"), str):
        findings.append("%s: command 'description' must be a string" % relpath)
    if not isinstance(fields.get("agent"), str):
        findings.append("%s: command 'agent' must be a string" % relpath)
    return findings


def _check_command_body(relpath: str, body: str) -> List[str]:
    findings: List[str] = []
    count = body.count("$ARGUMENTS")
    if count != 1:
        findings.append("%s: command body has %d occurrences of $ARGUMENTS, expected exactly 1" % (relpath, count))
    if _DOLLAR_DIGIT_RE.search(body):
        findings.append("%s: command body contains a $<digit> positional placeholder" % relpath)
    if _SHELL_EXPANSION_RE.search(body):
        findings.append("%s: command body contains a shell-expansion pattern" % relpath)
    if _FILE_REFERENCE_RE.search(body):
        findings.append("%s: command body contains an @ file reference" % relpath)
    return findings


def _check_universal_hazards(relpath: str, kind: str, text: str) -> List[str]:
    findings: List[str] = []
    if _SHELL_EXPANSION_RE.search(text):
        findings.append("%s: contains a shell-expansion pattern" % relpath)
    if _ASK_USER_QUESTION in text:
        findings.append("%s: contains %s" % (relpath, _ASK_USER_QUESTION))
    if _DISPATCH_SENTINEL in text:
        findings.append("%s: contains the %s sentinel" % (relpath, _DISPATCH_SENTINEL))
    if _SECTION_DIGIT_RE.search(text):
        findings.append("%s: contains a section-sign-plus-digit token" % relpath)
    if _MODEL_LINE_RE.search(text):
        findings.append("%s: contains a model: line naming a Claude tier" % relpath)
    for pattern in _model_id_patterns():
        if pattern.search(text):
            findings.append("%s: contains a real model id (%s)" % (relpath, pattern.pattern))
    if kind != "instructions":
        for token in _CLAUDE_HOME_PATTERNS:
            if token in text:
                findings.append("%s: contains a Claude home path (%s)" % (relpath, token))
    return findings


def _check_script_references(relpath: str, text: str) -> List[str]:
    findings: List[str] = []
    for name in scripts.referenced_scripts(text):
        if name not in scripts.ALLOWED_SCRIPTS:
            findings.append("%s: references unknown helper script '%s'" % (relpath, name))
    return findings


def check_rendered(files: Dict[str, RenderedFile]) -> List[str]:
    """Forbidden-output check over a rendered file set.

    Called by `render` (which raises `GenerationError` naming every
    finding when this is non-empty) and public for the later offline
    smoke. Findings name the relpath and the rule:

    - every command, skill and agent file carries frontmatter that
      `frontmatter.parse` accepts, restricted to its kind's key allowlist
      (no `model`, `variant`, `temperature`, `options`, `tools`, `hooks`
      or `subtask` key can appear, because none of those is in any
      allowlist);
    - agent frontmatter values are checked because a bad one fails the
      whole OpenCode config load for the instance: `mode` is one of
      `subagent`/`primary`/`all`; `permission` is a map whose values are
      each an action or a non-empty map from pattern to action, and the
      Action-only keys never carry a map;
    - command bodies carry exactly one `$ARGUMENTS`, no `$<digit>`
      positional placeholder, no shell-expansion pattern and no `@` file
      reference;
    - every rendered file is checked for shell-expansion patterns,
      `AskUserQuestion`, the `[no-redispatch]` sentinel, a section sign
      followed by a digit, a `model:` line naming a Claude tier, a real
      model id, and a Claude home path (the instruction document is
      exempt from the last one, because it names those paths as legacy
      discovery locations by design);
    - every `quoin opencode script <name>` reference in any file names an
      entry of `scripts.ALLOWED_SCRIPTS`.
    """
    findings: List[str] = []
    for relpath in sorted(files):
        rf = files[relpath]
        text = rf.content.decode("utf-8")

        if rf.kind in _FRONTMATTER_KINDS:
            try:
                fields, body = frontmatter.parse(text)
            except frontmatter.FrontmatterError as exc:
                findings.append("%s: frontmatter did not parse: %s" % (relpath, exc))
            else:
                findings.extend(_check_frontmatter_keys(relpath, rf.kind, fields))
                if rf.kind == "agent":
                    findings.extend(_check_agent_frontmatter_values(relpath, fields))
                elif rf.kind == "command":
                    findings.extend(_check_command_frontmatter_values(relpath, fields))
                    findings.extend(_check_command_body(relpath, body))

        findings.extend(_check_universal_hazards(relpath, rf.kind, text))
        findings.extend(_check_script_references(relpath, text))

    return findings


def render(inputs: GeneratorInputs) -> Dict[str, RenderedFile]:
    manifest_roles = inputs.manifest.get("roles") or {}
    supported_rows = sorted(
        (
            row
            for row in inputs.manifest.get("catalog_entries", [])
            if isinstance(row, dict) and row.get("status") == "supported"
        ),
        key=lambda row: row["id"],
    )

    command_pairs = [(names.normalize(row["id"]), row["id"]) for row in supported_rows]
    skill_pairs = list(command_pairs)
    agent_pairs = [(names.role_agent_name(role), role) for role in manifest_roles]

    names.check_unique("command", command_pairs)
    names.check_unique("skill", skill_pairs)
    names.check_unique("agent", agent_pairs)

    all_names = (
        [n for n, _ in command_pairs] + [n for n, _ in skill_pairs] + [n for n, _ in agent_pairs]
    )
    for candidate in all_names:
        err = names.name_error(candidate)
        if err:
            raise GenerationError(err)

    graph_errors = check_task_graph(manifest_roles)
    if graph_errors:
        raise GenerationError("; ".join(graph_errors))

    from quoin.opencode_adapter import boundaries

    inheritance_errors = boundaries.check_inheritance(
        {role: role_permissions(role) for role in manifest_roles},
        {role: task_targets(role) for role in manifest_roles},
    )
    if inheritance_errors:
        raise GenerationError("; ".join(inheritance_errors))

    catalog_id_set = {c["name"] for c in inputs.catalog if isinstance(c, dict) and isinstance(c.get("name"), str)}
    supported_id_set = {row["id"] for row in supported_rows}

    files: Dict[str, RenderedFile] = {}

    for row in supported_rows:
        cid = row["id"]
        name = names.normalize(cid)
        opencode = row["opencode"]
        role = opencode["agent_role"]
        agent = names.role_agent_name(role)
        overlay_entry = inputs.overlays["entries"][cid]
        description = _expand_artifact_root(overlay_entry["description"])
        expanded_command_note = _expand_artifact_root(overlay_entry["command_note"])
        # The template places {{COMMAND_NOTE}} directly before "Arguments
        # passed to this command:" on the same line, so a non-empty note
        # supplies its own trailing blank line and an empty one leaves
        # exactly one blank line between the intro paragraph and the
        # arguments line.
        command_note = expanded_command_note + "\n\n" if expanded_command_note else ""

        parts_c = {
            "kind": "command",
            "id": cid,
            "name": name,
            "agent": agent,
            "title": name,
            "description": description,
            "command_note": command_note,
            "template_command": inputs.templates["command"],
            "version": GENERATOR_SCHEMA_VERSION,
            "schema": ["description", "agent"],
        }
        command_digest = _digest(parts_c)
        command_relpath = ".opencode/commands/%s.md" % name
        files[command_relpath] = RenderedFile(
            relpath=command_relpath,
            content=render_command(parts_c).encode("utf-8"),
            kind="command",
            source_id=cid,
            source_digest=command_digest,
        )

        contract_text, dropped = _assemble_contract(
            cid, inputs.contracts[cid], overlay_entry, catalog_id_set, supported_id_set
        )
        notes_text = _render_notes([_expand_artifact_root(n) for n in overlay_entry["notes"]], dropped)

        parts_s = {
            "kind": "skill",
            "id": cid,
            "name": name,
            "command": name,
            "agent": agent,
            "description": description,
            "contract_text": contract_text,
            "notes": notes_text,
            "template_skill": inputs.templates["skill"],
            "version": GENERATOR_SCHEMA_VERSION,
            "schema": ["name", "description", "metadata"],
        }
        skill_digest = _digest(parts_s)
        skill_relpath = ".opencode/skills/%s/SKILL.md" % name
        files[skill_relpath] = RenderedFile(
            relpath=skill_relpath,
            content=render_skill(parts_s, skill_digest).encode("utf-8"),
            kind="skill",
            source_id=cid,
            source_digest=skill_digest,
        )

    for role, role_def in manifest_roles.items():
        agent = names.role_agent_name(role)
        mode = role_def.get("mode") if isinstance(role_def, dict) else None
        role_overlay = inputs.overlays["roles"][role]
        description = _expand_artifact_root(role_overlay["description"])
        prompt_text = "\n\n".join(_expand_artifact_root(p) for p in role_overlay["prompt"])
        permission = role_permissions(role)
        targets = task_targets(role)

        parts_a = {
            "kind": "agent",
            "role": role,
            "mode": mode,
            "description": description,
            "prompt": prompt_text,
            "permission": permission,
            "targets": targets,
            "template_agent": inputs.templates["agent"],
            "version": GENERATOR_SCHEMA_VERSION,
            "schema": ["description", "mode", "permission"],
        }
        agent_digest = _digest(parts_a)
        agent_relpath = ".opencode/agents/%s.md" % agent
        files[agent_relpath] = RenderedFile(
            relpath=agent_relpath,
            content=render_agent(parts_a).encode("utf-8"),
            kind="agent",
            source_id=role,
            source_digest=agent_digest,
        )

    command_list = "\n".join(
        "- `/%s` — %s" % (names.normalize(row["id"]), _expand_artifact_root(inputs.overlays["entries"][row["id"]]["description"]))
        for row in supported_rows
    )
    unavailable_rows = sorted(
        (
            row
            for row in inputs.manifest.get("catalog_entries", [])
            if isinstance(row, dict) and row.get("status") != "supported"
        ),
        key=lambda row: row["id"],
    )
    unavailable_list = "\n".join(
        "- `%s` (%s): %s" % (row["id"], row.get("status"), row.get("reason")) for row in unavailable_rows
    )
    role_table_lines = ["| Role | Mode | Summary |", "| --- | --- | --- |"]
    for role in sorted(manifest_roles):
        role_def = manifest_roles[role]
        role_table_lines.append(
            "| %s | %s | %s |" % (role, role_def.get("mode"), role_def.get("summary"))
        )
    role_table = "\n".join(role_table_lines)

    limits = inputs.manifest.get("limits") or {}
    limits_lines = []
    for key in sorted(limits):
        val = limits[key]
        if not isinstance(val, dict):
            continue
        enforcement = val.get("enforcement")
        note = val.get("note", "")
        limits_lines.append("- %s: %s — %s" % (key, enforcement, note))
    limits_text = "\n".join(limits_lines)

    # Dropping the H1 in _demote_headings_outside_fences leaves the blank
    # line that used to separate it from the body; strip that so the
    # template's own blank line after "## Core workflow rules" isn't doubled.
    core_rules = _demote_headings_outside_fences(inputs.rules).lstrip("\n")

    parts_i = {
        "kind": "instructions",
        "command_list": command_list,
        "unavailable_list": unavailable_list,
        "role_table": role_table,
        "limits": limits_text,
        "root": ARTIFACT_ROOT,
        "core_rules": core_rules,
        "template_instructions": inputs.templates["instructions"],
        "version": GENERATOR_SCHEMA_VERSION,
        "schema": ["instructions"],
    }
    instructions_digest = _digest(parts_i)
    files[INSTRUCTIONS_PATH] = RenderedFile(
        relpath=INSTRUCTIONS_PATH,
        content=render_instructions(parts_i).encode("utf-8"),
        kind="instructions",
        source_id=None,
        source_digest=instructions_digest,
    )

    parts_k = {
        "kind": "config",
        "instructions_path": INSTRUCTIONS_PATH,
        "version": GENERATOR_SCHEMA_VERSION,
        "schema": ["$schema", "instructions"],
    }
    config_digest = _digest(parts_k)
    files[CONFIG_PATH] = RenderedFile(
        relpath=CONFIG_PATH,
        content=render_config(parts_k).encode("utf-8"),
        kind="config",
        source_id=None,
        source_digest=config_digest,
    )

    findings = check_rendered(files)
    if findings:
        raise GenerationError("; ".join(findings))

    return dict(sorted(files.items()))


def render_source_dir(source_dir) -> Dict[str, RenderedFile]:
    return render(load_inputs(source_dir))
