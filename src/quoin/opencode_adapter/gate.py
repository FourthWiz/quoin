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


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

CHECK_STATUSES = ("PASS", "FAIL", "WARN", "SKIP")
Item = Tuple[str, str, str]  # (status, code, detail)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    codes: Tuple[str, ...] = ()
    details: Tuple[str, ...] = ()
    warn_codes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "status": self.status, "codes": list(self.codes),
            "warn_codes": list(self.warn_codes), "details": list(self.details),
        }


@dataclass(frozen=True)
class GateResult:
    task: str
    stage: Optional[int]
    phase: str
    verdict: str
    checks: Tuple[Check, ...]
    reasons: Tuple[str, ...]
    warnings: Tuple[str, ...]
    origin: Optional[str]
    evidence_ref: Optional[Mapping[str, Any]]
    explanation: Optional[str]
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task, "stage": self.stage, "phase": self.phase, "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks], "reasons": list(self.reasons),
            "warnings": list(self.warnings), "origin": self.origin,
            "evidence_ref": dict(self.evidence_ref) if self.evidence_ref else None,
            "explanation": self.explanation, "evaluated_at": self.evaluated_at,
        }


def _check(name: str, items: Sequence[Item]) -> Check:
    """Fold a check's items into one result: any failure fails it, else any
    warning warns it, else it passes."""
    fails = [i for i in items if i[0] == "FAIL"]
    warns = [i for i in items if i[0] == "WARN"]
    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    return Check(
        name, status,
        tuple(sorted({c for _s, c, _d in fails})),
        tuple(d for _s, _c, d in fails + warns if d),
        tuple(sorted({c for _s, c, _d in warns})),
    )


def _skip(name: str, why: str) -> Check:
    return Check(name, "SKIP", (), (why,))


def _fail(code: str, detail: str = "") -> Item:
    return ("FAIL", code, detail)


def adopt_command(task: str, stage: Optional[int], phase: str, project_root) -> str:
    stage_part = "" if stage is None else " --stage %d" % stage
    return "quoin opencode adopt --task %s%s --phase %s --project-root %s" % (task, stage_part, phase, project_root)


def _now_iso(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def _settings(state: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    value = (state or {}).get("settings")
    return value if isinstance(value, Mapping) else {}


def _check_evidence(task, stage, phase, project_root, state_error, entry) -> Check:
    if state_error is not None:
        return _check("evidence", [_fail("state-invalid", state_error)])
    if entry is None:
        return _check("evidence", [_fail("evidence-missing", adopt_command(task, stage, phase, project_root))])
    snapshot = entry.get("evidence")
    if not evidence.well_formed(snapshot) or snapshot.get("coverage") != "full":
        return _check("evidence", [_fail("evidence-incomplete", "the recorded evidence does not cover every file")])
    return _check("evidence", [("PASS", "", "")])


def _check_run_outcome(task, phase, entry, directory) -> Check:
    origin = entry["origin"]
    if origin == "adopted":
        return _check("run-outcome", [("WARN", "run-evidence-absent", "adopted evidence has no recorded run")])
    if origin == "continuation":
        if entry.get("continuation_validation") != "PASS":
            return _check("run-outcome", [_fail("continuation-not-validated", "the continuation was not validated")])
        return _check("run-outcome", [("PASS", "", "")])
    runs = [r for r in (entry.get("runs") or []) if isinstance(r, str)]
    if not runs:
        return _check("run-outcome", [_fail("run-not-completed", "the entry lists no run")])
    items: List[Item] = []
    producers = 0
    for run_id in runs:
        try:
            record = runstore.load_record(directory, run_id) if directory is not None else None
        except runstore.RunStoreError as exc:
            items.append(_fail("run-not-completed", "%s: %s" % (run_id, exc.code)))
            continue
        why = _mismatch(record, task, entry.get("stage"), phase)
        if why is not None:
            items.append(_fail("run-not-completed", "%s: %s" % (run_id, why)))
            continue
        if runstore.normalize_phase(record["request"]["phase"]) in runstore.PLAN_PRODUCER_PHASES:
            producers += 1
        outcome = record.get("outcome") if isinstance(record.get("outcome"), Mapping) else {}
        if outcome.get("state") != "completed":
            items.append(_fail("run-not-completed", "%s: outcome is %s" % (run_id, outcome.get("state"))))
        elif outcome.get("evidence") != "full":
            items.append(_fail("run-evidence-partial", "%s: evidence is %s" % (run_id, outcome.get("evidence"))))
    if phase == "plan" and producers == 0:
        items.append(_fail("run-not-completed", "no plan or thorough_plan run is listed"))
    return _check("run-outcome", items or [("PASS", "", "")])


def _mismatch(record: Optional[Mapping[str, Any]], task: str, stage: Optional[int], phase: str) -> Optional[str]:
    """Why a run record is not a run of this entry, or None when it is."""
    if record is None:
        return "no run record"
    request = record.get("request")
    if record.get("task") != task or not isinstance(request, Mapping):
        return "task differs"
    try:
        if runstore.normalize_stage(request.get("stage")) != stage:
            return "stage differs"
    except ValueError:
        return "stage differs"
    if runstore.normalize_phase(request.get("phase")) not in runstore.RUN_PHASES_FOR[phase]:
        return "phase differs"
    return None


def _check_boundary(entry) -> Check:
    origin, boundary = entry["origin"], entry.get("boundary")
    if boundary == "violation":
        return _check("boundary", [_fail("boundary-violation", "a run touched a path it may not")])
    if boundary == "ok":
        return _check("boundary", [("PASS", "", "")])
    if origin == "coordinator":
        return _check("boundary", [_fail("boundary-unrecorded", "no boundary result was recorded")])
    return _check("boundary", [("WARN", "boundary-unverified", "no boundary result was recorded")])



def precheck(project_root, task: str, stage: Any, phase: str) -> Tuple[Optional[int], str]:
    """The normalized `(stage, phase)`, or `GateRefused` when the request cannot
    name a gated phase of an existing task. Phases accept the hyphen spelling
    the run path accepts, since both share one normalization rule."""
    phase = runstore.normalize_phase(phase)
    try:
        runstore.check_task_name(task)
    except runstore.RunStoreError:
        raise GateRefused("invalid-task-name") from None
    if phase not in runstore.GATED_PHASES:
        raise GateRefused("phase-not-gated")
    info = _lstat(Path(project_root) / ".workflow_artifacts" / task)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise GateRefused("task-missing")
    try:
        stage = runstore.normalize_stage(stage)
    except ValueError:
        raise GateRefused("invalid-stage") from None
    if stage is not None and phase in runstore.STAGELESS_PHASES:
        raise GateRefused("invalid-stage")
    return stage, phase


def evaluate(
    project_root, task: str, stage: Optional[int], phase: str, *, source_dir,
    explanation: Optional[str] = None,
    runner: Optional[runstore.GitRunner] = None,
    bytes_runner: Optional[runstore.GitBytesRunner] = None,
    clock: Callable[[], float] = time.time,
) -> GateResult:
    """Run every check for one gated phase and return the verdict.

    `explanation` is stored on the result after redaction and truncation; it is
    never passed to a check, so it cannot change the verdict."""
    stage, phase = precheck(project_root, task, stage, phase)

    state_error: Optional[str] = None
    state: Optional[Dict[str, Any]] = None
    directory: Optional[Path] = None
    try:
        directory = runstore.inspect_store(project_root)
        if directory is not None:
            state = runstore.load_workflow_state(directory, task)
    except runstore.RunStoreError as exc:
        state_error = exc.code
    except OSError as exc:
        state_error = type(exc).__name__
    entry = runstore.current_entry(state, stage, phase) if state is not None else None
    settings = _settings(state)

    checks: List[Check] = []
    checks.append(_check_evidence(task, stage, phase, project_root, state_error, entry))
    have = entry is not None and state_error is None
    snapshot = entry.get("evidence") if have else None

    checks.append(_check_run_outcome(task, phase, entry, directory) if have else _skip("run-outcome", "no evidence entry"))
    checks.append(_check_boundary(entry) if have else _skip("boundary", "no evidence entry"))

    sdir: Optional[Path] = None
    path_error: Optional[str] = None
    try:
        sdir = stage_dir(project_root, task, stage, source_dir)
    except PathUnresolved as exc:
        path_error = exc.detail
    paths: List[Path] = []
    if have and path_error is None:
        paths, missing = expected_artifacts(project_root, task, stage, phase, entry, sdir)
        exist_items: List[Item] = [_fail("artifact-missing", m) for m in missing]
        checks.append(_check("artifacts-exist", exist_items or ([("PASS", "", "")] if paths else [])) if (paths or missing)
                      else _skip("artifacts-exist", "this phase has no required artifact"))
    elif have:
        checks.append(_check("artifacts-exist", [_fail("path-unresolved", path_error or "")]))
    else:
        checks.append(_skip("artifacts-exist", "no evidence entry"))

    if paths:
        invalid: List[Item] = []
        for path in paths:
            detail = run_validator(project_root, source_dir, path)
            if detail is not None:
                invalid.append(_fail("artifact-invalid", "%s: %s" % (_rel(Path(project_root), path), detail)))
        checks.append(_check("artifacts-valid", invalid or [("PASS", "", "")]))
    else:
        checks.append(_skip("artifacts-valid", "no artifact to validate"))

    if have:
        fresh = evidence.take_snapshot(project_root, task, phase, runner=runner, bytes_runner=bytes_runner, clock=clock)
        task_findings, repo_findings = evidence.compare(snapshot, fresh)
        checks.append(_check("artifact-hashes", [_fail(f.code, f.detail) for f in task_findings] or [("PASS", "", "")]))
        checks.append(_check("repo-state", [_fail(f.code, f.detail) for f in repo_findings] or [("PASS", "", "")]))
    else:
        checks.append(_skip("artifact-hashes", "no evidence entry"))
        checks.append(_skip("repo-state", "no evidence entry"))

    checks.append(_check_envelope(project_root, source_dir, entry) if have else _skip("envelope", "no evidence entry"))
    checks.append(_check_verdict(project_root, phase, entry, origin_of(entry), sdir, settings, paths) if have
                  else _skip("phase-verdict", "no evidence entry"))
    checks.append(_check_tests(phase, entry, settings) if have else _skip("tests", "no evidence entry"))
    checks.append(_check_ledger(entry) if have else _skip("ledger", "no evidence entry"))

    reasons = tuple(sorted({c for ch in checks if ch.status == "FAIL" for c in ch.codes}))
    warnings = tuple(sorted({c for ch in checks for c in ch.warn_codes}))
    verdict = "FAIL" if any(ch.status == "FAIL" for ch in checks) else "PASS"
    shown = None
    if explanation is not None:
        shown = _redact(explanation).encode("utf-8")[:MAX_EXPLANATION_BYTES].decode("utf-8", "ignore")
    ref = None
    if have:
        ref = {"recorded_at": entry.get("recorded_at"), "taken_at": (snapshot or {}).get("taken_at")}
    return GateResult(
        task=task, stage=stage, phase=phase, verdict=verdict, checks=tuple(checks), reasons=reasons,
        warnings=warnings, origin=entry.get("origin") if have else None, evidence_ref=ref,
        explanation=shown, evaluated_at=_now_iso(clock),
    )


def origin_of(entry: Optional[Mapping[str, Any]]) -> Optional[str]:
    return entry.get("origin") if entry else None


def _check_envelope(project_root, source_dir, entry) -> Check:
    recorded = entry.get("envelope_path")
    if not recorded:
        return _skip("envelope", "no envelope was recorded")
    path = _resolve_recorded(project_root, recorded)
    text = _read_text(path, MAX_ENVELOPE_BYTES)
    if text is None:
        return _check("envelope", [_fail("envelope-invalid", "the envelope file cannot be read")])
    try:
        messages = load_core(source_dir, "handoff_validate").validate(text, "return")
    except Exception:  # noqa: BLE001 - a validator that cannot run is a refusal
        return _check("envelope", [_fail("envelope-invalid", "validator-unavailable")])
    failures = [m for m in messages if m.startswith("FAIL")]
    if failures:
        return _check("envelope", [_fail("envelope-invalid", _redact(failures[0])[:300])])
    return _check("envelope", [("PASS", "", "")])


def _check_verdict(project_root, phase, entry, origin, sdir, settings, paths) -> Check:
    if phase == "plan":
        if sdir is None:
            return _skip("phase-verdict", "the stage folder is unresolved")
        items = critic_status(entry, origin or "", sdir, settings, project_root=project_root)
        return _check("phase-verdict", [(s, c, d) for s, c, d in items])
    if phase == "review":
        if not paths:
            return _skip("phase-verdict", "no review file")
        text = _read_text(paths[0], MAX_TEXT_BYTES)
        verdict = parse_verdict(text, REVIEW_VERDICTS) if text is not None else None
        if verdict is None:
            return _check("phase-verdict", [_fail("verdict-unparseable", paths[0].name)])
        if verdict != "APPROVED":
            return _check("phase-verdict", [_fail("review-not-approved", "the review verdict is %s" % verdict)])
        return _check("phase-verdict", [("PASS", "", "")])
    return _skip("phase-verdict", "this phase has no verdict file")


def _check_tests(phase, entry, settings) -> Check:
    if phase not in ("implement", "review"):
        return _skip("tests", "this phase does not run tests")
    if not settings.get("test_command"):
        return _check("tests", [("WARN", "tests-not-configured", "no test command is configured")])
    tests = entry.get("tests")
    code = tests.get("exit_code") if isinstance(tests, Mapping) else None
    if not isinstance(code, int) or isinstance(code, bool) or code != 0:
        return _check("tests", [_fail("tests-failed", "the recorded test run did not exit 0")])
    return _check("tests", [("PASS", "", "")])


def _check_ledger(entry) -> Check:
    if entry.get("ledger_lines_appended_during_run"):
        return _check("ledger", [("WARN", "ledger-appended-during-run", "the ledger grew while the run was active")])
    return _check("ledger", [("PASS", "", "")])


# ---------------------------------------------------------------------------
# audit file
# ---------------------------------------------------------------------------

import secrets as _secrets  # noqa: E402

MAX_CELL_CHARS = 300
_ID_SHAPE_RE = re.compile(r"\b[DTRFQS]-\d+\b")
_EVALUATOR_LINE = "evaluator: deterministic"


class GateArtifactError(Exception):
    """The audit file could not be written safely; `code` is a stable identifier."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class GateArtifactConflict(GateArtifactError):
    """A file of the same name exists and was not written by this gate."""

    def __init__(self, message: str = "") -> None:
        super().__init__("gate-artifact-conflict", message or "an audit file written by another tool exists")


class GateArtifactInvalid(GateArtifactError):
    """The file just written does not pass strict validation."""

    def __init__(self, message: str = "") -> None:
        super().__init__("gate-artifact-invalid", message or "the written audit file failed validation")


def _yaml_string(value: str) -> str:
    """A double-quoted YAML scalar. A value holding a short-ID-shaped token has
    each hyphen escaped so the artifact validator's reference check does not
    read it as a reference."""
    quoted = json.dumps(value)
    if _ID_SHAPE_RE.search(value):
        quoted = quoted.replace("-", "\\x2D")
    return quoted


def _cell(text: str) -> str:
    cleaned = _redact(" ".join(str(text).split())).replace("`", "'").replace("|", "/")
    cleaned = cleaned[:MAX_CELL_CHARS].strip()
    return "`%s`" % (cleaned or "-")


def render_artifact(result: GateResult, *, date: str) -> str:
    stage = "null" if result.stage is None else str(int(result.stage))
    origin = "null" if result.origin is None else _yaml_string(result.origin)
    lines = [
        "---",
        "phase: %s" % _yaml_string(result.phase),
        "date: %s" % _yaml_string(date),
        "task: %s" % _yaml_string(result.task),
        "stage: %s" % stage,
        "verdict: %s" % _yaml_string(result.verdict),
        _EVALUATOR_LINE,
        "origin: %s" % origin,
        "---",
        "",
        "## Automated checks",
        "",
        "| Check | Status | Codes | Details |",
        "|---|---|---|---|",
    ]
    for item in result.checks:
        codes = ", ".join(list(item.codes) + list(item.warn_codes))
        lines.append("| %s | %s | %s | %s |" % (
            _cell(item.name), _cell(item.status), _cell(codes), _cell("; ".join(item.details))))
    passed = sum(1 for c in result.checks if c.status in ("PASS", "WARN"))
    lines += ["", "## Verdict", ""]
    if result.verdict == "PASS":
        lines.append("PASS (%d checks passed, %d warnings)" % (passed, len(result.warnings)))
    else:
        failing = sum(1 for c in result.checks if c.status == "FAIL")
        lines.append("FAIL (%d failing checks: %s)" % (failing, ", ".join(result.reasons)))
    if result.verdict == "FAIL":
        lines += ["", "## Failures requiring attention", ""]
        for number, code in enumerate(result.reasons, 1):
            details = "; ".join(d for c in result.checks if code in c.codes for d in c.details)
            lines.append("%d. %s: %s" % (number, _cell(code), _cell(details)))
    if result.warnings:
        lines += ["", "## Warnings (non-blocking)", ""]
        for code in result.warnings:
            details = "; ".join(d for c in result.checks if code in c.warn_codes for d in c.details)
            lines.append("- %s: %s" % (_cell(code), _cell(details)))
    if result.explanation is not None:
        lines += ["", "## Summary of what was produced", "",
                  "Explanation supplied with this gate (not evaluated by any check):", "", "```"]
        lines += ["  " + ln for ln in _redact(result.explanation).splitlines()]
        lines.append("```")
    return "\n".join(lines) + "\n"


def _checked_dir(project_root, sdir: Path) -> Path:
    """`sdir` made safe to write into: it must lie under the artifact root and
    no component may be a symlink."""
    root = Path(project_root)
    artifacts = root / ".workflow_artifacts"
    rel = os.path.relpath(str(sdir), str(artifacts))
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise GateArtifactError("unsafe-path", "the stage folder is outside the artifact root")
    current = artifacts
    for part in ["."] + [p for p in rel.split(os.sep) if p != "."]:
        if part != ".":
            current = current / part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise GateArtifactError("unsafe-path", "a symlink lies on the audit file path")
    return current


def _written_by_gate(path: Path) -> bool:
    text = _read_text(path, 8 * 1024) if _lstat(path) else None
    if text is None:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line == _EVALUATOR_LINE:
            return True
    return False


def write_artifact(project_root, result: GateResult, sdir: Path, *, source_dir, clock: Callable[[], float] = time.time) -> Path:
    """Write `gate-PHASE-DATE.md` into the stage folder (the task root for a
    stage-less phase), replacing a file this writer made on the same day.
    Another tool's file of that name is never touched."""
    folder = _checked_dir(project_root, Path(sdir))
    date = time.strftime("%Y-%m-%d", time.gmtime(clock()))
    target = folder / ("gate-%s-%s.md" % (result.phase, date))
    info = _lstat(target)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GateArtifactError("unsafe-path", "the audit file path is not a regular file")
        if not _written_by_gate(target):
            raise GateArtifactConflict()
    os.makedirs(str(folder), exist_ok=True)
    tmp = folder / (".%s.%s.tmp" % (target.name, _secrets.token_hex(4)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, 0o666)
    try:
        try:
            data = render_artifact(result, date=date).encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(target))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    detail = run_validator(project_root, source_dir, target)
    if detail is not None:
        raise GateArtifactInvalid(detail)
    return target


def record_gate(
    project_root, task: str, stage: Optional[int], phase: str, result: GateResult, artifact_path,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Store the verdict on the phase's current entry. False when the phase has
    no entry (a gate with no evidence still writes its audit file)."""
    directory = runstore.inspect_store(project_root)
    if directory is None:
        return False
    state = runstore.load_workflow_state(directory, task)
    if state is None:
        return False
    sha = hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    updated = runstore.update_current_entry(state, stage, phase, gate={
        "verdict": result.verdict, "reasons": list(result.reasons), "warnings": list(result.warnings),
        "artifact": _rel(Path(project_root), Path(artifact_path)), "artifact_sha256": sha,
        "evaluated_at": result.evaluated_at,
    })
    if updated is None:
        return False
    state["updated_at"] = _now_iso(clock)
    runstore.write_workflow_state(directory, state)
    return True
