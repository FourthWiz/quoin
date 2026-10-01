"""Process table, process identities and descendant walks (stdlib only).

An identity is a pid plus the process start time, so a recycled pid is never
mistaken for the process it replaced. A zombie has exited even though signal
0 still succeeds for it, so zombies are never reported alive.

The listing returns `None` whenever it cannot be trusted; that is never the
same thing as "nothing is running".
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SUPPORTED = os.name == "posix"

PS_TIMEOUT_S = 10.0
MAX_DESCENDANTS = 4096
_PS_ARGV = ("ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart=")

# runner(argv, env, timeout_s) -> (returncode, stdout_text)
Runner = Callable[[Sequence[str], Mapping[str, str], float], Tuple[int, str]]


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    state: str
    start: str

    @property
    def zombie(self) -> bool:
        return self.state.startswith("Z")


@dataclass(frozen=True)
class Identity:
    pid: int
    start: str


def _default_runner(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> Tuple[int, str]:
    proc = subprocess.run(
        list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=timeout_s, check=False,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _ps_env() -> Dict[str, str]:
    # A fixed locale and zone make the start-time text identical no matter
    # which process (or caller time zone) recorded it.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    return env


def _resolve_proc_root(proc_root: Optional[Path]) -> Optional[Path]:
    if proc_root is not None:
        return Path(proc_root)
    if sys.platform.startswith("linux"):
        return Path("/proc")
    return None


def _parse_proc_stat(pid: int, text: str) -> Optional[ProcInfo]:
    close = text.rfind(")")
    if close < 0:
        return None
    rest = text[close + 1:].split()
    if len(rest) < 20:
        return None
    try:
        return ProcInfo(pid=pid, ppid=int(rest[1]), pgid=int(rest[2]), state=rest[0], start=str(int(rest[19])))
    except ValueError:
        return None


def parse_ps(text: str) -> Optional[Dict[int, ProcInfo]]:
    """Parse `ps -A -o pid=,ppid=,pgid=,stat=,lstart=` output, or `None`."""
    table: Dict[int, ProcInfo] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            return None
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None
        table[pid] = ProcInfo(pid=pid, ppid=ppid, pgid=pgid, state=parts[3], start=" ".join(parts[4].split()))
    return table or None


def _snapshot_proc(root: Path) -> Optional[Dict[int, ProcInfo]]:
    table: Dict[int, ProcInfo] = {}
    try:
        names = os.listdir(str(root))
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(str(root / name / "stat"), "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue  # the process exited while the table was being read
        info = _parse_proc_stat(int(name), text)
        if info is None:
            return None
        table[info.pid] = info
    return table or None


def snapshot(*, runner: Optional[Runner] = None, proc_root: Optional[Path] = None) -> Optional[Dict[int, ProcInfo]]:
    """The whole process table keyed by pid, or `None` when it cannot be trusted."""
    if not SUPPORTED:
        return None
    root = _resolve_proc_root(proc_root)
    if root is not None and (proc_root is not None or (root / "self").exists()):
        return _snapshot_proc(root)
    run = runner or _default_runner
    try:
        code, out = run(_PS_ARGV, _ps_env(), PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if code != 0:
        return None
    return parse_ps(out)


def start_time(pid: int, *, runner: Optional[Runner] = None, proc_root: Optional[Path] = None) -> Optional[str]:
    """Start-time text for one pid (same format as `ProcInfo.start`), or `None`."""
    if not SUPPORTED or not isinstance(pid, int) or pid <= 0:
        return None
    root = _resolve_proc_root(proc_root)
    if root is not None and (proc_root is not None or (root / "self").exists()):
        try:
            with open(str(root / str(pid) / "stat"), "r", encoding="utf-8", errors="replace") as handle:
                info = _parse_proc_stat(pid, handle.read())
        except OSError:
            return None
        return info.start if info else None
    run = runner or _default_runner
    try:
        code, out = run(("ps", "-p", str(pid), "-o", "lstart="), _ps_env(), PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(out.split())
    return text if code == 0 and text else None


def alive(identity: Identity, table: Optional[Mapping[int, ProcInfo]]) -> bool:
    if not table:
        return False
    info = table.get(identity.pid)
    return info is not None and info.start == identity.start and not info.zombie


def descendants(root_pid: int, table: Optional[Mapping[int, ProcInfo]]) -> List[ProcInfo]:
    """Non-zombie descendants of `root_pid` (breadth first, cycle-safe, capped).
    Zombies are walked for their edges but left out of the result."""
    if not table:
        return []
    children: Dict[int, List[ProcInfo]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info)
    seen = {root_pid}
    queue = [root_pid]
    out: List[ProcInfo] = []
    while queue and len(seen) <= MAX_DESCENDANTS:
        current = queue.pop(0)
        for child in sorted(children.get(current, ()), key=lambda i: i.pid):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            queue.append(child.pid)
            if not child.zombie:
                out.append(child)
    return out


def group_members(pgid: int, table: Optional[Mapping[int, ProcInfo]]) -> List[ProcInfo]:
    """Live (non-zombie) members of a process group, sorted by pid."""
    if not table:
        return []
    return sorted((i for i in table.values() if i.pgid == pgid and not i.zombie), key=lambda i: i.pid)
