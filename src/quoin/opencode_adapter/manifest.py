"""Load the OpenCode catalog manifest and check it for drift.

Pure, stdlib-only functions. `check_manifest` compares the manifest against
the portable skill catalog (`skills.json`) and the compatibility pin, and
returns a deterministic list of offender-naming error strings — empty when
the manifest matches. Used by the CI drift check in `__main__.py`, and by
the doctor in a later stage.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from quoin.opencode_adapter import names

MANIFEST_PARTS = ("adapters", "opencode", "feature-manifest.json")
CATALOG_PARTS = ("core", "workflow", "skills.json")
COMPAT_PARTS = ("adapters", "opencode", "compatibility.md")

STATUSES = ("supported", "documentation-only", "unsupported")
SUPPORTED_ASSETS = ["command", "skill"]
ROLE_MODES = ("primary", "subagent", "all")
ENFORCEMENT_LABELS = ("enforced-natively", "declared-not-enforced")

REQUIRED_TOP_KEYS = (
    "schema_version",
    "feature_name",
    "adapter",
    "status",
    "description",
    "opencode_version",
    "entrypoints",
    "portable_inputs",
    "generated_outputs",
    "unsupported_outputs",
    "validation",
    "milestones",
    "roles",
    "limits",
    "catalog_entries",
)

_VERSION_RE = re.compile(r"^Version:\s*(\d+\.\d+\.\d+)", re.MULTILINE)
_PINNED_SECTION_RE = re.compile(r"^## Pinned release\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)


class ManifestLoadError(Exception):
    pass


def load_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestLoadError("cannot read %s: %s" % (path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise ManifestLoadError("cannot decode %s as UTF-8: %s" % (path, exc)) from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ManifestLoadError("invalid JSON in %s: %s" % (path, exc)) from exc


def load_catalog(source_dir) -> List[dict]:
    path = Path(source_dir).joinpath(*CATALOG_PARTS)
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ManifestLoadError("%s is not a catalog object with a 'skills' list" % path)
    return data["skills"]


def load_manifest(source_dir) -> dict:
    path = Path(source_dir).joinpath(*MANIFEST_PARTS)
    data = load_json(path)
    if not isinstance(data, dict):
        raise ManifestLoadError("%s is not a JSON object" % path)
    return data


def read_pinned_version(source_dir) -> str:
    path = Path(source_dir).joinpath(*COMPAT_PARTS)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestLoadError("cannot read %s: %s" % (path, exc)) from exc
    section_match = _PINNED_SECTION_RE.search(text)
    if not section_match:
        raise ManifestLoadError("%s has no '## Pinned release' section" % path)
    rest = text[section_match.end():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    section_body = rest[: next_heading.start()] if next_heading else rest
    version_match = _VERSION_RE.search(section_body)
    if not version_match:
        raise ManifestLoadError("%s has no 'Version:' line in its Pinned release section" % path)
    return version_match.group(1)


def check_manifest(manifest: dict, catalog: List[dict], pinned_version: str) -> List[str]:
    errs: List[str] = []

    for key in REQUIRED_TOP_KEYS:
        if key not in manifest:
            errs.append("manifest is missing required top-level key '%s'" % key)
    if manifest.get("schema_version") != 2:
        errs.append("schema_version is %r, expected 2" % (manifest.get("schema_version"),))
    if manifest.get("adapter") != "opencode":
        errs.append("adapter is %r, expected 'opencode'" % (manifest.get("adapter"),))
    if manifest.get("opencode_version") != pinned_version:
        errs.append(
            "opencode_version is %r, compatibility.md pins %r"
            % (manifest.get("opencode_version"), pinned_version)
        )

    milestones = manifest.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        errs.append("milestones must be a non-empty list")
        milestones = []
    else:
        try:
            has_duplicates = len(set(milestones)) != len(milestones)
        except TypeError:
            errs.append("milestones contains an unhashable entry")
        else:
            if has_duplicates:
                errs.append("milestones contains duplicate entries")
        if "none" not in milestones:
            errs.append("milestones must include 'none'")

    roles = manifest.get("roles")
    role_names = set()
    role_agent_pairs = []
    if not isinstance(roles, dict):
        errs.append("roles must be an object")
        roles = {}
    for role_name, role_def in roles.items():
        role_names.add(role_name)
        if not isinstance(role_def, dict):
            errs.append("role '%s' is not an object" % role_name)
            continue
        agent_name = names.role_agent_name(role_name)
        name_err = names.name_error(agent_name)
        if name_err:
            errs.append("role '%s': %s" % (role_name, name_err))
        if role_def.get("mode") not in ROLE_MODES:
            errs.append(
                "role '%s' has mode %r, expected one of %s" % (role_name, role_def.get("mode"), ROLE_MODES)
            )
        if not role_def.get("summary"):
            errs.append("role '%s' has an empty summary" % role_name)
        role_agent_pairs.append((agent_name, role_name))
    errs.extend(names.find_collisions("agent", role_agent_pairs))

    limits = manifest.get("limits")
    if not isinstance(limits, dict):
        errs.append("limits must be an object")
        limits = {}
    for limit_key in ("delegation_depth", "concurrency"):
        limit_val = limits.get(limit_key)
        if not isinstance(limit_val, dict):
            errs.append("limits.%s must be an object" % limit_key)
            continue
        if limit_val.get("enforcement") not in ENFORCEMENT_LABELS:
            errs.append(
                "limits.%s.enforcement is %r, expected one of %s"
                % (limit_key, limit_val.get("enforcement"), ENFORCEMENT_LABELS)
            )

    catalog_ids = []
    catalog_by_id = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            errs.append("catalog contains a non-object entry")
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            errs.append("catalog entry has a non-string 'name': %r" % (name,))
            continue
        catalog_ids.append(name)
        catalog_by_id[name] = entry
    catalog_id_set = set(catalog_ids)

    rows = manifest.get("catalog_entries")
    if not isinstance(rows, list):
        errs.append("catalog_entries must be a list")
        rows = []

    row_ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not isinstance(rid, str):
            errs.append("catalog_entries row has a non-string 'id': %r" % (rid,))
            continue
        row_ids.append(rid)
    seen = set()
    dup_ids = set()
    for rid in row_ids:
        if rid in seen:
            dup_ids.add(rid)
        seen.add(rid)
    for rid in sorted(dup_ids):
        errs.append("duplicate row id '%s'" % rid)

    if row_ids != sorted(row_ids):
        errs.append("catalog_entries is not sorted by id")

    row_id_set = set(row_ids)
    for missing in sorted(catalog_id_set - row_id_set):
        errs.append("missing row for catalog id '%s'" % missing)
    for orphan in sorted(row_id_set - catalog_id_set):
        errs.append("orphan row '%s'" % orphan)

    command_pairs = []
    skill_pairs = []

    for row in rows:
        if not isinstance(row, dict):
            errs.append("catalog_entries contains a non-object row")
            continue
        rid = row.get("id")
        status = row.get("status")
        if status not in STATUSES:
            errs.append("row '%s' has status %r, expected one of %s" % (rid, status, STATUSES))
        if not row.get("reason"):
            errs.append("row '%s' has an empty reason" % rid)
        target = row.get("target_milestone")
        if not target or target not in milestones:
            errs.append("row '%s' has target_milestone %r, not in milestones" % (rid, target))
        if target == "none" and status != "unsupported":
            errs.append(
                "row '%s' uses target_milestone 'none' but status is %r, not 'unsupported'" % (rid, status)
            )

        catalog_entry = catalog_by_id.get(rid)
        if catalog_entry is not None:
            row_catalog = row.get("catalog")
            if not isinstance(row_catalog, dict):
                row_catalog = {}
            for flag in ("user_facing", "spawn_target"):
                if row_catalog.get(flag) != catalog_entry.get(flag):
                    errs.append(
                        "row '%s' catalog.%s is %r, skills.json has %r"
                        % (rid, flag, row_catalog.get(flag), catalog_entry.get(flag))
                    )

        assets = row.get("assets")
        opencode = row.get("opencode")
        if status == "supported":
            if assets != SUPPORTED_ASSETS:
                errs.append(
                    "row '%s' is supported but assets is %r, expected %s" % (rid, assets, SUPPORTED_ASSETS)
                )
            if isinstance(opencode, dict) and rid is not None:
                expected_name = names.normalize(rid)
                if opencode.get("command") != expected_name:
                    errs.append(
                        "row '%s' opencode.command is %r, expected %r"
                        % (rid, opencode.get("command"), expected_name)
                    )
                if opencode.get("skill") != expected_name:
                    errs.append(
                        "row '%s' opencode.skill is %r, expected %r" % (rid, opencode.get("skill"), expected_name)
                    )
                agent_role = opencode.get("agent_role")
                if agent_role not in role_names:
                    errs.append("row '%s' opencode.agent_role %r is not a declared role" % (rid, agent_role))
                command_pairs.append((expected_name, rid))
                skill_pairs.append((expected_name, rid))
            else:
                errs.append("row '%s' is supported but opencode is not an object" % rid)
        else:
            if assets:
                errs.append("row '%s' has status %r but assets is non-empty: %r" % (rid, status, assets))
            if opencode:
                errs.append("row '%s' has status %r but opencode is non-empty: %r" % (rid, status, opencode))

        live_evidence = row.get("live_runtime_evidence")
        evidence = row.get("evidence")
        if not isinstance(live_evidence, bool):
            errs.append("row '%s' live_runtime_evidence must be a bool" % rid)
        if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
            errs.append("row '%s' evidence must be a list of strings" % rid)
        if live_evidence is True and not evidence:
            errs.append("row '%s' live_runtime_evidence is true but evidence is empty" % rid)

    for cid in sorted(catalog_id_set):
        name_err = names.name_error(names.normalize(cid))
        if name_err:
            errs.append("catalog id '%s': %s" % (cid, name_err))

    catalog_wide_pairs = [(names.normalize(cid), cid) for cid in sorted(catalog_id_set)]
    errs.extend(names.find_collisions("catalog", catalog_wide_pairs))
    errs.extend(names.find_collisions("command", command_pairs))
    errs.extend(names.find_collisions("skill", skill_pairs))

    return errs


def check_source_dir(source_dir) -> List[str]:
    manifest = load_manifest(source_dir)
    catalog = load_catalog(source_dir)
    pinned_version = read_pinned_version(source_dir)
    return check_manifest(manifest, catalog, pinned_version)
