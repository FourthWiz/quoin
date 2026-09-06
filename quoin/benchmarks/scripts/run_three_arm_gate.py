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
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
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


def verify_hooks_deployed(arm_root: Path, home: Path) -> bool:
    """The hooks byte-compare `compute_drift` does not cover: each of the
    seven hook scripts, byte-for-byte, via the same
    `expected_deployed_content` helper the installer itself uses."""
    try:
        sys.path.insert(0, str(arm_root / "src"))
        import importlib
        installer = importlib.import_module("quoin.installer")
        importlib.reload(installer)
    except Exception:
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


def assert_config_root(arm: str, arm_env: dict, home: Path, raw_config_dir: Path) -> None:
    """D-15/R-19's config-root assertion, before the spawn. Raises GateStop
    on any mismatch — this is the ONLY check in the gate that can see a
    leaked CLAUDE_CONFIG_DIR at all."""
    resolved = arm_env.get("CLAUDE_CONFIG_DIR")
    if arm == "raw":
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


def build_arm_env(arm: str, raw_config_dir: Path) -> dict:
    """Per-arm environment dict (D-15, round-4 fix MAJ-2): set for `raw`,
    explicitly popped for `main`/`candidate`, and NEVER mutated on the
    driver's own os.environ — both cells inherit whatever the driver holds."""
    env = os.environ.copy()
    if arm == "raw":
        env["CLAUDE_CONFIG_DIR"] = str(raw_config_dir)
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    env["QUOIN_BENCHMARK_GATE"] = "1"
    return env


def default_run_arm(argv: list[str], env: dict) -> int:
    """Shell run_benchmark.py as a subprocess (round-5 fix, MIN-7 — never
    an in-process call, which would silently ignore `env`)."""
    result = subprocess.run(argv, env=env)
    return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Three-arm benchmark gate driver (T-07)")
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--fixture-repo", type=Path, required=True)
    parser.add_argument("--main-worktree", type=Path, required=True)
    parser.add_argument("--candidate-worktree", type=Path, required=True)
    parser.add_argument("--max-budget-usd-raw", type=float, required=True)
    parser.add_argument("--max-budget-usd-main", type=float, required=True)
    parser.add_argument("--max-budget-usd-candidate", type=float, required=True)
    parser.add_argument("--spend-ledger", type=Path, required=True)
    parser.add_argument("--wall-clock-seconds", type=int, default=600)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--rehearsal", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
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
        self.venv_python = sys.executable
        self.spawned_arms: set[str] = set()
        self.reservations: dict[str, str] = {}  # arm -> attempt_id
        self.raw_config_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "quoin-gate" / "raw-config"
        self.pre_provenance: dict = {}
        self.evidence: dict[str, dict] = {}
        self._aborted = False

    # -- Step 0: preflight -------------------------------------------------

    def preflight(self) -> list[str]:
        """Run every precondition; return the list of GATE-STOP messages
        (empty means all passed). Never spawns `claude`."""
        problems: list[str] = []

        # FIRST — before anything that can spend (round-5 fix, MIN-4).
        planned = [self.caps["raw"], self.caps["main"], self.caps["candidate"]]
        ok, message = spend_ledger.precheck(self.args.spend_ledger, planned_caps=planned)
        if not ok:
            problems.append(message)
            return problems  # nothing below may run until this passes

        if not self.args.main_worktree.exists() or not self.args.candidate_worktree.exists():
            problems.append("GATE-STOP: both arm worktrees must exist outside the project root")
            return problems

        for arm, root in self.arm_roots.items():
            if not verify_arm_installer_isolable(root, self.venv_python):
                problems.append(f"GATE-STOP: arm installer not isolable ({arm})")

        main_manifest = cross_arm_manifest(self.args.main_worktree)
        candidate_manifest = cross_arm_manifest(self.args.candidate_worktree)
        if main_manifest == candidate_manifest:
            problems.append(
                "GATE-STOP: main and candidate deploy identical bytes; the gate would measure noise"
            )

        if not self.args.rehearsal:
            model_override = os.environ.get("QUOIN_BENCH_CLAUDE_MODEL")
            if model_override:
                problems.append(
                    "GATE-STOP: QUOIN_BENCH_CLAUDE_MODEL is set outside --rehearsal mode; "
                    "full mode must verify the real pinned model"
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

        for arm, cap in self.caps.items():
            if cap is None:
                problems.append(f"GATE-STOP: no --max-budget-usd set for arm {arm}")

        if not self.args.rehearsal and not self.args.plan_only:
            rehearsal_record = self.args.spend_ledger.parent / "rehearsal.md"
            if not rehearsal_record.exists() or "GREEN" not in rehearsal_record.read_text(encoding="utf-8"):
                problems.append(
                    "GATE-STOP: no green rehearsal record found — run --rehearsal first"
                )

        return problems

    # -- Step 1: per-arm loop ----------------------------------------------

    def run_arm(self, arm: str) -> int:
        run_id = f"{self.args.gate_id}-{arm}"
        arm_env = build_arm_env(arm, self.raw_config_dir)
        if arm == "raw":
            self.raw_config_dir.mkdir(parents=True, exist_ok=True)

        assert_config_root(arm, arm_env, self.home, self.raw_config_dir)

        arm_root = self.arm_roots.get(arm)
        if arm_root is not None:
            settings_path = self.home / ".claude" / "settings.json"
            owned = arm_registered_stanzas(arm_root)
            before, after_wipe = wipe_arm_stanzas(settings_path, owned)
            install_result = install_arm(arm_root, self.venv_python)
            self.evidence.setdefault(arm, {})["stanzas_before"] = before
            self.evidence[arm]["install_returncode"] = install_result.returncode
            expected_commit = subprocess.run(
                ["git", "-C", str(arm_root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            self.evidence[arm]["installed_quoin_commit"] = expected_commit
        else:
            expected_commit = None

        self.spawned_arms.add(arm)
        attempt_id = str(uuid.uuid4())
        self.reservations[arm] = attempt_id
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        spend_ledger.append(self.args.spend_ledger, {
            "ts": ts, "attempt_id": attempt_id, "kind": "reservation",
            "invocation": "rehearsal" if self.args.rehearsal else "full",
            "gate_id": self.args.gate_id, "arm": arm, "cap_usd": self.caps[arm],
            "actual_usd": None, "run_id": run_id, "new_ceiling_usd": None, "note": "",
        })

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

    # -- Step 2: teardown ----------------------------------------------------

    def teardown(self) -> bool:
        """Always runs (finally block): flush the ledger for any spawned
        arm with neither a reservation nor a settlement recorded, restore
        settings.json from the verbatim PRE_PROVENANCE copy, then reinstall
        and re-verify the candidate. Returns True iff verified clean."""
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        for arm in self.spawned_arms:
            attempt_id = self.reservations.get(arm)
            if attempt_id is None:
                # An arm marked spawned before its own reservation was
                # appended (died between the two) — flush one now at cap.
                attempt_id = str(uuid.uuid4())
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
            settings_path.write_bytes(verbatim)

        candidate_root = self.arm_roots.get("candidate")
        if candidate_root is None:
            return True
        install_result = install_arm(candidate_root, self.venv_python)
        verified = (
            install_result.returncode == 0
            and verify_arm_installer_isolable(candidate_root, self.venv_python)
        )
        return verified

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
            self.pre_provenance["settings_json_bytes"] = (
                settings_path.read_bytes() if settings_path.exists() else b"{}"
            )

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
                comparison_evidence = {
                    arm: {"installed_quoin_commit": self.evidence.get(arm, {}).get("installed_quoin_commit"),
                          "max_budget_usd_applied": self.caps[arm]}
                    for arm in ARMS
                }
                out_path = compare_arms(
                    run_dir=self.args.run_dir, gate_id=self.args.gate_id,
                    arm_run_ids=arm_run_ids, arm_cells=self.arm_cells,
                    arm_evidence=comparison_evidence,
                )
                print(f"Comparison written to: {out_path}")
            except ImportError:
                print("WARN: compare_arms unavailable; comparison skipped")

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
