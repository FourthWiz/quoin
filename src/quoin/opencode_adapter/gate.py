"""The deterministic workflow gate.

Given a task, an optional stage and a gated phase, `evaluate` runs a fixed list
of checks over the recorded evidence, the run records, the phase's artifacts
and the current tree, and returns a verdict. Nothing a model wrote in prose can
change it: the optional explanation is carried on the result and is never given
to a check. The module reads files and runs the artifact validator as a
subprocess; it never starts a model and never asks for approval.

Part one (this top half): loading the shared core scripts, resolving paths,
validating artifacts and parsing verdicts. Part two: `evaluate`. Part three:
the audit-file writer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import errors, evidence, runstore

VALIDATOR_TIMEOUT_S = 30.0
MAX_DETAIL_BYTES = 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_EXPLANATION_BYTES = 8 * 1024
DEFAULT_MAX_CRITIC_ROUNDS = 5
CRITIC_VERDICTS = ("PASS", "REVISE")
REVIEW_VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "BLOCKED")

REASON_CODES = (
    "artifact-added", "artifact-invalid", "artifact-missing", "artifact-removed", "boundary-unrecorded",
    "boundary-violation", "continuation-not-validated", "critic-missing", "critic-not-converged",
    "envelope-invalid", "evidence-incomplete", "evidence-missing", "input-hash-changed", "path-unresolved",
    "repo-added", "repo-content-changed", "repo-content-unverifiable", "repo-dirty-changed",
    "repo-head-changed", "repo-missing", "review-not-approved", "run-evidence-partial",
    "run-not-completed", "state-invalid", "tests-failed", "verdict-unparseable",
)
WARNING_CODES = (
    "boundary-unverified", "critic-not-run", "ledger-appended-during-run", "run-evidence-absent",
    "tests-not-configured",
)


def _redact(text: str) -> str:
    """The same masking `launch_env.Redactor()` applies with no registered
    values; kept local so this module does not import the launcher."""
    return errors.SECRET_SHAPE_RE.sub("<redacted>", text)


class GateRefused(Exception):
    """The request cannot be evaluated at all; `code` is a stable identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PathUnresolved(Exception):
    """A stage or task path could not be resolved safely."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# core scripts
# ---------------------------------------------------------------------------

_CORE_CACHE: Dict[str, Any] = {}


def load_core(source_dir, name: str):
    """A pure-function core script loaded by file path, cached per path."""
    path = Path(source_dir) / "core" / "scripts" / (name + ".py")
    key = str(path.resolve())
    module = _CORE_CACHE.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location("_quoin_core_" + name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(name)
        module = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old
        _CORE_CACHE[key] = module
    return module


def _first_lines(text: str, limit: int = MAX_DETAIL_BYTES) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("FAIL")] or [ln for ln in text.splitlines() if ln.strip()]
    out = "; ".join(lines)
    return _redact(out.encode("utf-8")[:limit].decode("utf-8", "ignore"))


def run_validator(project_root, source_dir, path) -> Optional[str]:
    """None when the artifact passes strict validation, else a short detail:
    the first failing invariants, or `validator-unavailable` when the
    validator itself could not give a verdict (fail closed)."""
    script = Path(source_dir) / "core" / "scripts" / "validate_artifact.py"
    sidecar = Path(source_dir) / "memory" / "format-kit.sections.json"
    if not script.is_file() or not sidecar.is_file():
        return "validator-unavailable"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--sections-json", str(sidecar), "--quiet", str(path)],
            env=env, cwd=str(project_root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=VALIDATOR_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "validator-unavailable"
    if proc.returncode == 0:
        return None
    if proc.returncode == 1:
        return _first_lines(proc.stderr.decode("utf-8", "replace")) or "validation failed"
    return "validator-unavailable"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def _lstat(path: Path):
    try:
        return os.lstat(str(path))
    except OSError:
        return None


def _safe_join(project_root, *parts: str) -> Path:
    """`project_root/parts...`, refusing a symlink at any component below the
    project root. Components that do not exist yet are allowed."""
    current = Path(project_root)
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise PathUnresolved("a symlink at %s" % "/".join(parts[: index + 1]))
    return current


def task_root(project_root, task: str) -> Path:
    return _safe_join(project_root, ".workflow_artifacts", task)


def stage_dir(project_root, task: str, stage: Optional[int], source_dir) -> Path:
    """The directory holding a stage's artifacts: the task root without a
    stage, else `stage-N/` once the task's architecture lists stage N under
    its stage decomposition (the resolver's own row grammar)."""
    root = task_root(project_root, task)
    if stage is None:
        return root
    arch = root / "architecture.md"
    info = _lstat(arch)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise PathUnresolved("architecture.md is missing at the task root")
    resolver = load_core(source_dir, "path_resolve")
    text = _read_text(arch, MAX_TEXT_BYTES)
    if text is None:
        raise PathUnresolved("architecture.md cannot be read")
    match = resolver.SECTION_RE.search(text)
    if not match:
        raise PathUnresolved("architecture.md has no stage decomposition section")
    following = resolver.NEXT_H2_RE.search(text, match.end())
    body = text[match.end(): following.start() if following else len(text)]
    if not any(int(m.group(1)) == stage for m in resolver.ROW_RE.finditer(body)):
        raise PathUnresolved("stage %d is not listed in the stage decomposition" % stage)
    folder = _safe_join(project_root, ".workflow_artifacts", task, "stage-%d" % stage)
    if resolver.task_path(task, stage, str(Path(project_root).resolve())).name != folder.name:
        raise PathUnresolved("the stage folder name disagrees with the resolver")
    return folder


def _read_text(path: Path, limit: int) -> Optional[str]:
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _numbered(directory: Path, pattern: "re.Pattern[str]") -> List[Tuple[int, Path]]:
    found: List[Tuple[int, Path]] = []
    try:
        names = os.listdir(str(directory))
    except OSError:
        return found
    for name in names:
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1)), directory / name))
    return sorted(found)


_REVIEW_RE = re.compile(r"^review-(\d+)\.md$")
_CRITIC_RE = re.compile(r"^critic-response-(\d+)\.md$")


def expected_artifacts(
    project_root, task: str, stage: Optional[int], phase: str, entry: Mapping[str, Any], sdir: Optional[Path],
) -> Tuple[List[Path], List[str]]:
    """`(paths, missing)`: the files the phase must have produced and the
    names of those that are absent."""
    root = Path(project_root)
    paths: List[Path] = []
    missing: List[str] = []
    wanted: List[Path] = []
    if phase == "discover":
        wanted = [root / rel for rel in evidence.DISCOVER_FILES]
    elif phase == "architect":
        wanted = [task_root(project_root, task) / "architecture.md"]
    elif phase == "plan" and sdir is not None:
        wanted = [sdir / "current-plan.md"]
    elif phase == "review" and sdir is not None:
        chosen: Optional[Path] = None
        for item in entry.get("harvested") or []:
            candidate = item.get("path") if isinstance(item, Mapping) else None
            if isinstance(candidate, str) and _REVIEW_RE.match(os.path.basename(candidate)):
                chosen = Path(candidate) if os.path.isabs(candidate) else root / candidate
                break
        if chosen is None:
            reviews = _numbered(sdir, _REVIEW_RE)
            chosen = reviews[-1][1] if reviews else None
        if chosen is None:
            return [], ["review-N.md"]
        wanted = [chosen]
    for path in wanted:
        info = _lstat(path)
        if info is not None and stat.S_ISREG(info.st_mode):
            paths.append(path)
        else:
            missing.append(_rel(root, path))
    return paths, missing


def _rel(root: Path, path: Path) -> str:
    try:
        return os.path.relpath(str(path), str(root)).replace(os.sep, "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"`<verdict>([^<>`]*)</verdict>`")
_HEADING_VERDICT_RE = re.compile(r"^## Verdict:\s*(\S+)\s*$")


def _visible_lines(text: str) -> List[str]:
    lines: List[str] = []
    in_fence = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return lines


def parse_verdict(text: str, allowed: Sequence[str]) -> Optional[str]:
    """The verdict an artifact states, or None when it does not state exactly
    one allowed value. Three strict forms, tried in order, all ignoring fenced
    code: a `<verdict>` tag inside the `## Verdict` section; a `## Verdict: X`
    heading line; a `## Verdict` section whose only non-blank line is the value."""
    lines = _visible_lines(text)
    body: List[str] = []
    found = False
    for line in lines:
        if not found:
            found = line.rstrip() == "## Verdict"
            continue
        if line.startswith("## "):
            break
        body.append(line)
    if found:
        tags = {m.group(1) for m in _TAG_RE.finditer("\n".join(body))}
        if len(tags) == 1:
            (only,) = tags
            if only in allowed:
                return only
    values = set()
    for line in lines:
        match = _HEADING_VERDICT_RE.match(line)
        if match:
            values.add(match.group(1))
    if len(values) == 1:
        (only,) = values
        if only in allowed:
            return only
    if found:
        meaningful = [ln.strip() for ln in body if ln.strip()]
        if len(meaningful) == 1 and meaningful[0] in allowed:
            return meaningful[0]
    return None


def _resolve_recorded(project_root, value: str) -> Path:
    return Path(value) if os.path.isabs(value) else Path(project_root) / value


def critic_status(
    entry: Mapping[str, Any], origin: str, sdir: Path, settings: Mapping[str, Any], *, project_root=None,
) -> List[Tuple[str, str, str]]:
    """`(status, code, detail)` items for the plan's critic loop.

    A coordinator entry trusts only the responses it recorded; any other origin
    falls back to the responses on disk. No response is a refusal unless the
    settings say a critic is not required or the plan was adopted, which warns."""
    recorded = [r for r in (entry.get("critic_responses") or []) if isinstance(r, str)]
    if origin == "coordinator":
        used = [_resolve_recorded(project_root or sdir, r) for r in recorded]
    elif recorded:
        used = [_resolve_recorded(project_root or sdir, r) for r in recorded]
    else:
        used = [path for _n, path in _numbered(sdir, _CRITIC_RE)]
    if not used:
        if settings.get("critic_required") is False or origin == "adopted":
            return [("WARN", "critic-not-run", "no critic response was recorded")]
        return [("FAIL", "critic-missing", "no critic response was recorded for this plan")]
    out: List[Tuple[str, str, str]] = []
    cap = settings.get("max_critic_rounds")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else DEFAULT_MAX_CRITIC_ROUNDS
    text = _read_text(used[-1], MAX_TEXT_BYTES)
    if text is None:
        out.append(("FAIL", "artifact-missing", _rel(Path(project_root or sdir), used[-1])))
    else:
        verdict = parse_verdict(text, CRITIC_VERDICTS)
        if verdict is None:
            out.append(("FAIL", "verdict-unparseable", used[-1].name))
        elif verdict == "REVISE":
            out.append(("FAIL", "critic-not-converged", "the last critic response asks for a revision"))
    if len(used) > cap:
        out.append(("FAIL", "critic-not-converged", "%d critic rounds exceed the cap of %d" % (len(used), cap)))
    return out or [("PASS", "", "")]
