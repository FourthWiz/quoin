"""Install the quoin Claude Code plugin and retire the file-by-file deployment
that earlier versions wrote into ~/.claude (or <project>/.claude)."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional

PLUGIN_NAME = "quoin"
MARKETPLACE_NAME = "quoin"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"

# What the plugin needs at runtime, relative to the source tree: quoin/ in a
# checkout, quoin/data/ inside the wheel.
PLUGIN_PATHS = (
    ".claude-plugin",
    "adapters/claude/skills",
    "hooks",
    "scripts",
    "core",
    "memory",
    "CLAUDE.slim.md",
    "QUICKSTART.md",
)

STAGE_DIR_ENV = "QUOIN_PLUGIN_DIR"
_STAGE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules", "tests")


class InstallError(RuntimeError):
    """A step of the plugin install failed; the message says which."""


def default_stage_dir() -> pathlib.Path:
    """Where the plugin is staged before Claude Code copies it into its cache."""
    override = os.environ.get(STAGE_DIR_ENV)
    if override:
        return pathlib.Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(pathlib.Path.home() / ".local" / "share")
    return pathlib.Path(base) / "quoin" / "plugin"


def _tree_digest(root: pathlib.Path) -> str:
    """Hash of every staged file except plugin.json, which carries the result."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == ".claude-plugin/plugin.json":
            continue
        digest.update(rel.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def stage_plugin(source_dir: pathlib.Path, stage_dir: pathlib.Path) -> str:
    """Copy the plugin's runtime files from source_dir into stage_dir.

    The staged plugin.json version gets a content-hash suffix, so Claude Code
    re-caches the plugin whenever any shipped file changes, including in a
    checkout where the package version stays the same. Returns that version.
    """
    missing = [rel for rel in PLUGIN_PATHS if not (source_dir / rel).exists()]
    if missing:
        raise InstallError(f"{source_dir} is missing plugin files: {', '.join(missing)}")
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    fresh = stage_dir.with_name(stage_dir.name + ".new")
    old = stage_dir.with_name(stage_dir.name + ".old")
    for leftover in (fresh, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    for rel in PLUGIN_PATHS:
        src, dst = source_dir / rel, fresh / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_STAGE_IGNORE)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # Claude Code runs hook commands directly, and a wheel install does not
    # always keep the executable bit.
    for script in (fresh / "hooks").glob("*.sh"):
        script.chmod(script.stat().st_mode | 0o111)
    manifest = fresh / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    base = str(data.get("version", "0")).split("+", 1)[0]
    data["version"] = f"{base}+{_tree_digest(fresh)[:12]}"
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if stage_dir.exists():
        stage_dir.rename(old)
    fresh.rename(stage_dir)
    if old.exists():
        shutil.rmtree(old)
    return data["version"]


def _run_claude(args: list[str], cwd: Optional[pathlib.Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", *args], cwd=cwd, capture_output=True, text=True, timeout=300, check=False
    )


def _require_ok(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"`claude {what}` failed (exit {result.returncode}): {detail}")


def installed_plugin(scope: str = "user", cwd: Optional[pathlib.Path] = None) -> Optional[dict]:
    """The `claude plugin list` entry for quoin at this scope, or None."""
    result = _run_claude(["plugin", "list", "--json"], cwd=cwd)
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        return None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID and entry.get("scope") == scope:
            return entry
    return None


def install_plugin(
    stage_dir: pathlib.Path, *, scope: str = "user", cwd: Optional[pathlib.Path] = None
) -> str:
    """Register stage_dir as the quoin marketplace and install or update the plugin.

    Returns "install" or "update". Re-adding a known marketplace is a no-op in
    the claude CLI, so this is safe to repeat.
    """
    if shutil.which("claude") is None:
        raise InstallError("the claude CLI is not on PATH; install Claude Code first")
    _require_ok(
        _run_claude(["plugin", "marketplace", "add", str(stage_dir), "--scope", scope], cwd=cwd),
        "plugin marketplace add",
    )
    _require_ok(
        _run_claude(["plugin", "marketplace", "update", MARKETPLACE_NAME], cwd=cwd),
        "plugin marketplace update",
    )
    action = "update" if installed_plugin(scope, cwd) else "install"
    _require_ok(
        _run_claude(["plugin", action, PLUGIN_ID, "--scope", scope], cwd=cwd), f"plugin {action}"
    )
    return action


# ── Retiring the file-by-file deployment ─────────────────────────────────────

class LegacyCleanup(NamedTuple):
    removed_files: int
    kept_files: tuple[str, ...]
    removed_hook_stanzas: int
    removed_rules_block: bool
    had_manifest: bool


def _remove_manifest_files(dest_root: pathlib.Path, manifest: dict[str, str]) -> tuple[int, list[str]]:
    """Delete every file the old manifest lists while it still holds what quoin wrote.

    Opt-in mod folders are skipped: the mods are still managed file by file.
    An edited file is kept and reported.
    """
    protected = tuple(f"skills/{mod.name}/" for mod in OPT_IN_MODS)
    removed, kept = 0, []
    for rel, digest in sorted(manifest.items()):
        if rel.startswith(protected):
            continue
        target = dest_root / rel if rel in _LEGACY_TOP_LEVEL_FILES else _prunable_path(dest_root, rel)
        if target is None or target.is_symlink() or not target.is_file():
            continue
        try:
            unchanged = _sha256_file(target) == digest
        except OSError:
            unchanged = False
        if not unchanged:
            kept.append(str(target))
            continue
        try:
            target.unlink()
        except OSError:
            kept.append(str(target))
            continue
        removed += 1
        top = dest_root / pathlib.PurePosixPath(rel).parts[0]
        parent = target.parent
        while parent != dest_root:
            try:
                parent.rmdir()
            except OSError:
                break
            if parent == top:
                break
            parent = parent.parent
    return removed, kept


def _owned_hook_stanza(stanza, hooks_dir: pathlib.Path) -> bool:
    """True when every command in the stanza is one of quoin's old hook scripts."""
    if not isinstance(stanza, dict) or not isinstance(stanza.get("hooks"), list):
        return False
    commands = [h.get("command") for h in stanza["hooks"] if isinstance(h, dict)]
    if not commands:
        return False
    for command in commands:
        if not isinstance(command, str):
            return False
        path = pathlib.Path(command.strip().strip('"')).expanduser()
        if path.name not in _LEGACY_HOOK_BASENAMES:
            return False
        try:
            if path.resolve().parent != hooks_dir:
                return False
        except (OSError, RuntimeError):
            return False
    return True


def _remove_legacy_hook_stanzas(dest_root: pathlib.Path) -> int:
    """Drop quoin's old hook stanzas from dest_root/settings.json; returns how many."""
    settings_path = dest_root / "settings.json"
    if not settings_path.is_file():
        return 0
    try:
        original = settings_path.read_text(encoding="utf-8")
        settings = json.loads(original)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"quoin: warning: could not read {settings_path}; old hook stanzas left in place", file=sys.stderr)
        return 0
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return 0
    hooks_dir = (dest_root / "hooks").resolve()
    removed = 0
    for event in list(hooks):
        stanzas = hooks[event]
        if not isinstance(stanzas, list):
            continue
        kept = [s for s in stanzas if not _owned_hook_stanza(s, hooks_dir)]
        removed += len(stanzas) - len(kept)
        if kept:
            hooks[event] = kept
        elif len(stanzas):
            del hooks[event]
    if not removed:
        return 0
    if not hooks:
        del settings["hooks"]
    backup = _settings_backup_path(settings_path)
    shutil.copyfile(settings_path, backup)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    _prune_settings_backups(settings_path, _settings_backup_keep())
    return removed


def _remove_rules_block(claude_md_path: pathlib.Path) -> bool:
    """Remove every quoin marker block from a CLAUDE.md; the user's own lines stay."""
    if not claude_md_path.is_file():
        return False
    content = claude_md_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"\n?{re.escape(_MARKER_START)}.*?{re.escape(_MARKER_END)}\n?", re.DOTALL
    )
    updated, count = pattern.subn("\n", content)
    if not count:
        return False
    updated = re.sub(r"\n{3,}", "\n\n", updated).strip("\n")
    claude_md_path.write_text(updated + "\n" if updated else "", encoding="utf-8")
    return True


def remove_legacy_deployment(dest_root: pathlib.Path, claude_md_path: pathlib.Path) -> LegacyCleanup:
    """Undo what earlier quoin versions copied into dest_root.

    Files go only when the old manifest lists them and they are unchanged.
    The quoin hook stanzas leave settings.json (other hooks stay), and the
    marked rules block leaves CLAUDE.md. Safe to run when nothing is there.
    """
    manifest = read_deploy_manifest(dest_root)
    removed, kept = (0, [])
    if manifest is not None:
        removed, kept = _remove_manifest_files(dest_root, manifest)
        try:
            (dest_root / MANIFEST_FILENAME).unlink()
        except OSError:
            pass
    stanzas = _remove_legacy_hook_stanzas(dest_root)
    block = _remove_rules_block(claude_md_path)
    return LegacyCleanup(removed, tuple(kept), stanzas, block, manifest is not None)


def legacy_deployment_present(dest_root: pathlib.Path, claude_md_path: pathlib.Path) -> bool:
    """True when an earlier file-by-file install is still visible under dest_root."""
    if (dest_root / MANIFEST_FILENAME).exists():
        return True
    try:
        if _MARKER_START in claude_md_path.read_text(encoding="utf-8"):
            return True
    except OSError:
        pass
    try:
        settings = json.loads((dest_root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    hooks_dir = (dest_root / "hooks").resolve()
    return isinstance(hooks, dict) and any(
        _owned_hook_stanza(s, hooks_dir)
        for stanzas in hooks.values() if isinstance(stanzas, list)
        for s in stanzas
    )


def apply_autocompact_env(
    dest_root: pathlib.Path, *, pct: Optional[int], window: Optional[int], clear: bool
) -> None:
    """Write or clear quoin's two opt-in autocompact env keys in settings.json."""
    if not clear and pct is None and window is None:
        return
    settings_path = dest_root / "settings.json"
    original = None
    settings: dict = {}
    if settings_path.is_file():
        try:
            original = settings_path.read_text(encoding="utf-8")
            loaded = json.loads(original)
            settings = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            print(f"quoin: warning: could not read {settings_path}; autocompact env not changed", file=sys.stderr)
            return
    if clear:
        changed = _clear_env(settings)
    else:
        changed = _merge_env(settings, pct=pct, window=window)
        if changed is None:
            print(
                f"quoin: {settings_path}['env'] exists and is not an object; autocompact env not written",
                file=sys.stderr,
            )
            return
    if not changed:
        return
    if original is not None:
        shutil.copyfile(settings_path, _settings_backup_path(settings_path))
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    _prune_settings_backups(settings_path, _settings_backup_keep())
    print(("Cleared" if clear else "Set") + f" autocompact env in {settings_path}")


# ── Carried over: old manifest reading, opt-in mods, settings helpers ────────

# Every file the install writes into dest_root, with the sha256 of the bytes
# written. The next install deletes the files it no longer ships, but only
# while they still hold exactly what quoin wrote.
MANIFEST_FILENAME = "quoin-manifest.json"


MANIFEST_SCHEMA = 1


# Subdirectories under dest_root that quoin deploys to.
# assert_no_placeholders scans ONLY these (positive allowlist) to avoid
# false positives from Claude Code internal directories (projects/, todos/, etc.).
_QUOIN_DEPLOYED_SUBDIRS = frozenset({"skills", "scripts", "core", "memory", "hooks"})


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_deploy_manifest(dest_root: pathlib.Path) -> Optional[dict[str, str]]:
    """Return the previous install's {relative path: sha256}, or None.

    None means there is no usable manifest (first install with pruning, or an
    unreadable file); the caller then prunes nothing.
    """
    path = dest_root / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        data = None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or data.get("schema") != MANIFEST_SCHEMA:
        print(f"quoin: warning: ignoring unreadable deploy manifest {path}", file=sys.stderr)
        return None
    return {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)}


def _prunable_path(dest_root: pathlib.Path, rel: str) -> Optional[pathlib.Path]:
    """Map a manifest entry to a path under a quoin deploy subdirectory, or None."""
    pure = pathlib.PurePosixPath(rel)
    parts = pure.parts
    if pure.as_posix() != rel or pure.is_absolute() or ".." in parts:
        return None
    if len(parts) < 2 or parts[0] not in _QUOIN_DEPLOYED_SUBDIRS:
        return None
    return dest_root.joinpath(*parts)


# Canonical hook script basenames registered by quoin
_QUOIN_HOOK_BASENAMES = frozenset({
    "userpromptsubmit.sh",
    "precompact.sh",
    "postcompact.sh",
    "sessionstart.sh",
    "sessionend.sh",
    "stop.sh",
})


_MARKER_START = "# === DEV WORKFLOW START ==="


_MARKER_END = "# === DEV WORKFLOW END ==="


class OptInMod(NamedTuple):
    """An opt-in mod: its folder name (also its plugin name and flag stem) and
    the allowlist of source files that are deployed.

    A NamedTuple rather than a dataclass: several tests load this module with
    spec_from_file_location without registering it in sys.modules, which a
    dataclass cannot survive when annotations are strings.
    """

    name: str
    files: tuple[str, ...]


def mod_source(mod: OptInMod, source_dir: pathlib.Path) -> pathlib.Path:
    return source_dir / "plugins" / mod.name


def mod_dest(mod: OptInMod, dest_root: pathlib.Path) -> pathlib.Path:
    return dest_root / "skills" / mod.name


def mod_state(mod: OptInMod, dest_root: pathlib.Path) -> str:
    """Return "absent", "installed" or "foreign" for the mod folder. Never raises."""
    folder = mod_dest(mod, dest_root)
    try:
        if folder.is_symlink():
            return "foreign"
        if not folder.exists():
            return "absent"
        if not folder.is_dir():
            return "foreign"
        data = json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name") == mod.name:
            return "installed"
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return "foreign"


def missing_mod_sources(mod: OptInMod, source_dir: pathlib.Path) -> list[str]:
    src = mod_source(mod, source_dir)
    return [rel for rel in mod.files if not (src / rel).is_file()]


def mod_preflight(
    mod: OptInMod,
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    *,
    mode: Optional[str],
    is_project_mode: bool,
    home_dest_root: pathlib.Path,
    cwd_dest_root: pathlib.Path,
) -> tuple[list[str], list[str]]:
    """Pure check run before anything is written; returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if mode is None:
        return errors, warnings
    state = mod_state(mod, dest_root)
    folder = mod_dest(mod, dest_root)
    if mode == "with":
        missing = missing_mod_sources(mod, source_dir)
        if missing:
            errors.append(f"quoin: {mod.name} source files missing: " + ", ".join(missing))
        if state == "foreign":
            errors.append(
                f"quoin: {folder} exists and is not the quoin {mod.name} mod "
                "(or is a symlink); move it aside and re-run"
            )
        if is_project_mode and mod_state(mod, home_dest_root) == "installed":
            errors.append(
                f"quoin: user-scope copy installed at {mod_dest(mod, home_dest_root)}; "
                f"remove it first (--remove-{mod.name} --scope user) to avoid a double load"
            )
        if (
            not is_project_mode
            and cwd_dest_root.resolve() != dest_root.resolve()
            and mod_state(mod, cwd_dest_root) == "installed"
        ):
            warnings.append(
                f"quoin: project copy at {mod_dest(mod, cwd_dest_root)} "
                "will load alongside this user copy"
            )
    elif mode == "remove" and state == "foreign":
        warnings.append(f"quoin: {folder} is not the quoin {mod.name} mod; leaving it untouched")
    return errors, warnings


def deploy_mod(
    mod: OptInMod, source_dir: pathlib.Path, dest_root: pathlib.Path, *, strict: bool
) -> int:
    """Copy the allowlisted mod files; returns the number of files handled.

    strict=True aborts on a missing source file (explicit opt-in); strict=False
    warns and skips so a routine refresh never fails an install.
    """
    src_root = mod_source(mod, source_dir)
    dst_root = mod_dest(mod, dest_root)
    copied = 0
    for rel in mod.files:
        src = src_root / rel
        if not src.is_file():
            msg = f"quoin: Expected {mod.name} file {rel} at {src} but not found"
            if strict:
                print(msg, file=sys.stderr)
                sys.exit(1)
            print(f"{msg}; skipping", file=sys.stderr)
            continue
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # An unchanged file is left alone, so a refresh keeps its mtime.
        if not (dst.is_file() and dst.read_bytes() == src.read_bytes()):
            shutil.copy2(src, dst)
        copied += 1
    return copied


def remove_mod(mod: OptInMod, dest_root: pathlib.Path) -> bool:
    """Delete the mod folder, only when it is an installed (non-symlink) copy."""
    if mod_state(mod, dest_root) != "installed":
        return False
    shutil.rmtree(mod_dest(mod, dest_root))
    return True


def apply_mod(
    mod: OptInMod, source_dir: pathlib.Path, dest_root: pathlib.Path, *, mode: Optional[str]
) -> str:
    """Apply the requested mod action; returns noop/deployed/refreshed/removed/absent/foreign."""
    state = mod_state(mod, dest_root)
    folder = mod_dest(mod, dest_root)
    if mode == "remove":
        if state == "installed":
            remove_mod(mod, dest_root)
            print(f"Removed {mod.name} mod from {folder}")
            return "removed"
        if state == "absent":
            print(f"{mod.name} mod not installed at {folder}; nothing to remove")
            return "absent"
        return "foreign"
    if mode == "with":
        deploy_mod(mod, source_dir, dest_root, strict=True)
        if state == "installed":
            print(f"Refreshed {mod.name} mod at {folder}")
            return "refreshed"
        print(f"Deployed {mod.name} mod to {folder}")
        return "deployed"
    if state == "installed":
        deploy_mod(mod, source_dir, dest_root, strict=False)
        print(f"Refreshed {mod.name} mod at {folder}")
        return "refreshed"
    return "noop"


WORKFLOW_TASKS = OptInMod(
    "workflow-tasks",
    (
        ".claude-plugin/plugin.json",
        "hooks/hooks.json",
        "hooks/register.tsx",
        "hooks/tasks.ts",
        "types/index.d.ts",
    ),
)


# Every opt-in mod, in the order the install applies them.
OPT_IN_MODS: tuple[OptInMod, ...] = (WORKFLOW_TASKS,)


WORKFLOW_TASKS_NAME = WORKFLOW_TASKS.name


WORKFLOW_TASKS_FILES: tuple[str, ...] = WORKFLOW_TASKS.files


def workflow_tasks_source(source_dir: pathlib.Path) -> pathlib.Path:
    return mod_source(WORKFLOW_TASKS, source_dir)


def workflow_tasks_dest(dest_root: pathlib.Path) -> pathlib.Path:
    return mod_dest(WORKFLOW_TASKS, dest_root)


def workflow_tasks_state(dest_root: pathlib.Path) -> str:
    """Return "absent", "installed" or "foreign" for the mod folder. Never raises."""
    return mod_state(WORKFLOW_TASKS, dest_root)


def missing_workflow_tasks_sources(source_dir: pathlib.Path) -> list[str]:
    return missing_mod_sources(WORKFLOW_TASKS, source_dir)


def workflow_tasks_preflight(
    source_dir: pathlib.Path,
    dest_root: pathlib.Path,
    *,
    mode: Optional[str],
    is_project_mode: bool,
    home_dest_root: pathlib.Path,
    cwd_dest_root: pathlib.Path,
) -> tuple[list[str], list[str]]:
    """Pure check run before anything is written; returns (errors, warnings)."""
    return mod_preflight(
        WORKFLOW_TASKS,
        source_dir,
        dest_root,
        mode=mode,
        is_project_mode=is_project_mode,
        home_dest_root=home_dest_root,
        cwd_dest_root=cwd_dest_root,
    )


def deploy_workflow_tasks(source_dir: pathlib.Path, dest_root: pathlib.Path, *, strict: bool) -> int:
    """Copy the allowlisted mod files; returns the number of files handled."""
    return deploy_mod(WORKFLOW_TASKS, source_dir, dest_root, strict=strict)


def remove_workflow_tasks(dest_root: pathlib.Path) -> bool:
    """Delete the mod folder, only when it is an installed (non-symlink) copy."""
    return remove_mod(WORKFLOW_TASKS, dest_root)


def apply_workflow_tasks(
    source_dir: pathlib.Path, dest_root: pathlib.Path, *, mode: Optional[str]
) -> str:
    """Apply the requested mod action; returns noop/deployed/refreshed/removed/absent/foreign."""
    return apply_mod(WORKFLOW_TASKS, source_dir, dest_root, mode=mode)


_QUOIN_ENV_KEYS = ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")


def _merge_env(settings: dict, *, pct: int | None, window: int | None) -> int | None:
    """Merge the auto-compaction delegation vars into settings['env'].

    Writes ONLY the keys whose value is not None (D-02: the two are
    independent). Returns the count of keys added or changed. Never
    removes a key here.

    If settings['env'] already exists and is not a dict (e.g. a
    hand-edited `"env": null` or `"env": "FOO=bar"`), returns None as a
    sentinel instead of raising or overwriting the existing value — the
    caller turns that into a stderr warning naming the key and leaves
    the file untouched.
    """
    if pct is not None and not (1 <= pct <= 100):
        raise ValueError(f"autocompact pct must be 1..100, got {pct}")
    if window is not None and not (100000 <= window <= 1000000):
        raise ValueError(f"autocompact window must be 100000..1000000, got {window}")

    if "env" in settings and not isinstance(settings["env"], dict):
        return None

    env = settings.setdefault("env", {})
    changed = 0
    if pct is not None:
        value = str(pct)
        if env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") != value:
            env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = value
            changed += 1
    if window is not None:
        value = str(window)
        if env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") != value:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = value
            changed += 1
    return changed


def _clear_env(settings: dict) -> int:
    """Remove ONLY the two keys quoin manages from settings['env'].

    Leaves every other env key untouched; deletes the 'env' dict itself
    only when it becomes empty (so a default install never leaves an
    empty {} behind for the no-env-by-default assertion in T-05). A
    non-dict 'env' (or a missing one) is treated as nothing to clear.
    """
    if "env" not in settings or not isinstance(settings["env"], dict):
        return 0
    existing = settings["env"]
    removed = 0
    for key in _QUOIN_ENV_KEYS:
        if key in existing:
            del existing[key]
            removed += 1
    if not existing:
        del settings["env"]
    return removed


SETTINGS_BACKUP_KEEP_DEFAULT = 5


SETTINGS_BACKUP_KEEP_ENV = "QUOIN_SETTINGS_BACKUP_KEEP"


def _settings_backup_keep() -> int:
    """Return how many settings.json backups to keep (env override, min 1)."""
    raw = os.environ.get(SETTINGS_BACKUP_KEEP_ENV, "").strip()
    if not raw:
        return SETTINGS_BACKUP_KEEP_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        print(
            f"quoin: ignoring {SETTINGS_BACKUP_KEEP_ENV}={raw!r} (not an integer); "
            f"keeping {SETTINGS_BACKUP_KEEP_DEFAULT} backups",
            file=sys.stderr,
        )
        return SETTINGS_BACKUP_KEEP_DEFAULT
    # Never prune the backup this run may have just written.
    return max(value, 1)


def _settings_backup_path(settings_path: pathlib.Path, kind: str = "bak") -> pathlib.Path:
    """Timestamped sibling path for a copy of settings_path.

    kind="bak" names the routine pre-change backups, which pruning rotates.
    kind="broken" names the copy of an unparseable file that the installer is
    about to replace with a fresh one; that copy may be the user's only record
    of their settings, so pruning never matches it.

    The timestamp has one-second resolution, so a second copy within the
    same second gets a ``-1``, ``-2``... suffix instead of overwriting the
    first one.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{settings_path.name}.{kind}-{ts}"
    candidate = settings_path.parent / base
    n = 0
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = settings_path.parent / f"{base}-{n}"
    return candidate


def _prune_settings_backups(settings_path: pathlib.Path, keep: int) -> int:
    """Delete all but the newest `keep` installer backups of settings_path.

    Only files named exactly ``<name>.bak-YYYYMMDDTHHMMSSZ``, optionally with
    a same-second ``-N`` suffix (the installer's own pattern), are considered;
    hand-made backups with other suffixes, and the ``<name>.broken-<ts>``
    copies of unparseable files, are never touched. Age order is the UTC
    timestamp, then the numeric suffix (no suffix first). Returns the number
    of files removed.
    """
    pattern = re.compile(
        re.escape(settings_path.name) + r"\.bak-(\d{8}T\d{6}Z)(?:-(\d+))?"
    )
    keyed = []
    try:
        for p in settings_path.parent.iterdir():
            m = pattern.fullmatch(p.name)
            if m and p.is_file() and not p.is_symlink():
                keyed.append(((m.group(1), int(m.group(2) or 0)), p))
    except OSError:
        return 0
    candidates = [p for _, p in sorted(keyed)]
    removed = 0
    for old in candidates[:-keep] if keep > 0 else candidates:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


MIN_PYTHON = (3, 10)  # keep in step with requires-python in pyproject.toml


def _bare_python3_version(path: str) -> Optional[tuple]:
    """Return (major, minor) reported by `path`, or None when it cannot say."""
    try:
        proc = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, timeout=10,
        )
        major, minor = proc.stdout.split()[:2]
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def check_prerequisites() -> list[str]:
    """Return list of missing required tools; warn about optional ones."""
    missing: list[str] = []
    if shutil.which("claude") is None:
        missing.append("claude (Claude Code CLI)")
    if shutil.which("git") is None:
        missing.append("git")
    if shutil.which("python3") is None:
        print(
            "Warning: no python3 on PATH — quoin's skills and hooks call bare "
            f"python3; link or alias {sys.executable} as python3.",
            file=sys.stderr,
        )
    else:
        found = _bare_python3_version(shutil.which("python3"))
        if found is not None and found < MIN_PYTHON:
            print(
                f"Warning: python3 on PATH is {found[0]}.{found[1]}, below the "
                f"required {MIN_PYTHON[0]}.{MIN_PYTHON[1]} — quoin's skills and hooks "
                f"call bare python3; put {sys.executable} first on PATH or link it as python3.",
                file=sys.stderr,
            )
    if shutil.which("gh") is None:
        print(
            "Warning: gh (GitHub CLI) not found — /end_of_task push will still work, but PR creation won't.",
            file=sys.stderr,
        )
    if shutil.which("npx") is None:
        print(
            "Warning: npx not found — cost tracking in /end_of_task requires npx "
            "(install Node.js from https://nodejs.org).",
            file=sys.stderr,
        )
    return missing


def _has_pyyaml() -> bool:
    """True if pyyaml is importable in the current interpreter.

    A monkeypatchable seam for tests. build_preambles.py --check needs pyyaml
    to parse preamble frontmatter; checking here first avoids spawning a
    child process that would fail for a reason unrelated to staleness.
    """
    import importlib.util

    return importlib.util.find_spec("yaml") is not None


def install_dev_deps() -> None:
    """Install dev Python dependencies via pip (uses quoin[dev] extras).

    Runs pip through the interpreter that is running quoin, not whichever
    pip is first on PATH (which can belong to a different Python), and drops
    --user inside a virtual environment where pip rejects it.
    """
    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
    )
    if pip_check.returncode != 0:
        print(
            "Warning: pip not found for "
            f"{sys.executable} — install quoin[dev] manually for dev tests",
            file=sys.stderr,
        )
        return
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    user_flag = [] if in_venv else ["--user"]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *user_flag, "--upgrade", "quoin[dev]"],
    )
    if result.returncode != 0:
        print(
            "Warning: pip install failed; install quoin[dev] manually for dev tests",
            file=sys.stderr,
        )



# Every hook script an earlier install registered in settings.json.
_LEGACY_HOOK_BASENAMES = _QUOIN_HOOK_BASENAMES | {"worktreecreate.sh"}

# Files an earlier install wrote directly into the deploy root.
_LEGACY_TOP_LEVEL_FILES = ("QUICKSTART.md",)
