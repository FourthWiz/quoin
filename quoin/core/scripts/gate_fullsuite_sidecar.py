#!/usr/bin/env python3
"""IVG-249 stage-3 (D-05): gate full-suite freshness sidecar.

`record` captures a completed full-suite run's verdict and per-repo SHA/clean
state to a JSON sidecar under `.workflow_artifacts/cache/`. `check` decides
whether that recorded run is still fresh enough to reuse, so `/end_of_task`'s
Step 1 pre-flight can skip a redundant re-run of the full suite.

Invariant: every non-zero exit from `check` means RE-RUN the suite — reuse is
granted only on exit 0.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_REASON_ORDER = (
    "disabled-by-env",
    "no-sidecar",
    "schema-unsupported",
    "no-provenance",
    "verdict-fail",
    "dirty-at-record",
    "repo-set-changed",
    "no-repos",
    "sha-mismatch",
    "dirty-now",
)

# gate_phase provenance guard (review round 1 MAJOR, IVG-249 S-03): `check`
# must only honor sidecars written by an actual gate `record` invocation.
# Both gate call sites (post-implement, post-review) already pass one of
# these two values; a hand-authored or ad-hoc `record` call (e.g. an
# acceptance-test recipe run against the live project cache) that omits
# --gate-phase produces a sidecar this predicate rejects.
_VALID_GATE_PHASES = frozenset({"post-implement", "post-review"})


def _load_branch_hygiene():
    """Load branch_hygiene.py by importlib from this same core/scripts/ dir (D-3)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "_quoin_core_branch_hygiene_for_sidecar", here / "branch_hygiene.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load branch_hygiene.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def derive_verdict(known_red_exit: int, pytest_rc: int, task_profile: str) -> str:
    """PASS iff known_red_exit == 0 AND (task_profile == "large" OR pytest_rc == 0).

    Every other combination is FAIL. At Large, `known_red_exit == 0` alone
    already encodes downgrade eligibility (known_red.py's own exit-code
    semantics). At Small/Medium, `pytest_rc` is additionally required so a
    red-but-known-baseline full suite does not silently auto-pass reuse.
    """
    if known_red_exit == 0 and (task_profile == "large" or pytest_rc == 0):
        return "PASS"
    return "FAIL"


def _cache_dir(project_root: Path) -> Path:
    return project_root / ".workflow_artifacts" / "cache"


def _repo_status(repo: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30,
    )
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30,
    )
    if head.returncode != 0 or status.returncode != 0:
        raise RuntimeError(f"git status failed for {repo}")
    return {
        "path": str(repo),
        "head_sha": head.stdout.strip(),
        "clean": status.stdout.strip() == "",
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _cmd_record(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)
    try:
        bh = _load_branch_hygiene()
        repos = bh.discover_repos(project_root)
        if not repos:
            print("gate_fullsuite_sidecar: record: no repos discovered", file=sys.stderr)
            return 3
        repo_entries = [_repo_status(r) for r in repos]
        all_clean = all(e["clean"] for e in repo_entries)
        verdict = derive_verdict(args.known_red_exit, args.rc, args.task_profile)
        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": recorded_at,
            "gate_phase": args.gate_phase,
            "run_token": args.run_token,
            "task_profile": args.task_profile,
            "pytest_rc": args.rc,
            "known_red_exit": args.known_red_exit,
            "verdict": verdict,
            "all_clean": all_clean,
            "repos": repo_entries,
        }
        ts_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        first_sha12 = repo_entries[0]["head_sha"][:12]
        out_path = _cache_dir(project_root) / f"gate-fullsuite-{ts_compact}-{first_sha12}.freshness.json"
        _atomic_write_json(out_path, payload)
    except Exception as exc:  # noqa: BLE001 - any git/IO/import failure -> exit 3
        print(f"gate_fullsuite_sidecar: record: failed ({exc})", file=sys.stderr)
        return 3

    if args.format == "json":
        print(json.dumps({"path": str(out_path), "verdict": verdict}))
    else:
        print(str(out_path))
    return 0


def _newest_sidecar(cache_dir: Path) -> Path | None:
    """Select the newest sidecar by the `recorded_at` FIELD, never mtime."""
    candidates = sorted(cache_dir.glob("*.freshness.json"))
    best: tuple[str, Path] | None = None
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recorded_at = data.get("recorded_at")
        if not isinstance(recorded_at, str):
            continue
        if best is None or recorded_at > best[0]:
            best = (recorded_at, candidate)
    return best[1] if best else None


def _emit_check_result(args: argparse.Namespace, *, reuse: bool, reason: str | None,
                        sha12: str | None = None, repo_count: int | None = None) -> None:
    if args.format == "json":
        payload: dict[str, Any] = {"reuse": reuse}
        if reuse:
            payload["sha12"] = sha12
            payload["repos"] = repo_count
        else:
            payload["reason"] = reason
        print(json.dumps(payload))
    else:
        if reuse:
            print(f"reuse: true\nsha12: {sha12}\nrepos: {repo_count}")
        else:
            print(f"reuse: false\nreason={reason}")


def _cmd_check(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root)

    if os.environ.get("QUOIN_DISABLE_FULLSUITE_REUSE") == "1":
        _emit_check_result(args, reuse=False, reason="disabled-by-env")
        return 1

    try:
        if args.sidecar:
            sidecar_path = Path(args.sidecar)
            if not sidecar_path.is_file():
                _emit_check_result(args, reuse=False, reason="no-sidecar")
                return 1
        else:
            cache_dir = _cache_dir(project_root)
            if not cache_dir.is_dir():
                _emit_check_result(args, reuse=False, reason="no-sidecar")
                return 1
            found = _newest_sidecar(cache_dir)
            if found is None:
                _emit_check_result(args, reuse=False, reason="no-sidecar")
                return 1
            sidecar_path = found

        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _emit_check_result(args, reuse=False, reason="no-sidecar")
            return 1

        if data.get("schema_version") != SCHEMA_VERSION:
            _emit_check_result(args, reuse=False, reason="schema-unsupported")
            return 1

        if data.get("gate_phase") not in _VALID_GATE_PHASES:
            _emit_check_result(args, reuse=False, reason="no-provenance")
            return 1

        if data.get("verdict") != "PASS":
            _emit_check_result(args, reuse=False, reason="verdict-fail")
            return 1

        if not data.get("all_clean"):
            _emit_check_result(args, reuse=False, reason="dirty-at-record")
            return 1

        bh = _load_branch_hygiene()
        current_repos = bh.discover_repos(project_root)
        recorded_repos = data.get("repos", [])

        # Empty-repo-set guard (review round 1 MINOR 1, IVG-249 S-03): an empty
        # `repos: []` sidecar paired with a `discover_repos` OSError (also `[]`)
        # made recorded_paths == current_paths == [] "match" with zero evidence
        # — the only fail-OPEN path in this otherwise fail-CLOSED predicate.
        # Reject before the repo-set/SHA comparisons below.
        if not recorded_repos or not current_repos:
            _emit_check_result(args, reuse=False, reason="no-repos")
            return 1

        recorded_paths = sorted(str(Path(e["path"]).resolve()) for e in recorded_repos)
        current_paths = sorted(str(r.resolve()) for r in current_repos)
        if recorded_paths != current_paths:
            _emit_check_result(args, reuse=False, reason="repo-set-changed")
            return 1

        current_status: dict[str, dict[str, Any]] = {}
        for r in current_repos:
            current_status[str(r.resolve())] = _repo_status(r)

        for entry in recorded_repos:
            key = str(Path(entry["path"]).resolve())
            live = current_status.get(key)
            if live is None or live["head_sha"] != entry["head_sha"]:
                _emit_check_result(args, reuse=False, reason="sha-mismatch")
                return 1

        for entry in recorded_repos:
            key = str(Path(entry["path"]).resolve())
            live = current_status.get(key)
            if live is None or not live["clean"]:
                _emit_check_result(args, reuse=False, reason="dirty-now")
                return 1

        sha12 = recorded_repos[0]["head_sha"][:12] if recorded_repos else ""
        _emit_check_result(args, reuse=True, reason=None, sha12=sha12, repo_count=len(recorded_repos))
        return 0

    except Exception as exc:  # noqa: BLE001 - undeterminable: git/discovery unavailable
        print(f"gate_fullsuite_sidecar: check: undeterminable ({exc})", file=sys.stderr)
        # MINOR 4 (review round 1, IVG-249 S-03): end_of_task's contract says
        # to echo the emitted `reason`; exit 3 previously emitted stderr only.
        # Emit a structured reason on stdout too, in the requested format.
        _emit_check_result(args, reuse=False, reason="undeterminable")
        return 3


def _exit_code(value: str) -> int:
    """argparse type for an exit code; an empty value gets a pointed hint.

    An empty string almost always means the caller read the code from bash's
    PIPESTATUS under zsh (which only has lowercase, 1-indexed ``pipestatus``),
    so say how to capture it portably instead of a bare "invalid int value".
    """
    if not value.strip():
        raise argparse.ArgumentTypeError(
            "empty exit code: capture it with RC=$? on the line right after a "
            "redirected command (cmd > file; RC=$?), never through PIPESTATUS"
        )
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid exit code: {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate full-suite freshness sidecar (record + check). "
                     "Every non-zero exit from check means RE-RUN the suite — "
                     "reuse is granted only on exit 0."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="Record a full-suite run's verdict")
    p_record.add_argument("--project-root", required=True)
    p_record.add_argument("--rc", required=True, type=_exit_code)
    p_record.add_argument("--known-red-exit", required=True, type=_exit_code, dest="known_red_exit")
    p_record.add_argument("--task-profile", required=True, choices=["small", "medium", "large"], dest="task_profile")
    p_record.add_argument("--run-token", default=None, dest="run_token")
    p_record.add_argument("--gate-phase", default=None, choices=["post-implement", "post-review"], dest="gate_phase")
    p_record.add_argument("--format", default="text", choices=["text", "json"])
    p_record.set_defaults(func=_cmd_record)

    p_check = sub.add_parser("check", help="Check whether a prior recorded run is reusable")
    p_check.add_argument("--project-root", required=True)
    p_check.add_argument("--sidecar", default=None)
    p_check.add_argument("--format", default="text", choices=["text", "json"])
    p_check.set_defaults(func=_cmd_check)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
