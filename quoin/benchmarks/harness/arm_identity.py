"""Arm identity checks for the paired benchmark.

The plain arm must run with no quoin skills, rules or hooks visible, and the
quoin arm must run with every canonical skill installed. The static checks look
at a sandbox config directory before any session starts; the runtime check
reads the CLI's own stream and decides from the first events whether the arm
the session reports is the arm that was intended.

Everything here is a pure function or a small state holder: no subprocess and
no network.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

QUOIN_FREE = "quoin-free"
QUOIN_REQUIRED = "quoin-required"

# Skills the CLI ships itself. `run` is listed by an isolated CLI with no
# quoin installed, so it cannot prove that quoin is present.
BUILTIN_NAME_COLLISIONS = frozenset({"run", "review", "status", "plan"})

WORKDIR_CONFIG_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude", ".mcp.json")

RULES_MARKERS = ("# === DEV WORKFLOW START ===", "# === DEV WORKFLOW END ===")

PLAIN_REASON = "plain-arm-contaminated"
QUOIN_REASON = "quoin-arm-missing-quoin"

_PLAIN_FORBIDDEN_ENTRIES = ("skills", "commands", "agents", "plugins", "hooks", "CLAUDE.md")


# A plugin-era tree installs quoin as a Claude Code plugin; the CLI lists its
# skills under the plugin's namespace, e.g. `quoin:run`.
PLUGIN_SKILL_PREFIX = "quoin:"
PLUGIN_SKILLS_DIR = Path("quoin") / "adapters" / "claude" / "skills"


def distinctive_quoin_skills(canonical: Iterable[str]) -> frozenset:
    """Canonical skill names that no CLI build ships on its own."""
    return frozenset(set(canonical) - BUILTIN_NAME_COLLISIONS)


def is_plugin_installer(installer) -> bool:
    """True for an arm installer that installs quoin as a plugin rather than file by file."""
    return installer is not None and hasattr(installer, "PLUGIN_ID") and not hasattr(
        installer, "CANONICAL_SKILLS"
    )


def plugin_skill_dirs(arm_root: Path) -> List[str]:
    """Skill directory names the arm's plugin ships, sorted."""
    root = Path(arm_root) / PLUGIN_SKILLS_DIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())


def canonical_skills(arm_root: Path) -> tuple:
    """The skill names a session of this arm must list in its init event.

    A pre-plugin tree names them in its installer; a plugin tree ships one
    directory per skill, which the CLI lists under the plugin's namespace.
    """
    installer = _arm_gate()._load_arm_installer(Path(arm_root))
    if installer is None:
        return ()
    if is_plugin_installer(installer):
        return tuple(PLUGIN_SKILL_PREFIX + name for name in plugin_skill_dirs(arm_root))
    return tuple(getattr(installer, "CANONICAL_SKILLS", ()))


def uses_plugin_skills(canonical: Iterable[str]) -> bool:
    """True when the canonical names are namespaced plugin skills."""
    return any(str(name).startswith(PLUGIN_SKILL_PREFIX) for name in canonical)


@dataclass(frozen=True)
class ArmIdentity:
    mode: str
    config_dir: Optional[Path]
    canonical: tuple
    distinctive: frozenset
    arm_config_mode: str


def _real(path) -> str:
    return os.path.realpath(str(path))


def _under(path, root) -> bool:
    p, r = _real(path), _real(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def check_plain_sandbox(home: Path) -> List[str]:
    """Problems with a freshly built plain sandbox (empty list means pass).

    Meant for the start of a run only: a CLI session writes its own state
    into the config directory, so the same check between sessions would trip
    on ordinary files.
    """
    problems: List[str] = []
    home = Path(home)
    claude = home / ".claude"
    try:
        st = os.lstat(claude)
    except OSError:
        return [f"config dir missing: {claude}"]
    import stat as _stat

    if not _stat.S_ISDIR(st.st_mode):
        return [f"config dir is not a real directory: {claude}"]
    for name in _PLAIN_FORBIDDEN_ENTRIES:
        entry = claude / name
        if not _lexists(entry):
            continue
        if entry.is_dir() and not entry.is_symlink():
            if any(entry.iterdir()):
                problems.append(f"{name}/ is not empty")
        else:
            problems.append(f"{name} present")
    settings = claude / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"settings.json unreadable: {exc}")
            data = {}
        if not isinstance(data, dict):
            problems.append("settings.json is not an object")
            data = {}
        if data.get("hooks"):
            problems.append("settings.json registers hooks")
        if data.get("enabledPlugins"):
            problems.append("settings.json enables plugins")
    for stray in (home / "CLAUDE.md", home / ".claude.json"):
        if _lexists(stray):
            problems.append(f"{stray.name} present in sandbox home")
    return problems


def _arm_gate():
    from quoin.benchmarks.scripts import run_three_arm_gate

    return run_three_arm_gate


def _installed_skill_names(home: Path) -> List[str]:
    skills = Path(home) / ".claude" / "skills"
    if not skills.is_dir():
        return []
    return sorted(p.name for p in skills.iterdir() if p.is_dir())


def extra_skills(home: Path, arm_root: Path) -> List[str]:
    """Installed skill directories that are not canonical, sorted."""
    installer = _arm_gate()._load_arm_installer(Path(arm_root))
    canonical = set(getattr(installer, "CANONICAL_SKILLS", ()))
    return sorted(set(_installed_skill_names(home)) - canonical)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
# What the stager leaves out of a staged plugin, and what an interpreter may
# write next to it; neither is a plugin component the CLI loads.
_PLUGIN_SKIP_DIRS = frozenset({"__pycache__", "node_modules", "tests"})
_PLUGIN_SKIP_NAMES = frozenset({".DS_Store"})


def _plugin_entry_bytes(path: Path) -> bytes:
    """A regular file's bytes; never follows a link or opens a special file."""
    import stat as _stat

    st = os.lstat(path)
    if _stat.S_ISLNK(st.st_mode):
        return b"\0symlink\0" + os.readlink(path).encode("utf-8", "surrogateescape")
    if not _stat.S_ISREG(st.st_mode):
        return b"\0special\0"
    return Path(path).read_bytes()


def _plugin_files(root: Path, rels: Optional[Iterable[str]] = None) -> dict:
    """Relative path -> bytes for a plugin tree: the `rels` it ships, or every file when None."""
    root = Path(root)
    bases = [root / rel for rel in rels] if rels is not None else [root]
    out = {}
    for base in bases:
        if not _lexists(base):
            continue
        if not base.is_dir() or base.is_symlink():
            out[base.relative_to(root).as_posix()] = _plugin_entry_bytes(base)
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if d not in _PLUGIN_SKIP_DIRS]
            names = files + [d for d in dirs if os.path.islink(os.path.join(current, d))]
            for name in names:
                path = Path(current) / name
                if name in _PLUGIN_SKIP_NAMES or name.endswith(".pyc"):
                    continue
                out[path.relative_to(root).as_posix()] = _plugin_entry_bytes(path)
    return out


def _manifest_without_version(raw: Optional[bytes]):
    try:
        data = json.loads(raw) if raw is not None else None
    except ValueError:
        return "<unparsable>"
    if isinstance(data, dict):
        data = {key: value for key, value in data.items() if key != "version"}
    return data


def _compare_plugin_copy(label: str, want: dict, copy: Path) -> List[str]:
    """Problems with one installed copy of the plugin against the arm's files."""
    have = _plugin_files(copy)
    problems: List[str] = []
    if _manifest_without_version(want.get(PLUGIN_MANIFEST)) != _manifest_without_version(
        have.get(PLUGIN_MANIFEST)
    ):
        problems.append(f"{label}: plugin.json differs from the arm's")
    want = {k: v for k, v in want.items() if k != PLUGIN_MANIFEST}
    have = {k: v for k, v in have.items() if k != PLUGIN_MANIFEST}
    missing = sorted(set(want) - set(have))
    if missing:
        problems.append(f"{label}: plugin files missing: {', '.join(missing[:3])}")
    changed = sorted(rel for rel in set(want) & set(have) if want[rel] != have[rel])
    if changed:
        problems.append(f"{label}: plugin files differ from the arm's sources: {', '.join(changed[:3])}")
    extra = sorted(set(have) - set(want))
    if extra:
        problems.append(f"{label}: plugin files the arm does not ship: {', '.join(extra[:3])}")
    return problems


def installed_plugin_path(claude_dir: Path, plugin_id: str) -> Optional[Path]:
    """Install path of the single user-scope entry for `plugin_id`, or None."""
    data = _read_json(Path(claude_dir) / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        return None
    if entries[0].get("scope") != "user" or not entries[0].get("installPath"):
        return None
    return Path(entries[0]["installPath"])


def marketplace_location(claude_dir: Path, marketplace: str) -> Optional[Path]:
    """Where a registered marketplace's files live, or None."""
    data = _read_json(Path(claude_dir) / "plugins" / "known_marketplaces.json")
    entry = data.get(marketplace) if isinstance(data, dict) else None
    location = entry.get("installLocation") if isinstance(entry, dict) else None
    return Path(location) if location else None


def _check_plugin_sandbox(home: Path, arm_root: Path, installer, *, exact: bool) -> List[str]:
    problems: List[str] = []
    claude = home / ".claude"
    plugin_id = installer.PLUGIN_ID
    marketplace = getattr(installer, "MARKETPLACE_NAME", plugin_id.split("@")[-1])
    rels = tuple(getattr(installer, "PLUGIN_PATHS", ()))
    if not rels:
        return ["arm installer names no plugin paths to compare"]
    settings = _read_json(claude / "settings.json")
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    if not isinstance(enabled, dict) or enabled.get(plugin_id) is not True:
        problems.append(f"settings.json does not enable {plugin_id}")
    if exact:
        if isinstance(enabled, dict):
            others = sorted(name for name, on in enabled.items() if on and name != plugin_id)
            if others:
                problems.append(f"unexpected plugins enabled: {', '.join(others)}")
        registry = _read_json(claude / "plugins" / "installed_plugins.json")
        installed = registry.get("plugins") if isinstance(registry, dict) else None
        if isinstance(installed, dict) and set(installed) - {plugin_id}:
            problems.append(f"unexpected plugins installed: {', '.join(sorted(set(installed) - {plugin_id}))}")
        known = _read_json(claude / "plugins" / "known_marketplaces.json")
        extra_known = settings.get("extraKnownMarketplaces") if isinstance(settings, dict) else None
        for source in (known, extra_known):
            if isinstance(source, dict) and set(source) - {marketplace}:
                problems.append(
                    f"unexpected marketplaces registered: {', '.join(sorted(set(source) - {marketplace}))}"
                )
                break
    install_path = installed_plugin_path(claude, plugin_id)
    if install_path is None:
        problems.append(f"{plugin_id} is not installed once at user scope")
        return problems
    # The CLI caches the plugin under the config directory, and a directory
    # marketplace's session loads it from the marketplace's own location, so
    # both copies must be inside the sandbox and match the arm.
    copies = [("installed copy", install_path, claude)]
    location = marketplace_location(claude, marketplace)
    if location is None:
        problems.append(f"marketplace {marketplace} is not registered")
    else:
        copies.append(("marketplace copy", location, home))
    want = _plugin_files(arm_root / "quoin", rels)
    for label, copy, root in copies:
        if not _under(copy, root):
            problems.append(f"{label} outside the sandbox: {copy}")
        elif not copy.is_dir():
            problems.append(f"{label} missing: {copy}")
        else:
            problems.extend(_compare_plugin_copy(label, want, copy))
    if exact:
        stray = _installed_skill_names(home)
        if stray:
            problems.append(f"unexpected skills: {', '.join(stray)}")
    return problems


def check_quoin_sandbox(home: Path, arm_root: Path, *, exact: bool = True) -> List[str]:
    """Problems with an installed quoin config under `home` (empty = pass).

    With `exact=False` the skill rule is "every canonical skill is installed";
    extra directories are not problems. A plugin tree is checked through the
    installed plugin instead: enabled, installed once inside the sandbox, and
    byte-identical to the arm's sources.
    """
    problems: List[str] = []
    home = Path(home)
    arm_root = Path(arm_root)
    gate = _arm_gate()
    installer = gate._load_arm_installer(arm_root)
    if installer is None:
        return [f"arm installer not loadable under {arm_root}"]
    if is_plugin_installer(installer):
        return _check_plugin_sandbox(home, arm_root, installer, exact=exact)
    canonical = set(installer.CANONICAL_SKILLS)
    installed = set(_installed_skill_names(home))
    missing = sorted(canonical - installed)
    if missing:
        problems.append(f"missing skills: {', '.join(missing)}")
    if exact:
        extra = sorted(installed - canonical)
        if extra:
            problems.append(f"unexpected skills: {', '.join(extra)}")
    for name in sorted(canonical & installed):
        if not (home / ".claude" / "skills" / name / "SKILL.md").is_file():
            problems.append(f"skill without SKILL.md: {name}")
    rules = home / ".claude" / "CLAUDE.md"
    try:
        text = rules.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not all(marker in text for marker in RULES_MARKERS):
        problems.append("CLAUDE.md lacks the quoin rules markers")
    wanted = gate.arm_registered_stanzas(arm_root)
    live = gate.live_registered_stanzas(home / ".claude" / "settings.json")
    absent = sorted(wanted - live)
    if absent:
        problems.append(f"hook stanzas not registered: {absent[:3]}")
    if not gate.verify_hooks_deployed(arm_root, home):
        problems.append("hook scripts differ from the arm's sources")
    return problems


def check_workdir(path: Path) -> List[str]:
    """Config files a session could pick up from the work root or above it."""
    problems: List[str] = []
    current = Path(_real(path))
    seen = []
    while True:
        seen.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in seen:
        for name in WORKDIR_CONFIG_NAMES:
            candidate = directory / name
            if _lexists(candidate):
                problems.append(f"{candidate} exists")
    return problems


def _plugin_name(plugin) -> str:
    return str(plugin.get("name") if isinstance(plugin, dict) else plugin)


def _is_builtin_plugin(plugin) -> bool:
    # Claude Code ships plugins inside the binary that load in every session,
    # even with an empty config dir or --safe-mode, so both arms carry them.
    if not isinstance(plugin, dict):
        return False
    return plugin.get("path") == "builtin" or str(plugin.get("source", "")).endswith("@builtin")


class InitIdentityCheck:
    """Reads stream events in order and trips once when the arm is wrong."""

    def __init__(self, identity: ArmIdentity) -> None:
        self.identity = identity
        self.reason: Optional[str] = None
        self.detail: Optional[str] = None
        self._init_seen = False
        self._record: dict = {
            "init_seen": False,
            "skills_count": None,
            "missing_names": [],
            "forbidden_names": [],
            "memory_paths": {},
            "apiKeySource": None,
            "mcp_servers": [],
            "plugins": [],
            "builtin_plugins": [],
            "claude_code_version": None,
            "hook_events": [],
            "failure_detail": None,
        }

    @property
    def _plain(self) -> bool:
        return self.identity.mode == QUOIN_FREE

    def _trip(self, detail: str) -> str:
        reason = PLAIN_REASON if self._plain else QUOIN_REASON
        self.reason = reason
        self.detail = detail
        self._record["failure_detail"] = detail
        return reason

    def observe(self, event: dict) -> Optional[str]:
        if self.reason is not None or not isinstance(event, dict):
            return None
        etype = event.get("type")
        subtype = event.get("subtype")
        if etype == "system" and subtype in ("hook_started", "hook_response"):
            name = str(event.get("hook_event") or event.get("hook_name") or "")
            self._record["hook_events"].append(name)
            if self._plain:
                return self._trip(f"hook-event:{name}")
            return None
        if etype == "system" and subtype == "init":
            return self._observe_init(event)
        if etype in ("assistant", "user", "result") and not self._init_seen:
            return self._trip("no-init-event")
        return None

    def _observe_init(self, event: dict) -> Optional[str]:
        self._init_seen = True
        rec = self._record
        rec["init_seen"] = True
        rec["apiKeySource"] = event.get("apiKeySource")
        rec["claude_code_version"] = event.get("claude_code_version")
        servers = event.get("mcp_servers") or []
        rec["mcp_servers"] = sorted(
            str(s.get("name") if isinstance(s, dict) else s) for s in servers
        )
        plugins = event.get("plugins") or []
        rec["plugins"] = [_plugin_name(p) for p in plugins]
        rec["builtin_plugins"] = [_plugin_name(p) for p in plugins if _is_builtin_plugin(p)]
        memory_paths = event.get("memory_paths")
        rec["memory_paths"] = dict(memory_paths) if isinstance(memory_paths, dict) else {}
        skills = event.get("skills")
        if not isinstance(skills, list):
            return self._trip("init-skills-unreadable")
        names = {str(s) for s in skills}
        rec["skills_count"] = len(names)
        ident = self.identity
        if self._plain:
            forbidden = sorted(names & set(ident.distinctive))
            rec["forbidden_names"] = forbidden
            if forbidden:
                return self._trip(f"quoin-skills-listed:{','.join(forbidden)}")
            installed = [_plugin_name(p) for p in plugins if not _is_builtin_plugin(p)]
            if installed:
                return self._trip(f"plugins-listed:{','.join(installed)}")
        else:
            missing = sorted(set(ident.canonical) - names)
            rec["missing_names"] = missing
            if missing:
                return self._trip(f"canonical-skills-absent:{','.join(missing)}")
            if uses_plugin_skills(ident.canonical):
                plugin_name = PLUGIN_SKILL_PREFIX.rstrip(":")
                for plugin in plugins:
                    if _is_builtin_plugin(plugin):
                        continue
                    if _plugin_name(plugin) != plugin_name:
                        return self._trip(f"unexpected-plugin:{_plugin_name(plugin)}")
                    path = plugin.get("path") if isinstance(plugin, dict) else None
                    if ident.config_dir is not None and (
                        not path or not _under(path, Path(ident.config_dir).parent)
                    ):
                        return self._trip(f"plugin-loaded-outside-sandbox:{path}")
        if ident.config_dir is not None:
            outside = sorted(
                str(v) for v in rec["memory_paths"].values() if not _under(v, ident.config_dir)
            )
            if outside:
                return self._trip(f"memory-path-outside-config-dir:{outside[0]}")
        return None

    def record(self) -> dict:
        return json.loads(json.dumps(self._record))
