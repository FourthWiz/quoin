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
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone
from typing import Optional


def _abort(msg: str, code: int = 2) -> None:
    """Print msg to stderr and sys.exit(code)."""
    print(msg, file=sys.stderr)
    sys.exit(code)

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
    1. Explicit --source-dir flag (always trusted if it has skills/).
    2. Editable-install detection: if __file__ is under src/quoin/, skip
       importlib.resources and jump straight to Tier 2.
    3. Tier 1 (wheel install): importlib.resources.files('quoin') / 'data'.
    4. Tier 2 (editable / src layout): walk __file__ up to repo/quoin/.
    5. Tier 3: abort with explicit error.
    """
    if explicit is not None:
        candidate = pathlib.Path(explicit).resolve()
        if not (candidate / "skills").is_dir():
            print(
                f"quoin: --source-dir {explicit!r} has no skills/ subdirectory",
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
                if (data_path / "skills").is_dir():
                    return data_path
        except (TypeError, AttributeError, FileNotFoundError):
            pass

    # Tier 2: editable or importlib fallback — walk from src/quoin/ up to repo/
    # pkg_file.parent = src/quoin/; two ".." reaches project root; then "quoin/"
    candidate = (pkg_file.parent / ".." / ".." / "quoin").resolve()
    if (candidate / "skills").is_dir():
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
    (c) (source_dir / "skills").is_dir() is True
    (d) source_dir.parent has .git/ OR pyproject.toml
    (e) source_dir.resolve() is NOT a descendant of the package directory
    """
    if not source_dir_explicit:
        return False

    # (b)
    if not os.access(source_dir, os.W_OK):
        return False

    # (c)
    if not (source_dir / "skills").is_dir():
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

    # Refuse root and home as project dir
    if project_dir == pathlib.Path("/") or project_dir == pathlib.Path.home():
        _abort(
            f"quoin: --scope project resolved to {project_dir}; "
            "refusing to use root or home directory as project root"
        )

    # Refuse if parent is not writable
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


def _cmd_claude_install(args: argparse.Namespace) -> int:
    from quoin import installer

    source_dir_explicit = args.source_dir is not None
    source_dir = _resolve_source_dir(args.source_dir)

    # Prompt for scope when not explicitly provided on the CLI
    if getattr(args, "scope", None) is None:
        args.scope = _prompt_scope()

    # Resolve dest_root via --scope flag
    dest_root = _resolve_dest_root(args)
    scope: str = getattr(args, "scope", None) or "user"
    is_project_mode = scope.startswith("project")

    # IVG-164 T-07: --claude-md-variant slim is a project-scope pilot only this wave.
    # Placed here (before check_prerequisites) so the guard is a literal no-op: nothing
    # has been probed or written yet when it fires.
    claude_md_variant: str = getattr(args, "claude_md_variant", "full") or "full"
    if claude_md_variant == "slim" and not is_project_mode:
        print(
            "quoin: --claude-md-variant slim is a project-scope pilot only this wave "
            "(see IVG-164); re-run with --scope project",
            file=sys.stderr,
        )
        return 1

    # review-1.md MAJOR 1: project installs never regenerate (allow_writes is forced
    # False below for is_project_mode), so a slim install must fail closed rather than
    # silently deploy a CLAUDE.slim.md / workflow-catalog.md that's stale vs the live
    # quoin/CLAUDE.md. Placed here (before any deploy_* call) so, like the guard
    # above, nothing has been probed or written yet when it fires.
    if claude_md_variant == "slim":
        stale = installer.check_slim_outputs_fresh(source_dir)
        if stale:
            print(
                "quoin: --claude-md-variant slim aborted — generated output(s) are "
                "stale vs a fresh regen of " + str(source_dir / "CLAUDE.md") + ": "
                + ", ".join(stale) + ". Run "
                "'python3 quoin/scripts/build_claude_slim.py' in the quoin repo "
                "checkout and commit the result before installing the slim variant.",
                file=sys.stderr,
            )
            return 1

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

    # allow_writes: only in --dev mode with a writable source tree.
    # This is the single dev/user division: user installs (pip or bash, with or without
    # --source-dir) never regenerate; dev installs (--dev + writable working tree) do.
    allow_writes = args.dev and _derive_allow_writes(source_dir, source_dir_explicit)
    if is_project_mode:
        allow_writes = False  # T-07 MAJ-6: project installs never write to source tree

    # Emit mode banner
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

    # T-04
    installer.deploy_memory(source_dir, dest_root)
    installer.deploy_quickstart(source_dir, dest_root)

    # IVG-69 Stage A: regenerate §0' Pollution dispatch BEFORE deploy_skills so the
    # freshly-injected adapter SKILL.md is what deploy_skills copies (T-06, R-11).
    installer.regenerate_pollution_dispatch(source_dir, allow_writes=allow_writes)

    # IVG-115 T-04: regenerate §V Ground-truth verification blocks BEFORE deploy_skills,
    # same ordering rationale as regenerate_pollution_dispatch above.
    installer.regenerate_verification_step(source_dir, allow_writes=allow_writes)

    # T-05
    installer.deploy_skills(source_dir, dest_root)
    installer.deploy_scripts(source_dir, dest_root)
    installer.deploy_core_scripts(source_dir, dest_root)
    installer.deploy_core_workflow(source_dir, dest_root)  # IVG-248: portable workflow docs (D-10)
    installer.deploy_dashboard_assets(source_dir, dest_root)  # T-12: SPA assets (D-11)
    installer.cleanup_obsolete_scripts(dest_root)

    # Hooks
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

    # agentdesk tool — user-mode only (user-level ~/.config/agentdesk/ location)
    if not is_project_mode:
        agentdesk_dest = pathlib.Path.home() / ".config" / "agentdesk"
        installer.deploy_agentdesk(source_dir, agentdesk_dest)
        if agentdesk_dest.exists():
            print()
            print("To complete agentdesk setup (install zellij, lazygit, fzf, patch .zshrc), run:")
            print(f"  bash {agentdesk_dest}/setup-agentdesk.sh")

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
        f"Using CLAUDE.md variant: {claude_md_variant} (source {source_claude_name}) "
        f"→ {claude_md_path}"
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

    # T-07: preamble regeneration last
    installer.regenerate_preambles(source_dir, allow_writes=allow_writes)

    # Dev deps (if --dev)
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

    # --profile is only meaningful for --runtime opencode — check before dispatch
    if getattr(args, "profile", None) is not None and runtime != "opencode":
        _abort("quoin: --profile is only valid with --runtime opencode")

    # --scope project cannot combine with --runtime codex or opencode — check before dispatch
    scope: str = getattr(args, "scope", None) or "user"
    if scope.startswith("project") and runtime in ("codex", "opencode"):
        _abort("quoin: --scope project is only valid with --runtime claude")

    if runtime == "opencode":
        return _cmd_opencode_install(args)
    if runtime == "codex":
        return _cmd_codex_init(args)
    if args.check:
        print(
            "quoin: install --check is only supported with --runtime codex "
            "or opencode; use 'quoin doctor' for Claude install health checks",
            file=sys.stderr,
        )
        return 2
    return _cmd_claude_install(args)


def _cmd_opencode_install(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    from quoin.opencode_adapter.install import run_install

    return run_install(
        args.project_root,
        source_dir,
        getattr(args, "profile", None),
        args.check,
        sys.stdout,
        sys.stderr,
    )


def _cmd_opencode_script(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    from quoin.opencode_adapter import scripts

    return scripts.run(args.name, args.script_args, source_dir)


# Environment names the configuration pipeline reads; nothing else is ever
# copied out of the real environment, so no credential variable can reach it.
CONFIG_ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY")


def _opencode_config_env() -> dict:
    return {key: os.environ[key] for key in CONFIG_ENV_KEYS if key in os.environ}


def _print_findings(findings) -> None:
    from quoin.opencode_adapter import errors

    for finding in findings:
        subject = " [%s]" % ", ".join(finding.subject) if finding.subject else ""
        print("%s: %s%s" % (finding.code, errors.FINDING_MESSAGES[finding.code], subject), file=sys.stderr)


def _opencode_config_evaluate(args: argparse.Namespace, *, allow_unqualified: bool):
    """Evaluate the configuration; returns (evaluation, env, home) or an int
    exit code after printing the reason."""
    from quoin.opencode_adapter import compiler, paths
    from quoin.opencode_adapter.errors import ConfigErrors
    from quoin.opencode_adapter.roles import AllowUnqualifiedRefused

    env = _opencode_config_env()
    home = pathlib.Path.home()
    try:
        ev = compiler.evaluate(
            project_root=pathlib.Path(args.project_root),
            profile=args.profile,
            env=env,
            home=home,
            now=datetime.now(timezone.utc),
            allow_unqualified=allow_unqualified,
        )
    except ConfigErrors as exc:
        for item in exc.errors:
            print(str(item), file=sys.stderr)
        return 2
    except paths.AdapterDataMissing:
        print("quoin: packaged adapter data not found; reinstall quoin", file=sys.stderr)
        return 2
    except AllowUnqualifiedRefused as exc:
        print("quoin: %s" % exc, file=sys.stderr)
        return 2
    return ev, env, home


def _cmd_opencode_config_explain(args: argparse.Namespace) -> int:
    from quoin.opencode_adapter import compiler, explain, paths

    got = _opencode_config_evaluate(args, allow_unqualified=False)
    if isinstance(got, int):
        return got
    ev, env, home = got
    try:
        output_dir = paths.compiled_output_dir(ev.profile, ev.project_root, env, home)
        result, gate = explain.try_build(ev)
        text = explain.render(
            ev, redact=args.redact, as_json=args.json, compile_result=result,
            output_dir=output_dir, gate_failure=gate,
        )
    except paths.AdapterDataMissing:
        print("quoin: packaged adapter data not found; reinstall quoin", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 1 if (gate is not None or compiler.compile_blockers(ev)) else 0


_COMPILE_IO_TEXT = "quoin: the compiled files could not be written or read; check the output directory"


def _cmd_opencode_config_compile(args: argparse.Namespace) -> int:
    from quoin.opencode_adapter import compiler, paths
    from quoin.opencode_adapter.jsonio import UnsafeDirectoryError

    got = _opencode_config_evaluate(args, allow_unqualified=args.allow_unqualified)
    if isinstance(got, int):
        return got
    ev, env, home = got
    try:
        directory = compiler.resolve_output_dir(ev, output=args.output, env=env, home=home)
    except compiler.OutputRefused as exc:
        print("quoin: %s\n  fix: %s" % (exc, exc.fix), file=sys.stderr)
        return 2
    except OSError:
        print(_COMPILE_IO_TEXT, file=sys.stderr)
        return 2
    try:
        if args.check:
            outcome = compiler.check(ev, directory)
            if outcome.ok:
                print("up to date")
                return 0
            print("stale: " + ", ".join(outcome.reasons))
            return 1
        result = compiler.build(ev)
        written = compiler.write(result, directory)
    except compiler.CompileBlocked as exc:
        _print_findings(exc.findings)
        return 1
    except compiler.CompileGateError as exc:
        print("quoin: %s [gate %s]" % (exc, exc.gate), file=sys.stderr)
        return 1
    except UnsafeDirectoryError as exc:
        print("quoin: the output directory is not private enough: %s" % exc, file=sys.stderr)
        return 2
    except paths.AdapterDataMissing:
        print("quoin: packaged adapter data not found; reinstall quoin", file=sys.stderr)
        return 2
    except OSError:
        print(_COMPILE_IO_TEXT, file=sys.stderr)
        return 2
    print("compiled: %s" % written)
    print("digest: %s" % result.digest)
    print("launchable: %s" % ("true" if result.launchable else "false"))
    return 0


def _cmd_opencode_config_import_preview(args: argparse.Namespace) -> int:
    from quoin.opencode_adapter import import_preview, paths
    from quoin.opencode_adapter.errors import ConfigErrors
    from quoin.opencode_adapter.jsonio import UnsafeDirectoryError

    if (args.confirm_model_id or args.force) and not args.apply:
        print("quoin: --confirm-model-id and --force need --apply", file=sys.stderr)
        return 2
    home = pathlib.Path.home()
    env = _opencode_config_env()
    try:
        tiers, from_file = import_preview.read_source(home)
        proposal = import_preview.propose(tiers, profile_name=args.profile_name)
        target = paths.profile_path(proposal.profile_name, env, home)
        if not args.apply:
            print("source: %s" % ("models.json (read only)" if from_file else "built-in defaults (no models.json)"))
            print("target: %s" % target)
            sys.stdout.write(proposal.text)
            print(
                "to apply, confirm every provider model id (the model_id values above, "
                "not the or-* names): quoin opencode config import-preview --apply "
                "--confirm-model-id ID ..."
            )
            print(
                "each model must be qualified with quoin opencode probe --profile %s "
                "--synthetic-only --model MODEL before it can be compiled" % proposal.profile_name
            )
            return 0
        written = import_preview.apply(
            proposal, confirmed=args.confirm_model_id, force=args.force, env=env, home=home
        )
    except ValueError as exc:
        print("quoin: %s" % exc, file=sys.stderr)
        return 2
    except ConfigErrors as exc:
        for item in exc.errors:
            print(str(item), file=sys.stderr)
        return 2
    except import_preview.ImportRefused as exc:
        print("quoin: %s" % exc, file=sys.stderr)
        return 2
    except UnsafeDirectoryError as exc:
        print("quoin: the profiles directory is not private enough: %s" % exc, file=sys.stderr)
        return 2
    except paths.AdapterDataMissing:
        print("quoin: packaged adapter data not found; reinstall quoin", file=sys.stderr)
        return 2
    except OSError:
        print("quoin: the profile could not be read or written; check the profiles directory", file=sys.stderr)
        return 2
    print("written: %s" % written)
    return 0


def _cmd_opencode_probe(args: argparse.Namespace) -> int:
    import types

    from quoin.opencode_adapter import paths, probe_cli
    from quoin.opencode_adapter.errors import ConfigErrors
    from quoin.opencode_adapter.jsonio import UnsafeDirectoryError

    env = _opencode_config_env()
    # A copy behind a read-only view: the probe can read credential variables
    # to resolve a reference but nothing can write through to the real
    # environment.
    environ = types.MappingProxyType(dict(os.environ))
    try:
        return probe_cli.run(
            profile=args.profile,
            model=args.model,
            project_root=pathlib.Path(args.project_root) if args.project_root else None,
            synthetic_only=args.synthetic_only,
            env=env,
            environ=environ,
            home=pathlib.Path.home(),
            now=datetime.now(timezone.utc),
            platform=sys.platform,
        )
    except ConfigErrors as exc:
        for item in exc.errors:
            print(str(item), file=sys.stderr)
        return 2
    except paths.AdapterDataMissing:
        print("quoin: packaged adapter data not found; reinstall quoin", file=sys.stderr)
        return 2
    except UnsafeDirectoryError as exc:
        print("quoin: the qualification directory is not private enough: %s" % exc, file=sys.stderr)
        return 2
    except OSError:
        print(
            "quoin: the probe could not read or write its files; check the qualification directory",
            file=sys.stderr,
        )
        return 2


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


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the quoin workflow dashboard server (D-06)."""
    source_dir = _resolve_source_dir(args.source_dir)
    script = source_dir / "scripts" / "dashboard_server.py"
    if not script.is_file():
        print(
            f"quoin: dashboard_server.py not found at {script}; "
            "re-run 'quoin install' or pass --source-dir <path-to-clone>/quoin",
            file=sys.stderr,
        )
        sys.exit(2)

    # Marshal argv for the server (--source-dir is NOT forwarded — server uses --project-root)
    server_argv = [
        "--port", str(args.port),
        "--project-root", str(pathlib.Path(args.project_root).resolve()),
    ]
    if args.no_browser:
        server_argv.append("--no-browser")

    return _run_codex_script(script, server_argv)


def _cmd_opencode_doctor(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    from quoin.opencode_adapter import doctor

    profile = getattr(args, "profile", None)
    return doctor.run_doctor(
        args.project_root,
        source_dir,
        args.smoke,
        args.json,
        sys.stdout,
        sys.stderr,
        profile=profile,
        config_env=_opencode_config_env() if profile is not None else None,
    )


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
    if getattr(args, "json", False) and args.runtime != "opencode":
        _abort("quoin: --json is only valid with --runtime opencode")

    scope: str = getattr(args, "scope", None) or "user"
    if scope.startswith("project") and args.runtime == "opencode":
        _abort("quoin: --scope project is only valid with --runtime claude")
    if getattr(args, "profile", None) is not None:
        if args.runtime != "opencode":
            _abort("quoin: --profile is only valid with --runtime opencode")
        if getattr(args, "smoke", False):
            _abort(
                "quoin: --profile cannot be combined with --smoke; "
                "the smoke checks are offline and read no profile"
            )

    if args.runtime == "opencode":
        return _cmd_opencode_doctor(args)
    if args.runtime == "codex":
        return _cmd_codex_doctor(args)

    from quoin import installer

    errors: list[str] = []
    warnings: list[str] = []

    print(f"quoin version: {__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    # Resolve dest_root via --scope (T-08: project-mode support)
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

    # Scripts
    print(f"Scripts ({dest_label}/scripts/):")
    for fname in installer.DEPLOYED_SCRIPTS:
        p = dest_root / "scripts" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing script: {fname}")

    print()

    # Core scripts
    print(f"Core scripts ({dest_label}/core/scripts/):")
    for fname in installer.CORE_SCRIPTS:
        p = dest_root / "core" / "scripts" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing core script: {fname}")

    print()

    # Core workflow docs
    print(f"Core workflow ({dest_label}/core/workflow/):")
    for fname in installer.CORE_WORKFLOW_FILES:
        p = dest_root / "core" / "workflow" / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing core workflow file: {fname}")

    print()

    # Assets block — runs in BOTH user and project modes (T-14, D-11 rationale)
    assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
    print(f"Assets ({dest_label}/core/scripts/dashboard_assets/):")
    for fname in installer._DASHBOARD_ASSETS:
        p = assets_dir / fname
        found = p.exists()
        status = "✓" if found else "✗"
        print(f"  {status} {fname}")
        if not found:
            errors.append(f"Missing dashboard asset: {fname}")

    print()

    # Skills
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

    # CLAUDE.md marker count
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



def _opencode_lock_reader(project_root: pathlib.Path):
    """Reader for the task lock that never follows a symlink: a lock that is
    present but unreadable reports no pid and no runtime."""
    from quoin.opencode_adapter import jsonio, runstore  # noqa: PLC0415

    def read(task: str):
        if not runstore.TASK_RE.match(task or ""):
            return None
        path = _supervisor_paths(project_root, task)["lock"]
        if not os.path.lexists(str(path)):
            return None
        got = jsonio.read_regular_bytes(path, max_bytes=4096)
        if got is None:
            return {"pid": None, "runtime": None}
        try:
            data = json.loads(got[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"pid": None, "runtime": "unknown"}
        if not isinstance(data, dict):
            return {"pid": None, "runtime": "unknown"}
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            pid = None
        return {"pid": pid, "runtime": _lock_runtime(data)}

    return read


def _cmd_opencode_status(args: argparse.Namespace) -> int:
    """`quoin opencode status`: read-only report on the latest phase run."""
    from quoin.opencode_adapter import status  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    try:
        report = status.collect(
            project_root, task=args.task, run_id=args.run_id,
            lock_reader=_opencode_lock_reader(project_root),
        )
    except status.StatusError as exc:
        print("quoin: opencode status: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        sys.stdout.write(status.render_text(report))
    return 0


_GATED_PHASE_CHOICES = ("discover", "architect", "plan", "implement", "review")
_GATE_EXPLANATION_MAX_BYTES = 64 * 1024


def _gated_phase(value: str) -> str:
    """Gated ids and run phases share one rule: a hyphen reads as an underscore
    (argparse applies `type` before the `choices` check)."""
    return value.replace("-", "_")


def _gate_json(payload: dict, code: int) -> int:
    payload["exit_code"] = code
    print(json.dumps(payload, sort_keys=True))
    return code


def _gate_refusal(code: str, message: str, exit_code: int = 2) -> int:
    return _gate_json(
        {"outcome": "GATE_REFUSED", "refusal": {"code": code, "message": message}}, exit_code
    )


def _read_explanation(path: str) -> "str | None":
    """The explanation file, at most 64 KiB, never through a symlink. Opened
    non-blocking and checked on the open descriptor so a pipe or device is an
    error rather than a hang."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(_GATE_EXPLANATION_MAX_BYTES).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _with_task_lock(project_root: pathlib.Path, task: str, body):
    """Run `body()` holding the task lock; `None` plus the holder's pid when it
    is held. A dead holder's lock is replaced the way the run path replaces it
    (which may leave an `ORPHANED` result file); the lock is released on exit."""
    paths = _supervisor_paths(project_root, task)
    acquired, held_pid = _acquire_supervisor_lock(
        paths["memory_dir"], paths["lock"], paths["result"], task, 0, None, runtime="opencode"
    )
    if not acquired:
        return None, held_pid
    try:
        return body(), None
    finally:
        _release_supervisor_lock(paths["lock"], os.getpid())


def _lock_refusal(project_root: pathlib.Path, task: str, held_pid) -> int:
    holder = _read_json(_supervisor_paths(project_root, task)["lock"])
    runtime = _lock_runtime(holder) if isinstance(holder, dict) else "unknown"
    return _gate_refusal("lock-held", f"the task lock is held by pid {held_pid} (runtime {runtime})", 3)


def _cmd_opencode_gate(args: argparse.Namespace) -> int:
    """`quoin opencode gate`: evaluate one gated phase and print one JSON line.

    No option approves, adopts or chooses evidence. Without `--write` nothing
    is written and no lock is taken."""
    from quoin.opencode_adapter import gate, runstore  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    explanation = None
    if args.explanation_file:
        try:
            explanation = _read_explanation(args.explanation_file)
        except OSError:
            return _gate_refusal("explanation-unreadable", "the explanation file cannot be read")
    try:
        source_dir = _resolve_source_dir(args.source_dir)
    except SystemExit:
        return _gate_refusal("source-unavailable", "the quoin source directory cannot be resolved")
    try:
        stage, phase = gate.precheck(project_root, args.task, args.stage, args.phase)
    except gate.GateRefused as exc:
        return _gate_refusal(exc.code, "the request cannot be gated")

    def work():
        result = gate.evaluate(
            project_root, args.task, stage, phase, source_dir=source_dir, explanation=explanation
        )
        payload = result.to_dict()
        payload["outcome"] = "GATE_PASSED" if result.verdict == "PASS" else "GATE_REFUSED"
        payload["artifact"] = None
        code = 0 if result.verdict == "PASS" else 7
        if args.write:
            try:
                sdir = gate.stage_dir(project_root, args.task, stage, source_dir)
                path = gate.write_artifact(project_root, result, sdir, source_dir=source_dir)
            except gate.PathUnresolved as exc:
                payload["artifact_error"] = {"code": "path-unresolved", "message": exc.detail}
                return payload, 8
            except gate.GateArtifactError as exc:
                payload["artifact_error"] = {"code": exc.code, "message": exc.message}
                return payload, 8
            except OSError as exc:
                payload["artifact_error"] = {"code": "artifact-write-failed", "message": type(exc).__name__}
                return payload, 8
            payload["artifact"] = os.path.relpath(str(path), str(project_root)).replace(os.sep, "/")
            try:
                gate.record_gate(project_root, args.task, stage, phase, result, path)
            except (runstore.RunStoreError, OSError) as exc:
                payload["record_error"] = {
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": type(exc).__name__,
                }
                return payload, 8
        return payload, code

    try:
        if args.write:
            outcome, held_pid = _with_task_lock(project_root, args.task, work)
            if outcome is None:
                return _lock_refusal(project_root, args.task, held_pid)
        else:
            outcome = work()
    except gate.GateRefused as exc:
        return _gate_refusal(exc.code, "the request cannot be gated")
    except (runstore.RunStoreError, OSError) as exc:
        return _gate_refusal("store-unreadable", "the run store cannot be read (%s)" % getattr(exc, "code", type(exc).__name__))
    payload, code = outcome
    return _gate_json(payload, code)


def _cmd_opencode_adopt(args: argparse.Namespace) -> int:
    """`quoin opencode adopt`: a human step recording evidence for a phase that
    finished outside a recorded run. The gate never treats it as run-verified."""
    from quoin.opencode_adapter import evidence, gate, runstore  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    try:
        stage, phase = gate.precheck(project_root, args.task, args.stage, args.phase)
    except gate.GateRefused as exc:
        return _gate_refusal(exc.code, "the request cannot be adopted")

    def work():
        snapshot = evidence.take_snapshot(project_root, args.task, phase)
        return evidence.record_evidence(project_root, args.task, stage, phase, "adopted", snapshot)

    try:
        entry, held_pid = _with_task_lock(project_root, args.task, work)
    except (runstore.RunStoreError, OSError) as exc:
        return _gate_refusal("store-unreadable", "the run store cannot be read (%s)" % getattr(exc, "code", type(exc).__name__))
    if entry is None:
        return _lock_refusal(project_root, args.task, held_pid)
    stage_part = "" if stage is None else f" --stage {stage}"
    quoted_root = shlex.quote(str(project_root))
    return _gate_json({
        "outcome": "ADOPTED",
        "entry": {
            "task": args.task, "stage": stage, "phase": phase, "origin": entry["origin"],
            "recorded_at": entry["recorded_at"], "coverage": entry["evidence"]["coverage"],
        },
        "next": f"quoin opencode gate --task {shlex.quote(args.task)}{stage_part} --phase {phase} --write --project-root {quoted_root}",
    }, 0)


def _is_posix() -> bool:
    return os.name == "posix"


def _exec_tui(argv, cwd, env) -> int:
    """Default launcher for `quoin opencode start`.

    On POSIX the TUI replaces this process, so it keeps the terminal and the
    process group, receives Ctrl-C, SIGQUIT and SIGTSTP directly and restores
    its own terminal state; its exit status is the command's. Nothing here
    needs to run afterwards (no lock, no run state). Returns only when the
    program could not be started."""
    import signal  # noqa: PLC0415

    sys.stdout.flush()
    sys.stderr.flush()
    if _is_posix():
        try:
            os.chdir(cwd)
            # Python ignores these two at startup and an ignored signal stays
            # ignored across exec; the TUI must see the default behaviour.
            for name in ("SIGPIPE", "SIGXFSZ"):
                number = getattr(signal, name, None)
                if number is not None:
                    signal.signal(number, signal.SIG_DFL)
            os.execve(argv[0], list(argv), env)
        except OSError as exc:
            print("quoin: opencode start failed: %s" % (exc.strerror or type(exc).__name__), file=sys.stderr)
            return 3
    # No exec here: keep Python alive but let the TUI own Ctrl-C.
    previous = None
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:  # not the main thread
        pass
    try:
        try:
            child = subprocess.Popen(list(argv), cwd=cwd, env=env)
        except OSError as exc:
            print("quoin: opencode start failed: %s" % (exc.strerror or type(exc).__name__), file=sys.stderr)
            return 3
        return child.wait()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


_opencode_tui_launcher = _exec_tui


def _cmd_opencode_start(args: argparse.Namespace) -> int:
    """`quoin opencode start`: open the OpenCode TUI with the compiled profile.

    Takes no task lock and writes no run state; the interface is interactive
    and nothing is captured."""
    from quoin.opencode_adapter import driver as _driver  # noqa: PLC0415
    from quoin.opencode_adapter import launch_env  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    redact = launch_env.Redactor()
    try:
        launch = _make_opencode_driver(project_root).prepare_interactive(args.profile)
    except _driver.PrepareRefused as exc:
        print(
            "quoin: opencode start refused (%s/%s): %s" % (exc.category, exc.code, redact(exc.message)),
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - an operator command never shows a traceback
        print("quoin: opencode start failed: %s" % type(exc).__name__, file=sys.stderr)
        return 3
    if args.dry_run:
        print(json.dumps({
            "argv": [redact(a) for a in launch.argv],
            "cwd": redact(str(launch.cwd)),
            "config_path": redact(str(launch.config_path)),
            "env_names": list(launch.env_names),
            "runtime_version": launch.runtime_version,
        }, sort_keys=True))
        return 0
    env = launch.launch_env.materialize()
    return _opencode_tui_launcher(list(launch.argv), str(launch.cwd), env)


def _make_opencode_driver(project_root):
    from quoin.opencode_adapter import driver as _driver  # noqa: PLC0415

    return _driver.OpenCodeDriver(project_root)


def _opencode_backoff(n: int) -> float:
    from quoin import supervisor as _supervisor  # noqa: PLC0415

    return _supervisor.default_backoff(n)


def _cmd_run_opencode(args: argparse.Namespace) -> int:
    """Run one workflow phase on the OpenCode runtime and print a JSON summary.

    The task lock is held for every write of task state (driver calls, the
    resume hint in the run record, the ``.result`` file); only the printed
    summary follows the release.
    """
    import signal  # noqa: PLC0415

    from quoin import supervisor as _supervisor  # noqa: PLC0415
    from quoin.opencode_adapter import driver as _driver  # noqa: PLC0415
    from quoin.opencode_adapter import phase_loop, runstore  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    ident = {
        "task": args.task, "stage": args.stage, "phase": args.phase, "profile": args.profile,
    }

    def refusal(category, code, message):
        return phase_loop.PhaseResult(
            outcome="REFUSED", reason=code,
            refusal={"category": category, "code": code, "message": message},
        )

    def emit(result, hint=None) -> int:
        print(json.dumps(phase_loop.summary(result, ident, hint), sort_keys=True))
        return phase_loop.exit_code(result)

    if not args.phase:
        return emit(refusal(
            "workflow-validation", "whole-task-unavailable", _supervisor.WHOLE_TASK_UNAVAILABLE
        ))
    try:
        runstore.check_task_name(args.task)
    except runstore.RunStoreError:
        return emit(refusal("workflow-validation", "invalid-task-name", "the task name is not valid"))

    cancel = phase_loop.CancelToken()

    def _on_signal(signum, _frame):
        cancel.cancel(signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _on_signal)
        except ValueError:  # not the main thread: no handlers, no cancel by signal
            break

    hint = None
    try:
        paths = _supervisor_paths(project_root, args.task)
        acquired, held_pid = _acquire_supervisor_lock(
            paths["memory_dir"], paths["lock"], paths["result"], args.task,
            args.max_relaunch, None, runtime="opencode",
        )
        if not acquired:
            holder = _read_json(paths["lock"])
            runtime = _lock_runtime(holder) if isinstance(holder, dict) else "unknown"
            result = refusal(
                None, "lock-held", f"the task lock is held by pid {held_pid} (runtime {runtime})"
            )
        else:
            try:
                try:
                    drv = _make_opencode_driver(project_root)
                    request = _driver.RunRequest(
                        project_root=project_root, task=args.task, stage=args.stage,
                        phase=args.phase, profile=args.profile, budget=args.budget,
                    )
                    result = phase_loop.run_phase(
                        drv, request, max_relaunch=args.max_relaunch, cancel=cancel,
                        new_run=args.new_run, backoff_fn=_opencode_backoff,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = phase_loop.PhaseResult(
                        outcome="ERROR", reason="driver-error: " + type(exc).__name__
                    )
                hint = phase_loop.resume_hint(result, ident, project_root)
                if result.run_id and hint:
                    try:
                        phase_loop.annotate_record(
                            project_root, result.run_id, hint, result.resume_blocked
                        )
                    except Exception:  # noqa: BLE001 - the hint still reaches the summary
                        pass
                if args.halt_on_abort and not paths["result"].exists():
                    _write_supervisor_result(paths["memory_dir"], paths["result"], result.outcome, 0)
            finally:
                _release_supervisor_lock(paths["lock"], os.getpid())
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
    return emit(result, hint)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quoin",
        description="Quoin — workflow state for stateless coding agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              quoin install                  Install for Claude Code (prompts for scope)
              quoin install --scope project  Install into ./.claude for this project only
              quoin doctor                   Health-check an existing install (read-only)
              quoin dashboard                Open the workflow dashboard in a browser
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
            "Codex generates or checks repo-local AGENTS.md scaffold only. "
            "OpenCode installs a repo-local .opencode/ scaffold under a project root."
        ),
        help=(
            "Install Quoin for a runtime: Claude globally to ~/.claude "
            "(default), Codex repo-local AGENTS.md scaffold, or OpenCode "
            "repo-local .opencode/ scaffold"
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
              quoin install --runtime opencode --project-root .
              quoin install --autocompact-pct 75
              quoin install --clear-autocompact-env
        """),
    )
    install_p.add_argument(
        "--runtime",
        choices=("claude", "codex", "opencode"),
        default="claude",
        help=(
            "Runtime target. 'claude' installs globally to ~/.claude; "
            "'codex' generates repo-local AGENTS.md only; 'opencode' installs "
            "a repo-local .opencode/ scaffold. Defaults to claude."
        ),
    )
    install_p.add_argument(
        "--project-root",
        default=".",
        help=(
            "Project root for --runtime codex AGENTS.md generation/checking, "
            "or --runtime opencode scaffold install; defaults to the current "
            "directory."
        ),
    )
    install_p.add_argument(
        "--profile",
        default=None,
        metavar="P",
        help=(
            "Only valid with --runtime opencode: an install profile label "
            "recorded in the install metadata. Omitted, a reinstall keeps "
            "the previously recorded label (null on a first install)."
        ),
    )
    install_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "For --runtime codex, check AGENTS.md without writing files "
            "(same behavior as 'quoin codex init --check'). For --runtime "
            "opencode, report what would change without writing files."
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
        "--scope", default=None, metavar="SCOPE",
        help="Install scope: user (~/.claude, default) or project (<CWD>/.claude). "
             "Omitted → interactive prompt. See 'Scope' below for values.",
    )
    install_p.add_argument(
        "--claude-md-variant",
        choices=("full", "slim"),
        default="full",
        help=(
            "CLAUDE.md variant to merge: 'full' (default) or 'slim' "
            "(IVG-164 project-scope pilot; requires --scope project)."
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
        choices=("claude", "codex", "opencode"),
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
            "Project root for Codex readiness checks or the OpenCode adapter's "
            "install/census checks; defaults to the current directory."
        ),
    )
    doctor_p.add_argument(
        "--source-dir",
        metavar="PATH",
        help="Override quoin data source directory for Codex/OpenCode adapter scripts.",
    )
    doctor_p.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "For --runtime codex, also run the deterministic repo-local smoke check. "
            "For --runtime opencode, run only the offline render/smoke checks "
            "(skip host checks that depend on this machine's install state)."
        ),
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Only valid with --runtime opencode: print a machine-readable report.",
    )
    doctor_p.add_argument(
        "--profile",
        default=None,
        metavar="PROFILE",
        help=(
            "Only valid with --runtime opencode (not with --smoke): also evaluate this "
            "profile's configuration the way a phase run does and report what would "
            "refuse it. Credentials are never resolved."
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

    opencode_p = sub.add_parser(
        "opencode",
        description="Repo-local OpenCode helpers (uninstall the .opencode/ scaffold; run an allowlisted Quoin script; explain or compile the runtime configuration; probe a gateway model).",
        help="Repo-local OpenCode helpers",
    )
    opencode_sub = opencode_p.add_subparsers(dest="opencode_command")

    opencode_uninstall_p = opencode_sub.add_parser(
        "uninstall",
        description="Remove everything Quoin owns under a project's .opencode/ scaffold.",
        help="Remove the repo-local .opencode/ scaffold Quoin owns",
    )
    opencode_uninstall_p.add_argument(
        "--project-root",
        default=".",
        help="Project root holding the .opencode/ scaffold; defaults to the current directory.",
    )
    opencode_uninstall_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without writing or deleting anything.",
    )

    opencode_script_p = opencode_sub.add_parser(
        "script",
        description="Run an allowlisted Quoin script by name, under the same output-safety policy an OpenCode role uses.",
        help="Run an allowlisted Quoin script by name",
    )
    opencode_script_p.add_argument(
        "--source-dir",
        metavar="PATH",
        help="Override quoin data source directory.",
    )
    opencode_script_p.add_argument(
        "name",
        help="Script name from the allowlist (e.g. path_resolve, validate_artifact).",
    )
    opencode_script_p.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the script unchanged.",
    )

    opencode_config_p = opencode_sub.add_parser(
        "config",
        description="Explain, compile or import the layered OpenCode runtime configuration.",
        help="Explain or compile the OpenCode runtime configuration",
    )
    opencode_config_sub = opencode_config_p.add_subparsers(dest="config_command")

    config_explain_p = opencode_config_sub.add_parser(
        "explain",
        description="Show how the layered configuration resolves for a profile and project.",
        help="Show how the configuration resolves",
    )
    config_explain_p.add_argument(
        "--profile", help="Profile name; defaults to the profile named by the project file."
    )
    config_explain_p.add_argument(
        "--project-root", default=".", help="Project root; defaults to the current directory."
    )
    config_explain_p.add_argument(
        "--redact",
        action="store_true",
        help="Mask endpoint hosts, host lists, keychain accounts and the output location.",
    )
    config_explain_p.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    config_compile_p = opencode_config_sub.add_parser(
        "compile",
        description="Compile the configuration into a native OpenCode config file outside the project.",
        help="Compile the native OpenCode config",
    )
    config_compile_p.add_argument("--profile", required=True, help="Profile name to compile.")
    config_compile_p.add_argument(
        "--project-root", default=".", help="Project root; defaults to the current directory."
    )
    config_compile_p.add_argument(
        "--output",
        metavar="DIR",
        help=(
            "Directory the compiled files are written inside (a directory, not a file); "
            "defaults to a per-project location under the state directory."
        ),
    )
    config_compile_p.add_argument(
        "--check",
        action="store_true",
        help="Compare the written files with a fresh build and report whether they are up to date; writes nothing.",
    )
    config_compile_p.add_argument(
        "--allow-unqualified",
        action="store_true",
        help="Accept models without a valid qualification record (refused for work under a managed policy).",
    )

    config_import_p = opencode_config_sub.add_parser(
        "import-preview",
        description=(
            "Propose a personal profile from the model mapping `quoin models` keeps. "
            "The preview writes nothing; --apply writes the profile after every provider "
            "model id is confirmed."
        ),
        help="Propose a personal profile from the quoin models mapping",
    )
    config_import_p.add_argument(
        "--profile-name", default="personal", help="Name of the proposed profile; defaults to personal."
    )
    config_import_p.add_argument("--apply", action="store_true", help="Write the proposed profile.")
    config_import_p.add_argument(
        "--confirm-model-id",
        action="append",
        default=[],
        metavar="ID",
        help="Confirm one provider model id (repeat for every id in the proposal).",
    )
    config_import_p.add_argument("--force", action="store_true", help="Replace an existing profile.")

    opencode_probe_p = opencode_sub.add_parser(
        "probe",
        description=(
            "Qualify a profile model against its gateway with a short synthetic handshake. "
            "The probe sends live requests that may be billed, and writes a qualification "
            "record that compile checks."
        ),
        help="Qualify a profile model against its gateway with a short synthetic handshake",
    )
    opencode_probe_p.add_argument("--profile", required=True, help="Profile name.")
    opencode_probe_p.add_argument(
        "--synthetic-only",
        action="store_true",
        help="Confirm that only synthetic prompts are sent; the probe refuses to run without it.",
    )
    opencode_probe_p.add_argument(
        "--model",
        metavar="NAME",
        help="Profile model name to probe; defaults to the profile's default model.",
    )
    opencode_probe_p.add_argument(
        "--project-root",
        default=None,
        help="Also apply this project's classification and policy; it must be classified work or personal.",
    )

    opencode_status_p = opencode_sub.add_parser(
        "status",
        description=(
            "Report the latest OpenCode phase run of a task, or one run id: state, "
            "child process, last event and the task lock. Read-only: nothing is "
            "repaired, reconciled or signalled."
        ),
        help="Report the latest OpenCode phase run of a task (read-only)",
    )
    opencode_status_p.add_argument(
        "--project-root", default=".", help="Project root the run belongs to; defaults to the current directory."
    )
    status_target = opencode_status_p.add_mutually_exclusive_group(required=True)
    status_target.add_argument("--task", help="Task name; reports its latest run.")
    status_target.add_argument("--run-id", help="Report this run id instead of a task's latest run.")
    opencode_status_p.add_argument("--json", action="store_true", help="Print the report as JSON.")

    opencode_start_p = opencode_sub.add_parser(
        "start",
        description=(
            "Open the OpenCode terminal interface in a project with the compiled profile "
            "configuration, an isolated data directory and the same checks as a phase run. "
            "The interface replaces this process; no events are captured and no task lock "
            "or run record is written."
        ),
        help="Open the OpenCode terminal interface with a compiled profile",
    )
    opencode_start_p.add_argument(
        "--project-root", default=".", help="Project to open; defaults to the current directory."
    )
    opencode_start_p.add_argument("--profile", required=True, help="Runtime profile to launch with.")
    opencode_start_p.add_argument(
        "--dry-run", action="store_true",
        help="Validate and print the command, directory and environment variable names without starting.",
    )

    opencode_gate_p = opencode_sub.add_parser(
        "gate",
        description=(
            "Evaluate one gated phase of a task with the deterministic checks and print one "
            "JSON line. Without --write nothing is written and no lock is taken. Exit 0 PASS, "
            "7 FAIL, 2 refused or unreadable store, 3 task lock held, 8 audit file not written."
        ),
        help="Evaluate a gated phase with the deterministic checks",
    )
    opencode_gate_p.add_argument("--task", required=True, help="Task name.")
    opencode_gate_p.add_argument(
        "--phase", required=True, type=_gated_phase, choices=_GATED_PHASE_CHOICES,
        help="Gated phase: " + ", ".join(_GATED_PHASE_CHOICES) + ".",
    )
    opencode_gate_p.add_argument("--stage", type=int, default=None, help="Stage number for a staged task.")
    opencode_gate_p.add_argument("--project-root", default=".", help="Project root; defaults to the current directory.")
    opencode_gate_p.add_argument(
        "--write", action="store_true",
        help="Write the audit file into the stage folder and record the verdict (takes the task lock).",
    )
    opencode_gate_p.add_argument(
        "--explanation-file", default=None,
        help="Text to carry in the audit file; read up to 64 KiB, never evaluated.",
    )
    opencode_gate_p.add_argument("--source-dir", default=None, help="Quoin source tree; defaults to the installed one.")

    opencode_adopt_p = opencode_sub.add_parser(
        "adopt",
        description=(
            "Human-only step: record evidence for a phase that finished outside a recorded run, "
            "from the tree as it is now. Takes the task lock. Exit 0, 2 refused, 3 lock held."
        ),
        help="Record evidence for a phase finished outside a recorded run",
    )
    opencode_adopt_p.add_argument("--task", required=True, help="Task name.")
    opencode_adopt_p.add_argument(
        "--phase", required=True, type=_gated_phase, choices=_GATED_PHASE_CHOICES,
        help="Gated phase: " + ", ".join(_GATED_PHASE_CHOICES) + ".",
    )
    opencode_adopt_p.add_argument("--stage", type=int, default=None, help="Stage number for a staged task.")
    opencode_adopt_p.add_argument("--project-root", default=".", help="Project root; defaults to the current directory.")

    dashboard_p = sub.add_parser(
        "dashboard",
        description=(
            "Launch the quoin workflow dashboard: a local read-only HTTP "
            "server (127.0.0.1) that visualizes .workflow_artifacts/ state."
        ),
        help="Launch the quoin workflow dashboard (local HTTP server, 127.0.0.1)",
    )
    dashboard_p.add_argument(
        "--port", type=int, default=8787,
        help="Port to listen on (default 8787; auto-increments if taken; 0 = ephemeral)",
    )
    dashboard_p.add_argument(
        "--no-browser", action="store_true",
        help="Do not open a browser window after startup",
    )
    dashboard_p.add_argument(
        "--project-root", default=".",
        help="Project root to scan for .workflow_artifacts/ (default: cwd)",
    )
    dashboard_p.add_argument(
        "--source-dir", metavar="PATH",
        help="Override quoin data source directory (same as 'quoin install --source-dir')",
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
            "appears, bounded by --max-relaunch and exponential backoff. "
            "With --runtime opencode it instead runs one workflow phase "
            "headlessly on OpenCode and prints a JSON summary."
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
        "--runtime",
        choices=("claude", "opencode"),
        default="claude",
        help="Runtime that drives the run (default: claude).",
    )
    run_p.add_argument(
        "--profile",
        default=None,
        help="Only with --runtime opencode: runtime profile to launch with.",
    )
    run_p.add_argument(
        "--phase",
        default=None,
        help=(
            "Only with --runtime opencode: the single workflow phase to run, "
            "for example plan or review."
        ),
    )
    run_p.add_argument(
        "--stage",
        default=None,
        help="Only with --runtime opencode: stage number for a multi-stage task.",
    )
    run_p.add_argument(
        "--new-run",
        action="store_true",
        help=(
            "Only with --runtime opencode: start the phase over instead of "
            "resuming an interrupted run."
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
        except ValueError as exc:
            install_p.error(str(exc))
        return _cmd_install(args)
    elif args.command == "dashboard":
        return _cmd_dashboard(args)
    elif args.command == "doctor":
        return _cmd_doctor(args)
    elif args.command == "codex":
        if args.codex_command == "init":
            return _cmd_codex_init(args)
        codex_p.print_help()
        return 1
    elif args.command == "opencode":
        if args.opencode_command == "uninstall":
            from quoin.opencode_adapter.install import run_uninstall

            return run_uninstall(args.project_root, args.dry_run, sys.stdout, sys.stderr)
        if args.opencode_command == "script":
            return _cmd_opencode_script(args)
        if args.opencode_command == "probe":
            return _cmd_opencode_probe(args)
        if args.opencode_command == "status":
            return _cmd_opencode_status(args)
        if args.opencode_command == "start":
            return _cmd_opencode_start(args)
        if args.opencode_command == "gate":
            return _cmd_opencode_gate(args)
        if args.opencode_command == "adopt":
            return _cmd_opencode_adopt(args)
        if args.opencode_command == "config":
            if args.config_command == "explain":
                return _cmd_opencode_config_explain(args)
            if args.config_command == "compile":
                return _cmd_opencode_config_compile(args)
            if args.config_command == "import-preview":
                return _cmd_opencode_config_import_preview(args)
            opencode_config_p.print_help()
            return 1
        opencode_p.print_help()
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
        # Bare 'quoin models' → show mapping.
        return _models._cmd_models_show(args)
    elif args.command == "run":
        if args.takeover and args.autonomous:
            run_p.error("--takeover cannot be combined with --autonomous")
        if args.runtime != "opencode":
            for flag, value in (
                ("--profile", args.profile), ("--phase", args.phase),
                ("--stage", args.stage), ("--new-run", args.new_run),
            ):
                if value:
                    _abort(f"quoin: {flag} is only valid with --runtime opencode")
            return _cmd_run(args)
        if args.takeover:
            _abort(
                "quoin: --takeover is only valid with --runtime claude; "
                "stop an opencode run with SIGTERM"
            )
        if not args.profile:
            _abort(
                "quoin: --runtime opencode needs --profile NAME "
                "(a profile from your OpenCode runtime configuration)"
            )
        if args.permission_mode == "bypassPermissions":
            _abort(
                "quoin: --permission-mode bypassPermissions is not available with "
                "--runtime opencode; approval prompts are never skipped"
            )
        if args.max_relaunch < 0:
            _abort("quoin: --max-relaunch must be 0 or more")
        return _cmd_run_opencode(args)

    parser.print_help()
    return 1
