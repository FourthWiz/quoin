#!/usr/bin/env python3
"""
validate_discovery_map.py — stdlib-only structural validator for quoin discovery maps.

No LLM calls. No network. Pure stdlib only.

Invariants implemented:
  DM-01  Top-level type is dict.
  DM-02  All top-level required keys present (schema_version, generated_at, project,
          artifact_roots, memory, tasks, repos).
  DM-03  No unknown top-level keys (strict at top level; extensions is the escape hatch).
  DM-04  schema_version == 1 (int, not string). Checked before DM-05.
  DM-05  For each typed field, value matches expected type (str/int/list/dict).
  DM-06  Each enum field's value is in the allowed set (task status, relation, stage status).
  DM-07  repos array elements conform to RepoSummary; tasks.active and tasks.finalized
          elements conform to TaskSummary; TaskSummary.stages elements conform to StageSummary.
  DM-08  extensions (if present) is dict; each value is a dict (object). Any string key
          is allowed — no closed allowlist. Non-conforming keys silently pass.
  DM-09  All path-bearing fields enumerated in PATH_FIELDS (except project.root_path)
          hold repo-relative strings: no leading './' or '/', forward slashes only.

Checks execute in the listed order. Wrong-type checks on `schema_version` precede the
generic missing-field check to produce the precise error for the f02 fixture.

CLI:
  Usage: validate_discovery_map.py [--quiet] [--verbose] <map.json>
  Exit:  0 = PASS (no errors); 1 = at least one error; 2 = invocation error
          (file not found, JSON parse error, or truncated/corrupted file)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List


# ── Allowed top-level keys ────────────────────────────────────────────────────

_TOP_LEVEL_REQUIRED: frozenset = frozenset({
    "schema_version",
    "generated_at",
    "project",
    "artifact_roots",
    "memory",
    "tasks",
    "repos",
})

_TOP_LEVEL_OPTIONAL: frozenset = frozenset({
    "dependency_hints",
    "freshness",
    "extensions",
})

_TOP_LEVEL_ALLOWED: frozenset = _TOP_LEVEL_REQUIRED | _TOP_LEVEL_OPTIONAL


# ── PATH_FIELDS constant ──────────────────────────────────────────────────────
# Authoritative contract for path-bearing fields checked by DM-09.
# All of these must be repo-relative strings (no leading './' or '/',
# forward slashes only). Exception: project.root_path is absolute (not listed here).
# Optional fields marked with (optional) in comments — absence does not trigger DM-09.

PATH_FIELDS = [
    # artifact_roots fields
    ("artifact_roots", "workflow_artifacts_path", False),      # required
    ("artifact_roots", "tasks_path", False),                   # required
    ("artifact_roots", "finalized_path", False),               # required
    ("artifact_roots", "memory_path", False),                  # required
    ("artifact_roots", "cache_path", True),                    # optional

    # memory fields
    ("memory", "lessons_learned", False),                      # required
    ("memory", "daily_dir", False),                            # required
    ("memory", "weekly_dir", False),                           # required
    ("memory", "sessions_dir", False),                         # required
    ("memory", "memory_md_index", True),                       # optional
    ("memory", "staleness_path", True),                        # optional
    ("memory", "repo_heads_path", True),                       # optional
]

# Path fields inside tasks (active and finalized arrays) — checked per element
_TASK_PATH_FIELDS = [
    ("path", False),                 # required
    ("architecture_path", True),     # optional
    ("current_plan_path", True),     # optional
]

# Path fields inside task stages
_STAGE_PATH_FIELDS = [
    ("path", False),                 # required
]

# Path fields inside repos
_REPO_PATH_FIELDS = [
    ("path", False),                 # required
    ("cache_index_path", True),      # optional
]
# repos[].entry_points[] — each element is a path string (checked separately)


# ── Enum allowlists ───────────────────────────────────────────────────────────

_TASK_STATUS_ENUM: frozenset = frozenset({"active", "finalized"})
_STAGE_STATUS_ENUM: frozenset = frozenset({"pending", "active", "finalized"})
_RELATION_ENUM: frozenset = frozenset({"references", "precedes", "derives_from"})


# ── Path style checker ────────────────────────────────────────────────────────

def _bad_repo_relative(value: str) -> bool:
    """Return True if value violates repo-relative path conventions (DM-09)."""
    if value.startswith("./") or value.startswith("/"):
        return True
    if "\\" in value:
        return True
    return False


# ── Sub-object validators ─────────────────────────────────────────────────────

def _check_project(obj: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(obj, dict):
        errors.append("DM-05: project: expected dict")
        return errors
    for req in ("name", "root_path", "runtime_adapters"):
        if req not in obj:
            errors.append(f"DM-05: project.{req}: required field missing")
    if "name" in obj and not isinstance(obj["name"], str):
        errors.append(f"DM-05: project.name: expected str, got {type(obj['name']).__name__}")
    if "root_path" in obj and not isinstance(obj["root_path"], str):
        errors.append(f"DM-05: project.root_path: expected str, got {type(obj['root_path']).__name__}")
    if "runtime_adapters" in obj:
        if not isinstance(obj["runtime_adapters"], list):
            errors.append(f"DM-05: project.runtime_adapters: expected list, got {type(obj['runtime_adapters']).__name__}")
        else:
            for i, adapter in enumerate(obj["runtime_adapters"]):
                if not isinstance(adapter, str):
                    errors.append(f"DM-05: project.runtime_adapters[{i}]: expected str, got {type(adapter).__name__}")
    return errors


def _check_artifact_roots(obj: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(obj, dict):
        errors.append("DM-05: artifact_roots: expected dict")
        return errors
    for req in ("workflow_artifacts_path", "tasks_path", "finalized_path", "memory_path"):
        if req not in obj:
            errors.append(f"DM-05: artifact_roots.{req}: required field missing")
    for fld in ("workflow_artifacts_path", "tasks_path", "finalized_path", "memory_path", "cache_path"):
        if fld in obj and not isinstance(obj[fld], str):
            errors.append(f"DM-05: artifact_roots.{fld}: expected str, got {type(obj[fld]).__name__}")
    return errors


def _check_memory(obj: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(obj, dict):
        errors.append("DM-05: memory: expected dict")
        return errors
    for req in ("lessons_learned", "daily_dir", "weekly_dir", "sessions_dir"):
        if req not in obj:
            errors.append(f"DM-05: memory.{req}: required field missing")
    for fld in ("lessons_learned", "daily_dir", "weekly_dir", "sessions_dir",
                "memory_md_index", "staleness_path", "repo_heads_path"):
        if fld in obj and not isinstance(obj[fld], str):
            errors.append(f"DM-05: memory.{fld}: expected str, got {type(obj[fld]).__name__}")
    return errors


def _check_stage_summary(stage: Any, prefix: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(stage, dict):
        errors.append(f"DM-07: {prefix}: expected dict, got {type(stage).__name__}")
        return errors
    for req in ("id", "path", "status"):
        if req not in stage:
            errors.append(f"DM-07: {prefix}.{req}: required field missing")
    if "id" in stage and not isinstance(stage["id"], int):
        errors.append(f"DM-07: {prefix}.id: expected int, got {type(stage['id']).__name__}")
    if "path" in stage and not isinstance(stage["path"], str):
        errors.append(f"DM-07: {prefix}.path: expected str, got {type(stage['path']).__name__}")
    if "status" in stage:
        if not isinstance(stage["status"], str):
            errors.append(f"DM-07: {prefix}.status: expected str, got {type(stage['status']).__name__}")
        elif stage["status"] not in _STAGE_STATUS_ENUM:
            errors.append(f"DM-06: {prefix}.status: invalid enum value '{stage['status']}', must be one of {sorted(_STAGE_STATUS_ENUM)}")
    return errors


def _check_task_summary(task: Any, prefix: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(task, dict):
        errors.append(f"DM-07: {prefix}: expected dict, got {type(task).__name__}")
        return errors
    for req in ("name", "status", "path", "last_updated"):
        if req not in task:
            errors.append(f"DM-07: {prefix}.{req}: required field missing")
    if "name" in task and not isinstance(task["name"], str):
        errors.append(f"DM-07: {prefix}.name: expected str, got {type(task['name']).__name__}")
    if "status" in task:
        if not isinstance(task["status"], str):
            errors.append(f"DM-07: {prefix}.status: expected str, got {type(task['status']).__name__}")
        elif task["status"] not in _TASK_STATUS_ENUM:
            errors.append(f"DM-06: {prefix}.status: invalid enum value '{task['status']}', must be one of {sorted(_TASK_STATUS_ENUM)}")
    if "path" in task and not isinstance(task["path"], str):
        errors.append(f"DM-07: {prefix}.path: expected str, got {type(task['path']).__name__}")
    if "last_updated" in task and not isinstance(task["last_updated"], str):
        errors.append(f"DM-07: {prefix}.last_updated: expected str, got {type(task['last_updated']).__name__}")
    if "stages" in task:
        if not isinstance(task["stages"], list):
            errors.append(f"DM-07: {prefix}.stages: expected list, got {type(task['stages']).__name__}")
        else:
            for i, stage in enumerate(task["stages"]):
                errors.extend(_check_stage_summary(stage, f"{prefix}.stages[{i}]"))
    for opt_path in ("architecture_path", "current_plan_path"):
        if opt_path in task and not isinstance(task[opt_path], str):
            errors.append(f"DM-07: {prefix}.{opt_path}: expected str, got {type(task[opt_path]).__name__}")
    return errors


def _check_tasks_container(obj: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(obj, dict):
        errors.append("DM-05: tasks: expected dict")
        return errors
    for req in ("active", "finalized"):
        if req not in obj:
            errors.append(f"DM-05: tasks.{req}: required field missing")
    for key in obj:
        if key not in ("active", "finalized"):
            errors.append(f"DM-03: tasks.{key}: unknown key in tasks container (only 'active' and 'finalized' are allowed)")
    for bucket in ("active", "finalized"):
        if bucket not in obj:
            continue
        if not isinstance(obj[bucket], list):
            errors.append(f"DM-05: tasks.{bucket}: expected list, got {type(obj[bucket]).__name__}")
            continue
        for i, task in enumerate(obj[bucket]):
            errors.extend(_check_task_summary(task, f"tasks.{bucket}[{i}]"))
    return errors


def _check_repo_summary(repo: Any, prefix: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(repo, dict):
        errors.append(f"DM-07: {prefix}: expected dict, got {type(repo).__name__}")
        return errors
    for req in ("name", "path", "head_sha", "head_short"):
        if req not in repo:
            errors.append(f"DM-07: {prefix}.{req}: required field missing")
    for str_fld in ("name", "path", "head_sha", "head_short", "language", "cache_index_path"):
        if str_fld in repo and not isinstance(repo[str_fld], str):
            errors.append(f"DM-07: {prefix}.{str_fld}: expected str, got {type(repo[str_fld]).__name__}")
    if "entry_points" in repo:
        if not isinstance(repo["entry_points"], list):
            errors.append(f"DM-07: {prefix}.entry_points: expected list, got {type(repo['entry_points']).__name__}")
        else:
            for i, ep in enumerate(repo["entry_points"]):
                if not isinstance(ep, str):
                    errors.append(f"DM-07: {prefix}.entry_points[{i}]: expected str, got {type(ep).__name__}")
    return errors


def _check_repos(repos: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(repos, list):
        errors.append(f"DM-05: repos: expected list, got {type(repos).__name__}")
        return errors
    for i, repo in enumerate(repos):
        errors.extend(_check_repo_summary(repo, f"repos[{i}]"))
    return errors


def _check_dependency_hints(hints: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(hints, list):
        errors.append(f"DM-05: dependency_hints: expected list, got {type(hints).__name__}")
        return errors
    for i, hint in enumerate(hints):
        prefix = f"dependency_hints[{i}]"
        if not isinstance(hint, dict):
            errors.append(f"DM-05: {prefix}: expected dict, got {type(hint).__name__}")
            continue
        for req in ("from_task", "to_task", "relation"):
            if req not in hint:
                errors.append(f"DM-05: {prefix}.{req}: required field missing")
        if "relation" in hint:
            if not isinstance(hint["relation"], str):
                errors.append(f"DM-05: {prefix}.relation: expected str, got {type(hint['relation']).__name__}")
            elif hint["relation"] not in _RELATION_ENUM:
                errors.append(f"DM-06: {prefix}.relation: invalid enum value '{hint['relation']}', must be one of {sorted(_RELATION_ENUM)}")
    return errors


def _check_freshness(freshness: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(freshness, dict):
        errors.append(f"DM-05: freshness: expected dict, got {type(freshness).__name__}")
        return errors
    for repo_name, entry in freshness.items():
        prefix = f"freshness['{repo_name}']"
        if not isinstance(entry, dict):
            errors.append(f"DM-05: {prefix}: expected dict, got {type(entry).__name__}")
            continue
        for req in ("head_sha", "recorded_at"):
            if req not in entry:
                errors.append(f"DM-05: {prefix}.{req}: required field missing")
        for str_fld in ("head_sha", "recorded_at", "cache_updated_at"):
            if str_fld in entry and not isinstance(entry[str_fld], str):
                errors.append(f"DM-05: {prefix}.{str_fld}: expected str, got {type(entry[str_fld]).__name__}")
    return errors


def _check_extensions(extensions: Any) -> List[str]:
    """DM-08: extensions must be dict; each value must be a dict. Any string key allowed."""
    errors: List[str] = []
    if not isinstance(extensions, dict):
        errors.append(f"DM-08: extensions: expected dict, got {type(extensions).__name__}")
        return errors
    for key, value in extensions.items():
        if not isinstance(value, dict):
            errors.append(f"DM-08: extensions.{key}: expected dict value, got {type(value).__name__}")
    return errors


def _check_path_fields(map_obj: dict) -> List[str]:
    """DM-09: check all path-bearing fields in PATH_FIELDS for repo-relative style."""
    errors: List[str] = []

    # Top-level section fields (artifact_roots, memory)
    for (section, fld, optional) in PATH_FIELDS:
        section_obj = map_obj.get(section)
        if not isinstance(section_obj, dict):
            continue
        if fld not in section_obj:
            if not optional:
                # Missing required field already reported by DM-02/DM-05
                pass
            continue
        val = section_obj[fld]
        if isinstance(val, str) and _bad_repo_relative(val):
            errors.append(f"DM-09: {section}.{fld}: path must be repo-relative (no './' or '/' prefix, forward slashes only), got '{val}'")

    # tasks.active and tasks.finalized
    tasks_obj = map_obj.get("tasks")
    if isinstance(tasks_obj, dict):
        for bucket in ("active", "finalized"):
            task_list = tasks_obj.get(bucket, [])
            if not isinstance(task_list, list):
                continue
            for i, task in enumerate(task_list):
                if not isinstance(task, dict):
                    continue
                for (fld, optional) in _TASK_PATH_FIELDS:
                    if fld not in task:
                        continue
                    val = task[fld]
                    if isinstance(val, str) and _bad_repo_relative(val):
                        errors.append(f"DM-09: tasks.{bucket}[{i}].{fld}: path must be repo-relative (no './' or '/' prefix, forward slashes only), got '{val}'")
                stages = task.get("stages", [])
                if not isinstance(stages, list):
                    continue
                for j, stage in enumerate(stages):
                    if not isinstance(stage, dict):
                        continue
                    for (fld, optional) in _STAGE_PATH_FIELDS:
                        if fld not in stage:
                            continue
                        val = stage[fld]
                        if isinstance(val, str) and _bad_repo_relative(val):
                            errors.append(f"DM-09: tasks.{bucket}[{i}].stages[{j}].{fld}: path must be repo-relative (no './' or '/' prefix, forward slashes only), got '{val}'")

    repos = map_obj.get("repos", [])
    if isinstance(repos, list):
        for i, repo in enumerate(repos):
            if not isinstance(repo, dict):
                continue
            for (fld, optional) in _REPO_PATH_FIELDS:
                if fld not in repo:
                    continue
                val = repo[fld]
                if isinstance(val, str) and _bad_repo_relative(val):
                    errors.append(f"DM-09: repos[{i}].{fld}: path must be repo-relative (no './' or '/' prefix, forward slashes only), got '{val}'")
            # entry_points — each element is a path
            entry_points = repo.get("entry_points", [])
            if isinstance(entry_points, list):
                for j, ep in enumerate(entry_points):
                    if isinstance(ep, str) and _bad_repo_relative(ep):
                        errors.append(f"DM-09: repos[{i}].entry_points[{j}]: path must be repo-relative (no './' or '/' prefix, forward slashes only), got '{ep}'")

    return errors


# ── Main validate function ────────────────────────────────────────────────────

def validate(map_obj: dict) -> List[str]:
    """
    Validate a discovery map dict. Returns a list of error strings (empty = PASS).

    validate() -> list[str] carries errors only. No NOTE or warning channel exists.
    Non-conforming-but-structurally-valid inputs silently pass.

    Checks execute in order DM-01 through DM-09.
    DM-04 (schema_version type/value) is checked before DM-02 (missing required keys)
    because DM-04 is more specific and produces the precise error for the f02 fixture.
    """
    errors: List[str] = []

    # DM-01: top-level type is dict
    if not isinstance(map_obj, dict):
        errors.append(f"DM-01: top-level value must be a dict (object), got {type(map_obj).__name__}")
        return errors

    # DM-04: schema_version must be int 1 (checked before DM-02 per plan D-04)
    if "schema_version" in map_obj:
        sv = map_obj["schema_version"]
        if not isinstance(sv, int) or isinstance(sv, bool):
            errors.append(f"DM-04: schema_version: expected int 1, got {type(sv).__name__} '{sv}'")
        elif sv != 1:
            errors.append(f"DM-04: schema_version: expected int 1, got {sv}")

    # DM-02: all required top-level keys present
    missing = _TOP_LEVEL_REQUIRED - set(map_obj.keys())
    for key in sorted(missing):
        errors.append(f"DM-02: {key}: required top-level key missing")

    # DM-03: no unknown top-level keys
    unknown = set(map_obj.keys()) - _TOP_LEVEL_ALLOWED
    for key in sorted(unknown):
        errors.append(f"DM-03: {key}: unknown top-level key (use 'extensions' namespace for adapter-specific data)")

    # DM-05 / DM-06 / DM-07: typed field checks per sub-object
    if "project" in map_obj:
        errors.extend(_check_project(map_obj["project"]))
    if "artifact_roots" in map_obj:
        errors.extend(_check_artifact_roots(map_obj["artifact_roots"]))
    if "memory" in map_obj:
        errors.extend(_check_memory(map_obj["memory"]))
    if "tasks" in map_obj:
        errors.extend(_check_tasks_container(map_obj["tasks"]))
    if "repos" in map_obj:
        errors.extend(_check_repos(map_obj["repos"]))
    if "dependency_hints" in map_obj:
        errors.extend(_check_dependency_hints(map_obj["dependency_hints"]))
    if "freshness" in map_obj:
        errors.extend(_check_freshness(map_obj["freshness"]))

    # DM-08: extensions structure
    if "extensions" in map_obj:
        errors.extend(_check_extensions(map_obj["extensions"]))

    # DM-09: path field style check
    errors.extend(_check_path_fields(map_obj))

    return errors


# ── File-level entry point ────────────────────────────────────────────────────

def validate_file(path: "str | Path") -> List[str]:
    """
    Read a JSON file and validate it. Returns list of error strings (empty = PASS).
    Raises SystemExit(2) on file-not-found or JSON parse error (including truncated files).
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            try:
                map_obj = json.load(fh)
            except json.JSONDecodeError as exc:
                print(f"ERROR: {path}: JSON parse error: {exc}", file=sys.stderr)
                sys.exit(2)
    except FileNotFoundError:
        print(f"ERROR: {path}: file not found", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"ERROR: {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    return validate(map_obj)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a quoin discovery-map JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  PASS — no errors found
  1  FAIL — at least one validation error
  2  ERROR — invocation error (file not found, JSON parse error)
        """,
    )
    parser.add_argument("map_json", metavar="<map.json>", help="Path to the discovery map JSON file.")
    parser.add_argument("--quiet", action="store_true", help="Suppress output on PASS.")
    parser.add_argument("--verbose", action="store_true", help="Print each check result.")
    args = parser.parse_args()

    errors = validate_file(args.map_json)

    if errors:
        for err in errors:
            print(err)
        return 1
    else:
        if not args.quiet:
            print(f"PASS: {args.map_json}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
