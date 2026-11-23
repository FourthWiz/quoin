"""Optional add-ons that live in their own packages and repositories.

An add-on is a separate distribution (for example `quoin-cockpit`) that
registers an object in the `quoin.addons` entry-point group:

    [project.entry-points."quoin.addons"]
    cockpit = "quoin_cockpit.addon:addon"

The object has `name`, `title`, `description`, `install(options) -> int` and
`remove() -> int`. `quoin install` asks about each add-on in `KNOWN` that is
available, installs the chosen package into this Python environment with pip
when it is missing, and calls its `install`. An add-on installed some other
way (pip extras, a git URL) is found the same way.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Callable, Optional

GROUP = "quoin.addons"


@dataclass(frozen=True)
class Known:
    name: str
    package: str
    title: str
    description: str
    available: bool = True
    note: str = ""


KNOWN: dict[str, Known] = {
    "cockpit": Known(
        "cockpit",
        "quoin-cockpit",
        "Cockpit",
        "Stream Deck panel, Agentdesk, dashboard and the context-tracker pane",
    ),
    "opencode": Known(
        "opencode",
        "quoin-opencode",
        "OpenCode support",
        "the OpenCode adapter and workctl",
        available=False,
        note="not yet ported to this quoin; install it from its repository when it is",
    ),
}


def discovered() -> dict:
    """Installed add-ons by name: `{name: entry point}`."""
    try:
        points = metadata.entry_points(group=GROUP)
    except TypeError:  # Python < 3.10 signature; kept for safety
        points = metadata.entry_points().get(GROUP, [])
    return {point.name: point for point in points}


def load(name: str):
    point = discovered().get(name)
    return point.load() if point is not None else None


def pip_install(spec: str, *, runner: Callable = subprocess.call) -> int:
    """Install `spec` into the Python environment quoin itself runs in."""
    return runner([sys.executable, "-m", "pip", "install", spec])


def parse_choice(text: str) -> tuple[str, Optional[str]]:
    """`cockpit` or `cockpit=<pip spec>` into (name, spec)."""
    name, sep, spec = text.partition("=")
    return name.strip(), (spec.strip() or None) if sep else None


def install(name: str, spec: Optional[str] = None, *, runner: Callable = subprocess.call,
            printer: Callable[[str], None] = print) -> int:
    """Make sure the add-on's package is present, then run its own install."""
    known = KNOWN.get(name)
    if name not in discovered():
        if known is None and spec is None:
            printer(f"quoin: unknown add-on {name!r}; known: {', '.join(KNOWN)}")
            return 2
        if known is not None and not known.available and spec is None:
            printer(f"quoin: the {known.title} add-on is {known.note}")
            return 2
        target = spec or known.package
        printer(f"Installing the {known.title if known else name} add-on ({target}) with pip")
        if pip_install(target, runner=runner) != 0:
            printer(f"quoin: pip could not install {target}")
            return 1
        metadata_cache_clear()
    addon = load(name)
    if addon is None:
        printer(f"quoin: {name} installed, but it does not register a `{GROUP}` entry point")
        return 1
    return int(addon.install({}) or 0)


def metadata_cache_clear() -> None:
    """Make entry points of a package pip just installed visible to this process."""
    import importlib

    importlib.invalidate_caches()


def offered() -> list[Known]:
    return [k for k in KNOWN.values() if k.available]


def status() -> list[dict]:
    found = discovered()
    rows = []
    for known in KNOWN.values():
        rows.append({"name": known.name, "package": known.package, "title": known.title,
                     "installed": known.name in found, "available": known.available, "note": known.note})
    for name in sorted(set(found) - set(KNOWN)):
        rows.append({"name": name, "package": found[name].value, "title": name, "installed": True,
                     "available": True, "note": "third-party"})
    return rows
