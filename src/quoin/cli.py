"""quoin CLI entrypoint — argparse wrapper + data-tree resolution."""
from __future__ import annotations

import argparse
import importlib.resources
import os
import pathlib
import runpy
import sys
import textwrap
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

    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    # --scope project cannot combine with --runtime codex — check before dispatch
    scope: str = getattr(args, "scope", None) or "user"
    if scope.startswith("project") and getattr(args, "runtime", "claude") == "codex":
        _abort("quoin: --scope project is only valid with --runtime claude")

    if args.runtime == "codex":
        return _cmd_codex_init(args)
    if args.check:
        print(
            "quoin: install --check is only supported with --runtime codex; "
            "use 'quoin doctor' for Claude install health checks",
            file=sys.stderr,
        )
        return 2
    return _cmd_claude_install(args)


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


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.runtime == "codex":
        return _cmd_codex_doctor(args)

    from quoin import installer
    import shutil

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

    # Open-model router probe (user-scope only — home CCR paths are not project-scoped)
    if not is_project_mode:
        from quoin import ccr_config as _ccr
        # A direct PATH check, not a version query: `ccr -v`/`ccr version`
        # misreport a healthy v3 install as absent.
        ccr_installed = bool(shutil.which("ccr"))
        ccr_cfg = _ccr.ccr_config_path().exists()
        ccr_live = _ccr.probe_service()
        if ccr_installed or ccr_cfg:
            mode = "open via CCR" if (ccr_live and ccr_cfg) else "native"
            print(
                f"  {'✓' if ccr_installed else '·'} claude-code-router: "
                f"{'installed' if ccr_installed else 'not installed'}, "
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


def _cmd_run(args: argparse.Namespace) -> int:
    """`quoin run --autonomous <task>` — external supervisor entrypoint (T-08).

    Lazily imports `supervisor` (mirrors the router/models lazy-import
    convention, R-11/D-01) so the base install path stays import-clean.
    Resolves --project-root, builds the real headless launch_fn, and runs
    the relaunch loop to a terminal condition (SUCCESS/HALTED/ABORTED).

    NOTE (MIN-2): --budget is a no-op stub this release — cost is bounded
    only by --max-relaunch + exponential backoff. See autonomous-mode.md.
    """
    from quoin import supervisor as _supervisor  # noqa: PLC0415

    project_root = pathlib.Path(args.project_root).resolve()
    launch_fn = _supervisor.make_launch_fn(
        project_root,
        permission_mode=args.permission_mode,
    )
    result = _supervisor.supervise(
        args.task,
        project_root,
        launch_fn=launch_fn,
        max_relaunch=args.max_relaunch,
    )

    label = result.status
    if result.reason:
        label += f" ({result.reason})"
    print(f"quoin run: {label}")
    print(f"  task: {args.task}")
    print(f"  relaunches: {result.relaunches}")

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
            "Codex generates or checks repo-local AGENTS.md scaffold only."
        ),
        help=(
            "Install Quoin for a runtime: Claude globally to ~/.claude "
            "(default), or Codex repo-local AGENTS.md scaffold"
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
            "'project' checks <CWD>/.claude/. 'project:/path' checks /path/.claude/."
        ),
    )
    doctor_p.add_argument(
        "--project-root",
        default=".",
        help="Project root for Codex readiness checks; defaults to the current directory.",
    )
    doctor_p.add_argument(
        "--source-dir",
        metavar="PATH",
        help="Override quoin data source directory for Codex adapter scripts.",
    )
    doctor_p.add_argument(
        "--smoke",
        action="store_true",
        help="For --runtime codex, also run the deterministic repo-local smoke check.",
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
        "--permission-mode",
        default="allowedTools",
        help=(
            "Headless permission mode for the relaunch subprocess: "
            "'allowedTools' (default; scoped allow-list, per the T-01 POC) "
            "or 'bypassPermissions' (--dangerously-skip-permissions)."
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
        return _cmd_run(args)

    parser.print_help()
    return 1
