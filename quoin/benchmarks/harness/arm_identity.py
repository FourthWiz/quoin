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

WORKDIR_CONFIG_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude")

RULES_MARKERS = ("# === DEV WORKFLOW START ===", "# === DEV WORKFLOW END ===")

PLAIN_REASON = "plain-arm-contaminated"
QUOIN_REASON = "quoin-arm-missing-quoin"

_PLAIN_FORBIDDEN_ENTRIES = ("skills", "commands", "agents", "plugins", "hooks", "CLAUDE.md")


def distinctive_quoin_skills(canonical: Iterable[str]) -> frozenset:
    """Canonical skill names that no CLI build ships on its own."""
    return frozenset(set(canonical) - BUILTIN_NAME_COLLISIONS)


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
    canonical = set(installer.CANONICAL_SKILLS) if installer is not None else set()
    return sorted(set(_installed_skill_names(home)) - canonical)


def check_quoin_sandbox(home: Path, arm_root: Path, *, exact: bool = True) -> List[str]:
    """Problems with an installed quoin config under `home` (empty = pass).

    With `exact=False` the skill rule is "every canonical skill is installed";
    extra directories are not problems.
    """
    problems: List[str] = []
    home = Path(home)
    arm_root = Path(arm_root)
    gate = _arm_gate()
    installer = gate._load_arm_installer(arm_root)
    if installer is None:
        return [f"arm installer not loadable under {arm_root}"]
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
        rec["plugins"] = [str(p.get("name") if isinstance(p, dict) else p) for p in plugins]
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
            if rec["plugins"]:
                return self._trip(f"plugins-listed:{','.join(rec['plugins'])}")
        else:
            missing = sorted(set(ident.canonical) - names)
            rec["missing_names"] = missing
            if missing:
                return self._trip(f"canonical-skills-absent:{','.join(missing)}")
        if ident.config_dir is not None:
            outside = sorted(
                str(v) for v in rec["memory_paths"].values() if not _under(v, ident.config_dir)
            )
            if outside:
                return self._trip(f"memory-path-outside-config-dir:{outside[0]}")
        return None

    def record(self) -> dict:
        return json.loads(json.dumps(self._record))
