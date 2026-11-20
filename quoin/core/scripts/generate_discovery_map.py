#!/usr/bin/env python3
"""
generate_discovery_map.py — stdlib-only generator for quoin discovery maps.

No LLM calls. No network. Pure stdlib only.

Public API:
  generate_map(project_root, *, now_iso=None, runtime_adapters=None,
               project_name=None, project_description=None) -> dict
  write_map(map_obj, out_path) -> None
  main(argv=None) -> int

Populate order (human-readability; serialization uses sort_keys=True):
  project → artifact_roots → memory → tasks → repos → freshness →
  dependency_hints → extensions

mtime-derived fields (`last_updated`, `cache_updated_at`) are deterministic
given the filesystem state at invocation time. They are NOT stable across
re-runs separated by filesystem-side events such as cloud-storage sync
rewrites or concurrent file edits. Use `--generated-at` to pin the only
wall-clock side-channel.

DM-09 exception: repos[].path uses the sentinel value "." when the project
root itself is a git repository. All other path-bearing fields follow the
repo-relative no-leading-dot convention.

Exit codes (CLI):
  0 = PASS
  1 = VALIDATION FAIL
  2 = INVOCATION ERROR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Reserved task directory names ────────────────────────────────────────────

_RESERVED_TASK_NAMES: frozenset = frozenset({"finalized", "memory", "cache", "trash"})


# ── Git helpers ───────────────────────────────────────────────────────────────

def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy (D-06) — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _read_git_head(repo_dir: Path) -> str:
    """
    Return the 40-char git HEAD SHA for repo_dir.

    Strategy:
    1. Try `git rev-parse HEAD` (subprocess). Uses GIT_CEILING_DIRECTORIES and
       GIT_DIR to prevent git from walking up past repo_dir's parent, ensuring
       fixture shims are used in tests (D-08 / CI safety).
    2. Fall back to reading .git/HEAD and resolving ref: pointer — parses any
       branch name (not just 'main'), per D-08 and MIN-2 fix.
    3. Return 40 zeros on complete failure.
    """
    # Strategy 1: subprocess, scoped to repo_dir
    try:
        env = os.environ.copy()
        # Prevent git from walking up past repo_dir's parent
        env["GIT_CEILING_DIRECTORIES"] = str(repo_dir.parent)
        git_dir = repo_dir / ".git"
        if git_dir.exists():
            env["GIT_DIR"] = str(git_dir)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=_subprocess_timeout(),
            env=env,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Strategy 2: read .git/HEAD directly
    try:
        git_dir = repo_dir / ".git"
        head_file = git_dir / "HEAD"
        head_content = head_file.read_text(encoding="utf-8").strip()
        if head_content.startswith("ref: "):
            ref_path = head_content[len("ref: "):]
            # ref_path e.g. "refs/heads/master" or "refs/heads/main" — any branch
            ref_file = git_dir / ref_path
            sha = ref_file.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                return sha
        elif re.fullmatch(r"[0-9a-fA-F]{40}", head_content):
            return head_content
    except (OSError, ValueError):
        pass

    return "0" * 40


# ── mtime helpers ─────────────────────────────────────────────────────────────

def _mtime_iso(path: Path) -> str:
    """Return ISO 8601 UTC Z string for path's mtime."""
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_last_updated(task_root: Path) -> str:
    """
    Return max mtime across:
      - task_root itself
      - immediate children of task_root
      - items inside any stage-N/ subdir of task_root (depth-2 max)
    Per MAJ-6 fix.
    """
    paths_to_check: List[Path] = [task_root]
    try:
        for child in task_root.iterdir():
            paths_to_check.append(child)
            if child.is_dir() and re.fullmatch(r"stage-\d+", child.name):
                try:
                    for grandchild in child.iterdir():
                        paths_to_check.append(grandchild)
                except OSError:
                    pass
    except OSError:
        pass

    max_mtime = 0.0
    for p in paths_to_check:
        try:
            max_mtime = max(max_mtime, p.stat().st_mtime)
        except OSError:
            pass

    if max_mtime == 0.0:
        max_mtime = task_root.stat().st_mtime
    return datetime.fromtimestamp(max_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Adapter auto-detection ────────────────────────────────────────────────────

def _detect_runtime_adapters(project_root: Path) -> List[str]:
    """
    D-03: Heuristic adapter detection.
    - "claude" if CLAUDE.md at project root OR quoin/adapters/claude/ exists.
    - "codex" if AGENTS.md at project root OR quoin/adapters/codex/ exists.
    Result is sorted ascending.
    """
    adapters: List[str] = []
    if (project_root / "CLAUDE.md").exists() or (project_root / "quoin" / "adapters" / "claude").exists():
        adapters.append("claude")
    if (project_root / "AGENTS.md").exists() or (project_root / "quoin" / "adapters" / "codex").exists():
        adapters.append("codex")
    return sorted(adapters)


# ── Repo enumeration ──────────────────────────────────────────────────────────

def _has_git(path: Path) -> bool:
    """Return True if path contains .git (as dir or file pointer)."""
    git = path / ".git"
    return git.exists()


def _make_repo_entry(repo_dir: Path, project_root: Path) -> Dict[str, Any]:
    """Build a RepoSummary dict for repo_dir."""
    head_sha = _read_git_head(repo_dir)
    if repo_dir == project_root:
        # DM-09 exception: use "." sentinel for root-is-repo case
        rel_path = "."
        name = project_root.resolve().name
    else:
        rel_path = repo_dir.resolve().relative_to(project_root.resolve()).as_posix()
        name = repo_dir.name

    entry: Dict[str, Any] = {
        "head_sha": head_sha,
        "head_short": head_sha[:7],
        "name": name,
        "path": rel_path,
    }

    # cache_index_path: rooted at project_root cache (MAJ-3 fix)
    cache_index = project_root / ".workflow_artifacts" / "cache" / name / "_index.md"
    if cache_index.exists():
        entry["cache_index_path"] = f".workflow_artifacts/cache/{name}/_index.md"

    return entry


def _enumerate_repos(project_root: Path) -> List[Dict[str, Any]]:
    """
    Two-phase repo enumeration (CRIT-1 fix):
    Phase 1: check project_root/.git — if present, include project root as a repo.
    Phase 2: enumerate depth-1 and depth-2 children with .git/.

    De-duplicates by resolved absolute path. Returns sorted by name ascending.
    """
    seen_paths: set = set()
    repos: List[Dict[str, Any]] = []

    def _add_repo(repo_dir: Path) -> None:
        resolved = repo_dir.resolve()
        if resolved in seen_paths:
            return
        seen_paths.add(resolved)
        repos.append(_make_repo_entry(repo_dir, project_root))

    # Phase 1: project root itself
    if _has_git(project_root):
        _add_repo(project_root)

    # Phase 2: depth-1 children
    try:
        depth1_dirs = sorted(project_root.iterdir())
    except OSError:
        depth1_dirs = []

    for child in depth1_dirs:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if _has_git(child):
            _add_repo(child)
        else:
            # depth-2 children (for nested-repo layouts like quoin/.git at depth 2)
            try:
                depth2_dirs = sorted(child.iterdir())
            except OSError:
                depth2_dirs = []
            for grandchild in depth2_dirs:
                if not grandchild.is_dir():
                    continue
                if grandchild.name.startswith("."):
                    continue
                if _has_git(grandchild):
                    _add_repo(grandchild)

    repos.sort(key=lambda r: r["name"])
    return repos


# ── Task enumeration ──────────────────────────────────────────────────────────

def _has_stage_decomposition(architecture_path: Path) -> bool:
    """Return True if architecture.md contains '## Stage decomposition' (MAJ-1 fix)."""
    try:
        content = architecture_path.read_text(encoding="utf-8")
        return "## Stage decomposition" in content
    except OSError:
        return False


def _enumerate_stages_for_task(task_root: Path, project_root: Path) -> Optional[List[Dict[str, Any]]]:
    """Enumerate stages, returning None if not applicable (MAJ-1 fix)."""
    architecture_path = task_root / "architecture.md"
    if not architecture_path.exists():
        return None
    if not _has_stage_decomposition(architecture_path):
        return None

    stages: List[Dict[str, Any]] = []
    try:
        for child in task_root.iterdir():
            m = re.fullmatch(r"stage-(\d+)", child.name)
            if m and child.is_dir():
                stage_id = int(m.group(1))
                try:
                    stage_path = child.resolve().relative_to(project_root.resolve()).as_posix()
                except ValueError:
                    stage_path = child.name
                stages.append({
                    "id": stage_id,
                    "path": stage_path,
                    "status": "pending",  # D-05: default pending
                })
    except OSError:
        pass

    stages.sort(key=lambda s: s["id"])
    return stages if stages else None


def _build_task_summary(
    task_dir: Path,
    status: str,
    project_root: Path,
) -> Dict[str, Any]:
    """Build a TaskSummary dict for task_dir."""
    try:
        rel_path = task_dir.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        rel_path = task_dir.name

    entry: Dict[str, Any] = {
        "last_updated": _task_last_updated(task_dir),
        "name": task_dir.name,
        "path": rel_path,
        "status": status,
    }

    arch_file = task_dir / "architecture.md"
    if arch_file.exists():
        try:
            entry["architecture_path"] = arch_file.resolve().relative_to(
                project_root.resolve()
            ).as_posix()
        except ValueError:
            pass

    plan_file = task_dir / "current-plan.md"
    if plan_file.exists():
        try:
            entry["current_plan_path"] = plan_file.resolve().relative_to(
                project_root.resolve()
            ).as_posix()
        except ValueError:
            pass

    # stages — MAJ-1: only when architecture.md has ## Stage decomposition
    stages = _enumerate_stages_for_task(task_dir, project_root)
    if stages is not None:
        entry["stages"] = stages

    return entry


def _enumerate_tasks(project_root: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Enumerate active and finalized tasks."""
    wa = project_root / ".workflow_artifacts"
    active: List[Dict[str, Any]] = []
    finalized: List[Dict[str, Any]] = []

    # Active tasks: immediate children of .workflow_artifacts/ not in reserved set,
    # not hidden, and containing current-plan.md or architecture.md (depth ≤ 2)
    if wa.is_dir():
        try:
            candidates = sorted(wa.iterdir())
        except OSError:
            candidates = []

        for child in candidates:
            if not child.is_dir():
                continue
            if child.name in _RESERVED_TASK_NAMES:
                continue
            if child.name.startswith("."):
                continue
            # Check for current-plan.md or architecture.md at depth ≤ 2
            has_plan = (child / "current-plan.md").exists()
            has_arch = (child / "architecture.md").exists()
            if not has_plan and not has_arch:
                # Check depth 2 (inside stage-N subdirs)
                try:
                    for subchild in child.iterdir():
                        if (subchild / "current-plan.md").exists() or (subchild / "architecture.md").exists():
                            has_plan = True
                            break
                except OSError:
                    pass
            if has_plan or has_arch:
                active.append(_build_task_summary(child, "active", project_root))

        active.sort(key=lambda t: t["name"])

        # Finalized tasks: immediate children of .workflow_artifacts/finalized/
        finalized_dir = wa / "finalized"
        if finalized_dir.is_dir():
            try:
                fin_candidates = sorted(finalized_dir.iterdir())
            except OSError:
                fin_candidates = []
            for child in fin_candidates:
                if not child.is_dir():
                    continue
                if child.name.startswith("."):
                    continue
                finalized.append(_build_task_summary(child, "finalized", project_root))
            finalized.sort(key=lambda t: t["name"])

    return {"active": active, "finalized": finalized}


# ── Core generator ────────────────────────────────────────────────────────────

def generate_map(
    project_root: Path,
    *,
    now_iso: Optional[str] = None,
    runtime_adapters: Optional[List[str]] = None,
    project_name: Optional[str] = None,
    project_description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a discovery map dict matching discovery-map.schema.json v1.

    Parameters:
      project_root: absolute Path to the project root directory.
      now_iso: override for generated_at (ISO 8601 string); if None, uses
               datetime.now(timezone.utc).
      runtime_adapters: override adapter list; if None, auto-detects (D-03).
      project_name: override project name; if None, uses basename of project_root.
      project_description: optional description; if None, field is OMITTED.

    Returns a fully populated dict. Pure — only reads filesystem under project_root.
    """
    project_root = project_root.resolve()

    if now_iso is None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if project_name is None:
        project_name = project_root.name
    if runtime_adapters is None:
        runtime_adapters = _detect_runtime_adapters(project_root)
    else:
        runtime_adapters = sorted(runtime_adapters)

    project_obj: Dict[str, Any] = {
        "name": project_name,
        "root_path": str(project_root),
        "runtime_adapters": runtime_adapters,
    }
    if project_description is not None:
        project_obj["description"] = project_description

    artifact_roots: Dict[str, Any] = {
        "finalized_path": ".workflow_artifacts/finalized",
        "memory_path": ".workflow_artifacts/memory",
        "tasks_path": ".workflow_artifacts",
        "workflow_artifacts_path": ".workflow_artifacts",
    }
    cache_dir = project_root / ".workflow_artifacts" / "cache"
    if cache_dir.is_dir():
        artifact_roots["cache_path"] = ".workflow_artifacts/cache"

    memory: Dict[str, Any] = {
        "daily_dir": ".workflow_artifacts/memory/daily",
        "lessons_learned": ".workflow_artifacts/memory/lessons-learned.md",
        "sessions_dir": ".workflow_artifacts/memory/sessions",
        "weekly_dir": ".workflow_artifacts/memory/weekly",
    }

    memory_md = project_root / ".workflow_artifacts" / "memory" / "MEMORY.md"
    if memory_md.exists():
        memory["memory_md_index"] = ".workflow_artifacts/memory/MEMORY.md"

    staleness = project_root / ".workflow_artifacts" / "cache" / "_staleness.md"
    if staleness.exists():
        memory["staleness_path"] = ".workflow_artifacts/cache/_staleness.md"

    repo_heads = project_root / ".workflow_artifacts" / "memory" / "repo-heads.md"
    if repo_heads.exists():
        memory["repo_heads_path"] = ".workflow_artifacts/memory/repo-heads.md"

    tasks = _enumerate_tasks(project_root)

    repos = _enumerate_repos(project_root)

    # freshness — per repo, rooted at project_root cache (MAJ-3 fix)
    freshness: Dict[str, Any] = {}
    for repo in repos:
        repo_name = repo["name"]
        freshn_entry: Dict[str, Any] = {
            "head_sha": repo["head_sha"],
            "recorded_at": now_iso,
        }
        cache_index = (
            project_root / ".workflow_artifacts" / "cache" / repo_name / "_index.md"
        )
        if cache_index.exists():
            freshn_entry["cache_updated_at"] = _mtime_iso(cache_index)
        freshness[repo_name] = freshn_entry

    # Build final map (populate order per module docstring)
    map_obj: Dict[str, Any] = {
        "artifact_roots": artifact_roots,
        "generated_at": now_iso,
        "memory": memory,
        "project": project_obj,
        "repos": repos,
        "schema_version": 1,
        "tasks": tasks,
    }
    if freshness:
        map_obj["freshness"] = freshness
    # dependency_hints: OMITTED in v1 (D-06)
    # extensions: OMITTED in v1

    return map_obj


# ── Write helper ──────────────────────────────────────────────────────────────

def write_map(map_obj: Dict[str, Any], out_path: Path) -> None:
    """
    Write map_obj to out_path as JSON with sort_keys=True, indent=2,
    ensure_ascii=False, trailing newline.

    Uses atomic-rename pattern: write to <out_path>.tmp, fsync, os.replace.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    content = json.dumps(map_obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, out_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    """
    CLI entry point.

    Resolution ordering (CRIT-2 fix):
    1. Resolve project_root to absolute FIRST.
    2. Default --output computed AFTER resolving project_root.
    3. Relative --output is resolved against cwd, NOT project_root.
    """
    parser = argparse.ArgumentParser(
        description="Generate a quoin discovery-map JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  PASS — map generated successfully
  1  VALIDATION FAIL — map failed validate_discovery_map checks
  2  ERROR — invocation error
        """,
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        metavar="project_root",
        help="Path to the project root (default: current directory).",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--output", "-o",
        metavar="<path>",
        help="Output path for the discovery map JSON. Default: <project_root>/.workflow_artifacts/discovery-map.json",
    )
    output_group.add_argument(
        "--stdout",
        action="store_true",
        help="Write JSON to stdout instead of a file.",
    )
    parser.add_argument(
        "--generated-at",
        metavar="<iso8601>",
        help="Override the generated_at field (for testing determinism).",
    )
    parser.add_argument(
        "--project-name",
        metavar="<name>",
        help="Override the auto-derived project name.",
    )
    parser.add_argument(
        "--project-description",
        metavar="<text>",
        help="Set optional project.description.",
    )
    parser.add_argument(
        "--runtime-adapter",
        metavar="<name>",
        action="append",
        dest="runtime_adapters",
        help="Override adapter auto-detection (repeatable). If supplied, auto-detect is skipped.",
    )
    validate_group = parser.add_mutually_exclusive_group()
    validate_group.add_argument(
        "--validate",
        action="store_true",
        default=True,
        help="Validate generated map before writing (default).",
    )
    validate_group.add_argument(
        "--no-validate",
        action="store_false",
        dest="validate",
        help="Skip validation.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout banner on success.",
    )

    args = parser.parse_args(argv)

    # Step 1: resolve project_root FIRST (CRIT-2 fix)
    raw_root = args.project_root if args.project_root is not None else os.getcwd()
    project_root = Path(raw_root).resolve()

    if not project_root.is_dir():
        print(f"ERROR: project_root is not a directory: {project_root}", file=sys.stderr)
        return 2

    # Step 2: compute default output AFTER resolving project_root
    if args.output is not None:
        out_path = Path(args.output).resolve()  # relative to cwd, not project_root
    else:
        out_path = project_root / ".workflow_artifacts" / "discovery-map.json"

    try:
        map_obj = generate_map(
            project_root,
            now_iso=args.generated_at,
            runtime_adapters=args.runtime_adapters,
            project_name=args.project_name,
            project_description=args.project_description,
        )
    except Exception as exc:
        print(f"ERROR: generation failed: {exc}", file=sys.stderr)
        return 2

    # Validate (MAJ-2 fix: Path(__file__).parent lazy-load)
    if args.validate:
        validator_path = Path(__file__).parent / "validate_discovery_map.py"
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("_quoin_validator", validator_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load validator from {validator_path}")
            _validator_mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(_validator_mod)
            errors = _validator_mod.validate(map_obj)
        except Exception as exc:
            print(f"ERROR: could not load validator: {exc}", file=sys.stderr)
            return 2

        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1

    if args.stdout:
        content = json.dumps(map_obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        sys.stdout.write(content)
    else:
        try:
            write_map(map_obj, out_path)
        except OSError as exc:
            print(f"ERROR: could not write {out_path}: {exc}", file=sys.stderr)
            return 2
        if not args.quiet:
            print(f"PASS: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
