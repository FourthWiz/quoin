"""Runtime driver for headless OpenCode runs.

This module currently holds only the capability constants the driver depends
on. Each flag mirrors the status of one row in the OpenCode compatibility
document and gates the code path that would otherwise rely on an unverified
claim; a test compares every flag with its row so they cannot drift apart.
"""
from __future__ import annotations

from typing import Dict

# Row key in the compatibility document -> why the driver depends on it.
# A key is listed only while its row is verified; an unverified capability is
# represented by its flag alone, and every code path that depends on it is
# gated off by that flag.
DRIVER_CITED_CAPABILITIES: Dict[str, str] = {
    "step-settling": (
        "every tool part of a step is terminal before its step-finish, so a "
        "stream that ends after a step-finish has no open tool part"
    ),
    "non-git-discovery": (
        "a project root that is not a git repository still loads .opencode "
        "configuration and commands, so the compiled launch is honoured there"
    ),
    "managed-config-layers": (
        "managed configuration outranks the compiled file, so it must be "
        "scanned before a launch"
    ),
    "data-dir-state": (
        "credentials, session storage and remote config live under the data "
        "directory, which the launcher isolates and reuses across attempts"
    ),
    "continuation-flags": (
        "resume selects a session with --session and treats an unknown "
        "session as exit 1 with no native events"
    ),
}

# Verified: session continuation after real work may be attempted, because a
# clean step-finish proves every tool of that step settled.
STEP_SETTLING_VERIFIED: bool = True

# Verified with a condition: a resumed run subscribes to live events only.
# Compaction pruning can republish earlier completed tool parts when a
# configuration layer enables it. This flag gates nothing: replayed part ids
# are dropped by the rebuilt deduper, and a replayed error without an id can
# only cause a false failure, never a false success.
CONTINUATION_NO_REPLAY_VERIFIED: bool = True

# Verified: a project root that is not a git repository is accepted.
NON_GIT_DISCOVERY_VERIFIED: bool = True
