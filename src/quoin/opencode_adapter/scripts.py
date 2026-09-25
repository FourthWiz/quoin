"""The allowlist of Quoin helper scripts an OpenCode role may run by name.

The command-line runner that actually executes these scripts arrives
with the install wiring in a later stage; that runner is responsible
for confining the output of every write-capable script to the current
project's artifact root before it runs anything. This module only
fixes the allowlist and resolves a name to its script path under a
given source directory — it never accepts an arbitrary path, and it
runs nothing itself.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

ALLOWED_SCRIPTS = (
    "checkpoint_picker",
    "classify_critic_issues",
    "generate_discovery_map",
    "handoff_validate",
    "path_resolve",
    "validate_artifact",
)

# The only allowlisted script that writes a file (its --output/-o flag, or
# the default path under its positional project root). The other five only
# read and print.
WRITE_CAPABLE_SCRIPTS = ("generate_discovery_map",)

REFERENCE_RE = re.compile(r"quoin opencode script ([a-z_]+)(?![\w-])")


def script_path(source_dir, name: str) -> Path:
    if name not in ALLOWED_SCRIPTS:
        raise ValueError(
            "unknown script %r, expected one of %s" % (name, ALLOWED_SCRIPTS)
        )
    return Path(source_dir) / "core" / "scripts" / ("%s.py" % name)


def referenced_scripts(text: str) -> List[str]:
    return sorted(set(REFERENCE_RE.findall(text)))
