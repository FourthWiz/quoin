"""
run_three_arm_gate.py — The sequential three-arm benchmark gate driver (T-07).

Runs the `raw`, `main`, `candidate` arms in that fixed order (architecture's
R-07: the harness reinstalls into the single global ~/.claude, so parallel
arms would cross-contaminate), inside `try: ... finally: teardown()`, with
SIGINT/SIGTERM trapped to raise so teardown runs on operator abort too.

Three modes:
  --plan-only  — spend-free. Prints the arm sequence and every preflight
                 verdict; invokes nothing. Not a rehearsal — it never
                 executes the real `claude` command.
  --rehearsal  — a real, cheap, paid invocation (T-16). Skips the
                 rehearsal-record precondition (circular otherwise) and
                 `--verify-model` (requires QUOIN_BENCH_CLAUDE_MODEL set
                 instead, recorded as the override).
  (default)    — full mode. Every precondition applies, including a green
                 rehearsal record.

The driver always exports QUOIN_BENCHMARK_GATE=1 into each arm's
environment — that is what arms T-02's cell-side install/commit/budget
refusals (D-08).

How an arm is invoked (round-5 fix, MIN-7): the driver SHELLS
run_benchmark.py as a SUBPROCESS with env=arm_env. An in-process call would
ignore arm_env entirely, since run_benchmark() carries no env parameter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import datetime
import hashlib
from pathlib import Path
from typing import Optional

# Path bootstrap, mirroring run_benchmark.py.
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
for _p in (str(_repo_root / "src"), str(_repo_root)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import quoin as _q
    _inner = str(_repo_root / "quoin")
    if _inner not in _q.__path__:
        _q.__path__.append(_inner)
except Exception:
    pass

from quoin.benchmarks.scripts import spend_ledger  # noqa: E402

ARMS = ("raw", "main", "candidate")

# Matches `verify_model`'s own default in run_benchmark.py — kept as an
# explicit constant here so the live model probe's cap can be folded into
# the ledger precheck's `planned` figure (T-04c) rather than spending
# outside what the precheck actually adds up.
MODEL_PROBE_MAX_BUDGET_USD = 1.0

DRIFT_CATEGORIES = ("skills", "scripts", "core-scripts", "core-workflow", "memory")
HOOK_SCRIPTS = (
    "_lib.sh", "userpromptsubmit.sh", "precompact.sh", "postcompact.sh",
    "sessionstart.sh", "sessionend.sh", "worktreecreate.sh",
)
CATEGORY_SUBDIRS = {
    "skills": "quoin/skills",
    "scripts": "quoin/scripts",
    "core-scripts": "quoin/core/scripts",
    "core-workflow": "quoin/core/workflow",
    "memory": "quoin/memory",
}

INSTALL_PY_CANDIDATES = ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a same-directory `.tmp` file plus
    `os.replace` — the destination is never observed half-written, and a
    crash mid-write leaves the ORIGINAL file intact rather than truncated
    or unparseable JSON. Used for every rewrite of the operator's
    `~/.claude/settings.json`, which this driver otherwise truncates and
    rewrites twice per arm."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


class GateStop(RuntimeError):
    """A named, fatal precondition failure. Callers print str(exc) and exit 2."""


def _install_py() -> Optional[str]:
    """Resolve the interpreter install.sh's own rule would select: the
    first of python3.13..python3.10/python3/python reporting version >=
    3.10 (install.sh:166-177)."""
    for candidate in INSTALL_PY_CANDIDATES:
        path = shutil.which(candidate)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, "-c", "import sys;print(sys.version_info[0]*1000+sys.version_info[1])"],
                capture_output=True, text=True, timeout=10,
            )
            version = int(result.stdout.strip() or "0")
        except Exception:
            version = 0
        if version >= 3010:
            return path
    return None


def _sha256_of_tree(root: Path, subdir: str) -> dict[str, str]:
    """sha256 of every file under {root}/{subdir}, keyed by relative path."""
    out: dict[str, str] = {}
    base = root / subdir
    if not base.exists():
        return out
    for path in sorted(base.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def cross_arm_manifest(arm_root: Path) -> dict[str, str]:
    """sha256 of the five DRIFT_CATEGORIES plus the seven hook scripts, for
    one arm's `quoin/` tree — the byte-diff manifest D-02/D-13/D-17 assert
    is non-empty between main and candidate."""
    manifest: dict[str, str] = {}
    for category, subdir in CATEGORY_SUBDIRS.items():
        manifest.update(_sha256_of_tree(arm_root, subdir))
    for fname in HOOK_SCRIPTS:
        hook_path = arm_root / "quoin" / "hooks" / fname
        if hook_path.exists():
            manifest[f"quoin/hooks/{fname}"] = hashlib.sha256(hook_path.read_bytes()).hexdigest()
    claude_md = arm_root / "quoin" / "CLAUDE.md"
    if claude_md.exists():
        manifest["quoin/CLAUDE.md"] = hashlib.sha256(claude_md.read_bytes()).hexdigest()
    return manifest


_STANZA_RE = re.compile(
    r'_append_stanza\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*f?"[^"]*\{hooks_dir\}/([^"]+)"'
)


def arm_registered_stanzas(arm_root: Path) -> set[tuple[str, str, str]]:
    """The (event, matcher, script-basename) triples THIS arm's own
    installer registers — read from that arm's own installer.py source
    (D-17). Deliberately NOT `installer._QUOIN_HOOK_BASENAMES`: verified it
    omits `worktreecreate.sh`, so that stanza would go un-wiped."""
    installer_path = arm_root / "src" / "quoin" / "installer.py"
    if not installer_path.exists():
        return set()
    text = installer_path.read_text(encoding="utf-8")
    return {(event, matcher, basename) for event, matcher, basename in _STANZA_RE.findall(text)}


def wipe_arm_stanzas(settings_path: Path, owned: set[tuple[str, str, str]]) -> tuple[list, list]:
    """Delete only the stanzas THIS arm's own installer registers, matched
    CONJUNCTIVELY on (event, matcher, basename) AND a `~/.claude/hooks/`
    command path (D-17, round-5 fix MAJ-2 — a path-only predicate also
    matches non-quoin scripts that happen to live in that directory).

    Returns (before, after) — the settings.json `hooks` dict's stanza
    lists, for evidence recording.
    """
    if not settings_path.exists():
        return ([], [])
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return ([], [])
    hooks = settings.get("hooks", {})
    before = json.loads(json.dumps(hooks))

    owned_by_event: dict[str, set[tuple[str, str]]] = {}
    for event, matcher, basename in owned:
        owned_by_event.setdefault(event, set()).add((matcher, basename))

    for event, entries in list(hooks.items()):
        kept = []
        for entry in entries:
            matcher = entry.get("matcher", "")
            matches_owned = False
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                basename = command.rsplit("/", 1)[-1]
                if (
                    (matcher, basename) in owned_by_event.get(event, set())
                    and "/.claude/hooks/" in command
                ):
                    matches_owned = True
            if not matches_owned:
                kept.append(entry)
        hooks[event] = kept

    settings["hooks"] = hooks
    _atomic_write_bytes(settings_path, (json.dumps(settings, indent=2) + "\n").encode("utf-8"))
    return (before, hooks)


def install_arm(arm_root: Path, venv_python: str) -> subprocess.CompletedProcess:
    """The D-12 arm-pinned install: PYTHONPATH={arm}/src {venv_python} -m
    quoin install --source-dir {arm}/quoin --scope user. NEVER `bash
    install.sh` here — for the main arm that would take tier 3 and
    pip-install a temp worktree (D-12, R-14)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(arm_root / "src")
    return subprocess.run(
        [venv_python, "-m", "quoin", "install", "--source-dir", str(arm_root / "quoin"), "--scope", "user"],
        capture_output=True, text=True, timeout=300, env=env,
    )


def verify_arm_installer_isolable(arm_root: Path, venv_python: str) -> bool:
    """Step 0's installer-mechanism assertion (D-12): PYTHONPATH={arm}/src
    {venv_python} -c "import quoin;print(quoin.__file__)" prints a path
    under {arm}/."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(arm_root / "src")
    try:
        result = subprocess.run(
            [venv_python, "-c", "import quoin;print(quoin.__file__)"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    resolved = Path(result.stdout.strip()).resolve()
    try:
        resolved.relative_to(arm_root.resolve())
        return True
    except ValueError:
        return False


def bare_import_quoin_file(venv_python: str) -> Optional[str]:
    """`{venv_python} -c "import quoin;print(quoin.__file__)"` with NO
    `PYTHONPATH` override — what a plain, unmodified import resolves to on
    this machine right now. Every arm-identity check elsewhere in this
    module deliberately SETS `PYTHONPATH={arm}/src`; this is the one call
    that deliberately does not, because its job is to prove the machine's
    default resolution — not any arm's — is what teardown leaves behind
    (R-14)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            [venv_python, "-c", "import quoin;print(quoin.__file__)"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def worktree_head(root: Path) -> Optional[str]:
    """`git -C {root} rev-parse HEAD`, or `None` on any failure. Used to
    assert a worktree's actual identity against an operator-supplied
    expectation (D-14) — never to derive the expectation itself."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def candidate_about_version_matches(arm_root: Path, venv_python: str) -> bool:
    """Step-0 sanity check on the candidate worktree's PYTHONPATH-based
    import isolation. Two independent assertions, not a value compared
    against itself: the regex-parsed `__version__` from the worktree's own
    `src/quoin/__about__.py` (a source-of-truth file read, no import
    machinery involved) must equal the version a FRESH subprocess reports
    when it imports `quoin` under `PYTHONPATH={arm}/src` — AND that same
    subprocess's `quoin.__file__` must resolve to a path INSIDE
    `arm_root`. Version equality alone would pass even when the import
    silently resolved to a completely different `quoin` installation that
    happened to declare the same version string (e.g. this venv's own
    editable-installed package shadowing the arm's PYTHONPATH entry); the
    file-path assertion is what actually proves the import came from this
    worktree, not merely that it named the right version.

    `quoin install` deploys skills/scripts/memory to `~/.claude`, not the
    Python package itself, so there is no separately-deployed version
    artifact to compare against — this checks that the arm-pinned import
    mechanism resolves to the arm's own tree, which is the property that
    actually matters here.
    """
    about_path = arm_root / "src" / "quoin" / "__about__.py"
    if not about_path.exists():
        return False
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', about_path.read_text(encoding="utf-8"))
    if not match:
        return False
    expected_version = match.group(1)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(arm_root / "src")
    try:
        result = subprocess.run(
            [venv_python, "-c", "import quoin; print(quoin.__version__); print(quoin.__file__)"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    lines = result.stdout.strip().splitlines()
    if len(lines) != 2:
        return False
    reported_version, reported_file = lines
    if reported_version != expected_version:
        return False
    try:
        Path(reported_file).resolve().relative_to(arm_root.resolve())
    except ValueError:
        return False
    return True


def suite_sha256(suite_path: Path) -> str:
    """sha256 of the suite file's bytes, for the "frozen suite" preflight
    check — recorded and (optionally) asserted against an operator-frozen
    value, rather than trusted implicitly."""
    return hashlib.sha256(suite_path.read_bytes()).hexdigest()


def verify_arm_deployed(arm_root: Path, project_root: Path) -> bool:
    """D-02's two-part verification: deploy_drift_check.py exits 0 with an
    empty drift list. The hooks byte-compare and the cross-arm digest
    assertion are separate, explicit checks (`cross_arm_manifest`,
    `verify_hooks_deployed`) — this function covers the drift-categories
    half only.
    """
    drift_script = arm_root / "quoin" / "scripts" / "deploy_drift_check.py"
    if not drift_script.exists():
        return False
    try:
        result = subprocess.run(
            [
                sys.executable, str(drift_script),
                "--project-root", str(project_root),
                "--source-dir", str(arm_root / "quoin"),
                "--scope", "user", "--no-scope-check", "--format", "json",
            ],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout)
    except Exception:
        return False
    return result.returncode == 0 and data.get("drift") == []


def _load_arm_installer(arm_root: Path):
    """Load `{arm_root}/src/quoin/installer.py` as its own module, by file
    path — never via `importlib.import_module("quoin.installer")`. The
    driver bootstraps its own `quoin` package at the top of this file, so
    that name is already bound in `sys.modules` before this function ever
    runs; `import_module` (and `reload`) then resolves through the ALREADY
    -IMPORTED package's `__path__`, not through a `sys.path` insert — the
    arm's own installer copy is never actually loaded, regardless of which
    arm is being checked. Loading by explicit file path sidesteps
    `sys.modules` entirely, so this always evaluates the arm's own bytes."""
    installer_path = arm_root / "src" / "quoin" / "installer.py"
    if not installer_path.exists():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("arm_installer", installer_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def verify_hooks_deployed(arm_root: Path, home: Path) -> bool:
    """The hooks byte-compare `compute_drift` does not cover: each of the
    seven hook scripts, byte-for-byte, via the same
    `expected_deployed_content` helper the installer itself uses — loaded
    from THIS arm's own `installer.py` (`_load_arm_installer`), not
    whichever `quoin.installer` happens to already be imported."""
    installer = _load_arm_installer(arm_root)
    if installer is None:
        return False
    dest_hooks = home / ".claude" / "hooks"
    for fname in HOOK_SCRIPTS:
        src = arm_root / "quoin" / "hooks" / fname
        dst = dest_hooks / fname
        if not src.exists() or not dst.exists():
            return False
        expected = installer.expected_deployed_content(src, home / ".claude")
        if dst.read_bytes() != expected:
            return False
    return True


def assert_config_root(
    arm: str, arm_env: dict, home: Path, raw_config_dir: Path, raw_isolated: bool = True,
) -> None:
    """D-15/R-19's config-root assertion, before the spawn. Raises GateStop
    on any mismatch — this is the ONLY check in the gate that can see a
    leaked CLAUDE_CONFIG_DIR at all.

    `raw_isolated=False` is the D-15 FALLBACK: the 2026-09-06 rehearsal
    proved an isolated, freshly-created CLAUDE_CONFIG_DIR does not carry
    this machine's auth (`"Not logged in · Please run /login"`), so a raw
    arm run under it produces a $0, zero-signal auth failure rather than a
    baseline. Under the fallback, raw is asserted exactly like main/
    candidate — config dir absent or resolving to the real `~/.claude` —
    and the honest label changes accordingly (see `raw_arm_note`).
    """
    resolved = arm_env.get("CLAUDE_CONFIG_DIR")
    if arm == "raw" and raw_isolated:
        expected = str(raw_config_dir)
        if resolved != expected:
            raise GateStop(
                f"GATE-STOP: arm {arm} would read config dir {resolved!r}, expected {expected!r}"
            )
    else:
        if resolved is not None and Path(resolved).resolve() != (home / ".claude").resolve():
            raise GateStop(
                f"GATE-STOP: arm {arm} would read config dir {resolved!r}, expected {home / '.claude'!r}"
            )


def build_arm_env(arm: str, raw_config_dir: Path, raw_isolated: bool = True) -> dict:
    """Per-arm environment dict (D-15, round-4 fix MAJ-2): set for `raw`
    (when isolated), explicitly popped for `main`/`candidate` (and for
    `raw` under the D-15 fallback), and NEVER mutated on the driver's own
    os.environ — both cells inherit whatever the driver holds."""
    env = os.environ.copy()
    if arm == "raw" and raw_isolated:
        env["CLAUDE_CONFIG_DIR"] = str(raw_config_dir)
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    env["QUOIN_BENCHMARK_GATE"] = "1"
    return env


def raw_arm_note(raw_isolated: bool) -> str:
    """The one-line, honest statement of what the raw arm actually is
    (D-15), for compare_arms' Notes section."""
    if raw_isolated:
        return "quoin-free floor, isolated CLAUDE_CONFIG_DIR"
    return "stock Claude prompt on a quoin-installed machine — NOT a quoin-free floor"


def _kill_process_group(pid: int) -> None:
    """SIGKILL the whole process group `pid` leads, not just `pid` itself.

    The arm's grandchild `claude` process inherits this group (it is
    spawned by run_benchmark.py, itself started with `start_new_session`
    below, so both live in the SAME new group) — killing only `pid` would
    leave the paid `claude` process running and spending after an abort.
    """
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def default_run_arm(argv: list[str], env: dict) -> int:
    """Shell run_benchmark.py as a subprocess (round-5 fix, MIN-7 — never
    an in-process call, which would silently ignore `env`).

    Spawned in its own process group (`start_new_session=True`) with a
    timeout derived from the arm's own `--wall-clock-seconds` argument
    (read back out of `argv`, plus margin for the harness's own teardown
    work) — a driver-level backstop, independent of the cell's own
    wall-clock check, that guarantees this call cannot block the driver
    forever. On either the timeout or any other exception unwinding
    through this call (a trapped SIGINT/SIGTERM raised as
    KeyboardInterrupt, in particular), the whole process group is killed
    before the exception (or the timeout's own return) reaches the
    caller — so a driver abort cannot leave the paid `claude` grandchild
    running while teardown starts rewriting ~/.claude underneath it.
    """
    timeout: Optional[float] = None
    if "--wall-clock-seconds" in argv:
        try:
            raw = argv[argv.index("--wall-clock-seconds") + 1]
            timeout = float(raw) + 120.0
        except (ValueError, IndexError):
            timeout = None

    proc = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return 124
    except BaseException:
        _kill_process_group(proc.pid)
        raise


def _resolved_path(raw: str) -> Path:
    """argparse `type=` for any path arg that becomes a spend-relevant or
    identity-relevant lookup key. Resolving at parse time (rather than
    leaving a relative path to drift with whatever the cwd happens to be
    across the process boundary) is what stops a mistyped or cwd-relative
    `--spend-ledger` from silently resolving to a fresh, empty ledger."""
    return Path(raw).resolve()


def _stranded_nested_ledger_problem(ledger_path: Path) -> Optional[str]:
    """GATE-STOP text iff `ledger_path` resolves to somewhere INSIDE the
    quoin git checkout itself (`_repo_root`) — the IVG-119 single-root-
    invariant violation that split a real $0.35 of spend off the canonical
    ledger during this stage's own first attempt (a `cd quoin` plus a
    cwd-relative `--spend-ledger` silently resolved into a stray nested
    `quoin/.workflow_artifacts/` tree instead of the canonical
    project-root one). The canonical root lives one level up, at
    `_repo_root.parent`; nothing under `_repo_root` itself is ever a valid
    `--spend-ledger` target."""
    resolved = ledger_path.resolve()
    try:
        resolved.relative_to(_repo_root.resolve())
    except ValueError:
        return None
    return (
        f"GATE-STOP: --spend-ledger {ledger_path} resolves inside the quoin git "
        f"checkout ({_repo_root}) rather than the canonical project "
        f".workflow_artifacts root one level up ({_repo_root.parent}) — this is "
        "the IVG-119 stranded-nested-root pattern; fix the path (or the cwd this "
        "command runs from) before proceeding"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Three-arm benchmark gate driver (T-07)")
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--suite", type=_resolved_path, required=True)
    parser.add_argument("--fixture-repo", type=_resolved_path, required=True)
    parser.add_argument("--main-worktree", type=_resolved_path, required=True)
    parser.add_argument("--candidate-worktree", type=_resolved_path, required=True)
    parser.add_argument(
        "--main-commit", required=True,
        help="Commit the main worktree's HEAD must equal (asserted in "
             "preflight, not derived from the worktree itself — see "
             "arm-identity note on --candidate-commit).",
    )
    parser.add_argument(
        "--candidate-commit", required=True,
        help="Commit the candidate worktree's HEAD must equal, asserted in "
             "preflight before either worktree's own `git rev-parse HEAD` "
             "is trusted for anything downstream. An operator-supplied "
             "value, not a value the driver derives from the same "
             "worktree it is meant to check — a self-derived expectation "
             "can never disagree with what it is checking.",
    )
    parser.add_argument("--max-budget-usd-raw", type=float, required=True)
    parser.add_argument("--max-budget-usd-main", type=float, required=True)
    parser.add_argument("--max-budget-usd-candidate", type=float, required=True)
    parser.add_argument("--spend-ledger", type=_resolved_path, required=True)
    parser.add_argument(
        "--new-spend-ledger", action="store_true",
        help="Acknowledge that --spend-ledger does not exist yet and a "
             "fresh $0 ledger should be started there. Without this flag "
             "a missing ledger file is a GATE-STOP, not a silent fresh "
             "start — a mistyped path must not read as zero recorded "
             "spend.",
    )
    parser.add_argument(
        "--authorised-usd", type=float, default=None,
        help="Operator-supplied spend ceiling for this invocation's "
             "preflight precheck. When omitted, the ceiling is derived "
             "from the ledger's own latest reauth-note (or the $50 "
             "default) — the same file the recorded spend is read from, "
             "which is convenient but not independently authoritative. "
             "Prefer passing this explicitly.",
    )
    parser.add_argument("--project-root", type=_resolved_path, default=None)
    parser.add_argument(
        "--expected-suite-sha256", default=None,
        help="If set, preflight GATE-STOPs unless the suite file's sha256 "
             "matches this value — freezes the suite an operator has "
             "already reviewed against silent edits.",
    )
    parser.add_argument("--wall-clock-seconds", type=int, default=600)
    parser.add_argument(
        "--run-dir", type=_resolved_path, required=True,
        help="Resolved at parse time, like --spend-ledger — this is a "
             "spend-relevant lookup key via _read_arm_actual_cost, so a "
             "cwd-relative path must not silently drift across the "
             "process boundary.",
    )
    parser.add_argument("--rehearsal", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--raw-no-isolation", action="store_true",
        help="D-15 FALLBACK: skip the isolated CLAUDE_CONFIG_DIR for the "
             "raw arm and run it against the real ~/.claude, honestly "
             "relabeled as 'stock Claude prompt on a quoin-installed "
             "machine' rather than a quoin-free floor. Use only after a "
             "rehearsal has shown the isolated form fails to authenticate.",
    )
    args = parser.parse_args(argv)

    driver = ThreeArmGateDriver(args)
    return driver.run()


class ThreeArmGateDriver:
    """Holds per-invocation state so tests can construct one, monkeypatch
    its collaborator methods, and drive `.run()` without touching a real
    filesystem/network/subprocess — see test_three_arm_gate.py."""

    def __init__(self, args, run_arm_fn=default_run_arm, project_root: Optional[Path] = None):
        self.args = args
        self.run_arm_fn = run_arm_fn
        self.project_root = project_root or Path(".")
        self.home = Path.home()
        self.caps = {
            "raw": args.max_budget_usd_raw,
            "main": args.max_budget_usd_main,
            "candidate": args.max_budget_usd_candidate,
        }
        self.arm_roots = {"main": args.main_worktree, "candidate": args.candidate_worktree}
        self.arm_cells = {"raw": "simple-claude", "main": "quoin-claude", "candidate": "quoin-claude"}
        self.raw_isolated = not getattr(args, "raw_no_isolation", False)
        self.venv_python = sys.executable
        self.spawned_arms: set[str] = set()
        self.reservations: dict[str, str] = {}  # arm -> attempt_id
        # Created lazily, per invocation, by `_ensure_raw_config_dir` — a
        # fixed, predictable path here would let `mkdir(exist_ok=True)`
        # silently ADOPT a pre-existing, attacker-owned directory. `None`
        # until the raw arm actually runs.
        self.raw_config_dir: Optional[Path] = None
        self.pre_provenance: dict = {}
        self.evidence: dict[str, dict] = {}
        self._aborted = False
        self._torn_down = False
        self._teardown_result: Optional[bool] = None
        self.new_spend_ledger = bool(getattr(args, "new_spend_ledger", False))
        self.authorised_usd = getattr(args, "authorised_usd", None)
        self.expected_worktree_commits = {
            "main": getattr(args, "main_commit", None),
            "candidate": getattr(args, "candidate_commit", None),
        }
        self.expected_suite_sha256 = getattr(args, "expected_suite_sha256", None)
        arg_project_root = getattr(args, "project_root", None)
        if arg_project_root is not None:
            self.project_root = arg_project_root

    # -- Step 0: preflight -------------------------------------------------

    def preflight(self) -> list[str]:
        """Run every precondition; return the list of GATE-STOP messages
        (empty means all passed). Never spawns `claude`."""
        problems: list[str] = []

        # Pure argument/path validation, before any file I/O that could
        # spend or that assumes the ledger exists.
        if not self.args.rehearsal and not self.args.plan_only and self.authorised_usd is None:
            problems.append(
                "GATE-STOP: --authorised-usd is required in full mode — the spend "
                "ceiling must be an explicit operator input, not silently derived "
                "from the ledger it polices"
            )
            return problems

        nested_problem = _stranded_nested_ledger_problem(self.args.spend_ledger)
        if nested_problem:
            problems.append(nested_problem)
            return problems

        # FIRST — before anything that can spend (round-5 fix, MIN-4). The
        # ledger's own existence is checked BEFORE recorded_total is even
        # read from it: a missing file reading as $0 recorded spend is
        # only safe when the operator has said, explicitly, that this is
        # meant to be a fresh ledger.
        ledger_path = self.args.spend_ledger
        if not ledger_path.exists() and not self.new_spend_ledger:
            problems.append(
                f"GATE-STOP: spend ledger not found at {ledger_path} — pass "
                "--new-spend-ledger to start a fresh one there, or fix the path"
            )
            return problems
        recorded_so_far = spend_ledger.recorded_total(ledger_path) if ledger_path.exists() else 0.0
        print(f"Spend ledger: {ledger_path} (recorded_total={recorded_so_far:.2f})")

        # The live model probe below (T-04c) spends against this same
        # ceiling — its cap belongs in `planned` too, not just the three
        # arm caps, or the precheck below would pass a combination that
        # actually exceeds the authorisation once the probe fires.
        planned = [self.caps["raw"], self.caps["main"], self.caps["candidate"], MODEL_PROBE_MAX_BUDGET_USD]
        resolved_authorised = spend_ledger.resolve_authorised_ceiling(ledger_path, self.authorised_usd)
        ok, message = spend_ledger.precheck(
            ledger_path, planned_caps=planned, authorised=resolved_authorised,
        )
        if not ok:
            problems.append(message)
            return problems  # nothing below may run until this passes

        if not self.args.main_worktree.exists() or not self.args.candidate_worktree.exists():
            problems.append("GATE-STOP: both arm worktrees must exist outside the project root")
            return problems

        for name, root in (("main", self.args.main_worktree), ("candidate", self.args.candidate_worktree)):
            try:
                root.resolve().relative_to(self.project_root.resolve())
                problems.append(
                    f"GATE-STOP: {name} worktree {root} is inside the project root "
                    f"{self.project_root}; arm worktrees must live outside it"
                )
            except ValueError:
                pass  # not inside project_root — the required condition

        for arm in ("main", "candidate"):
            root = self.arm_roots[arm]
            expected = self.expected_worktree_commits.get(arm)
            if not expected:
                problems.append(f"GATE-STOP: --{arm}-commit was not supplied")
                continue
            actual = worktree_head(root)
            if actual is None:
                problems.append(f"GATE-STOP: could not resolve {arm} worktree HEAD ({root})")
            elif actual != expected:
                problems.append(
                    f"GATE-STOP: {arm} worktree HEAD {actual!r} does not match "
                    f"expected commit {expected!r} ({root})"
                )

        if not candidate_about_version_matches(self.args.candidate_worktree, self.venv_python):
            problems.append(
                "GATE-STOP: candidate worktree's own __about__.py version does not "
                "match what the arm-pinned install reports for it"
            )

        if self.args.suite.exists() and self.expected_suite_sha256:
            actual_sha = suite_sha256(self.args.suite)
            if actual_sha != self.expected_suite_sha256:
                problems.append(
                    f"GATE-STOP: suite sha256 {actual_sha} does not match the frozen "
                    f"expected value {self.expected_suite_sha256}"
                )

        remote = subprocess.run(
            ["git", "-C", str(self.args.fixture_repo), "remote"],
            capture_output=True, text=True, timeout=30,
        )
        if remote.returncode == 0 and remote.stdout.strip():
            problems.append(
                f"GATE-STOP: fixture repo {self.args.fixture_repo} still has a remote "
                f"configured ({remote.stdout.strip()!r}); run "
                "`git remote remove origin` before using it as a quoin-arm fixture — "
                "the quoin arms run /run --autonomous, which can push a branch"
            )

        for arm, root in self.arm_roots.items():
            if not verify_arm_installer_isolable(root, self.venv_python):
                problems.append(f"GATE-STOP: arm installer not isolable ({arm})")

        if _install_py() is None:
            problems.append(
                "GATE-STOP: cannot resolve the interpreter install.sh would select "
                f"(tried {', '.join(INSTALL_PY_CANDIDATES)})"
            )
        else:
            self.pre_provenance["install_py"] = _install_py()

        bare_quoin_file = bare_import_quoin_file(self.venv_python)
        if bare_quoin_file is None:
            problems.append(
                "GATE-STOP: could not resolve a bare `import quoin` (no PYTHONPATH "
                "override) before the gate starts — teardown has nothing to verify "
                "the machine returns to"
            )
        else:
            self.pre_provenance["bare_quoin_file"] = bare_quoin_file

        main_manifest = cross_arm_manifest(self.args.main_worktree)
        candidate_manifest = cross_arm_manifest(self.args.candidate_worktree)
        if main_manifest == candidate_manifest:
            problems.append(
                "GATE-STOP: main and candidate deploy identical bytes; the gate would measure noise"
            )

        # Every check above only ever APPENDS to `problems` and falls
        # through — a run already guaranteed to abort must not still reach
        # the live, money-spending model probe below.
        if problems:
            return problems

        if not self.args.rehearsal:
            model_override = os.environ.get("QUOIN_BENCH_CLAUDE_MODEL")
            if model_override:
                problems.append(
                    "GATE-STOP: QUOIN_BENCH_CLAUDE_MODEL is set outside --rehearsal mode; "
                    "full mode must verify the real pinned model"
                )
            elif not self.args.plan_only:
                # The ONE live, spend-generating preflight call (T-04c) —
                # skipped under --plan-only (spend-free by definition) and
                # under --rehearsal (waived; the override above substitutes).
                from quoin.benchmarks.scripts.run_benchmark import verify_model
                code = verify_model(
                    ledger_path=self.args.spend_ledger,
                    max_budget_usd=MODEL_PROBE_MAX_BUDGET_USD,
                    authorised=resolved_authorised,
                )
                if code != 0:
                    problems.append(
                        "GATE-STOP: --verify-model did not confirm PINNED_MODEL "
                        f"(exit {code})"
                    )
        else:
            if not os.environ.get("QUOIN_BENCH_CLAUDE_MODEL"):
                problems.append("GATE-STOP: --rehearsal requires QUOIN_BENCH_CLAUDE_MODEL to be set")

        if not self.args.suite.exists():
            problems.append(f"GATE-STOP: suite file not found: {self.args.suite}")
        else:
            suite = json.loads(self.args.suite.read_text(encoding="utf-8"))
            task = suite["tasks"][0]
            from quoin.benchmarks.harness.cells.simple_claude import _build_prompt
            try:
                _build_prompt(task)
            except ValueError as exc:
                problems.append(
                    f"GATE-STOP: assembled arm prompt does not name the target subsystem; "
                    f"the quality comparison would be across different subsystems ({exc})"
                )

        if self.args.suite.exists():
            suite_for_gate = json.loads(self.args.suite.read_text(encoding="utf-8"))
            n_tasks = len(suite_for_gate["tasks"])
            from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

            # Each arm must individually have a cap set; the WORST CASE
            # check is the SUM across all three arms (T-09), not a per-arm
            # multiplication — each arm has its own distinct cap and is
            # invoked separately with a single cell, unlike the standalone
            # `run_benchmark.py --dry-run` CLI's cap*n_tasks*len(cells) shape.
            combined_worst_case = sum(
                (cap or 0.0) * n_tasks for cap in self.caps.values()
            )
            for arm, cap in self.caps.items():
                passed, reason = dry_run_gate_check(
                    max_budget_usd_per_task=cap,
                    worst_case_usd=combined_worst_case if cap is not None else None,
                    rehearsal=self.args.rehearsal,
                )
                if not passed:
                    problems.append(
                        f"GATE-STOP: dry-run precondition failed — {arm}: {reason}; "
                        "re-authorisation required before any paid arm"
                    )

        if not self.args.rehearsal and not self.args.plan_only:
            rehearsal_record = self.args.spend_ledger.parent / "rehearsal.md"
            if not rehearsal_record.exists() or "GREEN" not in rehearsal_record.read_text(encoding="utf-8"):
                problems.append(
                    "GATE-STOP: no green rehearsal record found — run --rehearsal first"
                )

        return problems

    # -- Step 1: per-arm loop ----------------------------------------------

    def _ensure_raw_config_dir(self) -> None:
        """Create the isolated raw-arm `CLAUDE_CONFIG_DIR` via `mkdtemp` —
        never a fixed, guessable path under a world-writable directory. A
        fixed path let `mkdir(exist_ok=True)` silently ADOPT a pre-
        existing, attacker-owned directory; adopting it here means that
        directory becomes the raw arm's `settings.json` — its HOOKS — i.e.
        command execution as the operator during a live paid session run
        with `--permission-mode acceptEdits`. `mkdtemp` guarantees a
        unique, `0o700` directory it created itself; nothing pre-existing
        can be adopted."""
        base = Path(os.environ.get("TMPDIR", "/tmp")) / "quoin-gate"
        base.mkdir(parents=True, exist_ok=True)
        self.raw_config_dir = Path(tempfile.mkdtemp(prefix="raw-config-", dir=str(base)))

    def run_arm(self, arm: str) -> int:
        run_id = f"{self.args.gate_id}-{arm}"
        if arm == "raw" and self.raw_isolated and self.raw_config_dir is None:
            self._ensure_raw_config_dir()
        arm_env = build_arm_env(arm, self.raw_config_dir, raw_isolated=self.raw_isolated)

        assert_config_root(arm, arm_env, self.home, self.raw_config_dir, raw_isolated=self.raw_isolated)
        self.evidence.setdefault(arm, {})["config_root"] = arm_env.get(
            "CLAUDE_CONFIG_DIR", str(self.home / ".claude")
        )

        arm_root = self.arm_roots.get(arm)
        if arm_root is not None:
            settings_path = self.home / ".claude" / "settings.json"
            owned = arm_registered_stanzas(arm_root)
            before, after_wipe = wipe_arm_stanzas(settings_path, owned)
            install_result = install_arm(arm_root, self.venv_python)
            self.evidence.setdefault(arm, {})["stanzas_before"] = before
            self.evidence[arm]["install_returncode"] = install_result.returncode
            if install_result.returncode != 0:
                # In gate mode the driver's own install is the ONLY
                # install (the cell is invoked with --quoin-install-mode
                # skip) — a failed one must never let the arm spawn, or it
                # silently measures whatever the PREVIOUS arm deployed.
                raise GateStop(
                    f"GATE-STOP: arm {arm} install failed (rc={install_result.returncode}): "
                    f"{install_result.stderr.strip()[-2000:]}"
                )
            expected_commit = subprocess.run(
                ["git", "-C", str(arm_root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            self.evidence[arm]["installed_quoin_commit"] = expected_commit

            # D-02: verify the install actually landed, byte-for-byte,
            # before spending anything against it — not just that the
            # installer process exited 0.
            deployed_ok = verify_arm_deployed(arm_root, self.project_root)
            hooks_ok = verify_hooks_deployed(arm_root, self.home)
            self.evidence[arm]["deployed_verified"] = deployed_ok
            self.evidence[arm]["hooks_verified"] = hooks_ok
            if not deployed_ok:
                raise GateStop(
                    f"GATE-STOP: arm {arm} deploy-drift verification failed after install "
                    f"(deploy_drift_check reported drift or errored)"
                )
            if not hooks_ok:
                raise GateStop(
                    f"GATE-STOP: arm {arm} hook-script byte-compare failed after install"
                )
        else:
            expected_commit = None

        ts = datetime.datetime.utcnow().isoformat() + "Z"
        attempt_id = str(uuid.uuid4())
        # Append the durable ledger row BEFORE the in-memory reservation is
        # set: if the append itself fails, the arm must not be considered
        # reserved at all, and the exception propagates as a GATE-STOP
        # before anything spawns. Setting the reservation first would let
        # a failed append still leave the arm spawned with no ledger row,
        # because teardown's own flush skips any arm already present in
        # `self.reservations`.
        spend_ledger.append(self.args.spend_ledger, {
            "ts": ts, "attempt_id": attempt_id, "kind": "reservation",
            "invocation": "rehearsal" if self.args.rehearsal else "full",
            "gate_id": self.args.gate_id, "arm": arm, "cap_usd": self.caps[arm],
            "actual_usd": None, "run_id": run_id, "new_ceiling_usd": None, "note": "",
        })
        self.reservations[arm] = attempt_id
        self.spawned_arms.add(arm)

        run_output_dir = self.args.run_dir / run_id
        argv = [
            self.venv_python, str(_repo_root / "quoin" / "benchmarks" / "scripts" / "run_benchmark.py"),
            "--suite", str(self.args.suite), "--cells", self.arm_cells[arm],
            "--run-id", run_id, "--run-dir", str(self.args.run_dir),
            "--max-parallel", "1", "--fixture-repo", str(self.args.fixture_repo),
            "--wall-clock-seconds", str(self.args.wall_clock_seconds),
            "--max-budget-usd-per-task", str(self.caps[arm]),
        ]
        if arm_root is not None:
            argv += [
                "--quoin-install-mode", "skip",
                "--arm-root", str(arm_root),
                "--expected-quoin-commit", str(expected_commit),
            ]
        returncode = self.run_arm_fn(argv, arm_env)

        actual_usd = self._read_arm_actual_cost(run_id, self.arm_cells[arm])
        arm_metrics = self._read_arm_metrics(run_id, self.arm_cells[arm])
        if "turn_count" in arm_metrics:
            self.evidence.setdefault(arm, {})["turn_count"] = arm_metrics["turn_count"]
        if "compaction_event_count" in arm_metrics:
            self.evidence.setdefault(arm, {})["compaction_event_count"] = arm_metrics["compaction_event_count"]
        spend_ledger.append(self.args.spend_ledger, {
            "ts": ts, "attempt_id": attempt_id, "kind": "settlement",
            "invocation": "rehearsal" if self.args.rehearsal else "full",
            "gate_id": self.args.gate_id, "arm": arm, "cap_usd": self.caps[arm],
            "actual_usd": actual_usd, "run_id": run_id, "new_ceiling_usd": None, "note": "",
        })

        if arm_root is not None:
            after_stanzas = arm_registered_stanzas(arm_root)
            self.evidence[arm]["stanzas_after_owned"] = sorted(after_stanzas)
            if arm == "main":
                if ("SessionStart", "compact", "sessionstart.sh") in after_stanzas:
                    raise GateStop(
                        "GATE-STOP: main arm's registered stanza set contains SessionStart/compact"
                    )

        return returncode

    def _read_arm_actual_cost(self, run_id: str, cell: str) -> Optional[float]:
        summary_path = self.args.run_dir / run_id / "summary.json"
        if not summary_path.exists():
            return None
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            return (summary.get("cells") or {}).get(cell, {}).get("total_cost_usd_or_null")
        except Exception:
            return None

    def _read_arm_metrics(self, run_id: str, cell: str) -> dict:
        """Read this arm's single task's metrics.json (the gate's suite is
        exactly one task — T-06). `turn_count` is a base key result_writer
        always writes; any telemetry-count field a cell records (e.g. a
        future `compaction_event_count`) rides along the same file rather
        than needing its own reader. Returns {} on anything missing or
        unreadable — the caller degrades to not_available, never fabricates."""
        try:
            suite = json.loads(self.args.suite.read_text(encoding="utf-8"))
            task_id = suite["tasks"][0]["id"]
        except Exception:
            return {}
        metrics_path = self.args.run_dir / run_id / cell / task_id / "metrics.json"
        if not metrics_path.exists():
            return {}
        try:
            return json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    # -- Step 2: teardown ----------------------------------------------------

    def teardown(self) -> bool:
        """Always runs (finally block): flush the ledger for any spawned
        arm with neither a reservation nor a settlement recorded, restore
        settings.json from the verbatim PRE_PROVENANCE copy, then reinstall
        and re-verify the candidate. Returns True iff verified clean.

        Idempotent: `run()`'s own `finally` always calls this once, and
        its `except GateStop`/`except KeyboardInterrupt` handlers call it
        again on the way out — a genuine double-teardown was demonstrated
        to double-charge the ledger (16.0 -> 32.0 against a cap) because
        each pass minted and flushed a FRESH uuid4 for the same
        still-unsettled arm. The second call now short-circuits to the
        first call's cached result instead of repeating the
        restore-and-reinstall sequence.
        """
        if self._torn_down:
            return bool(self._teardown_result)
        self._torn_down = True

        ts = datetime.datetime.utcnow().isoformat() + "Z"
        for arm in self.spawned_arms:
            attempt_id = self.reservations.get(arm)
            if attempt_id is None:
                # An arm marked spawned before its own reservation was
                # appended (died between the two) — flush one now at cap,
                # and record it into `self.reservations` immediately so
                # nothing downstream can mint or flush a second row for
                # the same arm.
                attempt_id = str(uuid.uuid4())
                self.reservations[arm] = attempt_id
                spend_ledger.append(self.args.spend_ledger, {
                    "ts": ts, "attempt_id": attempt_id, "kind": "reservation",
                    "invocation": "rehearsal" if self.args.rehearsal else "full",
                    "gate_id": self.args.gate_id, "arm": arm, "cap_usd": self.caps[arm],
                    "actual_usd": None, "run_id": f"{self.args.gate_id}-{arm}",
                    "new_ceiling_usd": None, "note": "flushed by teardown (unreserved spawn)",
                })

        settings_path = self.home / ".claude" / "settings.json"
        verbatim = self.pre_provenance.get("settings_json_bytes")
        if verbatim is not None:
            _atomic_write_bytes(settings_path, verbatim)

        if self.raw_config_dir is not None:
            shutil.rmtree(self.raw_config_dir, ignore_errors=True)

        candidate_root = self.arm_roots.get("candidate")
        if candidate_root is None:
            self._teardown_result = True
            return True
        install_result = install_arm(candidate_root, self.venv_python)
        # R-14's actual required property is the INVERSE of "the temp
        # worktree is still isolable" (that only proves the arm mechanism
        # still works, which says nothing about what the machine's own
        # PYTHONPATH-free `import quoin` resolves to). Assert a BARE
        # import — no PYTHONPATH override — still resolves to the exact
        # path recorded in PRE_PROVENANCE before the arm loop started, and
        # that this path lives under the git root, not under any arm's
        # temp worktree that T-08b is about to delete.
        current_bare_file = bare_import_quoin_file(self.venv_python)
        recorded_bare_file = self.pre_provenance.get("bare_quoin_file")
        verified = (
            install_result.returncode == 0
            and current_bare_file is not None
            and recorded_bare_file is not None
            and current_bare_file == recorded_bare_file
        )
        if verified:
            try:
                Path(current_bare_file).resolve().relative_to(_repo_root.resolve())
            except ValueError:
                verified = False
        self._teardown_result = verified
        return verified

    # -- Rehearsal record (T-16) -----------------------------------------

    def write_rehearsal_record(self) -> Path:
        """Write `stage-8/gate-evidence/rehearsal.md` (T-16), the record
        T-07 step 0's full-mode preflight requires before any real pilot
        run. GREEN only when every arm produced a non-error verdict, a
        numeric cost, and (for the quoin arms) a resolved
        `installed_quoin_commit` that differs between main and candidate —
        the acceptance bullets a fully-automated check can verify without
        re-parsing transcripts. Anything short of that is RED, with the
        specific reason recorded so a human can decide whether to fix and
        re-rehearse (T-16's own contract) rather than proceed.
        """
        lines = [f"# Rehearsal record — {self.args.gate_id}", ""]
        problems: list[str] = []
        commits: dict[str, Optional[str]] = {}

        for arm in ARMS:
            run_id = f"{self.args.gate_id}-{arm}"
            task_dir = self.args.run_dir / run_id / self.arm_cells[arm] / "scenario_medium_refactor_plan"
            judge_path = task_dir / "judge.json"
            metrics_path = task_dir / "metrics.json"
            transcript_path = task_dir / "transcript.jsonl"

            verdict = "not_available"
            cost = None
            if judge_path.exists():
                verdict = json.loads(judge_path.read_text(encoding="utf-8")).get("verdict", "not_available")
            if verdict == "error":
                problems.append(f"{arm}: verdict is error")

            if metrics_path.exists():
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                commits[arm] = metrics.get("installed_quoin_commit")
                if not metrics.get("budget_cap_armed", True) and metrics.get("max_budget_usd_applied") is not None:
                    problems.append(f"{arm}: budget cap was not armed despite a cap being applied")

            summary_path = self.args.run_dir / run_id / "summary.json"
            if summary_path.exists():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                cost = (summary.get("cells") or {}).get(self.arm_cells[arm], {}).get("total_cost_usd_or_null")
                if cost is None:
                    problems.append(f"{arm}: cost is not a number")

            transcript_non_empty = transcript_path.exists() and transcript_path.stat().st_size > 0
            if not transcript_non_empty:
                problems.append(f"{arm}: transcript.jsonl is empty or missing")

            lines.append(
                f"- **{arm}** ({self.arm_cells[arm]}): verdict={verdict}, cost={cost}, "
                f"installed_quoin_commit={commits.get(arm)}, "
                f"transcript_non_empty={transcript_non_empty}, config_root={self.evidence.get(arm, {}).get('config_root')}"
            )

        if commits.get("main") is not None and commits.get("main") == commits.get("candidate"):
            problems.append("main and candidate recorded the SAME installed_quoin_commit")

        status = "GREEN" if not problems else "RED"
        lines.insert(1, f"\nStatus: **{status}**\n")
        if problems:
            lines.append("\n## Problems")
            for p in problems:
                lines.append(f"- {p}")
        lines.append(f"\nRaw arm isolation: {'isolated' if self.raw_isolated else 'fallback (unisolated)'} — {raw_arm_note(self.raw_isolated)}")

        record_path = self.args.spend_ledger.parent / "rehearsal.md"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return record_path

    # -- Orchestration --------------------------------------------------------

    def run(self) -> int:
        def _raise_keyboard_interrupt(signum, frame):
            raise KeyboardInterrupt()

        old_sigint = signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
        old_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            problems = self.preflight()
            if self.args.plan_only:
                print("PLAN ONLY — arm sequence:", ", ".join(ARMS))
                if problems:
                    for p in problems:
                        print(p, file=sys.stderr)
                    return 2
                print("All preflight checks PASSED. No arm invoked.")
                return 0
            if problems:
                for p in problems:
                    print(p, file=sys.stderr)
                return 2

            settings_path = self.home / ".claude" / "settings.json"
            pre_gate_bytes = settings_path.read_bytes() if settings_path.exists() else b"{}"
            self.pre_provenance["settings_json_bytes"] = pre_gate_bytes
            # An on-disk copy, not just the in-memory one above: a crash
            # between here and teardown otherwise leaves the sole surviving
            # copy of the operator's pre-gate settings.json in a process
            # that no longer exists.
            backup_path = self.args.spend_ledger.parent / f"settings.json.pre-gate-{self.args.gate_id}"
            try:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(backup_path, pre_gate_bytes)
                print(f"Pre-gate settings.json backed up to: {backup_path}")
            except OSError as exc:
                print(f"WARN: could not write settings.json backup to {backup_path}: {exc}", file=sys.stderr)

            teardown_verified = True
            try:
                for arm in ARMS:
                    returncode = self.run_arm(arm)
                    if returncode != 0:
                        print(f"WARN: arm {arm} exited {returncode}", file=sys.stderr)
            finally:
                # A `return` here would swallow any exception propagating
                # out of the try block above — record the result instead
                # and act on it AFTER the finally completes, so a GateStop
                # or KeyboardInterrupt raised mid-arm-loop still reaches
                # the handlers below once teardown has run.
                teardown_verified = self.teardown()

            if not teardown_verified:
                print(
                    "GATE-STOP: teardown could not verify a clean candidate reinstall",
                    file=sys.stderr,
                )
                return 1

            try:
                from quoin.benchmarks.harness.compare_arms import compare_arms
                arm_run_ids = {arm: f"{self.args.gate_id}-{arm}" for arm in ARMS}
                comparison_evidence = {}
                for arm in ARMS:
                    row = {
                        "installed_quoin_commit": self.evidence.get(arm, {}).get("installed_quoin_commit"),
                        "max_budget_usd_applied": self.caps[arm],
                        "config_root": self.evidence.get(arm, {}).get("config_root"),
                    }
                    # Only set when actually read from metrics.json — an
                    # absent key degrades to compare_arms' own
                    # not_available default; a present key with value None
                    # would instead render the literal string "None".
                    for key in ("turn_count", "compaction_event_count"):
                        if key in self.evidence.get(arm, {}):
                            row[key] = self.evidence[arm][key]
                    comparison_evidence[arm] = row
                out_path = compare_arms(
                    run_dir=self.args.run_dir, gate_id=self.args.gate_id,
                    arm_run_ids=arm_run_ids, arm_cells=self.arm_cells,
                    arm_evidence=comparison_evidence,
                    raw_arm_note=raw_arm_note(self.raw_isolated),
                )
                print(f"Comparison written to: {out_path}")
            except ImportError:
                print("WARN: compare_arms unavailable; comparison skipped")

            if self.args.rehearsal:
                record_path = self.write_rehearsal_record()
                print(f"Rehearsal record written to: {record_path}")

            return 0
        except GateStop as exc:
            print(str(exc), file=sys.stderr)
            self.teardown()
            return 2
        except KeyboardInterrupt:
            print("Aborted by signal; running teardown.", file=sys.stderr)
            verified = self.teardown()
            return 1 if not verified else 130
        finally:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == "__main__":
    sys.exit(main())
