#!/usr/bin/env python3
"""Portable core implementation of the CI-parity gate check for non-Python deliverables.

Given a set of changed files (via --project-root, --files-from, or --files),
this helper detects non-Python "deliverables" (directories containing a
package.json, e.g. a TypeScript package) touched by the diff, derives the
same correctness steps CI runs for that deliverable (compile, typecheck,
lint, test — excluding packaging/publish/upload), and runs them locally.
The result is used by /gate as a HARD PRECONDITION for APPROVED whenever a
non-Python deliverable is in scope (IVG-138 / IVG-114).

Exit-code semantics (mirrors affected_tests.py, fail-CLOSED):
  0  — APPROVABLE (three sub-cases disambiguated by `ran_steps` + `exit_reason`):
       0a: CI-parity steps GREEN (`ran_steps=true`, `exit_reason="ci-mirror-green"`)
           every derived correctness step for every deliverable returned 0.
       0b: no non-Python deliverable in the diff (`ran_steps=false`,
           `exit_reason="no-deliverable"`) — the check does not fire.
       0c: clean tree (`ran_steps=false`, `exit_reason="no-changes"`) —
           --project-root mode only, git ran cleanly and nothing changed.
  1  — a correctness step returned non-zero (`exit_reason="ci-mirror-red"`,
       `failing_step` + `failing_returncode` recorded). BLOCKING.
  2  — argparse / malformed input (mutually-exclusive violation, unreadable
       --files-from).
  3  — UNDETERMINABLE (fail-CLOSED): git-root/repo resolution failed, npm/node
       missing, dependency install failed or timed out, a deliverable was
       detected but zero correctness steps could be derived
       (`exit_reason="no-steps-derived"`), a step timed out
       (`exit_reason="ci-mirror-timeout"`), OR QUOIN_DISABLE_CI_MIRROR=1.
       Treat as "cannot confirm green → do NOT auto-approve."
  5  — no active quoin task context (NON-approving, NON-blocking; IVG-151):
       --project-root + --require-task-context, when QUOIN_REQUIRE_TASK_CONTEXT
       !=0 and the sibling affected_tests detector finds no active task folder
       at/above the project root. A CLEAN-SKIP / N/A signal for a non-quoin
       session, never a WARN or gate FAIL. On a torn deploy (old affected_tests
       lacking has_active_task_context) the getattr-guard skips this branch and
       degrades to legacy always-run — never an AttributeError.

Fail-CLOSED philosophy: the bug this check exists to catch is "gate green
while CI red" — so any inability to CONFIRM green (missing toolchain,
undeterminable step list, timeout, disabled) must block/surface, never
silently pass. This is the OPPOSITE of branch_hygiene's/deploy_drift's
fail-OPEN env opt-out; QUOIN_DISABLE_CI_MIRROR=1 exits 3 (not 0) for the
same reason QUOIN_DISABLE_AFFECTED_TESTS=1 does in affected_tests.py. The
only fail-OPEN carve-out is "script binary absent" at the gate-wiring layer
(a brand-new install lacking the script must not hard-block legacy tasks) —
that carve-out lives in the SKILL.md wiring, not in this script.

Deliverable detection (D-01): for each changed path (excluding anything
under node_modules/), walk up from the file's directory to the repo root
looking for the nearest ancestor containing package.json. Each such
directory with >=1 changed file under it is a non-Python deliverable. If
none are found, the check is N/A (exit 0b) — it never fires for a
Python-only or docs-only diff.

Hybrid step derivation (D-02), first non-empty (post-filter) tier wins:
  Tier 1 — explicit manifest at <repo_root>/.quoin/gate-manifest.json
           (stdlib json; shape: {"deliverables": {"<dir>": {"steps": [...],
           "working-directory": ..., "install": ...}}}).
  Tier 2 — parse .github/workflows/*.yml: match each workflow's
           on.push.paths / on.pull_request.paths globs (fnmatch, "**" -> "*",
           over-inclusive is safe) against the changed files; extract each
           matched job's step `run:` commands + working-directory (job-level
           defaults.run.working-directory merged with per-step
           working-directory). Requires PyYAML — see the dependency note
           below.
  Tier 3 — read the deliverable's package.json `scripts` and select whichever
           of compile/typecheck/lint/test exist, run as `npm run <name>`
           (or `npm test` for test), cwd = deliverable dir.

Packaging/dependency filter (D-03): a derived step is dropped if a
whole TOKEN (split on non-alphanumeric boundaries, case-insensitive) of its
name or command matches the packaging denylist (vsce, publish, package —
"upload-artifact" is listed for documentation only: it never matches under
whole-token splitting since the hyphen is itself a split boundary, but
`uses:`-only steps have no `run:` and are excluded regardless). Dependency
-install commands (`npm ci`, `npm install`, `npm i`) are also dropped from
the correctness set — installation is handled separately (see D-04).

Dependency + tool preflight (D-04): for an npm-based deliverable (every
detected deliverable is npm-based by construction — D-01 requires
package.json), `shutil.which("npm") is None` -> exit 3 BEFORE running
anything. QUOIN_CI_MIRROR_INSTALL controls install behavior: "auto"
(default) installs only when node_modules/ is absent in the deliverable
dir; "always" always installs; "never" skips. The install command runs as
an argv list (NOT shell=True) so a missing binary raises FileNotFoundError
-> exit 3; a non-zero install -> exit 3 (cannot verify); install is bounded
by the same max(600, QUOIN_SUBPROCESS_TIMEOUT) timeout as correctness
steps — a TimeoutExpired there -> exit 3 (`ci-mirror-timeout`).

Step execution: correctness steps run via `subprocess.run(cmd, shell=True,
cwd=working_dir, timeout=max(600, _subprocess_timeout()))` — shell=True
because a derived command may be a shell string (`&&`-chained, or a
multi-line `run: |` workflow step), unlike the install command which stays
a plain argv list. The first non-zero step blocks (exit 1); a timeout is
exit 3, never a silent green nor a false hard-red.

Env:
  QUOIN_DISABLE_CI_MIRROR=1 — exit 3 immediately (fail-CLOSED opt-out;
      NOT a clean-pass bypass, mirrors QUOIN_DISABLE_AFFECTED_TESTS).
  QUOIN_REQUIRE_TASK_CONTEXT — literal "0" ONLY forces legacy always-run even
      when --require-task-context is passed (disarms the exit-5 branch); unset
      or any other value honors the flag. Shared with affected_tests (IVG-151).
  QUOIN_CI_MIRROR_INSTALL — auto (default) | always | never (D-04).
  QUOIN_SUBPROCESS_TIMEOUT — seconds, default 30; bounds every SHORT git
      subprocess run via the sibling affected_tests module. Correctness/
      install steps get the generous derived bound
      max(600, QUOIN_SUBPROCESS_TIMEOUT) instead.

Dependency decision (D-06): PyYAML is NOT assumed installed. Tier 1 (JSON
manifest) and Tier 3 (package.json) are stdlib-only. Tier 2 (workflow
parse) imports `yaml` inside a try/except ImportError; on absence it
degrades to Tier 3 and records a `note` in the output (NOT a silent pass —
Tier 3 still runs the correctness steps by convention).

Sibling-core reuse (T-01 / not the deploy_drift_check.py adapter->core hop):
  --project-root mode importlib-loads the SIBLING core module
  `affected_tests.py` (both live in quoin/core/scripts/, or both live in
  the deployed ~/.claude/core/scripts/ — the loader always finds a sibling
  in either invocation mode) and reuses `resolve_repo()` + `changed_files()`
  so the diff basis is byte-identical to the affected-test check. This is a
  core-to-core SIBLING load (`Path(__file__).resolve().parent /
  "affected_tests.py"`), NOT the adapter-only `parents[1]` hop
  deploy_drift_check.py uses (that script has no core twin).
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import-untyped]
    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    _yaml = None
    _YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# D-03: packaging/publish denylist. Whole-token match only (see _tokenize).
# "upload-artifact" is retained for documentation only — see module docstring.
_PACKAGING_DENYLIST_TERMS: frozenset[str] = frozenset(
    {"vsce", "publish", "upload-artifact", "package"}
)

# D-03: dependency-install commands are dropped from the correctness set
# (installation is handled separately by the D-04 preflight+install path).
_INSTALL_COMMAND_RE = re.compile(r"^\s*npm\s+(ci|install|i)\b")

# D-02 Tier 1 manifest location, repo-root relative.
_MANIFEST_REL_PATH = (".quoin", "gate-manifest.json")

# D-02 Tier 3 conventional script names, in the order CI runs them.
_TIER3_SCRIPT_NAMES: tuple[str, ...] = ("compile", "typecheck", "lint", "test")

# Default install command when no manifest supplies one (mirrors CI's `npm ci`).
_DEFAULT_INSTALL_COMMAND = "npm ci"


# ---------------------------------------------------------------------------
# Local subprocess-timeout helper (copy-not-import, D-06 convention)
# ---------------------------------------------------------------------------

def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy — do NOT cross-import; each touched core
    script owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


# ---------------------------------------------------------------------------
# Sibling-core reuse: affected_tests.resolve_repo / changed_files
# ---------------------------------------------------------------------------

def _load_affected_tests():
    """Importlib-load the sibling core `affected_tests.py` module.

    Core-to-core SIBLING load — both files live in the same directory in
    every invocation mode (direct core run, or the deployed wrapper whose
    __file__ sits in ~/.claude/core/scripts/, still a sibling of the
    deployed affected_tests.py). See module docstring for why this is NOT
    the deploy_drift_check.py adapter->core `parents[1]` idiom.
    """
    core_path = Path(__file__).resolve().parent / "affected_tests.py"
    spec = importlib.util.spec_from_file_location(
        "_quoin_core_affected_tests_sibling", core_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Step:
    """A single derived correctness step."""
    name: str
    command: str
    working_dir: str  # repo-root-relative posix path


@dataclasses.dataclass
class CiMirrorResult:
    """Result of a full ci_mirror run."""
    changed: list[str]
    deliverables: list[str]
    ran_steps: bool
    exit_reason: str
    steps: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    failing_step: str | None = None
    failing_returncode: int | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "changed": self.changed,
            "deliverables": self.deliverables,
            "ran_steps": self.ran_steps,
            "exit_reason": self.exit_reason,
            "steps": self.steps,
        }
        if self.failing_step is not None:
            d["failing_step"] = self.failing_step
            d["failing_returncode"] = self.failing_returncode
        if self.note:
            d["note"] = self.note
        return d


def _format_text(res: CiMirrorResult) -> str:
    """Human-readable text summary of a CiMirrorResult (mirrors affected_tests._format_text)."""
    lines: list[str] = []
    lines.append(f"exit_reason: {res.exit_reason}")
    lines.append(f"ran_steps: {res.ran_steps}")
    lines.append(f"changed ({len(res.changed)}): {', '.join(res.changed) or '(none)'}")
    lines.append(
        f"deliverables ({len(res.deliverables)}): {', '.join(res.deliverables) or '(none)'}"
    )
    if res.steps:
        lines.append(f"steps ({len(res.steps)}):")
        for s in res.steps:
            rc = s.get("returncode")
            rc_str = str(rc) if rc is not None else "not-run"
            lines.append(
                f"  - [{s.get('deliverable')}] {s.get('name')}: {s.get('command')} "
                f"(cwd={s.get('working_dir')}) -> returncode={rc_str}"
            )
    if res.failing_step is not None:
        lines.append(f"failing_step: {res.failing_step} (returncode={res.failing_returncode})")
    if res.note:
        lines.append(f"note: {res.note}")
    return "\n".join(lines)


def _emit(res: CiMirrorResult, fmt: str) -> None:
    if fmt == "text":
        print(_format_text(res))
    else:
        print(json.dumps(res.to_dict(), indent=2))


# ---------------------------------------------------------------------------
# D-01: deliverable detection
# ---------------------------------------------------------------------------

def detect_deliverables(changed: list[str], repo_root: Path) -> list[str]:
    """Return sorted, deduplicated repo-relative deliverable dirs.

    A deliverable is the nearest ancestor directory of a changed path that
    contains package.json, excluding any path under node_modules/. Returns
    "." if the repo root itself is the deliverable.
    """
    root_resolved = repo_root.resolve()
    deliverables: set[str] = set()

    for f in changed:
        posix = PurePosixPath(f).as_posix()
        if any(part == "node_modules" for part in posix.split("/")):
            continue

        fpath = Path(f)
        file_abs = fpath.resolve() if fpath.is_absolute() else (root_resolved / f).resolve()
        cur = file_abs.parent

        while True:
            if (cur / "package.json").exists():
                try:
                    rel = cur.relative_to(root_resolved)
                except ValueError:
                    # Ancestor walked outside repo_root without hitting it —
                    # should not happen for well-formed relative changed paths.
                    break
                rel_str = rel.as_posix() if str(rel) != "." else "."
                deliverables.add(rel_str)
                break
            if cur == root_resolved:
                break
            parent = cur.parent
            if parent == cur:
                break
            cur = parent

    return sorted(deliverables)


# ---------------------------------------------------------------------------
# D-03: packaging + install filters
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Split on non-alphanumeric boundaries, lowercase, drop empties."""
    return {t.lower() for t in re.split(r"[^a-zA-Z0-9]+", text) if t}


def _is_packaging_step(name: str, command: str) -> bool:
    tokens = _tokenize(name) | _tokenize(command)
    return bool(tokens & _PACKAGING_DENYLIST_TERMS)


def _is_install_command(command: str) -> bool:
    return bool(_INSTALL_COMMAND_RE.match(command))


def filter_packaging(steps: list[Step]) -> list[Step]:
    """D-03: drop packaging/publish steps and dependency-install commands."""
    out: list[Step] = []
    for s in steps:
        if _is_packaging_step(s.name, s.command):
            continue
        if _is_install_command(s.command):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# D-02: hybrid step derivation
# ---------------------------------------------------------------------------

def _tier1_steps(deliverable: str, repo_root: Path) -> tuple[list[Step], str] | None:
    """Tier 1: explicit .quoin/gate-manifest.json. Returns (steps, install_cmd) or None."""
    manifest_path = repo_root.joinpath(*_MANIFEST_REL_PATH)
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = (data.get("deliverables") or {}).get(deliverable)
    if not isinstance(entry, dict):
        return None

    working_dir = entry.get("working-directory") or deliverable
    install_cmd = entry.get("install") or _DEFAULT_INSTALL_COMMAND
    raw_steps = entry.get("steps") or []
    steps: list[Step] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        run = raw.get("run")
        if not run:
            continue
        steps.append(Step(name=str(raw.get("name", "")), command=str(run), working_dir=str(working_dir)))
    return steps, install_cmd


def _tier2_steps(
    deliverable: str, repo_root: Path, changed: list[str]
) -> tuple[list[Step], str] | None:
    """Tier 2: parse .github/workflows/*.yml. Returns (steps, install_cmd) or None.

    Caller MUST check _YAML_AVAILABLE before calling this.
    """
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return None

    changed_posix = [PurePosixPath(c).as_posix() for c in changed]
    wf_paths = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))

    steps_out: list[Step] = []
    for wf_path in wf_paths:
        try:
            data = _yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - any YAML/OS error skips this file
            continue
        if not isinstance(data, dict):
            continue

        # PyYAML 1.1 gotcha: an unquoted `on:` key parses as the boolean True.
        on_block = data.get("on")
        if on_block is None:
            on_block = data.get(True)
        if not isinstance(on_block, dict):
            continue

        all_paths: list[str] = []
        for trigger_name in ("push", "pull_request"):
            trigger = on_block.get(trigger_name)
            if isinstance(trigger, dict):
                paths = trigger.get("paths")
                if isinstance(paths, list):
                    all_paths.extend(str(p) for p in paths)
        if not all_paths:
            continue

        matched = False
        for pattern in all_paths:
            fn_pattern = pattern.replace("**", "*")
            for cf in changed_posix:
                if fnmatch.fnmatch(cf, fn_pattern):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            continue

        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            job_defaults = job.get("defaults")
            job_wd = None
            if isinstance(job_defaults, dict):
                job_run_defaults = job_defaults.get("run")
                if isinstance(job_run_defaults, dict):
                    job_wd = job_run_defaults.get("working-directory")
            job_steps = job.get("steps")
            if not isinstance(job_steps, list):
                continue
            for step in job_steps:
                if not isinstance(step, dict):
                    continue
                run_cmd = step.get("run")
                if not run_cmd:
                    continue
                step_wd = step.get("working-directory") or job_wd or deliverable
                steps_out.append(
                    Step(name=str(step.get("name", "")), command=str(run_cmd), working_dir=str(step_wd))
                )

    if not steps_out:
        return None
    return steps_out, _DEFAULT_INSTALL_COMMAND


def _tier3_steps(deliverable: str, repo_root: Path) -> tuple[list[Step], str] | None:
    """Tier 3: package.json conventional scripts. Returns (steps, install_cmd) or None."""
    pkg_path = repo_root / deliverable / "package.json"
    if not pkg_path.exists():
        return None
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pkg, dict):
        return None
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return None

    steps: list[Step] = []
    for name in _TIER3_SCRIPT_NAMES:
        if name in scripts:
            cmd = "npm test" if name == "test" else f"npm run {name}"
            steps.append(Step(name=name, command=cmd, working_dir=deliverable))
    if not steps:
        return None
    return steps, _DEFAULT_INSTALL_COMMAND


def derive_steps(
    deliverable: str, repo_root: Path, changed: list[str]
) -> tuple[list[Step], str, str | None]:
    """D-02: resolve steps for a deliverable, tier 1 -> tier 2 -> tier 3.

    Returns (steps, install_command, note). `note` is non-None only when
    PyYAML is unavailable and Tier 2 was skipped as a result (NOT a silent
    pass — Tier 3 still runs the correctness steps by convention).
    """
    tier1 = _tier1_steps(deliverable, repo_root)
    if tier1 is not None:
        raw_steps, install_cmd = tier1
        filtered = filter_packaging(raw_steps)
        if filtered:
            return filtered, install_cmd, None

    note: str | None = None
    if not _YAML_AVAILABLE:
        note = (
            "PyYAML not installed; Tier-2 CI-workflow parsing skipped, "
            "degraded to Tier-3 (package.json scripts)"
        )
    else:
        tier2 = _tier2_steps(deliverable, repo_root, changed)
        if tier2 is not None:
            raw_steps, install_cmd = tier2
            filtered = filter_packaging(raw_steps)
            if filtered:
                return filtered, install_cmd, None

    tier3 = _tier3_steps(deliverable, repo_root)
    if tier3 is not None:
        raw_steps, install_cmd = tier3
        filtered = filter_packaging(raw_steps)
        if filtered:
            return filtered, install_cmd, note

    return [], _DEFAULT_INSTALL_COMMAND, note


# ---------------------------------------------------------------------------
# D-04: preflight + install
# ---------------------------------------------------------------------------

def _npm_available() -> bool:
    return shutil.which("npm") is not None


def _should_install(deliverable_dir: Path) -> bool:
    mode = os.environ.get("QUOIN_CI_MIRROR_INSTALL", "auto").strip().lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    # auto (default): install only when node_modules/ is absent.
    return not (deliverable_dir / "node_modules").exists()


def _run_install(install_cmd: str, cwd: Path) -> tuple[bool, str]:
    """Run the install command as an argv list (shell=False). Returns (ok, exit_reason_on_fail)."""
    try:
        argv = shlex.split(install_cmd)
    except ValueError:
        return False, "ci-mirror-install-malformed-command"
    if not argv:
        return False, "ci-mirror-install-malformed-command"
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            timeout=max(600, _subprocess_timeout()),
        )
    except FileNotFoundError:
        return False, "ci-mirror-install-missing-binary"
    except subprocess.TimeoutExpired:
        return False, "ci-mirror-timeout"
    if proc.returncode != 0:
        return False, "ci-mirror-install-failed"
    return True, ""


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _run_step(step: Step, repo_root: Path) -> tuple[int | None, bool]:
    """Run a single correctness step. Returns (returncode, timed_out)."""
    cwd = repo_root / step.working_dir
    try:
        proc = subprocess.run(
            step.command,
            shell=True,
            cwd=str(cwd),
            timeout=max(600, _subprocess_timeout()),
        )
    except subprocess.TimeoutExpired:
        return None, True
    return proc.returncode, False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns exit code:
      0 — APPROVABLE (CI-parity green, no non-Python deliverable, or clean tree)
      1 — a correctness step returned non-zero (BLOCKING)
      2 — argparse / malformed input
      3 — UNDETERMINABLE (fail-CLOSED): git-root failure, npm missing, install
          failed/timed out, deliverable detected but zero steps derivable, a
          step timed out, or QUOIN_DISABLE_CI_MIRROR=1
      5 — no active quoin task context (NON-approving, NON-blocking): --project-root
          + --require-task-context, via the sibling affected_tests detector (IVG-151)
    """
    # Env opt-out — exits 3 (NOT 0) so disabling cannot silently green-light APPROVE.
    if os.environ.get("QUOIN_DISABLE_CI_MIRROR", "").strip() == "1":
        print(json.dumps({"disabled": True}))
        return 3

    parser = argparse.ArgumentParser(
        description=(
            "Run the same correctness steps CI runs (compile/typecheck/lint/test) for "
            "non-Python deliverables touched by the diff. A GREEN or N/A result is a "
            "hard precondition for APPROVED in /gate."
        ),
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--project-root",
        type=Path,
        metavar="PATH",
        help=(
            "PRIMARY workflow path. Resolves the git repo under PATH and computes the "
            "changed-file set itself via the sibling affected_tests module. The caller "
            "never runs git directly."
        ),
    )
    mode.add_argument(
        "--files-from",
        metavar="PATH",
        help="Newline-delimited list of changed files from PATH (use '-' for stdin).",
    )
    mode.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="Changed files passed inline.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        metavar="PATH",
        default=None,
        help=(
            "Root for deliverable detection and step execution. With --project-root it "
            "also names the git checkout whose changes are diffed (e.g. a task's git "
            "worktree); it must be the top of a work tree. Defaults to the resolved "
            "repo in --project-root mode and to cwd otherwise."
        ),
    )
    parser.add_argument(
        "--derive-only",
        action="store_true",
        help="Print derived steps as JSON/text and exit WITHOUT running them.",
    )
    parser.add_argument(
        "--require-task-context",
        action="store_true",
        dest="require_task_context",
        help=(
            "Opt-in: in --project-root mode, if no active quoin task context is "
            "found (and QUOIN_REQUIRE_TASK_CONTEXT!=0), exit 5 (no-quoin-task-context) "
            "WITHOUT deriving/running steps. Inert in --files/--files-from modes."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    fmt = args.format

    # ------------------------------------------------------------------
    # Step 1: resolve changed files
    # ------------------------------------------------------------------
    changed: list[str] = []
    repo_root: Path | None = args.repo_root

    if args.project_root is not None:
        affected = _load_affected_tests()
        # IVG-151: opt-in early exit-5 when NO active quoin task context is
        # found, via the sibling affected_tests detector. Runs BEFORE
        # resolve_repo so a non-quoin session never resolves a foreign git
        # root. getattr-guard: on a torn deploy (new ci_mirror caller, old
        # affected_tests lacking the detector) _has_ctx is None -> the guard is
        # skipped -> legacy always-run (fail-OPEN to old behavior, never an
        # AttributeError crash). Precedence: QUOIN_DISABLE_CI_MIRROR=1 already
        # returned 3 at the top of main(); QUOIN_REQUIRE_TASK_CONTEXT literal
        # "0" forces legacy always-run.
        _has_ctx = getattr(affected, "has_active_task_context", None)
        if (
            args.require_task_context
            and _has_ctx is not None
            and os.environ.get("QUOIN_REQUIRE_TASK_CONTEXT", "").strip() != "0"
            and not _has_ctx(args.project_root)
        ):
            res = CiMirrorResult(
                changed=[],
                deliverables=[],
                ran_steps=False,
                exit_reason="no-quoin-task-context",
            )
            _emit(res, fmt)
            return 5
        # An explicit --repo-root names the checkout to diff (a task's git
        # worktree, say) instead of the repo the depth-1 scan finds.
        try:
            if args.repo_root is not None:
                root = args.repo_root.expanduser().resolve()
                repo = root if (root / ".git").exists() else None
            else:
                repo = affected.resolve_repo(args.project_root)
        except RuntimeError as exc:
            print(json.dumps({
                "error": str(exc),
                "exit_reason": "undeterminable-multiple-repos",
                "ran_steps": False,
                "changed": [],
                "deliverables": [],
                "steps": [],
            }), file=sys.stderr)
            return 3
        if repo is None:
            print(json.dumps({
                "error": (
                    f"No git repo found at --repo-root {args.repo_root}"
                    if args.repo_root is not None
                    else f"No git repo found under --project-root {args.project_root}"
                ),
                "exit_reason": "undeterminable-no-repo",
                "ran_steps": False,
                "changed": [],
                "deliverables": [],
                "steps": [],
            }), file=sys.stderr)
            return 3

        repo_root = repo

        files, reason = affected.changed_files(repo)
        if reason == "git-error":
            print(json.dumps({
                "error": "git error while computing changed files",
                "exit_reason": "undeterminable-git-error",
                "ran_steps": False,
                "changed": [],
                "deliverables": [],
                "steps": [],
            }), file=sys.stderr)
            return 3
        if reason == "no-changes":
            res = CiMirrorResult(
                changed=[], deliverables=[], ran_steps=False, exit_reason="no-changes"
            )
            _emit(res, fmt)
            return 0
        changed = files

    elif args.files_from is not None:
        if args.files_from == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(args.files_from).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot read --files-from {args.files_from}: {exc}", file=sys.stderr)
                return 2
        changed = [l.strip() for l in raw.splitlines() if l.strip()]

    else:
        changed = list(args.files)

    if repo_root is None:
        repo_root = Path.cwd()
    repo_root = repo_root.resolve()

    # ------------------------------------------------------------------
    # Step 2: deliverable detection (D-01)
    # ------------------------------------------------------------------
    deliverables = detect_deliverables(changed, repo_root)
    if not deliverables:
        res = CiMirrorResult(
            changed=changed, deliverables=[], ran_steps=False, exit_reason="no-deliverable"
        )
        _emit(res, fmt)
        return 0

    # ------------------------------------------------------------------
    # Step 3: per-deliverable derive -> preflight/install -> run (proc P-01)
    # ------------------------------------------------------------------
    steps_out: list[dict[str, Any]] = []
    notes: list[str] = []

    for deliverable in deliverables:
        steps, install_cmd, note = derive_steps(deliverable, repo_root, changed)
        if note and note not in notes:
            notes.append(note)

        if not steps:
            res = CiMirrorResult(
                changed=changed,
                deliverables=deliverables,
                ran_steps=False,
                exit_reason="no-steps-derived",
                steps=steps_out,
                note="; ".join(notes) if notes else None,
            )
            _emit(res, fmt)
            return 3

        if args.derive_only:
            for s in steps:
                steps_out.append({
                    "deliverable": deliverable,
                    "name": s.name,
                    "command": s.command,
                    "working_dir": s.working_dir,
                    "returncode": None,
                })
            continue

        if not _npm_available():
            res = CiMirrorResult(
                changed=changed,
                deliverables=deliverables,
                ran_steps=False,
                exit_reason="npm-missing",
                steps=steps_out,
                note="; ".join(notes) if notes else None,
            )
            _emit(res, fmt)
            return 3

        deliverable_dir = repo_root / deliverable
        if _should_install(deliverable_dir):
            ok, install_fail_reason = _run_install(install_cmd, deliverable_dir)
            if not ok:
                res = CiMirrorResult(
                    changed=changed,
                    deliverables=deliverables,
                    ran_steps=False,
                    exit_reason=install_fail_reason,
                    steps=steps_out,
                    note="; ".join(notes) if notes else None,
                )
                _emit(res, fmt)
                return 3

        for s in steps:
            rc, timed_out = _run_step(s, repo_root)
            if timed_out:
                steps_out.append({
                    "deliverable": deliverable,
                    "name": s.name,
                    "command": s.command,
                    "working_dir": s.working_dir,
                    "returncode": None,
                })
                res = CiMirrorResult(
                    changed=changed,
                    deliverables=deliverables,
                    ran_steps=True,
                    exit_reason="ci-mirror-timeout",
                    steps=steps_out,
                    failing_step=s.name,
                    note="; ".join(notes) if notes else None,
                )
                _emit(res, fmt)
                return 3

            steps_out.append({
                "deliverable": deliverable,
                "name": s.name,
                "command": s.command,
                "working_dir": s.working_dir,
                "returncode": rc,
            })
            if rc != 0:
                res = CiMirrorResult(
                    changed=changed,
                    deliverables=deliverables,
                    ran_steps=True,
                    exit_reason="ci-mirror-red",
                    steps=steps_out,
                    failing_step=s.name,
                    failing_returncode=rc,
                    note="; ".join(notes) if notes else None,
                )
                _emit(res, fmt)
                return 1

    if args.derive_only:
        res = CiMirrorResult(
            changed=changed,
            deliverables=deliverables,
            ran_steps=False,
            exit_reason="derive-only",
            steps=steps_out,
            note="; ".join(notes) if notes else None,
        )
        _emit(res, fmt)
        return 0

    res = CiMirrorResult(
        changed=changed,
        deliverables=deliverables,
        ran_steps=True,
        exit_reason="ci-mirror-green",
        steps=steps_out,
        note="; ".join(notes) if notes else None,
    )
    _emit(res, fmt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
