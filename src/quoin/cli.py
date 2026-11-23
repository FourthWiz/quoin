"""quoin CLI entrypoint — argparse wrapper + data-tree resolution."""
from __future__ import annotations

import argparse
import importlib.resources
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
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


def _prompt_scope() -> str:
    """Interactively ask the user for install scope when --scope is omitted.

    Returns 'user' or 'project'. Falls back to 'user' silently in non-interactive
    (no tty) environments so CI/pipe usage is unaffected.
    """
    if not sys.stdin.isatty():
        print("quoin: non-interactive mode — defaulting to --scope user (plugin loads in every session)")
        return "user"

    print()
    print("Where should quoin install?")
    print("  g) Global  — user scope: every Claude Code session on this machine")
    print("  p) Project — project scope: only sessions started in this folder")
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


def _claude_config_dir() -> pathlib.Path:
    """Claude Code's user config folder: CLAUDE_CONFIG_DIR when set, else ~/.claude."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return pathlib.Path(override).expanduser() if override else pathlib.Path.home() / ".claude"


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
        return _claude_config_dir()

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
    addons: Tuple[Tuple[str, Optional[str]], ...] = ()


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
                home_dest_root=_claude_config_dir(),
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
    return _InstallAnswers(modes["workflow-tasks"], tuple(warned), _choose_addons(args))


def _choose_addons(args: argparse.Namespace) -> Tuple[Tuple[str, Optional[str]], ...]:
    """Add-ons named with --with-addon, plus those the person accepts when asked."""
    from quoin import addons  # noqa: PLC0415

    chosen = [addons.parse_choice(text) for text in (getattr(args, "with_addon", None) or [])]
    if getattr(args, "no_addons", False) or not install_prompts.is_interactive():
        return tuple(chosen)
    named = {name for name, _ in chosen}
    found = addons.discovered()
    for known in addons.offered():
        if known.name in named:
            continue
        if known.name in found:
            chosen.append((known.name, None))
            continue
        if install_prompts.ask_yes_no(
            f"Also install the {known.title} add-on ({known.description})? "
            f"It is a separate package, {known.package}.",
            default=False,
        ):
            chosen.append((known.name, None))
    return tuple(chosen)


def _cmd_claude_install(args: argparse.Namespace) -> int:
    from quoin import installer

    source_dir = _resolve_source_dir(args.source_dir)

    if getattr(args, "scope", None) is None:
        args.scope = _prompt_scope()

    dest_root = _resolve_dest_root(args)
    scope: str = getattr(args, "scope", None) or "user"
    is_project_mode = scope.startswith("project")
    plugin_scope = "project" if is_project_mode else "user"
    project_dir = dest_root.parent if is_project_mode else None
    claude_md_path = dest_root.parent / "CLAUDE.md" if is_project_mode else dest_root / "CLAUDE.md"

    if is_project_mode and getattr(args, "check", False):
        _abort("quoin: --scope project cannot combine with --check")

    try:
        autocompact_pct, autocompact_window, clear_autocompact_env = _validate_autocompact_args(args)
    except ValueError as exc:
        _abort(f"quoin: {exc}")
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
        home_dest_root=_claude_config_dir(),
        cwd_dest_root=pathlib.Path.cwd() / ".claude",
    )
    if "workflow-tasks" not in answers.warned_mods:
        for warning in warnings:
            print(warning, file=sys.stderr)
    if mod_errors:
        for error in mod_errors:
            print(error, file=sys.stderr)
        return 1

    missing = installer.check_prerequisites()
    if missing:
        print("quoin: Missing required tools:", file=sys.stderr)
        for tool in missing:
            print(f"       - {tool}", file=sys.stderr)
        print("\nInstall them and re-run this script.", file=sys.stderr)
        return 1

    stage_dir = installer.default_stage_dir()
    # Earlier installs wrote an install record for the auto-resume hand-off,
    # which no longer exists; nothing reads the file now.
    for old_record in (stage_dir / "quoin-runtime.json", dest_root / "quoin-runtime.json"):
        try:
            old_record.unlink()
        except OSError:
            pass
    try:
        version = installer.stage_plugin(source_dir, stage_dir)
        print(f"Staged plugin {version} from {source_dir} in {stage_dir}")
        action = installer.install_plugin(stage_dir, scope=plugin_scope, cwd=project_dir)
    except (installer.InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"quoin: plugin install failed: {exc}", file=sys.stderr)
        return 1
    print(f"Plugin {installer.PLUGIN_ID}: {action}ed at {plugin_scope} scope")

    cleanup = installer.remove_legacy_deployment(dest_root, claude_md_path)
    if cleanup.removed_files or cleanup.removed_hook_stanzas or cleanup.removed_rules_block:
        print(
            f"Removed the earlier file-by-file install under {dest_root}: "
            f"{cleanup.removed_files} file(s), {cleanup.removed_hook_stanzas} hook stanza(s)"
            + (", the quoin rules block in " + str(claude_md_path) if cleanup.removed_rules_block else "")
        )
    for path in cleanup.kept_files:
        print(f"quoin: warning: kept {path}: it was changed after an earlier install", file=sys.stderr)

    installer.apply_workflow_tasks(source_dir, dest_root, mode=wt_mode)
    installer.apply_autocompact_env(
        dest_root, pct=autocompact_pct, window=autocompact_window, clear=clear_autocompact_env
    )

    if args.dev:
        installer.install_dev_deps()

    if answers.addons:
        from quoin import addons  # noqa: PLC0415

        for name, spec in answers.addons:
            if addons.install(name, spec) != 0:
                print(f"quoin: warning: the {name} add-on did not install; retry with 'quoin addons install {name}'",
                      file=sys.stderr)

    print("Restart Claude Code to load the plugin; its skills are named /quoin:<skill>.")
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


def _agent_skills_builder(source_dir: pathlib.Path):
    """The source tree's `build_agent_skills` module, loaded from its file."""
    import importlib.util  # noqa: PLC0415

    path = source_dir / "core" / "scripts" / "build_agent_skills.py"
    if not path.is_file():
        _abort(f"quoin: cannot find {path}; pass --source-dir <path-to-clone>/quoin if needed")
    spec = importlib.util.spec_from_file_location("_quoin_build_agent_skills", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _check_agent_skills(source_dir: pathlib.Path, out: pathlib.Path) -> int:
    builder = _agent_skills_builder(source_dir)
    missing = [
        builder.agent_name(s) for s in builder.KEPT_SKILLS
        if (source_dir / "adapters" / "claude" / "skills" / s / "SKILL.md").is_file()
        and not (out / builder.agent_name(s) / "SKILL.md").is_file()
    ]
    if missing or not (out / builder.SUPPORT).is_dir():
        print(f"quoin: {out} is missing {', '.join(missing) or builder.SUPPORT}; run 'quoin install --runtime codex'")
        return 1
    print(f"Agent Skills folder {out}: every quoin skill is present")
    return 0


def _cmd_codex_init(args: argparse.Namespace) -> int:
    """`quoin install --runtime codex`: the kept skills as `.agents/skills/` in the project."""
    source_dir = _resolve_source_dir(args.source_dir)
    out = pathlib.Path(args.project_root).resolve() / ".agents" / "skills"
    if args.check:
        return _check_agent_skills(source_dir, out)
    result = _agent_skills_builder(source_dir).build(out, source_dir)
    print(f"Wrote {len(result['skills'])} skills to {out}: {', '.join(result['skills'])}")
    print("Codex and OpenCode read them from there.")
    return 0


def _cmd_codex_doctor(args: argparse.Namespace) -> int:
    source_dir = _resolve_source_dir(args.source_dir)
    return _check_agent_skills(source_dir, pathlib.Path(args.project_root).resolve() / ".agents" / "skills")


def _cmd_skills(args: argparse.Namespace) -> int:
    """`quoin skills build --out DIR`: the kept skills as a plain Agent Skills folder."""
    source_dir = _resolve_source_dir(args.source_dir)
    result = _agent_skills_builder(source_dir).build(pathlib.Path(args.out), source_dir)
    print(f"Wrote {len(result['skills'])} skills to {result['out']}")
    return 0


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
    dest_root = _resolve_dest_root(args)
    plugin_scope = "project" if is_project_mode else "user"
    project_dir = dest_root.parent if is_project_mode else None
    claude_md_path = dest_root.parent / "CLAUDE.md" if is_project_mode else dest_root / "CLAUDE.md"
    print(f"Checking install scope: {scope}  →  {dest_root}")
    print()

    if not is_project_mode:
        for tool in ("claude", "git", "gh", "npx"):
            found = shutil.which(tool) is not None
            print(f"  {'✓' if found else '✗'} {tool}")
            if tool in ("claude", "git") and not found:
                errors.append(f"Required tool missing: {tool}")
        print()

    entry = installer.installed_plugin(plugin_scope, project_dir) if shutil.which("claude") else None
    if entry is None:
        print(f"  ✗ plugin {installer.PLUGIN_ID} — not installed at {plugin_scope} scope")
        errors.append(f"plugin {installer.PLUGIN_ID} is not installed; run 'quoin install'")
    else:
        enabled = entry.get("enabled", True)
        print(
            f"  {'✓' if enabled else '✗'} plugin {installer.PLUGIN_ID} {entry.get('version', '?')} "
            f"({'enabled' if enabled else 'disabled'})"
        )
        if not enabled:
            errors.append(f"plugin {installer.PLUGIN_ID} is disabled; run 'claude plugin enable {installer.PLUGIN_ID}'")

    stage_dir = installer.default_stage_dir()
    staged = (stage_dir / ".claude-plugin" / "plugin.json").is_file()
    print(f"  {'✓' if staged else '✗'} staged plugin at {stage_dir}")
    if not staged:
        errors.append(f"no staged plugin at {stage_dir}; run 'quoin install'")

    if installer.legacy_deployment_present(dest_root, claude_md_path):
        print(f"  ⚠ an earlier file-by-file install is still under {dest_root}")
        warnings.append(
            f"an earlier file-by-file install is still under {dest_root}; "
            "'quoin install' removes it"
        )
    print()

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


def _cmd_addons(args: argparse.Namespace) -> int:
    """`quoin addons [install NAME[=SPEC] | remove NAME]`."""
    from quoin import addons  # noqa: PLC0415

    if args.addons_command == "install":
        name, spec = addons.parse_choice(args.addon)
        return addons.install(name, spec)
    if args.addons_command == "remove":
        addon = addons.load(args.addon)
        if addon is None:
            print(f"quoin: the {args.addon} add-on is not installed")
            return 1
        return int(addon.remove() or 0)
    for row in addons.status():
        state = "installed" if row["installed"] else ("available" if row["available"] else "not yet available")
        note = f"; {row['note']}" if row["note"] and not row["installed"] else ""
        print(f"{row['name']:<10} {state:<18} {row['package']}{note}")
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    """`quoin open [setup|remove|probe|launch]`: the open-model pack."""
    from quoin import open_pack  # noqa: PLC0415

    action = args.open_action or "launch"
    if action == "setup":
        try:
            result = open_pack.setup(_resolve_source_dir(args.source_dir), overwrite_profile=args.reset_profile)
        except FileNotFoundError as exc:
            print(f"quoin open: {exc}", file=sys.stderr)
            return 2
        print(f"Open-model pack copied to {result['pack']}")
        state = "written" if result["profile_written"] else "kept (pass --reset-profile to replace it)"
        print(f"Launch profile {result['profile']}: {state}")
        print("Check the gateway with `quoin open probe`, then start a session with `quoin open`.")
        return 0
    if action == "remove":
        removed = open_pack.remove()["removed"]
        print("Removed " + ", ".join(removed) if removed else "Nothing to remove.")
        return 0
    if action == "probe":
        return open_pack.probe()
    return open_pack.launch(list(args.claude_args))


def _cmd_run(args: argparse.Namespace) -> int:
    """`quoin run --autonomous <task>`: each unfinished stage as one headless `/goal` session."""
    from quoin import goal_run  # noqa: PLC0415

    outcome = goal_run.run(
        args.project_root,
        args.task,
        max_budget_usd=args.max_budget_usd,
        dry_run=args.dry_run,
        model=args.model,
    )
    print(f"quoin run: {outcome.status}: {outcome.message}")
    if outcome.stages:
        print(f"  stages done this run: {', '.join(outcome.stages)}")
    return outcome.exit_code()



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
              quoin run --autonomous <task>  Run a staged task unattended, one /goal per stage

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
            "Install Quoin for a runtime. Claude installs the quoin plugin for Claude Code "
            "(user scope by default) and removes an earlier file-by-file ~/.claude install. "
            "Codex generates or checks repo-local AGENTS.md scaffold only."
        ),
        help=(
            "Install Quoin for a runtime: the Claude Code plugin "
            "(default) or Codex repo-local AGENTS.md scaffold"
        ),
        epilog=textwrap.dedent("""\
            Scope (--scope):
              user             install the plugin at user scope (default; loads in every session)
              project          install the plugin at project scope for <CWD>
              project:/path    install the plugin at project scope for /path
            Note: a project-scope plugin loads only in sessions started in that
            folder, not in sessions started inside a repository below it.

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
            "'codex' writes the kept skills to <project>/.agents/skills (Agent Skills, also read by OpenCode). Defaults to claude."
        ),
    )
    install_p.add_argument(
        "--project-root",
        default=".",
        help=(
            "Project root for --runtime codex (.agents/skills is written there); "
            "defaults to the current directory."
        ),
    )
    install_p.add_argument(
        "--check",
        action="store_true",
        help=(
            "For --runtime codex, check .agents/skills holds every quoin skill without writing files."
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
        "--scope", default=None, metavar="SCOPE",
        help="Plugin scope: user (default) or project (<CWD>). "
             "Omitted → interactive prompt. See 'Scope' below for values.",
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
        "--with-addon",
        action="append",
        metavar="NAME[=SPEC]",
        help=(
            "Also install an add-on from its own package (repeatable), e.g. --with-addon cockpit. "
            "SPEC is any pip requirement, such as a git URL, for an add-on not on PyPI yet. "
            "'quoin addons' lists them."
        ),
    )
    install_p.add_argument(
        "--no-addons",
        action="store_true",
        help="Do not ask about add-ons.",
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

    skills_p = sub.add_parser(
        "skills",
        description="Write quoin's kept skills as a plain Agent Skills folder (for Codex, OpenCode and others).",
        help="Write the kept skills as an Agent Skills folder",
    )
    skills_sub = skills_p.add_subparsers(dest="skills_command")
    skills_build_p = skills_sub.add_parser("build", help="Write the folder")
    skills_build_p.add_argument("--out", required=True, help="Folder to write, e.g. <project>/.agents/skills")
    skills_build_p.add_argument("--source-dir", default=None, help="Source tree (default: this package).")

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

    addons_p = sub.add_parser(
        "addons",
        description=(
            "Optional add-ons that live in their own packages: list them, install one "
            "(pip-installs its package into this environment, then runs its own install) "
            "or remove one."
        ),
        help="List, install or remove add-ons (cockpit, opencode)",
    )
    addons_sub = addons_p.add_subparsers(dest="addons_command")
    addons_install_p = addons_sub.add_parser("install", help="Install an add-on")
    addons_install_p.add_argument("addon", metavar="NAME[=SPEC]")
    addons_remove_p = addons_sub.add_parser("remove", help="Undo an add-on's own install")
    addons_remove_p.add_argument("addon", metavar="NAME")

    open_p = sub.add_parser(
        "open",
        description=(
            "Run Claude Code on an open model through a gateway. 'setup' copies the "
            "open-model pack and writes ~/.config/quoin/open.json (Qwen3 Coder through "
            "OpenRouter by default; edit it for another model); 'probe' checks the "
            "gateway; 'remove' deletes both; 'launch' (the default) starts claude with "
            "the profile's environment and the pack. Claude Code's own settings and "
            "the default quoin plugin are never changed."
        ),
        help="Run Claude Code on an open model (setup, probe, remove, launch)",
    )
    open_p.add_argument("open_action", nargs="?", choices=("setup", "remove", "probe", "launch"), default=None)
    open_p.add_argument("claude_args", nargs=argparse.REMAINDER, help="Arguments passed to claude on launch.")
    open_p.add_argument("--source-dir", default=None, help="Source tree for setup (default: this package).")
    open_p.add_argument("--reset-profile", action="store_true", help="With setup, replace an existing profile.")

    run_p = sub.add_parser(
        "run",
        description=(
            "Run a staged task unattended: each unfinished stage of "
            ".workflow_artifacts/<task>/architecture.md becomes one headless "
            '`claude -p "/goal ..."` session whose condition is the stage\'s '
            "acceptance checks; a task with no stage list runs its brief as one "
            "goal. Rerunning continues at the first unfinished stage. Exit 0 "
            "complete, 1 stopped, 2 invalid, 75 paused at a usage limit."
        ),
        help="Run a staged task unattended, one /goal session per stage",
    )
    run_p.add_argument("task", help="Task name (matches the .workflow_artifacts/<task> folder).")
    run_p.add_argument(
        "--autonomous",
        action="store_true",
        help="Accepted for the callers that pass it; the command is always unattended.",
    )
    run_p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .workflow_artifacts/ (default: cwd).",
    )
    run_p.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="Spending cap for each stage's session, passed to claude as --max-budget-usd.",
    )
    run_p.add_argument(
        "--model",
        default=None,
        help="Model for each stage's session (an alias such as sonnet, or a full id).",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command for the next unfinished stage and stop.",
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
    elif args.command == "skills":
        if args.skills_command == "build":
            return _cmd_skills(args)
        skills_p.print_help()
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
    elif args.command == "addons":
        return _cmd_addons(args)
    elif args.command == "open":
        return _cmd_open(args)
    elif args.command == "run":
        return _cmd_run(args)

    parser.print_help()
    return 1
