#!/usr/bin/env python3
"""IVG-136: post-merge deploy-drift guard.

Detects when the deployed quoin copies under ~/.claude/ (or <project>/.claude/)
have fallen out of sync with the quoin SOURCE tree — e.g. a merged PR touched
quoin/** or src/quoin/** but `bash quoin/install.sh` was never re-run, so the
running agent is executing stale skills/scripts/memory files.

This is an ADAPTER-ONLY script (it is about ~/.claude/ deployment and imports the
pip-only `quoin` package). It has NO core/scripts twin — see plan D-05. It is
deployed to ~/.claude/scripts/ by the installer and invoked by /gate.

IMPORT POLICY (plan D-11 / round-2 MAJ-A): the deployed checker runs
UNCONDITIONALLY in every project (the scope gate lives inside this script), so
`import quoin` at module top-level is unsafe — an arbitrary non-quoin project's
python3 may not have the quoin package installed. Therefore:
  * module top-level carries NO `quoin` package import. Only stdlib + the
    importlib-loaded core `affected_tests` helper appear here.
  * the scope gate runs FIRST (portable, no `import quoin`); an out-of-scope diff
    exits 0 `scope=out` before the quoin package is ever touched.
  * ALL `quoin.*` imports are DEFERRED inside main()'s try region, so a scope=IN
    project where quoin is not importable degrades to exit 3 (fail-OPEN WARN),
    never an uncaught module-load ImportError -> exit 1 -> blocking FAIL.

EXIT-CODE CONTRACT (plan D-08):
  0 — clean (no drift) OR scope=out OR QUOIN_DISABLE_DEPLOY_DRIFT=1  -> gate PASS
  1 — drift found (>=1 file missing/stale) -> the ONLY blocking code
      (post-implement WARN, post-review FAIL)
  2 — THIS checker's OWN argparse usage error (e.g. --format bogus), raised by
      argparse BEFORE the try/except boundary -> gate WARN, non-blocking
  3 — undeterminable / checker broke: quoin unimportable, source_dir unresolvable
      (wrapped SystemExit(2) from cli._resolve_source_dir), dest_root absent, git
      error, or any uncaught exception -> gate WARN, non-blocking (fail-OPEN)

Env: QUOIN_DISABLE_DEPLOY_DRIFT=1 -> exit 0 (mirrors QUOIN_DISABLE_BRANCH_HYGIENE;
     drift is a negative check so opt-out=exit-0 is safe).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys

# ── Portable core loader (NO `import quoin` — mirrors branch_hygiene.py) ──────
# affected_tests is a core script; load it via the parents[1] importlib pattern
# so the scope gate has no dependency on the pip-installed quoin package.
_CORE_AT_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "core" / "scripts" / "affected_tests.py"
)
_AT_SPEC = importlib.util.spec_from_file_location(
    "_quoin_core_affected_tests_ddc", _CORE_AT_PATH
)
_affected_tests = importlib.util.module_from_spec(_AT_SPEC)
assert _AT_SPEC.loader is not None
sys.modules[_AT_SPEC.name] = _affected_tests
_AT_SPEC.loader.exec_module(_affected_tests)


# Categories compute_drift compares vs. the full deploy surface (plan D-07/D-09).
# Kept as a literal here (not imported from quoin.installer) so the coverage
# qualifier can be emitted even on the scope=out / import-failure paths that must
# never touch the quoin package. Spelling matches installer.DRIFT_CATEGORIES verbatim
# (review MINOR-2: previously "memory-tier1" here vs "memory" in the installer).
_CHECKED_CATEGORIES = ("skills", "scripts", "core-scripts", "core-workflow", "memory", "plugins")
_UNCOVERED_CATEGORIES = (
    "hooks",
    "CLAUDE.md",
    "settings.json",
    "dashboard assets",
    "QUICKSTART.md",
)

_COVERAGE_QUALIFIER = (
    "Deploy drift: PASS (checked: "
    + ", ".join(_CHECKED_CATEGORIES)
    + "; not covered: "
    + ", ".join(_UNCOVERED_CATEGORIES)
    + " — see D-07/D-09)"
)

_REMEDIATION = (
    "remediation: bash quoin/install.sh"
    "  (NOTE: reinstall deploys the full current branch, not only this task's changes)"
)


def _scope_is_in(project_root: pathlib.Path, no_scope_check: bool) -> tuple[bool, str]:
    """Portable scope gate (NO `import quoin`). Returns (scope_in, reason).

    Uses the importlib-loaded affected_tests.resolve_repo + changed_files. A diff is
    IN scope iff any changed path is under `quoin/` or `src/quoin/`. Git-error,
    zero-repo, or multiple-repo conditions map the caller to exit 3 (WARN) via the
    reason token, so a git failure is never surfaced as a false exit-0 PASS
    (deferred MINOR (1) from the post-plan gate).

    reason ∈ {"in", "out", "git-error", "no-repo", "multiple-repos"}.
    --no-scope-check forces ("in", "forced").
    """
    if no_scope_check:
        return True, "forced"
    try:
        repo = _affected_tests.resolve_repo(project_root)
    except RuntimeError:
        return False, "multiple-repos"
    if repo is None:
        return False, "no-repo"
    files, diff_reason = _affected_tests.changed_files(repo)
    if diff_reason == "git-error":
        return False, "git-error"
    for f in files:
        posix = f.replace("\\", "/")
        if posix.startswith("quoin/") or posix.startswith("src/quoin/"):
            return True, "in"
    return False, "out"


def _resolve_dest_root(scope: str) -> pathlib.Path:
    """Self-contained dest_root resolver (plan D-11 / MIN-C).

    Does NOT call cli._resolve_dest_root (whose _abort -> sys.exit(2) sites would
    violate this checker's exit-2 reservation). Read-only comparison needs none of
    that helper's write-time validations (root/home refusal, writability).

      --scope user (default)      -> ~/.claude
      --scope project             -> <CWD>/.claude
      --scope project:PATH        -> <PATH>/.claude
    """
    if scope == "user":
        return pathlib.Path.home() / ".claude"
    if scope == "project" or scope == "project:":
        return pathlib.Path.cwd() / ".claude"
    if scope.startswith("project:"):
        return pathlib.Path(scope[len("project:"):]).expanduser() / ".claude"
    # Unknown scope value — treat as user (safe read-only default)
    return pathlib.Path.home() / ".claude"


def _emit(fmt: str, payload: dict, text_lines: list[str]) -> None:
    if fmt == "json":
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(text_lines))


def main(argv: list[str] | None = None) -> int:
    # Env opt-out first — no quoin touch at all (mirrors QUOIN_DISABLE_BRANCH_HYGIENE).
    if os.environ.get("QUOIN_DISABLE_DEPLOY_DRIFT", "").strip() == "1":
        print(json.dumps({"disabled": True, "scope": "disabled"}))
        return 0

    parser = argparse.ArgumentParser(
        description="Detect deployed ~/.claude copies that drifted from quoin source.",
        add_help=True,
    )
    parser.add_argument("--project-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--source-dir", default=None,
                        help="Explicit quoin data source dir (passed to cli._resolve_source_dir).")
    parser.add_argument("--scope", default="user",
                        help="user (default), project, or project:PATH.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--no-scope-check", action="store_true",
                        help="Skip the scope gate; always compare (used by tests / forced runs).")

    # argparse errors (exit 2) happen HERE, before the try boundary below — so a
    # bad --format value stays exit 2 and is never remapped to exit 3.
    args = parser.parse_args(argv)
    fmt = args.format

    # ── Scope gate FIRST (portable, no `import quoin`) ──────────────────────
    scope_in, scope_reason = _scope_is_in(args.project_root, args.no_scope_check)
    if scope_reason in ("git-error", "no-repo", "multiple-repos"):
        # Deferred MINOR (1): git/repo-resolution failure -> exit 3 (WARN), never
        # a false exit-0 PASS.
        _emit(fmt,
              {"scope": "undeterminable", "reason": scope_reason, "exit_code": 3},
              [f"Deploy drift: UNDETERMINABLE (scope gate: {scope_reason}) — fail-OPEN WARN"])
        return 3
    if not scope_in:
        _emit(fmt,
              {"scope": "out", "drift": [], "exit_code": 0},
              ["Deploy drift: PASS (scope=out — no quoin source touched)"])
        return 0

    # ── scope=IN: defer ALL quoin imports inside the try region (plan D-11) ──
    src_injected = False

    def _payload(d: dict) -> dict:
        """Tag a JSON payload with src_injected when the working-tree
        injection below fired. Text output is deliberately never touched —
        only the JSON payload carries this diagnostic."""
        if src_injected:
            d["src_injected"] = True
        return d

    try:
        # Working-tree src injection: when this checker runs against an
        # uninstalled/edited checkout, `import quoin` below would otherwise
        # resolve whatever quoin package happens to be on sys.path (e.g. a
        # stale installed wheel) instead of the tree being checked. Put the
        # resolved repo's own src/ ahead of sys.path first, scope=IN only,
        # so the import picks up the working tree.
        try:
            repo = _affected_tests.resolve_repo(args.project_root)
        except RuntimeError:
            repo = None  # multiple repos — degrade to the pip-installed quoin
        if repo is not None:
            src = repo / "src"
            if (src / "quoin" / "__init__.py").is_file() and str(src) not in sys.path:
                sys.path.insert(0, str(src))
                src_injected = True

        from quoin.cli import _resolve_source_dir
        from quoin.installer import DriftEntry, compute_drift  # noqa: F401

        # _resolve_source_dir calls sys.exit(2) internally for a bad --source-dir
        # or unresolvable data tree; that 2 is cli.py's convention for a DIFFERENT
        # caller. Wrap it so it degrades to THIS checker's exit 3, not a spurious 2.
        try:
            source_dir = _resolve_source_dir(args.source_dir)
        except SystemExit:
            _emit(fmt,
                  _payload({"scope": "in", "reason": "source-unresolvable", "exit_code": 3}),
                  ["Deploy drift: UNDETERMINABLE (source dir unresolvable) — fail-OPEN WARN"])
            return 3

        dest_root = _resolve_dest_root(args.scope)
        if not dest_root.exists():
            _emit(fmt,
                  _payload({"scope": "in", "reason": "dest-root-absent",
                            "dest_root": str(dest_root), "exit_code": 3}),
                  [f"Deploy drift: UNDETERMINABLE (dest_root absent: {dest_root}) — fail-OPEN WARN"])
            return 3

        drift = compute_drift(source_dir, dest_root)

        if not drift:
            # Clean PASS — MUST name checked vs not-covered categories (plan MAJ-2/D-09).
            _emit(fmt,
                  _payload({"scope": "in", "drift": [],
                            "checked_categories": list(_CHECKED_CATEGORIES),
                            "uncovered_categories": list(_UNCOVERED_CATEGORIES),
                            "exit_code": 0}),
                  [_COVERAGE_QUALIFIER])
            return 0

        # Drift found -> exit 1 (the only blocking code).
        drift_dicts = [
            {"category": d.category, "source": d.source_path,
             "deployed": d.deployed_path, "reason": d.reason}
            for d in drift
        ]
        text_lines = [f"Deploy drift: DRIFT FOUND ({len(drift)} file(s))"]
        for d in drift:
            text_lines.append(f"  [{d.reason}] {d.category}: {d.deployed_path}")
        text_lines.append(_REMEDIATION)
        _emit(fmt,
              _payload({"scope": "in", "drift": drift_dicts,
                        "remediation": "bash quoin/install.sh", "exit_code": 1}),
              text_lines)
        return 1

    except SystemExit:
        # A bare SystemExit escaping the inner wraps (SystemExit does not subclass
        # Exception, so the generic handler below would miss it) — map to exit 3.
        _emit(fmt,
              _payload({"scope": "in", "reason": "systemexit", "exit_code": 3}),
              ["Deploy drift: UNDETERMINABLE (unexpected SystemExit) — fail-OPEN WARN"])
        return 3
    except Exception as exc:  # noqa: BLE001
        # quoin unimportable (deferred `import quoin...` raised ImportError),
        # compute_drift bug, or any other runtime failure -> exit 3 (fail-OPEN WARN).
        _emit(fmt,
              _payload({"scope": "in", "reason": "exception",
                        "error": str(exc), "exit_code": 3}),
              [f"Deploy drift: UNDETERMINABLE ({type(exc).__name__}: {exc}) — fail-OPEN WARN"])
        return 3


if __name__ == "__main__":
    sys.exit(main())
