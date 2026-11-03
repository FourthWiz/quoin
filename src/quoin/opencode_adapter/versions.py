"""The OpenCode version policy: a list of qualified releases with a floor.

`qualified-versions.json` beside the adapter data is the single source of the
qualified list. Its lowest entry is the floor. A runtime version is judged as:

- ``qualified``: listed; every profile launches.
- ``newer``: above the highest listed release; personal profiles launch with a
  warning, work profiles refuse.
- ``unqualified``: at or above the floor but not listed (a release between two
  qualified ones); treated like ``newer``.
- ``below-floor``: older than the floor; every profile refuses.
- ``unknown``: no version could be read; every profile refuses.

Work profiles refuse anything unqualified because their isolation rests on
verified configuration and plugin loading behaviour, which an upstream release
can change without notice.

Stdlib only; nothing here runs a binary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

QUALIFIED_PARTS = ("adapters", "opencode", "qualified-versions.json")
SCHEMA = "quoin.opencode.qualified-versions"

QUALIFIED = "qualified"
NEWER = "newer"
UNQUALIFIED = "unqualified"
BELOW_FLOOR = "below-floor"
UNKNOWN = "unknown"
STATUSES = (QUALIFIED, NEWER, UNQUALIFIED, BELOW_FLOOR, UNKNOWN)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class QualifiedVersionsError(Exception):
    pass


def parse(version: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """A plain ``X.Y.Z`` release as a comparable tuple, or None. Pre-release
    or build suffixes are not releases and never parse."""
    match = _VERSION_RE.fullmatch(version or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _validate(values: object, origin: str) -> List[str]:
    if not isinstance(values, list) or not values:
        raise QualifiedVersionsError("%s has no non-empty 'qualified' list" % origin)
    if not all(isinstance(v, str) and parse(v) for v in values):
        raise QualifiedVersionsError("%s lists a value that is not an X.Y.Z release" % origin)
    if len(set(values)) != len(values):
        raise QualifiedVersionsError("%s lists a release twice" % origin)
    return sorted(values, key=parse)


def read_qualified(source_dir) -> List[str]:
    """The qualified releases, lowest first, from the source tree or package
    data rooted at `source_dir` (the directory that holds ``adapters/``)."""
    path = Path(source_dir).joinpath(*QUALIFIED_PARTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QualifiedVersionsError("cannot read %s: %s" % (path, exc)) from exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise QualifiedVersionsError("invalid JSON in %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise QualifiedVersionsError("%s is not a %s document" % (path, SCHEMA))
    return _validate(data.get("qualified"), str(path))


@dataclass(frozen=True)
class Verdict:
    version: Optional[str]
    status: str
    qualified: Tuple[str, ...]
    # The qualified release whose native-config schema subset applies: the
    # version itself when qualified, else the nearest lower qualified release.
    schema_version: Optional[str]

    @property
    def floor(self) -> str:
        return self.qualified[0]

    @property
    def highest(self) -> str:
        return self.qualified[-1]

    @property
    def launches_personal(self) -> bool:
        return self.status in (QUALIFIED, NEWER, UNQUALIFIED)

    @property
    def launches_work(self) -> bool:
        return self.status == QUALIFIED

    @property
    def unqualified_runtime(self) -> bool:
        return self.status in (NEWER, UNQUALIFIED)

    def launches(self, classification: str) -> bool:
        # Anything that is not exactly "personal" is held to the work rule,
        # matching how profile defaults treat classifications.
        return self.launches_personal if classification == "personal" else self.launches_work

    def warning(self) -> Optional[str]:
        """The one-line warning a launch on this runtime carries, or None."""
        if not self.unqualified_runtime:
            return None
        relation = "newer than the qualified releases" if self.status == NEWER else "not a qualified release"
        return (
            "OpenCode %s is %s (%s); it runs with the native configuration schema of %s "
            "and Quoin's lifecycle plugin, if enabled, is untested on it"
            % (self.version, relation, ", ".join(self.qualified), self.schema_version)
        )


def nearest_qualified(version: Optional[str], qualified: Sequence[str]) -> Optional[str]:
    """The highest qualified release that is not newer than `version`."""
    key = parse(version)
    if key is None:
        return None
    lower = [v for v in qualified if parse(v) <= key]  # type: ignore[operator]
    return max(lower, key=parse) if lower else None


def judge(version: Optional[str], qualified: Sequence[str]) -> Verdict:
    ordered = tuple(_validate(list(qualified), "qualified list"))
    key = parse(version)
    if key is None:
        status = UNKNOWN
    elif version in ordered:
        status = QUALIFIED
    elif key < parse(ordered[0]):  # type: ignore[operator]
        status = BELOW_FLOOR
    elif key > parse(ordered[-1]):  # type: ignore[operator]
        status = NEWER
    else:
        status = UNQUALIFIED
    return Verdict(
        version=version, status=status, qualified=ordered,
        schema_version=nearest_qualified(version, ordered),
    )


def refusal_message(verdict: Verdict, classification: str) -> str:
    """The actionable reason a launch on this runtime is refused."""
    listed = ", ".join(verdict.qualified)
    if verdict.status == UNKNOWN:
        return (
            "the opencode executable reports no version; install a qualified OpenCode release (%s)" % listed
        )
    if verdict.status == BELOW_FLOOR:
        return (
            "the opencode executable reports %s, older than the supported floor %s; "
            "upgrade to a qualified OpenCode release (%s)" % (verdict.version, verdict.floor, listed)
        )
    return (
        "the opencode executable reports %s, which is not a qualified release (%s), and the %s profile "
        "launches only qualified releases; install a qualified release, or qualify %s first "
        "(see release-acceptance.md, Upgrade qualification)"
        % (verdict.version, listed, classification, verdict.version)
    )
