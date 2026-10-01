"""quoin.supervisor — external relaunch-loop core for autonomous runs.

Stage 2 of IVG-153 (autonomous-run-mode). This module is the pure
relaunch-loop logic behind the ``quoin run --autonomous <task>`` CLI
subcommand: it carries a Large task across context-window boundaries a
single interactive session cannot fit in, by relaunching fresh headless
sessions and reading a small sentinel contract to decide when to stop.

Sentinel contract (T-05; also documented in
``quoin/quoin/core/skills/run.md`` and
``quoin/quoin/memory/autonomous-mode.md`` — this module, both docs, and
``test_autonomous_sentinel_contract.py`` are kept byte-identical on the
path templates):

- Marker: ``autonomous-run-{task}.marker`` — written once at
  autonomous-span entry; read by a resumed session to re-establish
  autonomous mode before its own first decision point.
- Per-phase completion sentinels: ``autonomous-progress-{task}/{phase}.done``
  for the full resumable ``/run`` phase roster (see ``RESUMABLE_PHASES``),
  plus optional finer-grained ``autonomous-progress-{task}/{phase}.{subphase}.done``
  sentinels. The counting glob ``autonomous-progress-{task}/*.done`` is
  the UNION of both forms.
- Done sentinel: ``autonomous-done-{task}.md`` — written last, after
  finalization's other side effects complete.
- Halt sentinel: ``autonomous-halt-{task}.md`` — Stage 1, unchanged;
  read-only from this module's perspective.

The supervisor only reads these files: a completion marker such as
``implement.tasks.done`` is counted as progress and can earn a bounded
repair allowance, but the supervisor never writes any ``.done`` itself.

All four templates resolve under ``.workflow_artifacts/memory/``,
deliberately outside the task-scoped artifact folder, so each survives
that folder's later archival into ``finalized/``.

This module keeps its imports lean (stdlib only) so it stays safe to
import from the CLI's lazy-import dispatch path (mirrors the
``router``/``models`` lazy-import convention in ``cli.py``).
"""
from __future__ import annotations

import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple, Union

PathLike = Union[str, Path]

# ---------------------------------------------------------------------------
# Sentinel path templates (T-05 contract)
# ---------------------------------------------------------------------------

#: Sentinel root, relative to the project root. All four sentinel kinds
#: below resolve under this directory.
SENTINEL_ROOT = ".workflow_artifacts/memory"

MARKER_TEMPLATE = "autonomous-run-{task}.marker"
PROGRESS_DIR_TEMPLATE = "autonomous-progress-{task}"
DONE_TEMPLATE = "autonomous-done-{task}.md"
HALT_TEMPLATE = "autonomous-halt-{task}.md"

#: Counting glob (pattern only — resolved beneath PROGRESS_DIR_TEMPLATE).
#: Matches BOTH phase-granular `{phase}.done` and sub-phase-granular
#: `{phase}.{subphase}.done` sentinels (union counting, MAJ-2).
COMPLETION_GLOB_TEMPLATE = "autonomous-progress-{task}/*.done"

#: Full resumable `/run` phase roster (run/SKILL.md `## Phase sequence`,
#: 9 phases). `enrich` (1.4), `specify` (1.5), and `fast_path_triage` (1.6)
#: are IN-SET — never abbreviated as "Phases 1..6", which would silently
#: drop them. This tuple is the single source of truth other tests/docs are
#: checked against (see the coverage guard in test_autonomous_sentinel_contract.py).
RESUMABLE_PHASES = (
    "discover",
    "enrich",
    "specify",
    "fast_path_triage",
    "architect",
    "thorough_plan",
    "implement",
    "review",
    "end_of_task",
)

DEFAULT_MAX_RELAUNCH = 10

_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _memory_dir(project_root: PathLike) -> Path:
    return Path(project_root) / SENTINEL_ROOT


def marker_path(task: str, project_root: PathLike) -> Path:
    """Path to the autonomous-span marker for ``task``."""
    return _memory_dir(project_root) / MARKER_TEMPLATE.format(task=task)


def progress_dir(task: str, project_root: PathLike) -> Path:
    """Path to the per-phase completion-sentinel directory for ``task``."""
    return _memory_dir(project_root) / PROGRESS_DIR_TEMPLATE.format(task=task)


def done_path(task: str, project_root: PathLike) -> Path:
    """Path to the done-sentinel for ``task``."""
    return _memory_dir(project_root) / DONE_TEMPLATE.format(task=task)


def halt_path(task: str, project_root: PathLike) -> Path:
    """Path to the halt-sentinel for ``task``."""
    return _memory_dir(project_root) / HALT_TEMPLATE.format(task=task)


# ---------------------------------------------------------------------------
# Sentinel readers (no side effects)
# ---------------------------------------------------------------------------


def read_done(task: str, project_root: PathLike) -> bool:
    """Return True if the done-sentinel exists for ``task``."""
    return done_path(task, project_root).is_file()


def read_halt(task: str, project_root: PathLike) -> Optional[str]:
    """Return the halt reason if a halt-sentinel exists, else None.

    Best-effort parse of the ``reason:`` line in the halt-sentinel's
    five-field schema (task/phase/reason/timestamp/resume_hint); falls
    back to the raw file contents (or a generic message) if that line
    is absent, so a malformed halt-sentinel still HALTS rather than
    silently allowing another relaunch.
    """
    p = halt_path(task, project_root)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("reason:"):
            return stripped.split(":", 1)[1].strip()
    return text.strip() or "halted (no reason recorded)"


def count_completion_sentinels(task: str, project_root: PathLike) -> int:
    """Count completion sentinels for ``task`` (union glob, MAJ-2).

    Globs ``autonomous-progress-{task}/*.done`` — BOTH phase-granular
    ``{phase}.done`` and sub-phase-granular ``{phase}.{subphase}.done``
    sentinels count, so a long phase making only sub-phase progress
    across several relaunches is not false-aborted by the
    no-forward-progress guard in :func:`supervise`.
    """
    d = progress_dir(task, project_root)
    if not d.is_dir():
        return 0
    return len(list(d.glob("*.done")))


# ---------------------------------------------------------------------------
# Progress and repair predicates
# ---------------------------------------------------------------------------

#: Phases whose finer-grained completion marker (``{phase}.{sub}.done``) means
#: the phase's work is finished and only its ``{phase}.done`` write is missing.
COMPLETION_SIGNALS = {"implement": "tasks"}

DEFAULT_REPAIR_RELAUNCHES = 2
REPAIR_RELAUNCHES_ENV = "QUOIN_SUPERVISOR_REPAIR_RELAUNCHES"

#: A sha whose repo moved on the task branch counts as forward progress; set
#: this knob to ``0`` to ignore commits and count completion sentinels only.
HEAD_PROBE_ENV = "QUOIN_SUPERVISOR_HEAD_PROBE"
HEAD_PROBE_BUDGET_SECS = 5.0

#: Set to ``1`` in every headless child's environment.
HEADLESS_CHILD_ENV = "QUOIN_HEADLESS_CHILD"

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_PROBE_CANDIDATES = 50


def _clamped_env_int(
    environ: Mapping[str, str], name: str, default: int, lo: int, hi: int
) -> int:
    """Read an int env knob; absent or non-int -> default, else clamp to lo..hi."""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(lo, min(hi, value))


def repair_allowance_from_env(environ: Optional[Mapping[str, str]] = None) -> int:
    """Extra relaunches allowed for a phase missing only its ``.done`` write."""
    env = os.environ if environ is None else environ
    return _clamped_env_int(env, REPAIR_RELAUNCHES_ENV, DEFAULT_REPAIR_RELAUNCHES, 0, 5)


def launch_timeout_from_env(environ: Optional[Mapping[str, str]] = None) -> float:
    """Per-launch timeout in seconds (default 5400, clamped 900..14400)."""
    env = os.environ if environ is None else environ
    return float(
        _clamped_env_int(
            env, LAUNCH_TIMEOUT_ENV, int(DEFAULT_LAUNCH_TIMEOUT_SECONDS), 900, 14400
        )
    )


def head_probe_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """False only when the commit-progress probe is switched off with ``0``."""
    env = os.environ if environ is None else environ
    return str(env.get(HEAD_PROBE_ENV, "")).strip() != "0"


def heads_changed(before: tuple, after: tuple) -> bool:
    """True when both snapshots are non-empty and a shared repo moved."""
    if not before or not after:
        return False
    old = dict(before)
    for key, sha in after:
        if key in old and old[key] != sha:
            return True
    return False


def progress_made(
    done_before: int, done_after: int, heads_before: tuple, heads_after: tuple
) -> bool:
    """New completion sentinel, or a task-branch commit, since the snapshot."""
    return done_after > done_before or heads_changed(heads_before, heads_after)


def repair_pending_phases(progress_dir_path: PathLike) -> tuple:
    """Phases that have their completion marker but not their ``.done``."""
    d = Path(progress_dir_path)
    try:
        return tuple(
            sorted(
                phase
                for phase, sub in COMPLETION_SIGNALS.items()
                if (d / f"{phase}.{sub}.done").is_file()
                and not (d / f"{phase}.done").is_file()
            )
        )
    except OSError:
        return ()


def next_streak(
    progressed: bool,
    repair_phases: tuple,
    streak: int,
    repairs_used: int,
    allowance: int,
) -> tuple:
    """Advance the no-progress streak; returns (streak, repairs_used, outcome)."""
    if progressed:
        return (0, 0, "progress")
    if repair_phases and repairs_used < allowance:
        return (streak, repairs_used + 1, "repair")
    return (streak + 1, repairs_used, "stall")


def abort_reason(streak: int, repair_phases: tuple) -> Optional[str]:
    """Abort reason once the streak reaches two, else None."""
    if streak < 2:
        return None
    if not repair_phases:
        return "no forward progress"
    return "phase completion not repaired: " + ", ".join(repair_phases)


# Inherited repository-selection variables override `git -C`, so the probe drops them.
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_PARAMETERS", "GIT_CEILING_DIRECTORIES")


def probe_task_heads(
    task: str,
    project_root: PathLike,
    *,
    budget_secs: float = HEAD_PROBE_BUDGET_SECS,
    runner: Optional[Callable[..., object]] = None,
    clock: Optional[Callable[[], float]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple:
    """Snapshot of ``(repo_key, sha)`` for repos currently on the task branch.

    Candidates are directories holding their own ``.git`` entry: the project
    root, else its immediate children. Only repos whose current branch equals
    the task name or ends with ``/<task>`` are included, so a branch switch
    drops a repo from the snapshot instead of reading as a commit. Any
    failure yields ``()``; with no candidate no process is spawned.
    """
    try:
        env_in = os.environ if environ is None else environ
        if not head_probe_enabled(env_in) or not _TASK_NAME_RE.match(task):
            return ()
        root = Path(project_root)
        if (root / ".git").exists():
            candidates = [(".", root)]
        else:
            candidates = []
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    candidates.append((child.name, child))
            candidates = candidates[:_MAX_PROBE_CANDIDATES]
        if not candidates:
            return ()
        import subprocess  # local import — keeps module-top import-lean

        run = subprocess.run if runner is None else runner
        now = time.monotonic if clock is None else clock
        child_env = dict(env_in)
        for _name in _GIT_ENV_DROP:
            child_env.pop(_name, None)
        child_env["GIT_OPTIONAL_LOCKS"] = "0"
        child_env["GIT_TERMINAL_PROMPT"] = "0"
        deadline = now() + budget_secs
        pairs = []
        for key, directory in candidates:
            remaining = deadline - now()
            if remaining <= 0:
                return ()
            result = run(
                ["git", "-C", str(directory), "rev-parse", "HEAD", "--abbrev-ref", "HEAD"],
                timeout=max(remaining, 0.1),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=child_env,
            )
            if result.returncode != 0:
                continue
            lines = result.stdout.splitlines()
            if len(lines) < 2:
                continue
            sha, branch = lines[0].strip(), lines[1].strip()
            if not _SHA_RE.match(sha):
                continue
            if branch == task or branch.endswith("/" + task):
                pairs.append((key, sha))
        return tuple(sorted(pairs))
    except Exception:
        return ()


# ---------------------------------------------------------------------------
# Backoff + clock
# ---------------------------------------------------------------------------


def default_backoff(relaunch_number: int) -> float:
    """Exponential backoff: base 5s, doubling per relaunch, capped at 300s."""
    exponent = max(0, relaunch_number - 1)
    seconds = _BACKOFF_BASE_SECONDS * (2**exponent)
    return min(seconds, _BACKOFF_CAP_SECONDS)


class _RealClock:
    """Default clock: sleeps for real. Injected as ``clock`` in tests."""

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------


@dataclass
class SuperviseResult:
    """Terminal outcome of a :func:`supervise` run."""

    status: str  # "SUCCESS" | "HALTED" | "ABORTED"
    reason: Optional[str] = None
    relaunches: int = 0


def supervise(
    task: str,
    project_root: PathLike,
    *,
    launch_fn: Callable[[str], object],
    max_relaunch: int = DEFAULT_MAX_RELAUNCH,
    backoff_fn: Callable[[int], float] = default_backoff,
    clock: object = None,
    repair_allowance: int = DEFAULT_REPAIR_RELAUNCHES,
    progress_probe: Optional[Callable[[], tuple]] = None,
) -> SuperviseResult:
    """Run the relaunch loop for ``task`` until a terminal condition.

    Terminal conditions, checked in this order on every iteration:

    1. Done-sentinel present -> ``SUCCESS`` (0 further relaunches).
    2. Halt-sentinel present -> ``HALTED``, reason surfaced, no relaunch.
    3. ``relaunches >= max_relaunch`` -> ``ABORTED("relaunch cap")``.

    Otherwise: snapshot the completion-sentinel count and the task-branch
    HEADs, call ``launch_fn(task)``, snapshot again. A launch made progress
    when the count rose (union glob of :func:`count_completion_sentinels`) or
    a task-branch commit appeared (:func:`probe_task_heads`). Two consecutive
    launches without progress -> ``ABORTED("no forward progress")``, except
    that a phase holding its completion marker but not its ``{phase}.done``
    gets up to ``repair_allowance`` extra launches first; if that phase is
    still unrepaired the reason is ``phase completion not repaired: <phase>``.
    This loop never writes any ``.done`` file. A net DECREASE counts as non-progress too — a
    mid-flight fast-route escalation deletes `.done` sentinels as part of
    its atomic unit (see `run/SKILL.md`), and a strict `==` comparison
    would have misread that net-negative relaunch as forward progress and
    reset the streak, delaying stall detection. Any relaunch that produces
    a net INCREASE resets that streak. Otherwise increment ``relaunches``
    and sleep ``backoff_fn(relaunches)`` (via the injected ``clock``)
    before the next iteration.

    ``launch_fn`` and ``clock`` are the only side-effecting
    dependencies and are both injectable, so this loop is fully unit
    testable with a mocked launcher and a fake clock — no real
    subprocess is spawned by this function itself.
    """
    if clock is None:
        clock = _RealClock()

    if progress_probe is None:
        progress_probe = lambda: probe_task_heads(task, project_root)  # noqa: E731

    def _heads() -> tuple:
        try:
            return tuple(progress_probe())
        except Exception:
            return ()

    relaunches = 0
    zero_progress_streak = 0
    repairs_used = 0

    while True:
        if read_done(task, project_root):
            return SuperviseResult(status="SUCCESS", relaunches=relaunches)

        halt_reason = read_halt(task, project_root)
        if halt_reason is not None:
            return SuperviseResult(
                status="HALTED", reason=halt_reason, relaunches=relaunches
            )

        if relaunches >= max_relaunch:
            return SuperviseResult(
                status="ABORTED", reason="relaunch cap", relaunches=relaunches
            )

        count_before = count_completion_sentinels(task, project_root)
        heads_before = _heads()
        launch_fn(task)
        count_after = count_completion_sentinels(task, project_root)
        heads_after = _heads()
        repair = repair_pending_phases(progress_dir(task, project_root))
        zero_progress_streak, repairs_used, _ = next_streak(
            progress_made(count_before, count_after, heads_before, heads_after),
            repair,
            zero_progress_streak,
            repairs_used,
            repair_allowance,
        )
        reason = abort_reason(zero_progress_streak, repair)
        if reason is not None:
            return SuperviseResult(
                status="ABORTED", reason=reason, relaunches=relaunches
            )

        relaunches += 1
        clock.sleep(backoff_fn(relaunches))


# ---------------------------------------------------------------------------
# Headless launcher (T-07)
# ---------------------------------------------------------------------------

#: Permission mode recorded by the T-01 POC (poc-headless-decision.md): a
#: scoped `--allowedTools` allow-list cleared the first tool approval
#: unattended, while `--dangerously-skip-permissions` was blocked by this
#: machine's auto-mode classifier. `allowedTools` is therefore the correct
#: default; `bypassPermissions` remains available for operators running the
#: supervisor from a standard (non-auto-mode) context.
DEFAULT_PERMISSION_MODE = "allowedTools"

#: Tool allow-list covering everything the `/run --resume --autonomous`
#: pipeline's phases use (discover/enrich/specify/architect/thorough_plan/
#: implement/review/end_of_task), per the T-01 POC decision note. This is the
#: TOOL-PERMISSION roster, not RESUMABLE_PHASES above — `fast_path_triage`
#: (Phase 1.6) runs inline in the orchestrator's own session (never spawned
#: as a subagent), so it needs no entry here; left unchanged deliberately.
DEFAULT_ALLOWED_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "Agent",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
)

#: Timeout for one relaunch subprocess. One launch can run implement plus a
#: full gate, which takes 45-60 minutes on a slow synced file system, so the
#: default leaves headroom above that.
DEFAULT_LAUNCH_TIMEOUT_SECONDS = 5400.0
LAUNCH_TIMEOUT_ENV = "QUOIN_SUPERVISOR_LAUNCH_TIMEOUT_SECS"


@dataclass
class LaunchResult:
    """Result of one headless relaunch subprocess invocation.

    ``timed_out`` distinguishes a hard subprocess timeout from an ordinary
    non-zero exit; both are surfaced to :func:`supervise` without raising,
    so a single bad relaunch never crashes the loop.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

# ---------------------------------------------------------------------------
# Child session identity + takeover text
# ---------------------------------------------------------------------------

#: Prefix of the run-notes line that records each headless child launch.
CHILD_NOTE_PREFIX = "[quoin-autonomous-child]"

_SPACE_RUN_RE = re.compile(r" {2,}")


def is_child_session_id(s: object) -> bool:
    """True for a canonical lowercase UUID4 string (safe to pass to claude)."""
    if not isinstance(s, str):
        return False
    try:
        parsed = uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == s


def takeover_command(cwd: PathLike, sid: str) -> str:
    """Shell command that resumes the child session interactively."""
    return f"cd {shlex.quote(str(cwd))} && claude --resume {sid}"


def takeover_pointer(task: str, project_root: PathLike) -> str:
    """Command that stops the run and prints the resume command."""
    return (
        f"quoin run --takeover {task} "
        f"--project-root {shlex.quote(str(project_root))}"
    )


def takeover_hint(task: str, project_root: PathLike, sid: Optional[str] = None) -> str:
    """Pointer, prefixed with the child session id when it is valid."""
    pointer = takeover_pointer(task, project_root)
    if is_child_session_id(sid):
        return f"child_session={sid} {pointer}"
    return pointer


def _sanitize_safe(text: str) -> bool:
    """True when the run-state sanitizer would leave ``text`` unchanged."""
    if '"' in text or "\\" in text:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return False
    return _SPACE_RUN_RE.search(text) is None


def takeover_notice_pointer(task: str, project_root: PathLike) -> str:
    """Pointer for sanitized one-line surfaces (notices, notes lines).

    Falls back to the bare form when the full pointer would be rewritten by
    the sanitizer, so the line never shows an altered path.
    """
    pointer = takeover_pointer(task, project_root)
    if _sanitize_safe(pointer):
        return pointer
    return f"quoin run --takeover {task}"


def resolve_repo_root(project_root: PathLike) -> Path:
    """Resolve the real git repo root via ``git rev-parse --show-toplevel``.

    Never assumes ``project_root`` itself is a git repo (lesson 2026-07-18)
    — PROJECT_ROOT can be a plain, non-git outer folder wrapping the git
    repo (e.g. a cloud-synced workspace). Falls back to ``project_root``
    itself if git resolution fails for any reason, so callers still get a
    usable cwd.
    """
    import subprocess  # local import — keeps module-top import-lean (D-01)

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            top = result.stdout.strip()
            if top:
                return Path(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(project_root)


def build_relaunch_argv(
    task: str,
    *,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    allowed_tools: "tuple[str, ...]" = DEFAULT_ALLOWED_TOOLS,
    session_id: Optional[str] = None,
) -> "list[str]":
    """Build the argv for the headless relaunch subprocess (T-07).

    The prompt string carries BOTH ``--resume`` and ``--autonomous`` (D-06:
    belt-and-suspenders alongside the marker-read in `/run --resume`, so a
    relaunch never reverts to interactive). Permission mode defaults to the
    T-01 POC's scoped ``--allowedTools`` allow-list; ``bypassPermissions``
    uses ``--dangerously-skip-permissions`` instead, for operators who
    explicitly choose that mode outside an auto-mode-restricted context.
    """
    prompt = f"/run --resume --autonomous {task}"
    argv = ["claude", "-p", prompt]
    if permission_mode == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    else:
        argv.extend(["--allowedTools", *allowed_tools])
    if session_id:
        argv.extend(["--session-id", session_id])
    argv.extend(["--output-format", "text"])
    return argv


def make_launch_fn(
    project_root: PathLike,
    *,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    allowed_tools: "tuple[str, ...]" = DEFAULT_ALLOWED_TOOLS,
    timeout: float = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> Callable[[str], LaunchResult]:
    """Build the real headless ``launch_fn`` for :func:`supervise` (T-07).

    Each call resolves REPO_ROOT fresh via :func:`resolve_repo_root`, runs
    the relaunch subprocess with that cwd and ``stdin`` redirected from
    ``/dev/null`` (headless print mode otherwise waits ~3s for stdin — POC
    Probe A2), and captures exit code + stdout/stderr. A non-zero exit or a
    subprocess timeout is surfaced as a :class:`LaunchResult` rather than
    raised, so a single bad relaunch never crashes :func:`supervise`'s loop.
    """
    import subprocess  # local import — keeps module-top import-lean (D-01)

    def _launch(
        task: str,
        *,
        session_id: Optional[str] = None,
        cwd: Optional[PathLike] = None,
        on_spawn: Optional[Callable[[int], object]] = None,
    ) -> LaunchResult:
        repo_root = Path(cwd) if cwd is not None else resolve_repo_root(project_root)
        argv = build_relaunch_argv(
            task,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            session_id=session_id,
        )
        if on_spawn is not None:
            return _launch_with_spawn_hook(
                subprocess, argv, repo_root, timeout, on_spawn, env
            )
        try:
            run_kwargs: "dict[str, object]" = {}
            if env is not None:
                run_kwargs["env"] = dict(env)
            proc = subprocess.run(
                argv,
                cwd=str(repo_root),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                **run_kwargs,
            )
            return LaunchResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return LaunchResult(
                returncode=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=((exc.stderr or "") if isinstance(exc.stderr, str) else "")
                + "\n[launch timed out]",
                timed_out=True,
            )
        except OSError as exc:
            # e.g. `claude` binary not found on PATH — surface, don't crash.
            return LaunchResult(returncode=-1, stderr=str(exc))

    #: Tells the tracking wrapper this launcher accepts session_id/cwd/on_spawn.
    _launch.supports_child_tracking = True  # type: ignore[attr-defined]
    return _launch


def _launch_with_spawn_hook(
    subprocess: object,
    argv: "list[str]",
    repo_root: Path,
    timeout: float,
    on_spawn: Callable[[int], object],
    env: Optional[Mapping[str, str]] = None,
) -> LaunchResult:
    """Popen-based launch that reports the child pid right after spawn.

    Mirrors the ``subprocess.run`` path's result shape. On timeout the child
    is killed and reaped with a bounded wait; there is deliberately no second
    ``communicate()`` because grandchildren holding the pipes open would make
    it block forever.
    """
    popen_kwargs: "dict[str, object]" = {}
    if env is not None:
        popen_kwargs["env"] = dict(env)
    try:
        proc = subprocess.Popen(  # type: ignore[attr-defined]
            argv,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,  # type: ignore[attr-defined]
            stdout=subprocess.PIPE,  # type: ignore[attr-defined]
            stderr=subprocess.PIPE,  # type: ignore[attr-defined]
            text=True,
            **popen_kwargs,
        )
    except OSError as exc:
        return LaunchResult(returncode=-1, stderr=str(exc))
    try:
        # A failing hook must not abort the launch; BaseException (a signal
        # handler's SystemExit) still unwinds through the outer handler.
        try:
            on_spawn(proc.pid)
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:  # type: ignore[attr-defined]
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            out_text = exc.stdout if isinstance(exc.stdout, str) else ""
            err_text = exc.stderr if isinstance(exc.stderr, str) else ""
            return LaunchResult(
                returncode=-1,
                stdout=out_text or "",
                stderr=(err_text or "") + "\n[launch timed out]",
                timed_out=True,
            )
        return LaunchResult(returncode=proc.returncode, stdout=out, stderr=err)
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Tracked launch: known child session id per launch
# ---------------------------------------------------------------------------


@dataclass
class ChildLaunch:
    """Identity of one headless child launch."""

    session_id: str
    cwd: str
    launch_no: int
    started_at: str


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_tracked_launch_fn(
    task: str,
    project_root: PathLike,
    base_launch: Callable[..., object],
    *,
    first_session_id: Optional[str] = None,
    id_fn: Optional[Callable[[], str]] = None,
    repo_root_fn: Callable[[PathLike], Path] = resolve_repo_root,
    record_fn: Optional[Callable[[Optional[ChildLaunch], bool], object]] = None,
    on_pid_fn: Optional[Callable[[str, int], object]] = None,
    halt_fn: Callable[[str, PathLike], Optional[str]] = read_halt,
    now_fn: Optional[Callable[[], str]] = None,
) -> Callable[[str], object]:
    """Wrap ``base_launch`` so every child gets a known session id.

    The id and cwd are recorded (``record_fn(entry, True)``) BEFORE the child
    spawns, so a takeover can always find it. A halt sentinel that appears
    between the record and the spawn reverts the record
    (``record_fn(previous, False)``) and skips the launch. Launchers that do
    not advertise ``supports_child_tracking`` are called with just ``(task,)``.
    """
    if id_fn is None:
        id_fn = lambda: str(uuid.uuid4())  # noqa: E731
    if now_fn is None:
        now_fn = _utc_now
    state = {"first_used": False}

    def _safe(fn: Optional[Callable[..., object]], *args: object) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            pass

    def _skipped() -> LaunchResult:
        launch_fn.skipped += 1  # type: ignore[attr-defined]
        return LaunchResult(returncode=-1, stderr="[launch skipped: halted]")

    def launch_fn(t: str) -> object:
        if not getattr(base_launch, "supports_child_tracking", False):
            return base_launch(t)
        if halt_fn(task, project_root) is not None:
            return _skipped()
        if not state["first_used"] and is_child_session_id(first_session_id):
            sid = str(first_session_id)
        else:
            sid = id_fn()
        state["first_used"] = True
        cwd = str(repo_root_fn(project_root))
        n = launch_fn.launches + 1  # type: ignore[attr-defined]
        entry = ChildLaunch(sid, cwd, n, now_fn())
        prev = launch_fn.last  # type: ignore[attr-defined]
        _safe(record_fn, entry, True)
        launch_fn.last = entry  # type: ignore[attr-defined]
        if halt_fn(task, project_root) is not None:
            _safe(record_fn, prev, False)
            launch_fn.last = prev  # type: ignore[attr-defined]
            return _skipped()
        launch_fn.launches = n  # type: ignore[attr-defined]
        return base_launch(
            t,
            session_id=sid,
            cwd=cwd,
            on_spawn=lambda pid: _safe(on_pid_fn, sid, pid),
        )

    launch_fn.last = None  # type: ignore[attr-defined]
    launch_fn.skipped = 0  # type: ignore[attr-defined]
    launch_fn.launches = 0  # type: ignore[attr-defined]
    return launch_fn
