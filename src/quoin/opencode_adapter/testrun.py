"""Run a task's configured test command against a throwaway copy of the tree.

The command and the include list are fixed in the task's workflow state by a
human-side `configure` call; nothing a model can pass on a command line changes
them. `run_tests` builds a detached git worktree per repository at the current
HEAD, overlays the working-tree changes (tracked edits and untracked files,
never `.env*` files), links the configured include directories, runs the
command there with a scrubbed environment, removes the worktrees and then
checks that the real repositories did not change. The result is written outside
the project, under the adapter state directory, stamped with the run id and
attempt number of the phase run that started it, so a project-confined agent
cannot forge one by writing a file.

The module never takes the task lock: it runs inside a phase run that holds it.
Its own busy file keeps two test runs of the same task from overlapping.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import jsonio, launch_env, paths, runstore
from .errors import ConfigErrors

DEFAULT_TIMEOUT_S = 600
DEFAULT_OUTPUT_CAP = 1024 * 1024
TAIL_BYTES = 4096
GIT_TIMEOUT_S = 120.0
RESULT_SCHEMA_VERSION = 1
BUSY_NAME = ".busy"
ROOT_STAGE = "root"

PASSED = "PASSED"
FAILED = "FAILED"
REFUSED = "REFUSED"


_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


def _clean_tail(raw: bytes) -> str:
    """Output text with control characters (except newline and tab) replaced,
    so it is safe to print or store."""
    return _CONTROL_RE.sub("?", raw.decode("utf-8", "replace"))


class TestRunError(Exception):
    """A configuration or request problem; `code` is a stable short id."""

    __test__ = False

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class TestRun:
    __test__ = False

    outcome: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    output_sha256: Optional[str] = None
    output_tail: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    run_id: Optional[str] = None
    attempt: Optional[int] = None
    elapsed_s: Optional[float] = None
    workspace_removed: Optional[bool] = None
    real_tree: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _git(args: Sequence[str], cwd: str, *, timeout_s: float = GIT_TIMEOUT_S,
         stdin: Optional[bytes] = None) -> Tuple[int, bytes]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", "-C", cwd, *args], env=env, input=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s, check=False,
    )
    return proc.returncode, proc.stdout


def _repo_holding(root: str, rel: str) -> str:
    """The nearest directory at or above `root/rel` (never above `root`) that
    holds a `.git` entry; the project root when there is none."""
    current = os.path.dirname(os.path.join(root, rel))
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        if current == root or os.path.dirname(current) == current:
            return root
        current = os.path.dirname(current)


def _check_include(root: str, item: Any) -> str:
    if not isinstance(item, str) or not item or "\0" in item or os.path.isabs(item):
        raise TestRunError("include-invalid", "each include must be a relative path")
    parts = item.replace("\\", "/").split("/")
    if any(part in ("..",) for part in parts):
        raise TestRunError("include-invalid", "an include path must not contain '..'")
    rel = os.path.normpath(item)
    if rel == "." or rel.startswith(".."):
        raise TestRunError("include-invalid", "an include path must name a subdirectory")
    full = os.path.join(root, rel)
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if not os.path.isdir(full) or not (real == real_root or real.startswith(real_root + os.sep)):
        raise TestRunError("include-invalid", "an include path must be a directory inside the project")
    holder = _repo_holding(root, rel)
    inner = os.path.relpath(full, holder)
    try:
        code, _ = _git(("check-ignore", "-q", "--", inner), holder)
    except (OSError, subprocess.SubprocessError):
        raise TestRunError("include-invalid", "git is not available to check the include path") from None
    if code != 0:
        raise TestRunError("include-not-ignored", "an include path must be ignored by git")
    return rel.replace(os.sep, "/")


def configure(
    project_root, task: str, *, command: Sequence[str], include: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S, clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Record the test command, include paths and timeout in the task's
    workflow state and return the stored settings. The caller holds the task
    lock. Raises `TestRunError` for a bad value."""
    if (
        isinstance(command, (str, bytes)) or not isinstance(command, (list, tuple)) or not command
        or any(not isinstance(item, str) or not item or "\0" in item for item in command)
    ):
        raise TestRunError("command-invalid", "the command must be a non-empty list of non-empty strings")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
        raise TestRunError("timeout-invalid", "the timeout must be a positive number of seconds")
    if isinstance(include, (str, bytes)):
        raise TestRunError("include-invalid", "include must be a list of paths")
    root = os.path.realpath(str(project_root))
    cleaned = [_check_include(root, item) for item in include]
    directory = runstore.store_dir(project_root, create=True)
    state = runstore.load_workflow_state(directory, task) or runstore.new_workflow_state(task, clock)
    settings = state["settings"]
    settings["test_command"] = list(command)
    settings["test_include"] = cleaned
    settings["test_timeout_s"] = timeout_s
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    runstore.write_workflow_state(directory, state)
    return dict(settings)


# ---------------------------------------------------------------------------
# environment and locations
# ---------------------------------------------------------------------------


def command_env(environ: Mapping[str, str]) -> Dict[str, str]:
    """The environment the configured command runs with.

    When the launcher's state-directory variable is present the caller is an
    agent shell built by the launcher, so only names the launcher itself lets
    through ambiently are kept; that drops the launcher's own variables, any
    provider credential and the proxy settings. Otherwise only the launcher's
    own names are removed. The state-directory variable never reaches the
    command either way."""
    launched = paths.ENV_STATE_DIR in environ
    out: Dict[str, str] = {}
    for name, value in environ.items():
        if name in launch_env.LAUNCHER_ENV_NAMES:
            continue
        if launched and not launch_env.allowed_ambient(name):
            continue
        out[name] = value
    return out


def _stage_name(stage: Optional[int]) -> str:
    return ROOT_STAGE if stage is None else str(runstore.normalize_stage(stage))


def result_dir(state_root, project_root, task: str) -> Path:
    return Path(state_root) / "tests" / paths.project_key(Path(project_root)) / runstore.check_task_name(task)


def result_path(state_root, project_root, task: str, stage: Optional[int]) -> Path:
    """Where the latest result for a task and stage lives: outside the project,
    under the adapter state directory."""
    return result_dir(state_root, project_root, task) / ("%s-latest.json" % _stage_name(stage))


def _private_dirs(base: Path, *parts: str) -> Path:
    current = Path(base)
    for part in parts:
        current = current / part
        try:
            os.mkdir(str(current), 0o700)
        except FileExistsError:
            pass
        info = os.lstat(str(current))
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise TestRunError("state-dir-unsafe", "the test state directory is not a plain directory of this user")
        if info.st_mode & 0o077:
            os.chmod(str(current), 0o700)
    return current


def _ensure_state_root(state_root: Path) -> None:
    missing = []
    probe = Path(state_root)
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for item in reversed(missing):
        os.mkdir(str(item), 0o700)


# ---------------------------------------------------------------------------
# run identity
# ---------------------------------------------------------------------------


def _running_identity(project_root, task: str) -> Tuple[Optional[str], Optional[int]]:
    """The run id of the task's pointer when that run is `running`, and the
    highest attempt number of it that is itself `running`; `(None, None)`
    when there is no such run."""
    try:
        directory = runstore.inspect_store(project_root)
        if directory is None:
            return None, None
        pointer = runstore.load_pointer(directory, task)
        if not pointer:
            return None, None
        record = runstore.load_record(directory, pointer["run_id"])
    except (runstore.RunStoreError, ConfigErrors, OSError, KeyError, ValueError):
        return None, None
    if not record or record.get("state") != "running":
        return None, None
    numbers = [
        item.get("attempt") for item in record.get("attempts") or []
        if isinstance(item, dict) and item.get("state") == "running"
        and isinstance(item.get("attempt"), int) and not isinstance(item.get("attempt"), bool)
    ]
    return str(record["run_id"]), (max(numbers) if numbers else None)


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


@dataclass
class _Repo:
    path: str  # absolute, real
    rel: str  # relative to the project root, "." or a ".."-prefixed path for the holding repository
    is_top: bool


def _list_repos(project_root: str) -> List[_Repo]:
    entries = runstore.repo_revisions(project_root)
    repos: List[_Repo] = []
    for entry in entries:
        rel = entry["path"]
        if entry.get("error") or not entry.get("head"):
            raise TestRunError("repo-unreadable", "the repository at %s cannot be read" % rel)
        full = os.path.normpath(os.path.join(project_root, rel))
        is_top = project_root == full or project_root.startswith(full + os.sep)
        repos.append(_Repo(full, rel, is_top))
    return repos


def _ref_digest(repo: str) -> Optional[str]:
    try:
        code, out = _git(("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"), repo)
    except (OSError, subprocess.SubprocessError):
        return None
    return hashlib.sha256(out).hexdigest() if code == 0 else None


def _state_of(project_root: str, repos: Sequence[_Repo]) -> List[Dict[str, Any]]:
    by_path = {item["path"]: item for item in runstore.repo_revisions(project_root, source=True)}
    out = []
    for repo in repos:
        item = by_path.get(repo.rel) or {}
        out.append({
            "path": repo.rel, "head": item.get("head"), "source_dirty": item.get("source_dirty"),
            "source_digest": item.get("source_digest"), "refs": _ref_digest(repo.path),
        })
    return out


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def _overlay(repo: _Repo, worktree: str, project_root: str, repos: Sequence[_Repo]) -> None:
    specs = runstore.source_pathspecs(repo.path, project_root, [r.path for r in repos])
    code, diff = _git(("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                       "HEAD", "--", *specs), repo.path)
    if code != 0:
        raise TestRunError("tests-workspace-failed", "the working-tree changes could not be read")
    if diff.strip():
        code, _ = _git(("apply", "--binary", "--whitespace=nowarn", "-"), worktree, stdin=diff)
        if code != 0:
            raise TestRunError("tests-workspace-failed", "the working-tree changes could not be applied")
    code, listing = _git(("ls-files", "-z", "--others", "--exclude-standard", "--", *specs), repo.path)
    if code != 0:
        raise TestRunError("tests-workspace-failed", "the untracked files could not be listed")
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        if os.path.basename(name.rstrip("/")).startswith(".env"):
            continue
        source = os.path.join(repo.path, name)
        target = os.path.join(worktree, name)
        try:
            info = os.lstat(source)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(source), target)
        elif stat.S_ISREG(info.st_mode):
            shutil.copy2(source, target)


def _link_includes(project_root: str, effective_root: str, include: Sequence[str]) -> List[str]:
    links = []
    for rel in include:
        source = os.path.realpath(os.path.join(project_root, rel))
        target = os.path.join(effective_root, rel)
        if os.path.lexists(target) or not os.path.isdir(source):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.symlink(source, target)
        links.append(target)
    return links


def _build_workspace(
    base: str, project_root: str, repos: Sequence[_Repo], include: Sequence[str], made: List[Tuple[_Repo, str]],
    links: List[str],
) -> str:
    top = next((item for item in repos if item.is_top), None)
    if top is not None:
        effective = os.path.normpath(os.path.join(base, os.path.relpath(project_root, top.path)))
    else:
        os.mkdir(base, 0o700)
        effective = base
    for repo in sorted(repos, key=lambda item: not item.is_top):
        if repo.is_top:
            target = base
        else:
            target = os.path.normpath(os.path.join(effective, repo.rel))
            os.makedirs(os.path.dirname(target), exist_ok=True)
        code, _ = _git(("worktree", "add", "--detach", target, "HEAD"), repo.path)
        if code != 0:
            raise TestRunError("tests-workspace-failed", "a worktree could not be created for %s" % repo.rel)
        made.append((repo, target))
        _overlay(repo, target, project_root, repos)
    os.makedirs(effective, exist_ok=True)
    links.extend(_link_includes(project_root, effective, include))
    return effective


def _remove_workspace(base: str, made: Sequence[Tuple[_Repo, str]], links: Sequence[str]) -> bool:
    for link in links:
        try:
            if os.path.islink(link):
                os.unlink(link)
        except OSError:
            pass
    for repo, target in reversed(list(made)):
        try:
            code, _ = _git(("worktree", "remove", "--force", target), repo.path)
        except (OSError, subprocess.SubprocessError):
            code = 1
        if code != 0:
            try:
                _git(("worktree", "prune"), repo.path)
            except (OSError, subprocess.SubprocessError):
                pass
            shutil.rmtree(target, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)
    return not os.path.lexists(base)


# ---------------------------------------------------------------------------
# running the command
# ---------------------------------------------------------------------------


def _execute(command: Sequence[str], cwd: str, env: Mapping[str, str], timeout_s: float,
             cap: int) -> Tuple[Optional[int], bool, str, str]:
    """`(exit code, timed out, output sha256, output tail text)`."""
    proc = subprocess.Popen(
        list(command), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    fired = threading.Event()

    def kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass

    def expire() -> None:
        fired.set()
        kill_group()

    timer = threading.Timer(max(timeout_s, 0.0), expire)
    timer.daemon = True
    timer.start()
    digest = hashlib.sha256()
    hashed = 0
    tail = b""
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            if hashed < cap:
                part = chunk[: cap - hashed]
                digest.update(part)
                hashed += len(part)
            tail = (tail + chunk)[-TAIL_BYTES:]
        proc.wait()
    finally:
        timer.cancel()
        kill_group()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    return proc.returncode, fired.is_set(), digest.hexdigest(), _clean_tail(tail)


def _stamp(clock: Callable[[], float]) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def _take_busy(directory: Path) -> Path:
    busy = directory / BUSY_NAME
    for _ in range(2):
        try:
            fd = os.open(str(busy), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                pid = int((busy.read_text() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except PermissionError:
                    alive = True
                except OSError:
                    alive = False
            if alive:
                raise TestRunError("test-run-busy", "another test run of this task is in progress") from None
            try:
                os.unlink(str(busy))
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write("%d\n" % os.getpid())
        return busy
    raise TestRunError("test-run-busy", "another test run of this task is in progress")


def run_tests(
    project_root, task: str, stage: Optional[int] = None, *, state_root,
    clock: Callable[[], float] = time.time, output_cap: int = DEFAULT_OUTPUT_CAP,
) -> TestRun:
    """Run the task's configured test command in a throwaway workspace and
    write the result under `state_root`. A request that cannot start returns
    outcome `REFUSED` with the reason and writes nothing."""
    root = os.path.realpath(str(project_root))
    try:
        runstore.check_task_name(task)
        stage_value = runstore.normalize_stage(stage)
    except (runstore.RunStoreError, ValueError):
        return TestRun(outcome=REFUSED, reason="request-invalid")
    try:
        directory = runstore.inspect_store(root)
        state = runstore.load_workflow_state(directory, task) if directory is not None else None
    except (runstore.RunStoreError, ConfigErrors, OSError):
        return TestRun(outcome=REFUSED, reason="state-unreadable")
    settings = (state or {}).get("settings") or {}
    command = settings.get("test_command")
    if not isinstance(command, list) or not command or any(not isinstance(i, str) for i in command):
        return TestRun(outcome=REFUSED, reason="tests-not-configured")
    include = [i for i in settings.get("test_include") or [] if isinstance(i, str)]
    timeout_s = settings.get("test_timeout_s")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s > 0:
        timeout_s = DEFAULT_TIMEOUT_S

    try:
        _ensure_state_root(Path(state_root))
        key = paths.project_key(Path(root))
        tests_dir = _private_dirs(Path(state_root), "tests", key)
        task_dir = _private_dirs(tests_dir, task)
        work_dir = _private_dirs(tests_dir, "work")
        busy = _take_busy(task_dir)
    except TestRunError as exc:
        return TestRun(outcome=REFUSED, reason=exc.code)
    except OSError:
        return TestRun(outcome=REFUSED, reason="state-dir-unsafe")

    base = str(work_dir / ("%s-%s" % (os.getpid(), hashlib.sha256(os.urandom(16)).hexdigest()[:10])))
    made: List[Tuple[_Repo, str]] = []
    links: List[str] = []
    result: Optional[TestRun] = None
    try:
        run_id, attempt = _running_identity(root, task)
        started = clock()
        try:
            repos = _list_repos(root)
        except TestRunError as exc:
            return TestRun(outcome=REFUSED, reason=exc.code)
        before = _state_of(root, repos)
        exit_code: Optional[int] = None
        timed_out = False
        sha: Optional[str] = None
        tail = ""
        failure: Optional[str] = None
        try:
            effective = _build_workspace(base, root, repos, include, made, links)
            began = time.monotonic()
            exit_code, timed_out, sha, tail = _execute(
                command, effective, command_env(os.environ), float(timeout_s), output_cap,
            )
            elapsed = time.monotonic() - began
        except TestRunError as exc:
            failure = exc.code
            elapsed = 0.0
        except (OSError, subprocess.SubprocessError):
            failure = "tests-workspace-failed"
            elapsed = 0.0
        removed = _remove_workspace(base, made, links)
        after = _state_of(root, repos)
        changed = after != before
        if failure is not None:
            outcome, reason = FAILED, failure
        elif changed:
            outcome, reason = FAILED, "tests-real-tree-changed"
        elif timed_out:
            outcome, reason = FAILED, "tests-timeout"
        elif exit_code != 0:
            outcome, reason = FAILED, "tests-failed"
        else:
            outcome, reason = PASSED, None
        result = TestRun(
            outcome=outcome, exit_code=exit_code, timed_out=timed_out, output_sha256=sha, output_tail=tail,
            started_at=_stamp(lambda: started), finished_at=_stamp(clock), run_id=run_id, attempt=attempt,
            elapsed_s=round(elapsed, 3), workspace_removed=removed,
            real_tree="changed" if changed else "unchanged", reason=reason,
        )
        payload = result.to_dict()
        payload.update(schema_version=RESULT_SCHEMA_VERSION, task=task, stage=stage_value)
        data = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        jsonio.write_private_atomic(result_path(state_root, root, task, stage_value), data)
        return result
    finally:
        if result is None:
            _remove_workspace(base, made, links)
        try:
            os.unlink(str(busy))
        except OSError:
            pass
