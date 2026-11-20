"""quoin CLI entrypoint — argparse wrapper + data-tree resolution."""
from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import pathlib
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from typing import NamedTuple, Optional, Tuple


def _abort(msg: str, code: int = 2) -> None:
    """Print msg to stderr and sys.exit(code)."""
    print(msg, file=sys.stderr)
    sys.exit(code)

from quoin import install_prompts
from quoin.__about__ import __version__


def _validate_autocompact_args(args: argparse.Namespace) -> tuple[int | None, int | None, bool]:
    """Validate the three --autocompact-* install flags.

    Returns (pct, window, clear). Raises ValueError on a bad combination
    or an out-of-range value. The primary call site is `main()`, right
    after `parse_args`, before any `deploy_*` call runs — it converts a
    raised ValueError into `install_p.error(...)`, so a bad value never
    leaves a partial install on disk. `_cmd_claude_install` also calls
    this (converting via `_abort`) as a defense-in-depth fallback for
    callers that construct args and invoke it directly, bypassing
    `main()` (as several tests do). Kept independent of argparse so it
    can be unit tested directly (T-05).
    """
    pct: int | None = getattr(args, "autocompact_pct", None)
    window: int | None = getattr(args, "autocompact_window", None)
    clear: bool = getattr(args, "clear_autocompact_env", False)

    if clear and (pct is not None or window is not None):
        raise ValueError(
            "--clear-autocompact-env cannot be combined with "
            "--autocompact-pct or --autocompact-window"
        )
    if pct is not None and not (1 <= pct <= 100):
        raise ValueError(f"--autocompact-pct must be 1..100, got {pct}")
    if window is not None and not (100000 <= window <= 1000000):
        raise ValueError(
            "--autocompact-window must be a plain integer token count in "
            f"100000..1000000 (no suffix such as 'k'), got {window!r}"
        )
    return pct, window, clear


def _validate_workflow_tasks_args(args: argparse.Namespace) -> Optional[str]:
    """Return "with", "remove" or None for the workflow-tasks mod flags.

    getattr defaults for Namespaces that predate the flags, ValueError when
    both are given.
    """
    with_wt = getattr(args, "with_workflow_tasks", False)
    remove_wt = getattr(args, "remove_workflow_tasks", False)
    if with_wt and remove_wt:
        raise ValueError("--with-workflow-tasks cannot be combined with --remove-workflow-tasks")
    if with_wt:
        return "with"
    if remove_wt:
        return "remove"
    return None


def _autocompact_window_type(raw: str) -> int:
    """argparse type= for --autocompact-window: a plain integer, no suffix.

    Rejects a suffixed form such as '500k' up front with a message naming
    quoin's own requirement — it does not claim what the platform does
    with a suffixed value (undocumented; D-07).
    """
    stripped = raw.strip()
    if not (stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit())):
        raise argparse.ArgumentTypeError(
            "must be a plain integer token count in 100000..1000000 "
            f"(no suffix such as 'k'), got {raw!r}"
        )
    return int(stripped)


def _resolve_source_dir(explicit: Optional[str]) -> pathlib.Path:
    """Resolve the quoin data source directory (proc:T-03).

    Resolution order:
    1. Explicit --source-dir flag (always trusted if it has adapters/claude/skills/).
    2. Editable-install detection: if __file__ is under src/quoin/, skip
       importlib.resources and jump straight to Tier 2.
    3. Tier 1 (wheel install): importlib.resources.files('quoin') / 'data'.
    4. Tier 2 (editable / src layout): walk __file__ up to repo/quoin/.
    5. Tier 3: abort with explicit error.
    """
    if explicit is not None:
        candidate = pathlib.Path(explicit).resolve()
        if not (candidate / "adapters" / "claude" / "skills").is_dir():
            print(
                f"quoin: --source-dir {explicit!r} has no adapters/claude/skills/ subdirectory",
                file=sys.stderr,
            )
            sys.exit(2)
        return candidate

    import quoin as _quoin_pkg

    pkg_file = pathlib.Path(_quoin_pkg.__file__).resolve()

    # Editable-install detection: src/quoin/__init__.py → parent is 'quoin', grandparent is 'src'
    is_editable = (
        pkg_file.parent.name == "quoin" and pkg_file.parent.parent.name == "src"
    )

    if not is_editable:
        # Tier 1: wheel install — data is bundled inside the package
        try:
            data_ref = importlib.resources.files("quoin") / "data"
            # Convert to a concrete path for is_dir() checks
            with importlib.resources.as_file(data_ref) as data_path:  # type: ignore[attr-defined]
                data_path = pathlib.Path(data_path)
                if (data_path / "adapters" / "claude" / "skills").is_dir():
                    return data_path
        except (TypeError, AttributeError, FileNotFoundError):
            pass

    # Tier 2: editable or importlib fallback — walk from src/quoin/ up to repo/
    # pkg_file.parent = src/quoin/; two ".." reaches project root; then "quoin/"
    candidate = (pkg_file.parent / ".." / ".." / "quoin").resolve()
    if (candidate / "adapters" / "claude" / "skills").is_dir():
        return candidate

    # Tier 3: abort
    print(
        "quoin: cannot resolve data tree; pass --source-dir <path> "
        "(typically <path-to-clone>/quoin)",
        file=sys.stderr,
    )
    sys.exit(2)


def _derive_allow_writes(source_dir: pathlib.Path, source_dir_explicit: bool) -> bool:
    """Five-conjunct guard (proc:T-07 / MAJ-1 round-4 fix).

    True iff ALL of:
    (a) --source-dir was explicitly passed
    (b) os.access(source_dir, os.W_OK) is True
    (c) (source_dir / "adapters" / "claude" / "skills").is_dir() is True
    (d) source_dir.parent has .git/ OR pyproject.toml
    (e) source_dir.resolve() is NOT a descendant of the package directory
    """
    if not source_dir_explicit:
        return False

    # (b)
    if not os.access(source_dir, os.W_OK):
        return False

    # (c)
    if not (source_dir / "adapters" / "claude" / "skills").is_dir():
        return False

    # (d)
    parent = source_dir.parent
    if not ((parent / ".git").is_dir() or (parent / "pyproject.toml").is_file()):
        return False

    # (e) — refuse if source_dir is inside the package directory
    import quoin as _quoin_pkg

    pkg_dir = pathlib.Path(_quoin_pkg.__file__).resolve().parent
    src_resolved = source_dir.resolve()
    try:
        src_resolved.relative_to(pkg_dir)
        # Succeeded → source_dir IS inside pkg_dir → refuse
        return False
    except ValueError:
        # ValueError means not a descendant → safe
        pass

    return True


def _prompt_scope() -> str:
    """Interactively ask the user for install scope when --scope is omitted.

    Returns 'user' or 'project'. Falls back to 'user' silently in non-interactive
    (no tty) environments so CI/pipe usage is unaffected.
    """
    if not sys.stdin.isatty():
        print("quoin: non-interactive mode — defaulting to --scope user (global ~/.claude/)")
        return "user"

    print()
    print("Where should quoin install?")
    print("  g) Global  — ~/.claude/  (all Claude Code sessions on this machine)")
    print("  p) Project — ./.claude/  (this project only)")
    print()
    while True:
        try:
            answer = input("Choose [g/p] (default: g): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if answer in ("", "g", "global", "user"):
            return "user"
        if answer in ("p", "project"):
            return "project"
        print("Please enter 'g' for global or 'p' for project.")


def _resolve_dest_root(args: argparse.Namespace) -> pathlib.Path:
    """Resolve the dest_root for a Claude install based on --scope (proc:T-03).

    --scope user (default)  → ~/.claude/
    --scope project         → <CWD>/.claude/
    --scope project:/path   → /path/.claude/

    Validates that the resolved path is not home's .claude, that the parent is
    writable, and that the parent is not / or HOME.
    """
    scope: str = getattr(args, "scope", None) or "user"

    if not scope.startswith("project"):
        return pathlib.Path.home() / ".claude"

    # Parse "project" or "project:/absolute/path"
    parts = scope.split(":", 1)
    raw_dir = parts[1] if len(parts) == 2 else None
    project_dir = pathlib.Path(raw_dir or os.getcwd()).resolve()

    if project_dir == pathlib.Path("/") or project_dir == pathlib.Path.home():
        _abort(
            f"quoin: --scope project resolved to {project_dir}; "
            "refusing to use root or home directory as project root"
        )

    if not os.access(project_dir, os.W_OK):
        _abort(
            f"quoin: --scope project dir {project_dir} is not writable"
        )

    dest = project_dir / ".claude"

    # Refuse if dest resolves to the home .claude (catches --scope project:~)
    if dest.resolve() == (pathlib.Path.home() / ".claude").resolve():
        _abort(
            "quoin: --scope project resolved to the user home install path; "
            "use bare 'quoin install' (default --scope user) instead"
        )

    return dest


class _InstallAnswers(NamedTuple):
    wt_mode: Optional[str]
    warned_mods: Tuple[str, ...]


def _ask_install_questions(
    args: argparse.Namespace,
    installer,
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    *,
    is_project_mode: bool,
    wt_mode: Optional[str],
) -> _InstallAnswers:
    """Ask every install question up front, before anything is written.

    Questions appear only when stdin and stdout are both terminals. Explicit
    flags suppress their own question; an installed mod is refreshed silently.
    """
    modes = {"workflow-tasks": wt_mode}
    warned: list = []
    if install_prompts.is_interactive():
        for mod in (installer.WORKFLOW_TASKS,):
            if modes[mod.name] is not None:
                continue
            if installer.mod_state(mod, dest_root) == "installed":
                continue
            errors, warnings = installer.mod_preflight(
                mod,
                source_dir,
                dest_root,
                mode="with",
                is_project_mode=is_project_mode,
                home_dest_root=pathlib.Path.home() / ".claude",
                cwd_dest_root=pathlib.Path.cwd() / ".claude",
            )
            if errors:
                first = errors[0]
                if first.startswith("quoin: "):
                    first = first[len("quoin: "):]
                print(f"quoin: not offering the {mod.name} mod: {first}")
                continue
            for warning in warnings:
                print(warning, file=sys.stderr)
            if install_prompts.ask_yes_no(
                install_prompts.mod_question(mod.name, installer.mod_dest(mod, dest_root)),
                default=True,
            ):
                modes[mod.name] = "with"
                warned.append(mod.name)
    return _InstallAnswers(modes["workflow-tasks"], tuple(warned))


CLAUDE_MD_VARIANT_STATE_FILE = ".quoin-claude-md-variant"
CLAUDE_MD_VARIANT_ENV = "QUOIN_CLAUDE_MD_VARIANT"
_CLAUDE_MD_VARIANTS = ("full", "slim")


def _resolve_claude_md_variant(args: argparse.Namespace, dest_root: pathlib.Path) -> tuple[str, str]:
    """Return (variant, origin). Precedence: flag, env, stored choice, slim default.

    Origin is one of flag, env, stored, default. Invalid env or stored values
    are ignored with a warning.
    """
    flag = getattr(args, "claude_md_variant", None)
    if flag:
        return flag, "flag"
    env = os.environ.get(CLAUDE_MD_VARIANT_ENV, "").strip()
    if env:
        if env in _CLAUDE_MD_VARIANTS:
            return env, "env"
        print(
            f"quoin: ignoring invalid {CLAUDE_MD_VARIANT_ENV}={env!r} (expected full or slim)",
            file=sys.stderr,
        )
    state = dest_root / CLAUDE_MD_VARIANT_STATE_FILE
    try:
        stored = state.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    if stored:
        if stored in _CLAUDE_MD_VARIANTS:
            return stored, "stored"
        print(
            f"quoin: ignoring invalid value {stored!r} in {state} (expected full or slim)",
            file=sys.stderr,
        )
    return "slim", "default"


def _store_claude_md_variant(dest_root: pathlib.Path, variant: str) -> None:
    """Persist an explicit choice: 'full' writes the state file, 'slim' removes it."""
    state = dest_root / CLAUDE_MD_VARIANT_STATE_FILE
    try:
        if variant == "full":
            state.write_text("full\n", encoding="utf-8")
        else:
            state.unlink(missing_ok=True)
    except OSError as exc:
        print(f"quoin: could not update {state}: {exc}", file=sys.stderr)


def _cmd_claude_install(args: argparse.Namespace) -> int:
    from quoin import installer

    source_dir_explicit = args.source_dir is not None
    source_dir = _resolve_source_dir(args.source_dir)

    if getattr(args, "scope", None) is None:
        args.scope = _prompt_scope()

    dest_root = _resolve_dest_root(args)
    scope: str = getattr(args, "scope", None) or "user"
    is_project_mode = scope.startswith("project")

    # Slim is the default rules variant in both scopes. An explicit choice is
    # remembered per scope (state file in the deploy root), so a later plain
    # reinstall keeps it.
    claude_md_variant, variant_origin = _resolve_claude_md_variant(args, dest_root)

    # Project installs never regenerate (allow_writes is forced False below), so a
    # stale generated slim must be caught here, before any deploy_* call. An
    # explicitly requested slim fails closed; a merely defaulted slim falls back
    # to the full rules for this install so a stale generated file never blocks it.
    if claude_md_variant == "slim":
        stale = installer.check_slim_outputs_fresh(source_dir)
        if stale:
            broken = any(
                "staleness check unavailable" in e or "regen aborted" in e or "core text unreadable" in e
                for e in stale
            )
            if variant_origin in ("flag", "env"):
                print(
                    "quoin: --claude-md-variant slim aborted — generated output(s) are "
                    "stale vs a fresh regen of " + str(source_dir / "CLAUDE.md") + ": "
                    + ", ".join(stale) + ". Either run "
                    "'python3 quoin/scripts/build_claude_slim.py' in the quoin repo "
                    "checkout and commit the result, or pass "
                    "'--claude-md-variant full' to install the full rules.",
                    file=sys.stderr,
                )
                return 1
            if broken:
                headline = "slim freshness check is broken (please report this)"
            else:
                headline = "generated slim rules are stale"
            print(
                "WARNING: " + headline + "; installing the full rules for this run.\n"
                "  " + "\n  ".join(stale) + "\n"
                "  Regenerate with 'python3 quoin/scripts/build_claude_slim.py' in the quoin "
                "repo checkout, or pass '--claude-md-variant full' to silence this.",
                file=sys.stderr,
            )
            claude_md_variant = "full"

    # Mutex: --scope project is only valid with --runtime claude
    if is_project_mode and getattr(args, "runtime", "claude") == "codex":
        _abort("quoin: --scope project is only valid with --runtime claude")

    # Mutex: --scope project cannot combine with --check
    if is_project_mode and getattr(args, "check", False):
        _abort("quoin: --scope project cannot combine with --check")

    # T-13: fail-fast when home ~/.claude/settings.json has quoin hook stanzas
    # (hooks MERGE across scopes and fire multiple times; double-fire has real side-effects)
    allow_hook_merge: bool = getattr(args, "allow_hook_merge", False)
    if is_project_mode and not allow_hook_merge:
        if installer.detect_home_hook_conflict():
            _abort(
                "quoin: Home-level quoin hook stanzas detected in ~/.claude/settings.json.\n"
                "Running project-mode install alongside home-mode hooks causes each hook to\n"
                "fire TWICE per event (hooks MERGE across settings.json files, not override).\n"
                "Options:\n"
                "  (a) Remove home-level quoin stanzas manually or via\n"
                "      'quoin uninstall --scope user --hooks-only' (not yet implemented).\n"
                "  (b) Run with --allow-hook-merge to proceed anyway (documents the double-fire)."
            )

    try:
        wt_mode = _validate_workflow_tasks_args(args)
    except ValueError as exc:
        _abort(f"quoin: {exc}")
    answers = _ask_install_questions(
        args,
        installer,
        source_dir,
        dest_root,
        is_project_mode=is_project_mode,
        wt_mode=wt_mode,
    )
    wt_mode = answers.wt_mode
    mod_errors, warnings = installer.workflow_tasks_preflight(
        source_dir,
        dest_root,
        mode=wt_mode,
        is_project_mode=is_project_mode,
        home_dest_root=pathlib.Path.home() / ".claude",
        cwd_dest_root=pathlib.Path.cwd() / ".claude",
    )
    if "workflow-tasks" not in answers.warned_mods:
        for warning in warnings:
            print(warning, file=sys.stderr)
    if mod_errors:
        # Every refusal is reported at once, before anything is written.
        for error in mod_errors:
            print(error, file=sys.stderr)
        return 1

    # allow_writes: only in --dev mode with a writable source tree.
    # This is the single dev/user division: user installs (pip or bash, with or without
    # --source-dir) never regenerate; dev installs (--dev + writable working tree) do.
    allow_writes = args.dev and _derive_allow_writes(source_dir, source_dir_explicit)
    if is_project_mode:
        allow_writes = False  # T-07 MAJ-6: project installs never write to source tree

    mode_label = "project mode" if is_project_mode else "user mode"
    print(f"Installing under {dest_root.resolve()} ({mode_label})")

    # T-07: prerequisites first
    missing = installer.check_prerequisites()
    if missing:
        print("quoin: Missing required tools:", file=sys.stderr)
        for tool in missing:
            print(f"       - {tool}", file=sys.stderr)
        print("\nInstall them and re-run this script.", file=sys.stderr)
        return 1
    print("Prerequisites OK")

    # Remove any previous install record before the first deploy step, so a
    # failed or partial install (below) never leaves an older install's
    # record sitting next to a partially deployed hook tree — see
    # runtime_record.remove_existing_record.
    from quoin import runtime_record

    runtime_record.remove_existing_record(dest_root)

    # Every file deployed from here to deploy_hooks is recorded, so files the
    # previous install deployed and this one did not can be pruned below.
    previous_manifest = installer.read_deploy_manifest(dest_root)
    installer.start_deploy_record()

    installer.deploy_memory(source_dir, dest_root)
    installer.deploy_quickstart(source_dir, dest_root)

    # IVG-69 Stage A: regenerate §0' Pollution dispatch BEFORE deploy_skills so the
    # freshly-injected adapter SKILL.md is what deploy_skills copies (T-06, R-11).
    installer.regenerate_pollution_dispatch(source_dir, allow_writes=allow_writes)

    # IVG-115 T-04: regenerate §V Ground-truth verification blocks BEFORE deploy_skills,
    # same ordering rationale as regenerate_pollution_dispatch above.
    installer.regenerate_verification_step(source_dir, allow_writes=allow_writes)

    installer.deploy_skills(source_dir, dest_root)
    installer.apply_workflow_tasks(source_dir, dest_root, mode=wt_mode)
    installer.deploy_scripts(source_dir, dest_root)
    installer.deploy_core_scripts(source_dir, dest_root)
    installer.deploy_core_workflow(source_dir, dest_root)  # IVG-248: portable workflow docs (D-10)
    installer.cleanup_obsolete_scripts(dest_root)
    installer.cleanup_obsolete_skills(dest_root)
    installer.cleanup_obsolete_memory(dest_root)

    try:
        autocompact_pct, autocompact_window, clear_autocompact_env = _validate_autocompact_args(args)
    except ValueError as exc:
        _abort(f"quoin: {exc}")
    installer.deploy_hooks(
        source_dir,
        dest_root,
        is_project_mode=is_project_mode,
        autocompact_pct=autocompact_pct,
        autocompact_window=autocompact_window,
        clear_autocompact_env=clear_autocompact_env,
    )

    deployed = installer.take_deploy_record()
    manifest = dict(deployed)
    if getattr(args, "no_prune", False):
        # Keep the stale entries so a later install can still prune them.
        for rel, digest in (previous_manifest or {}).items():
            manifest.setdefault(rel, digest)
    elif previous_manifest is None:
        installer.remove_pre_manifest_files(dest_root)
    else:
        foreign_mods = tuple(
            f"skills/{mod.name}/"
            for mod in installer.OPT_IN_MODS
            if installer.mod_state(mod, dest_root) == "foreign"
        )
        _, retry = installer.prune_stale_deploys(
            dest_root, previous_manifest, deployed, protected=foreign_mods
        )
        manifest.update(retry)
    installer.write_deploy_manifest(dest_root, manifest)

    # T-06: CLAUDE.md placement differs by mode (D-02)
    if is_project_mode:
        # project mode: write to <project>/CLAUDE.md (one level above .claude/)
        claude_md_path = dest_root.parent / "CLAUDE.md"
        # D-02: 3-second abort window so users can Ctrl-C if wrong project root
        print(
            f"Will write workflow rules to {claude_md_path} in 3 seconds. "
            "Press Ctrl-C to abort."
        )
        import time
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nAborted — no files written.", file=sys.stderr)
            return 1
    else:
        claude_md_path = dest_root / "CLAUDE.md"
    source_claude_name = "CLAUDE.slim.md" if claude_md_variant == "slim" else "CLAUDE.md"
    print(
        f"Using CLAUDE.md variant: {claude_md_variant} ({variant_origin}; source "
        f"{source_claude_name}) → {claude_md_path}"
    )
    installer.merge_workflow_rules(
        source_dir,
        dest_root,
        force_merge=args.force_merge,
        claude_md_path=claude_md_path,
        source_claude_name=source_claude_name,
    )

    # proc:R-02: post-install placeholder validator
    violations = installer.assert_no_placeholders(dest_root)
    if violations:
        print(
            f"quoin: install error — {len(violations)} unsubstituted __QUOIN_HOME__ "
            f"placeholder(s) found:",
            file=sys.stderr,
        )
        for v in violations[:5]:
            print(f"  {v}", file=sys.stderr)
        if len(violations) > 5:
            print(f"  ... ({len(violations) - 5} more)", file=sys.stderr)
        return 1

    # Remember an explicit variant choice only after the install proved clean.
    if variant_origin == "flag":
        _store_claude_md_variant(dest_root, claude_md_variant)

    # T-07: preamble regeneration last
    installer.regenerate_preambles(source_dir, allow_writes=allow_writes)

    if args.dev:
        installer.install_dev_deps()

    # Warn if pyyaml absent (parity with install.sh lines 184-186)
    try:
        import yaml  # type: ignore[import]  # noqa: F401
    except ImportError:
        print(
            "Warning: Python package 'pyyaml' is not installed — "
            "validate_artifact.py V-01 frontmatter check will fail at runtime.",
            file=sys.stderr,
        )
        print("  Install with: pip install pyyaml", file=sys.stderr)

    # Records the interpreter and package tree this install deployed from,
    # so an auto-resume hand-off can relaunch the exact same CLI instead of
    # guessing via PATH. Never fails the install.
    from quoin import runtime_record

    record_path = runtime_record.write_runtime_record(dest_root, source_dir)
    if record_path is not None:
        print(f"Wrote install record {record_path}")

    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    runtime = getattr(args, "runtime", "claude")

    # --scope project cannot combine with --runtime codex — check before dispatch
    scope: str = getattr(args, "scope", None) or "user"
    if scope.startswith("project") and runtime == "codex":
        _abort("quoin: --scope project is only valid with --runtime claude")

    if runtime != "claude" and (
        getattr(args, "with_workflow_tasks", False)
        or getattr(args, "remove_workflow_tasks", False)
    ):
        _abort(
            "quoin: --with-workflow-tasks/--remove-workflow-tasks are only "
            "valid with --runtime claude"
        )

    if runtime == "codex":
        return _cmd_codex_init(args)
    if args.check:
        print(
            "quoin: install --check is only supported with --runtime codex; "
            "use 'quoin doctor' for Claude install health checks",
            file=sys.stderr,
        )
        return 2
    return _cmd_claude_install(args)


# Environment names the configuration pipeline reads; nothing else is ever
# copied out of the real environment, so no credential variable can reach it.


def _codex_script(source_dir: pathlib.Path, name: str) -> pathlib.Path:
    script = source_dir / "adapters" / "codex" / name
    if not script.is_file():
        print(
            f"quoin: cannot find Codex adapter script {script}; "
            "pass --source-dir <path-to-clone>/quoin if needed",
            file=sys.stderr,
        )
        sys.exit(2)
    return script


def _run_codex_script(script: pathlib.Path, argv: list[str]) -> int:
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script), *argv]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            print(code, file=sys.stderr)
            return 1
    finally:
        sys.argv = old_argv
    return 0


def _cmd_codex_doctor(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    project_root = pathlib.Path(args.project_root).resolve()

    readiness = _codex_script(source_dir, "verify_codex_readiness.py")
    print(f"Codex readiness: {project_root}")
    readiness_rc = _run_codex_script(
        readiness,
        ["--project-root", str(project_root)],
    )
    if readiness_rc != 0:
        return readiness_rc

    if args.smoke:
        smoke = _codex_script(source_dir, "smoke_codex_workflow.py")
        print()
        print(f"Codex smoke: {project_root}")
        return _run_codex_script(smoke, ["--project-root", str(project_root)])

    return 0


def _cmd_codex_init(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    project_root = pathlib.Path(args.project_root).resolve()
    generator = _codex_script(source_dir, "generate_codex_assets.py")

    script_args = ["--project-root", str(project_root)]
    if args.check:
        script_args.append("--check")

    return _run_codex_script(generator, script_args)


def _doctor_auto_resume_cli(
    dest_root: pathlib.Path,
    project_root: pathlib.Path,
    errors: list[str],
    warnings: list[str],
    *,
    run=subprocess.run,
    which=shutil.which,
) -> None:
    """Verify the auto-resume hand-off CLI the same way the hooks do: via
    the deployed resolver's read-only ``cli-check``, in a scrubbed env. This
    matches a hook running with a clean environment; a hook inherits the
    caller's PYTHONPATH and probes on a tighter budget, so the two can still
    differ on an unusual host."""
    dest_label = str(dest_root)
    print(f"Auto-resume CLI ({dest_label}/quoin-runtime.json):")

    record_path = dest_root / "quoin-runtime.json"
    if not record_path.exists():
        print("  ✗ no install record")
        errors.append(
            f"no install record at {record_path}; re-run 'quoin install' (same scope)"
        )
    else:
        resolver = dest_root / "core" / "scripts" / "auto_resume.py"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "QUOIN_HANDOFF_PYTHONPATH")}
        env["PATH"] = "/usr/bin:/bin"
        predates_msg = (
            "deployed auto_resume.py could not report the hand-off CLI "
            "(predates the install record or failed); re-run 'quoin install'"
        )
        parsed = None
        try:
            proc = run(
                [sys.executable, str(resolver), "cli-check", "--project-root", str(project_root)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            if lines:
                candidate = json.loads(lines[-1])
                if isinstance(candidate, dict):
                    parsed = candidate
        except (subprocess.TimeoutExpired, OSError, ValueError):
            parsed = None

        if parsed is None or "source" not in parsed or "status" not in parsed:
            print("  ✗ could not report the hand-off CLI")
            errors.append(predates_msg)
        elif parsed.get("status") == "error":
            print("  ✗ could not report the hand-off CLI")
            errors.append(f"{predates_msg}: {parsed.get('message')}")
        elif parsed.get("source") != "record":
            print("  ✗ resolver ignored the install record")
            errors.append(predates_msg)
        elif parsed.get("status") == "stale":
            kind = parsed.get("kind")
            message = parsed.get("message")
            print(f"  ✗ not usable ({kind})")
            errors.append(f"auto-resume hand-off CLI is not usable ({kind}): {message}")
        else:
            probed_version = parsed.get("probed_version")
            if probed_version is not None and probed_version != __version__:
                print("  ✗ recorded interpreter runs a different quoin version")
                errors.append(
                    f"recorded interpreter runs quoin {probed_version} but this CLI is "
                    f"{__version__}; re-run 'quoin install' with the CLI you use"
                )
            else:
                print("  ✓ hand-off CLI is usable")

            pythonpath = parsed.get("pythonpath")
            if pythonpath:
                print(f"  · hand-off relies on PYTHONPATH={pythonpath}")
                warnings.append(
                    f"hand-off relies on PYTHONPATH={pythonpath} (install.sh source-tree "
                    "fallback); installing the package (pip install -e, uv tool, pipx) and "
                    "re-running 'quoin install' from it avoids this"
                )

    # Project scope gets a plain warning; user scope already errors on this
    # earlier in _cmd_doctor's prerequisites block, so avoid a duplicate.
    is_project_mode = dest_root.parent == project_root
    found = which("claude")
    if found is None:
        if is_project_mode:
            print("  ✗ claude not found on PATH")
            warnings.append(
                "claude not found on PATH; an auto-resume supervisor cannot relaunch a session"
            )
    else:
        if which("claude", path="/usr/bin:/bin") is None:
            print(
                "  · claude resolves only outside /usr/bin:/bin; a supervisor started "
                "from a minimal-PATH hook may not find it"
            )

    print()


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.runtime == "codex":
        return _cmd_codex_doctor(args)

    from quoin import installer

    errors: list[str] = []
    warnings: list[str] = []

    print(f"quoin version: {__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    scope: str = getattr(args, "scope", None) or "user"
    is_project_mode = scope.startswith("project")
    if is_project_mode:
        dest_root = _resolve_dest_root(args)
        dest_label = str(dest_root)
    else:
        dest_root = pathlib.Path.home() / ".claude"
        dest_label = "~/.claude"

    print(f"Checking install scope: {scope}  →  {dest_label}")
    print()

    # Prerequisites (user-scope only — project-scope doesn't need these)
    if not is_project_mode:
        for tool in ("claude", "git", "gh", "npx"):
            found = shutil.which(tool) is not None
            status = "✓" if found else "✗"
            print(f"  {status} {tool}")
            if tool in ("claude", "git") and not found:
                errors.append(f"Required tool missing: {tool}")
        print()

    # Tier-1 memory files — deploy_memory() copies all TIER1_MEMORY_FILES
    # unconditionally in BOTH scopes (installer.py:304-315), so this check
    # runs in project scope too (IVG-164 stage 1 T-10; was previously
    # user-scope-only behind a stale comment/guard that silently skipped a
    # check `quoin doctor --scope project` could perform).
    print(f"Memory files ({dest_label}/memory/):")
    for fname in installer.TIER1_MEMORY_FILES:
        p = dest_root / "memory" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing memory file: {fname}")
    print()

    print(f"Scripts ({dest_label}/scripts/):")
    for fname in installer.DEPLOYED_SCRIPTS:
        p = dest_root / "scripts" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing script: {fname}")

    print()

    print(f"Core scripts ({dest_label}/core/scripts/):")
    for fname in installer.CORE_SCRIPTS:
        p = dest_root / "core" / "scripts" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing core script: {fname}")

    print()

    print(f"Core workflow ({dest_label}/core/workflow/):")
    for fname in installer.CORE_WORKFLOW_FILES:
        p = dest_root / "core" / "workflow" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing core workflow file: {fname}")

    print()

    print(f"Skills ({dest_label}/skills/):")
    for skill in installer.CANONICAL_SKILLS:
        p = dest_root / "skills" / skill
        found = p.is_dir()
        status = "✓" if found else "✗"
        print(f"  {status} {skill}")
        if not found:
            errors.append(f"Missing skill: {skill}")

    print()

    # T-08 CRIT-2: skill conflict warning for project mode
    # User-scope skills shadow project-scope skills of the same name.
    if is_project_mode:
        home_skills = pathlib.Path.home() / ".claude" / "skills"
        if home_skills.is_dir():
            shadowed = []
            for skill in installer.CANONICAL_SKILLS:
                if (home_skills / skill).is_dir() and (dest_root / "skills" / skill).is_dir():
                    shadowed.append(skill)
            if shadowed:
                print("⚠ Skill shadow warning:")
                print(
                    f"  The following skills exist in BOTH {dest_label}/skills/ AND "
                    "~/.claude/skills/."
                )
                print(
                    "  Claude Code resolves user scope (~/.claude/skills/) BEFORE project scope."
                )
                print(
                    "  The project-scope versions below will be HIDDEN by the user-scope versions:"
                )
                for skill in shadowed:
                    print(f"    - {skill}")
                print(
                    "  To use project-scope skills, remove the user-scope versions or"
                    " run 'quoin install' without --scope project."
                )
                print()
                warnings.append(
                    f"{len(shadowed)} skill(s) shadowed by user-scope install: "
                    + ", ".join(shadowed)
                )

    if is_project_mode:
        # In project mode, CLAUDE.md is at the project root (parent of .claude/)
        claude_md = dest_root.parent / "CLAUDE.md"
        claude_md_label = f"{dest_root.parent}/CLAUDE.md"
    else:
        claude_md = dest_root / "CLAUDE.md"
        claude_md_label = f"{dest_label}/CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text()
        marker_count = content.count("# === DEV WORKFLOW START ===")
        status = "✓" if marker_count == 1 else "✗"
        print(f"  {status} {claude_md_label} — {marker_count} DEV WORKFLOW marker pair(s)")
        if marker_count > 1 and is_project_mode:
            # T-08 acceptance bullet: explicit warning for double-install in project mode
            print(
                f"  ⚠ {claude_md_label} — {marker_count} DEV WORKFLOW marker pairs "
                "(expected 1); run 'quoin install --scope project --force-merge' to fix"
            )
            warnings.append(
                f"CLAUDE.md has {marker_count} marker pairs (double-install detected); "
                "run 'quoin install --scope project --force-merge' to fix"
            )
        elif marker_count != 1:
            errors.append(
                f"CLAUDE.md has {marker_count} marker pairs (expected 1); "
                "run 'quoin install --force-merge' to fix"
            )
    else:
        print(f"  ✗ {claude_md_label} — not found")
        errors.append(f"CLAUDE.md not found at {claude_md_label}; run 'quoin install'")

    print()
    doctor_project_root = dest_root.parent if is_project_mode else pathlib.Path.cwd()
    _doctor_auto_resume_cli(dest_root, doctor_project_root, errors, warnings)

    # Open-model router probe (user-scope only — home CCR paths are not project-scoped)
    if not is_project_mode:
        from quoin import ccr_config as _ccr
        from quoin import router as _router
        # A direct PATH check, not a version query: `ccr -v`/`ccr version`
        # misreport a healthy v3 install as absent.
        ccr_installed = bool(shutil.which("ccr"))
        ccr_cfg = _ccr.ccr_config_path().exists() or _ccr.ccr_store_path().exists()
        ccr_live = _ccr.probe_service()
        if ccr_installed or ccr_cfg:
            mode = "open via CCR" if (ccr_live and ccr_cfg) else "native"
            print(
                f"  {'✓' if ccr_installed else '·'} claude-code-router: "
                f"{'installed' if ccr_installed else 'not installed'}, "
                f"version {_router.ccr_version_line()}, "
                f"config {'present' if ccr_cfg else 'absent'}, "
                f"proxy {'running' if ccr_live else 'stopped'} → {mode}"
            )
        else:
            print("  · claude-code-router: not set up (run 'quoin router setup' to enable open-model routing)")
        print()

    print()
    if warnings:
        print(f"doctor: {len(warnings)} warning(s):")
        for w in warnings:
            print(f"  ⚠ {w}")
        print()

    if errors:
        print(f"doctor: {len(errors)} issue(s) found:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("doctor: all checks passed")
    return 0


# ---------------------------------------------------------------------------
# Supervisor single-driver lock (T-04). Filename templates are duplicated
# from `quoin/core/scripts/auto_resume.py` (a standalone deployed script,
# not part of this package, so it cannot be imported here) — a parity test
# pins the two copies byte-identical.
# ---------------------------------------------------------------------------

_SUPERVISOR_LOCK_TEMPLATE = "run-supervisor-{task}.pid"
_SUPERVISOR_RESULT_TEMPLATE = "run-supervisor-{task}.result"
_SUPERVISOR_HALT_TEMPLATE = "autonomous-halt-{task}.md"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_json(path: pathlib.Path):
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _atomic_write_text(memory_dir: pathlib.Path, path: pathlib.Path, content: str) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(content)
    os.replace(str(tmp), str(path))


def _atomic_write_json(memory_dir: pathlib.Path, path: pathlib.Path, data: dict) -> None:
    _atomic_write_text(memory_dir, path, json.dumps(data, sort_keys=True) + "\n")


def _supervisor_paths(project_root: pathlib.Path, task: str) -> dict:
    memory_dir = project_root / ".workflow_artifacts" / "memory"
    return {
        "memory_dir": memory_dir,
        "lock": memory_dir / _SUPERVISOR_LOCK_TEMPLATE.format(task=task),
        "result": memory_dir / _SUPERVISOR_RESULT_TEMPLATE.format(task=task),
        "halt": memory_dir / _SUPERVISOR_HALT_TEMPLATE.format(task=task),
    }


def _write_supervisor_result(memory_dir: pathlib.Path, result_path: pathlib.Path, status: str, relaunches: int) -> None:
    data = {"status": status, "relaunches": relaunches, "finished_at": _iso_now()}
    _atomic_write_json(memory_dir, result_path, data)


def _write_abort_halt(
    memory_dir: pathlib.Path,
    halt_path: pathlib.Path,
    task: str,
    reason: str,
    takeover_hint: "str | None" = None,
) -> bool:
    """Write the halt sentinel unless one already exists (never overwritten, D-22).

    Returns True when a halt was written, False when one was already there.
    """
    from quoin import supervisor as _supervisor  # noqa: PLC0415

    if halt_path.exists():
        return False
    project_root = memory_dir.parent.parent
    hint = takeover_hint or _supervisor.takeover_pointer(task, project_root)
    content = (
        f"task: {task}\n"
        "phase: run\n"
        f"reason: {reason}\n"
        f"timestamp: {_iso_now()}\n"
        f"resume_hint: /run --resume {task}\n"
        f"takeover_hint: {hint}\n"
    )
    _atomic_write_text(memory_dir, halt_path, content)
    return True


_FIRST_CHILD_ENV = "QUOIN_FIRST_CHILD_SESSION_ID"
_CORE_SCRIPT_MEMO: dict = {}


def _load_core_script(name: str):
    """Load a bundled portable script (``core/scripts/<name>.py``) by path.

    Looks in the packaged data tree first, then the source checkout layout.
    Returns None when it cannot be loaded; never raises or exits.
    """
    if name in _CORE_SCRIPT_MEMO:
        return _CORE_SCRIPT_MEMO[name]
    module = None
    try:
        import importlib.util  # noqa: PLC0415
        import quoin as _quoin_pkg  # noqa: PLC0415

        candidates = []
        try:
            candidates.append(
                pathlib.Path(str(importlib.resources.files("quoin") / "data" / "core" / "scripts" / f"{name}.py"))
            )
        except Exception:  # noqa: BLE001
            pass
        candidates.append(
            pathlib.Path(_quoin_pkg.__file__).resolve().parent.parent.parent
            / "quoin" / "core" / "scripts" / f"{name}.py"
        )
        for path in candidates:
            if path.is_file():
                spec = importlib.util.spec_from_file_location(f"_quoin_cli_core_{name}", path)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                module = mod
                break
    except Exception:  # noqa: BLE001
        module = None
    _CORE_SCRIPT_MEMO[name] = module
    return module


def _update_supervisor_lock(paths: dict, token: "str | None", **fields) -> bool:
    """Merge ``fields`` into our supervisor lock (a None value deletes the key).

    Only touches a lock that names this process or carries our adoption
    token; anything else is left alone. Returns True when the lock was written.
    """
    try:
        data = _read_json(paths["lock"])
        if not isinstance(data, dict):
            return False
        try:
            held = int(data.get("pid", -1))
        except (TypeError, ValueError):
            held = -1
        if held != os.getpid() and not (token and data.get("token") == token):
            return False
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        data["pid"] = os.getpid()
        _atomic_write_json(paths["memory_dir"], paths["lock"], data)
        return True
    except Exception:  # noqa: BLE001
        return False


def _release_supervisor_lock(lock_path: pathlib.Path, our_pid: int) -> None:
    """Remove the lock only when it still names our own pid (never a
    lock some other process has since taken over)."""
    data = _read_json(lock_path)
    if data is None:
        return
    try:
        held_pid = int(data.get("pid", -1))
    except (TypeError, ValueError):
        return
    if held_pid == our_pid:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _create_lock_exclusive(lock_path: pathlib.Path, payload: bytes) -> bool:
    """Creates `lock_path` atomically and already fully populated with
    `payload` — mirrors `auto_resume.py`'s helper of the same name (kept in
    sync by hand; this module cannot import the standalone helper script,
    which must stand alone under a bare system Python).

    `payload` is written in full to a private tempfile in the same
    directory first, then published under `lock_path` with `os.link` —
    `os.link` fails with `FileExistsError` if `lock_path` already exists,
    so at most one caller can ever win, and the file it publishes is
    always the fully-written one. The previous O_CREAT|O_EXCL-then-
    `os.write` split left a window where the lock existed but was still
    empty; a reader hitting that window parsed it as `None` and could
    unlink a winner's in-flight lock, and a lock left in exactly that
    state by a crash made every later acquire refuse with `pid -1`
    forever, since the path already existed but never parsed."""
    tmp_path = lock_path.parent / f".{lock_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(str(tmp_path), str(lock_path))
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass


_STALE_UNPARSEABLE_LOCK_SECS = 5.0


def _lock_is_stale(lock_path: pathlib.Path) -> bool:
    """Mirrors `auto_resume.py`'s helper of the same name. A lock is
    reclaimable only when its owner is provably gone: a parseable lock
    naming a dead pid, or a lock that still fails to parse well past the
    atomic creator's own write latency. An unparseable-but-fresh lock is
    never treated as stale on emptiness alone — it could be another
    creator's in-flight write."""
    data = _read_json(lock_path)
    if data is not None:
        try:
            pid = int(data.get("pid", -1))
        except (TypeError, ValueError):
            return True
        return not (pid > 0 and _pid_alive(pid))
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age > _STALE_UNPARSEABLE_LOCK_SECS


def _claim_lock_for_removal(lock_path: pathlib.Path):
    """Mirrors `auto_resume.py`'s helper of the same name: atomically takes
    exclusive ownership of `lock_path` for removal and returns the JSON
    content it held, or `None` if it had none or another racer already
    claimed it first. `os.rename` within the same directory is atomic on
    POSIX, so at most one of two racing reclaimers can ever succeed
    against the same source name — closing the race where two callers
    both read the same dead lock before either removed it."""
    claim_path = lock_path.parent / f"{lock_path.name}.stale-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        os.rename(str(lock_path), str(claim_path))
    except OSError:
        return None
    try:
        data = _read_json(claim_path)
    finally:
        try:
            claim_path.unlink()
        except OSError:
            pass
    return data


def _acquire_supervisor_lock(
    memory_dir: pathlib.Path,
    lock_path: pathlib.Path,
    result_path: pathlib.Path,
    task: str,
    max_relaunch: int,
    token: "str | None",
    _retried: bool = False,
    runtime: "str | None" = None,
) -> tuple:
    """Best-effort single-driver lock (D-06/D-19).

    A live lock naming a different pid refuses the run. A dead-pid lock is
    replaced; a dead `writer: "handoff"` lock with no `.result` yet is
    charged its full grant as an `ORPHANED` result before replacement (the
    supervisor that held it never got to report in). A live `writer:
    "handoff"` lock whose `token` field matches `token` (our own
    `QUOIN_SUPERVISOR_LOCK_TOKEN`) is adopted — this process IS the child
    that lock was reserved for.

    Lock creation is atomic (`_create_lock_exclusive`), so a reader can
    never observe a lock that exists but is still empty, and replacing a
    lock that looks abandoned is conditional on it actually being stale
    (`_lock_is_stale`) rather than on a bare failed parse — an unparseable
    lock that is still fresh is refused, not clobbered, since it could be
    another creator's in-flight write.

    Returns ``(acquired, held_pid)``; ``held_pid`` is only meaningful when
    ``acquired`` is False.
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    existing = _read_json(lock_path)
    if existing is not None:
        try:
            held_pid = int(existing.get("pid", -1))
        except (TypeError, ValueError):
            held_pid = -1
        alive = held_pid > 0 and _pid_alive(held_pid)
        if alive:
            if existing.get("writer") == "handoff" and token and existing.get("token") == token:
                return True, None
            return False, held_pid
        if existing.get("writer") == "handoff" and not result_path.exists():
            try:
                granted = int(existing.get("granted", 0) or 0)
            except (TypeError, ValueError):
                granted = 0
            _write_supervisor_result(memory_dir, result_path, "ORPHANED", granted)
        _claim_lock_for_removal(lock_path)
    elif lock_path.exists():
        # `existing` failed to parse but the path is occupied — never
        # treat that as "no lock": the atomic creator above could still be
        # mid-write. Only reclaim once the lock is provably stale.
        if not _lock_is_stale(lock_path):
            current = _read_json(lock_path) or {}
            try:
                held_pid = int(current.get("pid", -1))
            except (TypeError, ValueError):
                held_pid = -1
            return False, held_pid
        _claim_lock_for_removal(lock_path)
    content = {
        "pid": os.getpid(),
        "started_at": _iso_now(),
        "granted": max_relaunch,
        "writer": "cli",
        "task": task,
    }
    if runtime is not None:
        content["runtime"] = runtime
    payload = (json.dumps(content, sort_keys=True) + "\n").encode("utf-8")
    if _create_lock_exclusive(lock_path, payload):
        return True, None
    if _retried:
        existing2 = _read_json(lock_path) or {}
        try:
            held_pid2 = int(existing2.get("pid", -1))
        except (TypeError, ValueError):
            held_pid2 = -1
        return False, held_pid2
    return _acquire_supervisor_lock(
        memory_dir, lock_path, result_path, task, max_relaunch, token,
        _retried=True, runtime=runtime,
    )


def _lock_runtime(data) -> str:
    """Runtime that owns a parsed lock; a missing key means claude and a
    value outside the known runtimes reads as ``unknown``."""
    from quoin import supervisor as _supervisor  # noqa: PLC0415

    value = data.get("runtime") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        return "claude"
    return value if value in _supervisor.RUNTIMES else "unknown"


# (flag, argparse attribute, value that means "not given")


def _strip_handoff_pythonpath() -> None:
    """Removes the `PYTHONPATH` entry an auto-resume hand-off prepended so
    it could relaunch the recorded interpreter, before this process
    relaunches `claude` — the relaunched `claude` subprocess inherits
    `os.environ`, and it must see the user's own `PYTHONPATH`, not the
    hand-off's."""
    marker = os.environ.pop("QUOIN_HANDOFF_PYTHONPATH", None)
    if not marker:
        return
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    try:
        entries.remove(marker)
    except ValueError:
        return
    if entries:
        os.environ["PYTHONPATH"] = os.pathsep.join(entries)
    else:
        del os.environ["PYTHONPATH"]


def _light_task_refusal(project_root: pathlib.Path, task: str, memory_dir: pathlib.Path) -> Optional[str]:
    """Why the supervisor must not drive `task`, or None.

    The supervisor relaunches `/run --resume`, which only the heavy pipeline
    can continue. A task the light `/run` started (a brief, but no heavy
    run-state record or autonomous marker) would never write a done sentinel,
    so each relaunch would be a wasted full session until the no-progress abort.
    """
    brief = project_root / ".workflow_artifacts" / task / "brief.md"
    heavy = (memory_dir / f"run-state-{task}.json").exists() or (
        memory_dir / f"autonomous-run-{task}.marker"
    ).exists()
    if brief.exists() and not heavy:
        return (
            f"quoin run: REFUSED: '{task}' was started by the light /run, which runs in one "
            "session and cannot be relaunched; continue it with `/run --resume "
            f"{task}` in Claude Code, or restart it with `/run hard: ...` for the supervised pipeline."
        )
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    """`quoin run --autonomous <task>` — external supervisor entrypoint (T-08).

    Lazily imports `supervisor` (mirrors the router/models lazy-import
    convention, R-11/D-01) so the base install path stays import-clean.
    Resolves --project-root, builds the real headless launch_fn, and runs
    the relaunch loop to a terminal condition (SUCCESS/HALTED/ABORTED).

    A single-driver lock (T-04) guards against two supervisors racing on
    the same task: a live foreign lock refuses with exit 3 before any
    launch happens. Under `--halt-on-abort`, a non-SUCCESS terminal state
    also writes a halt sentinel and a `.result` file, and SIGTERM/SIGINT
    stop the run the same way, so a hand-off never leaves the task silently
    stuck (D-20). Without the flag, behavior is unchanged: no lock is
    contended in the normal case, no `.result` and no halt are written.

    NOTE (MIN-2): --budget is a no-op stub this release — cost is bounded
    only by --max-relaunch + exponential backoff. See autonomous-mode.md.
    """
    from quoin import supervisor as _supervisor  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    paths = _supervisor_paths(project_root, args.task)

    if getattr(args, "takeover", False):
        from quoin import takeover as _takeover  # noqa: PLC0415

        return _takeover.run_takeover(args.task, project_root)

    refusal = _light_task_refusal(project_root, args.task, paths["memory_dir"])
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    # Strips an auto-resume hand-off's PYTHONPATH prepend before any launch
    # so the relaunched `claude` subprocess (which inherits os.environ) sees
    # the user's own PYTHONPATH, not the hand-off's.
    _strip_handoff_pythonpath()

    # Popped immediately after the lock decision so a relaunch child this
    # process itself spawns never inherits our own adoption token.
    token = os.environ.pop("QUOIN_SUPERVISOR_LOCK_TOKEN", None)
    first_sid = os.environ.pop(_FIRST_CHILD_ENV, None)
    if first_sid and not _supervisor.is_child_session_id(first_sid):
        print(f"quoin run: ignoring invalid {_FIRST_CHILD_ENV}", file=sys.stderr)
        first_sid = None
    acquired, held_pid = _acquire_supervisor_lock(
        paths["memory_dir"], paths["lock"], paths["result"], args.task, args.max_relaunch, token
    )
    if not acquired:
        print(f"quoin run: REFUSED (supervisor lock held by pid {held_pid})")
        print(f"  task: {args.task}")
        print(f"  takeover: {_supervisor.takeover_pointer(args.task, project_root)}")
        return 3

    child_env = dict(os.environ)
    child_env[_supervisor.HEADLESS_CHILD_ENV] = "1"
    launch_fn = _supervisor.make_launch_fn(
        project_root,
        permission_mode=args.permission_mode,
        timeout=_supervisor.launch_timeout_from_env(),
        env=child_env,
    )
    launches = 0
    memory_dir = paths["memory_dir"]
    run_state = _load_core_script("run_state")
    if run_state is None:
        print("quoin run: run-state module unavailable; child id recorded in the lock only", file=sys.stderr)

    def _warn(what: str, exc: BaseException) -> None:
        print(f"quoin run: could not {what}: {exc}", file=sys.stderr)

    def _record_child(entry, forward):
        if run_state is not None:
            try:
                if entry is None:
                    run_state.set_child_fields(memory_dir, args.task, "", "", "")
                else:
                    run_state.set_child_fields(
                        memory_dir, args.task, entry.session_id, entry.cwd, entry.started_at
                    )
            except Exception as exc:  # noqa: BLE001
                _warn("record the child in run-state", exc)
        try:
            # child_pid is cleared: a pid recorded for an earlier launch must
            # never be read as belonging to this one.
            _update_supervisor_lock(
                paths, token,
                child_session_id=entry.session_id if entry else None,
                child_cwd=entry.cwd if entry else None,
                child_started_at=entry.started_at if entry else None,
                child_pid=None,
            )
        except Exception as exc:  # noqa: BLE001
            _warn("record the child in the lock", exc)
        if forward and entry is not None and run_state is not None:
            try:
                run_state.append_note(
                    memory_dir, args.task,
                    f"{_supervisor.CHILD_NOTE_PREFIX} task={args.task} launch={entry.launch_no} "
                    f"session={entry.session_id} cwd={shlex.quote(entry.cwd)} "
                    f"takeover: {_supervisor.takeover_notice_pointer(args.task, project_root)}",
                )
            except Exception as exc:  # noqa: BLE001
                _warn("note the child launch", exc)

    def _record_pid(sid, pid):
        lock = _read_json(paths["lock"])
        if isinstance(lock, dict) and lock.get("child_session_id") == sid:
            _update_supervisor_lock(paths, token, child_pid=pid)

    tracked = _supervisor.make_tracked_launch_fn(
        args.task,
        project_root,
        launch_fn,
        first_session_id=first_sid,
        record_fn=_record_child,
        on_pid_fn=_record_pid,
    )

    def _counting_launch_fn(task):
        nonlocal launches
        launches += 1
        return tracked(task)

    def _abort_hint():
        last = tracked.last
        return _supervisor.takeover_hint(
            args.task, project_root, last.session_id if last else None
        )

    old_handlers = None
    if args.halt_on_abort:
        def _on_signal(signum, _frame):
            _write_abort_halt(
                paths["memory_dir"], paths["halt"], args.task, "supervisor stopped by signal",
                takeover_hint=_abort_hint(),
            )
            _write_supervisor_result(paths["memory_dir"], paths["result"], "STOPPED", launches)
            _release_supervisor_lock(paths["lock"], os.getpid())
            raise SystemExit(143 if signum == signal.SIGTERM else 130)

        old_handlers = (
            signal.signal(signal.SIGTERM, _on_signal),
            signal.signal(signal.SIGINT, _on_signal),
        )

    try:
        result = _supervisor.supervise(
            args.task,
            project_root,
            launch_fn=_counting_launch_fn,
            max_relaunch=args.max_relaunch,
            repair_allowance=_supervisor.repair_allowance_from_env(),
        )
    except SystemExit:
        raise
    except BaseException:
        if args.halt_on_abort:
            _write_abort_halt(
                paths["memory_dir"], paths["halt"], args.task, "supervisor error",
                takeover_hint=_abort_hint(),
            )
            _write_supervisor_result(paths["memory_dir"], paths["result"], "ERROR", launches)
        _release_supervisor_lock(paths["lock"], os.getpid())
        raise
    finally:
        if old_handlers is not None:
            signal.signal(signal.SIGTERM, old_handlers[0])
            signal.signal(signal.SIGINT, old_handlers[1])

    if args.halt_on_abort:
        if result.status == "ABORTED":
            _write_abort_halt(
                paths["memory_dir"], paths["halt"], args.task, result.reason or "aborted",
                takeover_hint=_abort_hint(),
            )
        _write_supervisor_result(paths["memory_dir"], paths["result"], result.status, launches)
    _release_supervisor_lock(paths["lock"], os.getpid())

    label = result.status
    if result.reason:
        label += f" ({result.reason})"
    print(f"quoin run: {label}")
    print(f"  task: {args.task}")
    print(f"  relaunches: {result.relaunches}")
    if result.status != "SUCCESS":
        last = tracked.last
        if last is not None:
            print(f"  takeover: {_supervisor.takeover_command(last.cwd, last.session_id)}")
        else:
            print(f"  takeover: {_supervisor.takeover_pointer(args.task, project_root)}")

    if result.status == "SUCCESS":
        return 0
    if result.status == "HALTED":
        return 1
    return 2  # ABORTED


_BENCH_CHECKOUT_MSG = (
    "quoin bench needs a source checkout of the quoin repo "
    "(run from the repo root or pass --repo-root)"
)


def _bench_candidates(rest: list[str]) -> list[pathlib.Path]:
    """Directories that may be a quoin source checkout, most explicit first."""
    for i, token in enumerate(rest):
        # An explicit root is the only candidate: falling back would mix code from one tree
        # with run state from another.
        if token == "--repo-root" and i + 1 < len(rest):
            return [pathlib.Path(rest[i + 1]).expanduser()]
        if token.startswith("--repo-root="):
            return [pathlib.Path(token.split("=", 1)[1]).expanduser()]
    found = [pathlib.Path(__file__).resolve().parents[2]]
    cwd = pathlib.Path.cwd()
    found.append(cwd)
    # Only the enclosing git checkout is considered, never arbitrary ancestors, so a
    # writable parent directory cannot supply code that gets imported.
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if top.returncode == 0 and top.stdout.strip():
            found.append(pathlib.Path(top.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def _locate_bench_checkout(rest: list[str]) -> Optional[pathlib.Path]:
    """The first candidate holding the benchmark scripts; makes them importable."""
    for candidate in _bench_candidates(rest):
        bench = candidate / "quoin" / "benchmarks"
        scripts = bench / "scripts"
        if (
            (bench / "__init__.py").is_file()
            and (scripts / "run_benchmark.py").is_file()
            and (scripts / "tiered_entry.py").is_file()
        ):
            import quoin

            inner = str(candidate / "quoin")
            if inner not in quoin.__path__:
                quoin.__path__.append(inner)
            return candidate
    return None


def _cmd_bench(rest: list[str]) -> int:
    """`quoin bench tiered ...`: hand everything after `tiered` to the benchmark entry point."""
    if rest[:1] == ["tiered"]:
        if _locate_bench_checkout(rest[1:]) is None:
            print(_BENCH_CHECKOUT_MSG, file=sys.stderr)
            return 2
        from quoin.benchmarks.scripts import tiered_entry

        return tiered_entry.main(rest[1:], prog="quoin bench tiered")
    if not rest or rest[0] in ("-h", "--help"):
        if _locate_bench_checkout(rest[1:]) is None:
            print(_BENCH_CHECKOUT_MSG, file=sys.stderr)
            return 2
        from quoin.benchmarks.scripts import tiered_entry

        print(tiered_entry.build_parser().format_help())
        return 0
    print("usage: quoin bench tiered [options]", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["bench"]:
        return _cmd_bench(argv[1:])
    parser = argparse.ArgumentParser(
        prog="quoin",
        description="Quoin — workflow state for stateless coding agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              quoin install                  Install for Claude Code (prompts for scope)
              quoin install --scope project  Install into ./.claude for this project only
              quoin doctor                   Health-check an existing install (read-only)
              quoin router setup             Enable open-model routing (claude-code-router)
              quoin models set opus glm      Map the opus tier to an OpenRouter model
              quoin run --autonomous <task>  Run the external autonomous supervisor

            Run 'quoin <command> --help' for command-specific options.
            See QUICKSTART.md for the full command reference.
        """),
    )
    parser.add_argument("--version", action="version", version=f"quoin {__version__}")

    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")

    install_p = sub.add_parser(
        "install",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Install Quoin for a runtime. Claude installs globally to ~/.claude. "
            "Codex generates or checks repo-local AGENTS.md scaffold only."
        ),
        help=(
            "Install Quoin for a runtime: Claude globally to ~/.claude "
            "(default) or Codex repo-local AGENTS.md scaffold"
        ),
        epilog=textwrap.dedent("""\
            Scope (--scope):
              user             install to ~/.claude/ (global; default)
              project          install to <CWD>/.claude/ (this project only)
              project:/path    install to /path/.claude/
            Note: for skills, Claude Code personal scope overrides project scope —
            a prior home install shadows project skills. Run
            'quoin doctor --scope project' to detect conflicts.

            Examples:
              quoin install
              quoin install --dev
              quoin install --scope project
              quoin install --runtime codex --project-root .
              quoin install --autocompact-pct 75
              quoin install --clear-autocompact-env
              quoin install --with-workflow-tasks
              quoin install --remove-workflow-tasks
        """),
    )
    install_p.add_argument(
        "--runtime",
        choices=("claude", "codex"),
        default="claude",
        help=(
            "Runtime target. 'claude' installs globally to ~/.claude; "
            "'codex' generates repo-local AGENTS.md only. Defaults to claude."
        ),
    )
    install_p.add_argument(
        "--project-root",
        default=".",
        help=(
            "Project root for --runtime codex AGENTS.md generation/checking; "
            "defaults to the current directory."
        ),
    )
    install_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "For --runtime codex, check AGENTS.md without writing files "
            "(same behavior as 'quoin codex init --check')."
        ),
    )
    install_p.add_argument("--dev", action="store_true", help="Install dev dependencies")
    install_p.add_argument("--source-dir", metavar="PATH", help="Override data source directory")
    install_p.add_argument(
        "--upgrade",
        "--use-pip",
        dest="use_pip",
        action="store_true",
        help="Force pip reinstall before install",
    )
    install_p.add_argument(
        "--force-merge",
        action="store_true",
        help="Keep first DEV WORKFLOW marker pair; remove extra pairs",
    )
    install_p.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep files a previous install deployed that this version no longer ships",
    )
    install_p.add_argument(
        "--scope", default=None, metavar="SCOPE",
        help="Install scope: user (~/.claude, default) or project (<CWD>/.claude). "
             "Omitted → interactive prompt. See 'Scope' below for values.",
    )
    install_p.add_argument(
        "--claude-md-variant",
        choices=("full", "slim"),
        default=None,
        help=(
            "CLAUDE.md variant to merge: 'slim' (default: core rules plus pointers "
            "to the memory catalog) or 'full' (all rules inline); an explicit "
            "choice is remembered for later installs"
        ),
    )
    install_p.add_argument(
        "--allow-hook-merge",
        action="store_true",
        default=False,
        help=(
            "For --scope project: proceed even if home ~/.claude/settings.json already "
            "has quoin hook stanzas. By default, project-mode install fails fast when "
            "home hooks are detected to prevent double-fire side effects."
        ),
    )
    install_p.add_argument(
        "--autocompact-pct",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Opt-in: write CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=N (1..100) to settings.json's "
            "env block, delegating the auto-compaction trigger to the platform. Off by "
            "default (no env key is written unless this or --autocompact-window is passed)."
        ),
    )
    install_p.add_argument(
        "--autocompact-window",
        type=_autocompact_window_type,
        default=None,
        metavar="TOKENS",
        help=(
            "Opt-in: write CLAUDE_CODE_AUTO_COMPACT_WINDOW=TOKENS (100000..1000000, a "
            "plain integer, no suffix) to settings.json's env block. Independent of "
            "--autocompact-pct — supplying either flag alone is a valid opt-in."
        ),
    )
    install_p.add_argument(
        "--clear-autocompact-env",
        action="store_true",
        default=False,
        help=(
            "Remove quoin's two autocompact env keys from settings.json's env block, "
            "leaving every other env key untouched. Mutually exclusive with "
            "--autocompact-pct and --autocompact-window."
        ),
    )
    install_p.add_argument(
        "--with-workflow-tasks",
        action="store_true",
        default=False,
        help=(
            "Not installed unless you pass this flag or answer yes when asked: deploy the workflow-tasks mod (/quoin-tasks pane) to "
            "skills/workflow-tasks/. Once installed, later installs refresh it."
        ),
    )
    install_p.add_argument(
        "--remove-workflow-tasks",
        action="store_true",
        default=False,
        help=(
            "Remove the opt-in workflow-tasks mod folder. Mutually exclusive "
            "with --with-workflow-tasks."
        ),
    )

    doctor_p = sub.add_parser(
        "doctor",
        description=(
            "Check the health of a quoin install (read-only): prerequisites, "
            "deployed skills/scripts/memory, CLAUDE.md markers, router state."
        ),
        help="Check quoin installation health (read-only)",
    )
    doctor_p.add_argument(
        "--runtime",
        choices=("claude", "codex"),
        default="claude",
        help="Runtime to check; defaults to claude.",
    )
    doctor_p.add_argument(
        "--scope",
        default="user",
        metavar="user|project[:DIR]",
        help=(
            "Installation scope to check. 'user' (default) checks ~/.claude/. "
            "'project' checks <CWD>/.claude/. 'project:/path' checks /path/.claude/. "
            "Only valid with --runtime claude."
        ),
    )
    doctor_p.add_argument(
        "--project-root",
        default=".",
        help=(
            "Project root for Codex readiness checks; defaults to the current directory."
        ),
    )
    doctor_p.add_argument(
        "--source-dir",
        metavar="PATH",
        help="Override quoin data source directory for Codex adapter scripts.",
    )
    doctor_p.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "For --runtime codex, also run the deterministic repo-local smoke check."
        ),
    )

    codex_p = sub.add_parser(
        "codex",
        description="Repo-local Codex setup helpers (generate/check AGENTS.md).",
        help="Repo-local Codex setup helpers",
    )
    codex_sub = codex_p.add_subparsers(dest="codex_command")

    codex_init_p = codex_sub.add_parser(
        "init",
        description="Generate or check the repo-local Codex AGENTS.md scaffold.",
        help="Generate or check repo-local Codex AGENTS.md",
    )
    codex_init_p.add_argument(
        "--project-root",
        default=".",
        help="Project root where AGENTS.md is generated or checked.",
    )
    codex_init_p.add_argument(
        "--check",
        action="store_true",
        help="Check AGENTS.md without writing files.",
    )
    codex_init_p.add_argument(
        "--source-dir",
        metavar="PATH",
        help="Override quoin data source directory for Codex adapter scripts.",
    )

    sub.add_parser(
        "bench",
        help="Run the tiered paired SWE-bench benchmark from a source checkout "
             "(quoin bench tiered --help)",
    )

    router_p = sub.add_parser(
        "router",
        description=(
            "Set up open-model routing via claude-code-router (CCR), opt-in. "
            "Reads OPENROUTER_API_KEY from the environment."
        ),
        help="Set up open-model routing via claude-code-router (opt-in)",
    )
    router_sub = router_p.add_subparsers(
        dest="router_command", title="router commands", metavar="<subcommand>"
    )

    router_setup_p = router_sub.add_parser(
        "setup",
        description="Install claude-code-router and scaffold an OpenRouter config.",
        help=(
            "Install claude-code-router and scaffold an OpenRouter config. "
            "Reads OPENROUTER_API_KEY from the environment."
        ),
    )
    router_setup_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any files.",
    )

    router_sub.add_parser(
        "status",
        description="Show CCR install state, config, proxy liveness, and launch mode (read-only).",
        help="Show CCR install state, config, proxy liveness, and active launch mode (read-only).",
    )

    models_p = sub.add_parser(
        "models",
        description=(
            "Manage the tier→open-model mapping for claude-code-router (opt-in). "
            "Bare 'quoin models' prints the current mapping."
        ),
        help="Manage tier→open-model mapping for claude-code-router (opt-in)",
    )
    models_sub = models_p.add_subparsers(
        dest="models_command", title="models commands", metavar="<subcommand>"
    )

    models_set_p = models_sub.add_parser(
        "set",
        description="Set the OpenRouter slug (or alias flash|pro|glm) for one tier.",
        help="Set the slug for one tier (haiku, sonnet, or opus).",
    )
    models_set_p.add_argument(
        "tier",
        help="The tier to update: haiku, sonnet, or opus.",
    )
    models_set_p.add_argument(
        "model",
        help=(
            "OpenRouter slug (e.g. 'deepseek/deepseek-v4-pro') or "
            "friendly alias (flash, pro, glm)."
        ),
    )

    models_preset_p = models_sub.add_parser(
        "preset",
        description="Apply a preset mapping (currently only 'open').",
        help="Apply a preset mapping (currently only 'open' is supported).",
    )
    models_preset_p.add_argument(
        "name",
        choices=["open"],
        help="Preset name. Currently only 'open' (apply default open-model mapping).",
    )

    models_reset_p = models_sub.add_parser(
        "reset",
        description="Document native-launch instructions and back up the CCR config (non-destructive).",
        help="Document native-launch instructions and back up the CCR config (non-destructive).",
    )
    models_reset_p.add_argument(
        "--native",
        action="store_true",
        help="Explicit-intent alias for reset; produces identical behaviour.",
    )

    run_p = sub.add_parser(
        "run",
        description=(
            "Run the external autonomous supervisor: relaunches "
            '`claude -p "/run --resume --autonomous <task>"` in fresh '
            "headless sessions until the task's done- or halt-sentinel "
            "appears, bounded by --max-relaunch and exponential backoff."
        ),
        help="Run the external autonomous supervisor for a task (relaunch loop)",
    )
    run_p.add_argument(
        "task",
        help="Task name (matches the .workflow_artifacts/<task> folder).",
    )
    run_p.add_argument(
        "--autonomous",
        action="store_true",
        help=(
            "This subcommand IS the autonomous supervisor entrypoint; the "
            "flag is explicit/required for clarity in the relaunch string."
        ),
    )
    run_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .workflow_artifacts/ (default: cwd).",
    )
    run_p.add_argument(
        "--max-relaunch",
        type=int,
        default=10,
        help=(
            "Maximum relaunch count before aborting (default: 10; mirrors "
            "supervisor.DEFAULT_MAX_RELAUNCH)."
        ),
    )
    run_p.add_argument(
        "--halt-on-abort",
        action="store_true",
        help=(
            "Write a halt sentinel and a .result file on a non-SUCCESS "
            "terminal state (incl. a caught signal); used by the automatic "
            "hand-off so an unattended run never goes silently stuck."
        ),
    )
    run_p.add_argument(
        "--permission-mode",
        default="allowedTools",
        help=(
            "Headless permission mode for the relaunch subprocess: "
            "'allowedTools' (default; scoped allow-list, per the T-01 POC) "
            "or 'bypassPermissions' (--dangerously-skip-permissions)."
        ),
    )
    run_p.add_argument(
        "--takeover",
        action="store_true",
        help=(
            "Stop the supervisor and headless child for <task>, confirm both "
            "are dead, then print the command that resumes the child "
            "interactively."
        ),
    )
    run_p.add_argument(
        "--budget",
        default=None,
        help=(
            "Cross-session cost ceiling (nice-to-have). NOT YET ENFORCED "
            "this release — cost is bounded by --max-relaunch + backoff "
            "only; see autonomous-mode.md."
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "install" or args.command is None:
        if args.command is None:
            # bare 'quoin' with no subcommand → install with no args
            args = install_p.parse_args([])
        # Validate the --autocompact-* combination here, before any deploy_*
        # call runs, so a bad value never leaves a partial install on disk
        # (review-1.md MIN-1: the previous call site, inside
        # _cmd_claude_install, ran after every deploy_* except deploy_hooks).
        try:
            _validate_autocompact_args(args)
            _validate_workflow_tasks_args(args)
        except ValueError as exc:
            install_p.error(str(exc))
        return _cmd_install(args)
    elif args.command == "doctor":
        return _cmd_doctor(args)
    elif args.command == "codex":
        if args.codex_command == "init":
            return _cmd_codex_init(args)
        codex_p.print_help()
        return 1
    elif args.command == "router":
        # Lazy import keeps quoin install path import-clean (R-11 / D-01).
        from quoin import router as _router
        if args.router_command == "setup":
            return _router._cmd_router_setup(args)
        if args.router_command == "status":
            return _router._cmd_router_status(args)
        router_p.print_help()
        return 1
    elif args.command == "models":
        # Lazy import keeps quoin install path import-clean (R-05 / D-01).
        from quoin import models as _models
        if args.models_command == "set":
            return _models._cmd_models_set(args)
        if args.models_command == "preset":
            return _models._cmd_models_preset(args)
        if args.models_command == "reset":
            return _models._cmd_models_reset(args)
        return _models._cmd_models_show(args)
    elif args.command == "run":
        if args.takeover and args.autonomous:
            run_p.error("--takeover cannot be combined with --autonomous")
        return _cmd_run(args)

    parser.print_help()
    return 1
