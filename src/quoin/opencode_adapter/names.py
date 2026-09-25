"""Name normalization, validation and per-namespace collision detection.

OpenCode keeps separate maps for commands, skills and agents, so the same
generated name may legally appear in more than one namespace. Collision
detection here is always scoped to a single namespace by the caller.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

PREFIX = "quoin-"
MAX_NAME_LEN = 64
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Documents the OpenCode maps this adapter generates names into. This is a
# label list for callers, not something `name_error` or `find_collisions`
# validates against.
NAMESPACES = ("command", "skill", "agent")


def normalize(canonical_id: str) -> str:
    """Turn a portable catalog id into an OpenCode-facing name.

    No lowercasing and no other rewriting beyond the prefix and the
    underscore-to-hyphen swap, so an id that is otherwise invalid surfaces
    as an invalid name instead of being silently repaired.
    """
    return PREFIX + canonical_id.replace("_", "-")


def role_agent_name(role: str) -> str:
    return PREFIX + role


def name_error(name: str) -> Optional[str]:
    """Return None when `name` is a valid OpenCode-facing name, else a message."""
    if not NAME_RE.match(name):
        return "name '%s' does not match the required pattern %s" % (name, NAME_RE.pattern)
    if not (1 <= len(name) <= MAX_NAME_LEN):
        return "name '%s' is %d characters, outside the allowed range 1..%d" % (
            name,
            len(name),
            MAX_NAME_LEN,
        )
    return None


def find_collisions(namespace: str, pairs: Iterable[Tuple[str, str]]) -> List[str]:
    """Group (generated_name, source_label) pairs by name within `namespace`.

    `namespace` is a free-form label used only in the returned messages; it
    is not validated, so a catalog-wide call may pass a label like
    "catalog" that is not itself one of the OpenCode maps in `NAMESPACES`.
    Returns one message per name produced by more than one distinct source,
    sorted by name, with sources sorted and comma-separated when there are
    three or more.
    """
    by_name = {}
    for name, source in pairs:
        by_name.setdefault(name, set()).add(source)

    messages = []
    for name in sorted(by_name):
        sources = sorted(by_name[name])
        if len(sources) <= 1:
            continue
        if len(sources) == 2:
            messages.append(
                "%s name '%s' is produced by both '%s' and '%s'" % (namespace, name, sources[0], sources[1])
            )
        else:
            joined = ", ".join("'%s'" % s for s in sources)
            messages.append("%s name '%s' is produced by %s" % (namespace, name, joined))
    return messages


class NameCollisionError(ValueError):
    pass


def check_unique(namespace: str, pairs: Iterable[Tuple[str, str]]) -> None:
    messages = find_collisions(namespace, pairs)
    if messages:
        raise NameCollisionError("; ".join(messages))
