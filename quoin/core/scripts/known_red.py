#!/usr/bin/env python3
"""known-red manifest reader/matcher (IVG-144 S-02, portable core).

Consumes a committed manifest of intentionally-red baseline tests
(`quoin/dev/tests/known-red.toml`) and a caller-captured pytest report, and
answers: "given this run's observed failures, are they ALL known-baseline
(safe to downgrade) or is there a net-new failure (must block)?"

Fail-closed spine (FR-9): an ABSENT manifest yields zero exemptions (every
failure is net-new → caller blocks); a MALFORMED manifest raises and the CLI
exits 2 (surfaced error, never exempt-all); an UNRECONCILED report (the
caller's observed pytest rc disagrees with what the report can account for, or
an independent junit count disagrees with the parsed failure count) exits 3
(fail-closed — the report could not be trusted).

Node-id identity comes SOLELY from `pytest -rA` short-summary stdout
(`PASSED/FAILED/ERROR <nodeid>` lines). junit-xml (`--junitxml`) is used only
as an independent COUNT ORACLE for reconciliation — it never reconstructs a
node-id (its dotted `classname` is not deterministically reversible).

See `.workflow_artifacts/known-red-manifest/stage-2/current-plan.md` (D-01..D-10).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MalformedManifest(Exception):
    """Raised when the manifest cannot be parsed or fails schema validation."""


# ---------------------------------------------------------------------------
# T-03 — load + validate (tomllib-or-internal)
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = ("id", "reason", "added")
_OPTIONAL_KEYS = ("issue", "owner")
_ALLOWED_KEYS = frozenset(_REQUIRED_KEYS + _OPTIONAL_KEYS)


def _decode_basic_string(raw: str) -> str:
    """Decode a TOML basic (double-quoted) string body per the minimal grammar.

    Handles the common escapes only; any other backslash escape is malformed.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    escapes = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                raise MalformedManifest("dangling escape in string value")
            nxt = raw[i + 1]
            if nxt not in escapes:
                raise MalformedManifest(f"unsupported escape '\\{nxt}' in string value")
            out.append(escapes[nxt])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_quoted_value(value: str) -> str:
    """Parse a value that MUST be a single double-quoted string.

    An optional trailing ``# comment`` after the closing quote is allowed.
    Anything else (bare word, number, bool, single-quote, array, unterminated
    quote, multiline) raises MalformedManifest.
    """
    value = value.strip()
    if not value.startswith('"'):
        raise MalformedManifest(f"value is not a double-quoted string: {value!r}")
    # Scan for the closing (unescaped) double quote.
    i = 1
    n = len(value)
    body: list[str] = []
    while i < n:
        ch = value[i]
        if ch == "\\":
            if i + 1 >= n:
                raise MalformedManifest("unterminated string (dangling escape)")
            body.append(value[i])
            body.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            # Closing quote found; the remainder must be blank or a comment.
            rest = value[i + 1:].strip()
            if rest and not rest.startswith("#"):
                raise MalformedManifest(f"trailing content after string value: {rest!r}")
            return _decode_basic_string("".join(body))
        body.append(ch)
        i += 1
    raise MalformedManifest("unterminated double-quoted string")


def _parse_known_red_toml(text: str) -> list[dict]:
    """Minimal internal TOML parser restricted to the known-red grammar.

    Grammar: ``[[entry]]`` array-of-tables headers, ``key = "double-quoted"``
    assignments, ``#`` comments, blank lines. ANY construct outside this
    grammar raises MalformedManifest (never silently accepted).
    """
    entries: list[dict] = []
    current: dict | None = None
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line == "[[entry]]":
                current = {}
                entries.append(current)
                continue
            raise MalformedManifest(
                f"line {lineno}: only [[entry]] tables allowed, got {line!r}"
            )
        if "=" not in line:
            raise MalformedManifest(f"line {lineno}: not a key = value assignment: {line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise MalformedManifest(f"line {lineno}: invalid key {key!r}")
        if current is None:
            raise MalformedManifest(
                f"line {lineno}: assignment outside any [[entry]] table"
            )
        current[key] = _parse_quoted_value(value)
    return entries


def _validate_entries(entries) -> list[dict]:
    """Schema-validate the extracted entry list (uniform across both parsers)."""
    if not isinstance(entries, list):
        raise MalformedManifest("`entry` must be an array of tables")
    validated: list[dict] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MalformedManifest(f"entry {idx} is not a table")
        for key in _REQUIRED_KEYS:
            if key not in entry:
                raise MalformedManifest(f"entry {idx} missing required key {key!r}")
        for key, val in entry.items():
            if key not in _ALLOWED_KEYS:
                raise MalformedManifest(f"entry {idx} has unknown key {key!r}")
            if not isinstance(val, str):
                raise MalformedManifest(
                    f"entry {idx} key {key!r} must be a string, got {type(val).__name__}"
                )
        validated.append({k: entry[k] for k in entry})
    return validated


def load_manifest(path) -> list[dict]:
    """Load and validate the manifest. Absent path → [] (fail-closed, D-05)."""
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - exercised via monkeypatch
            raise MalformedManifest(f"TOML decode error: {exc}") from exc
        entries = data.get("entry", [])
    else:
        entries = _parse_known_red_toml(text)
    return _validate_entries(entries)


# ---------------------------------------------------------------------------
# T-04 — matcher + partition + downgrade decision (pure fns)
# ---------------------------------------------------------------------------


def normalize(entries) -> tuple[set, set]:
    """Split entry ids into (node-id ids, whole-file ids) by `::` presence."""
    nodeid_ids = {e["id"] for e in entries if "::" in e["id"]}
    file_ids = {e["id"] for e in entries if "::" not in e["id"]}
    return nodeid_ids, file_ids


def match_failures(failed, entries) -> tuple[list[dict], list[str]]:
    """Partition a trusted failed-set into (known_red entries, net_new ids).

    D-04 anchored/exact rule: a node-id entry matches iff string-equal; a
    whole-file entry matches a failed nodeid iff `nodeid.split('::',1)[0] == id`.
    No substring, no glob, no prefix. `known_red` carries the matched entries'
    `{id,reason,added}`, deduped and deterministically sorted.
    """
    nodeid_ids, file_ids = normalize(entries)
    by_id = {e["id"]: e for e in entries}
    matched_entry_ids: set = set()
    net_new: list[str] = []
    for nodeid in failed:
        if nodeid in nodeid_ids:
            matched_entry_ids.add(nodeid)
        else:
            file_prefix = nodeid.split("::", 1)[0]
            if file_prefix in file_ids:
                matched_entry_ids.add(file_prefix)
            else:
                net_new.append(nodeid)
    known_red = [
        {"id": by_id[i]["id"], "reason": by_id[i]["reason"], "added": by_id[i]["added"]}
        for i in sorted(matched_entry_ids)
    ]
    return known_red, sorted(net_new)


def downgrade_ok(failed, net_new) -> bool:
    """True iff there is something to downgrade AND nothing net-new remains."""
    return net_new == [] and failed != set()


# ---------------------------------------------------------------------------
# T-05 — node-id sourcing (-rA PRIMARY identity / junit COUNT ORACLE / filter)
# ---------------------------------------------------------------------------

# Node-id: a non-whitespace, non-bracket run optionally followed by a single
# [...] param group. The bracket is bounded by the literal `]`, so an internal
# ` - ` inside the param (e.g. test[a - b]) never truncates the node-id
# (round-3 MIN-1 fix over round-2's non-greedy `(.+?)`).
_REPORT_LINE_RE = re.compile(
    r"^(PASSED|FAILED|ERROR)\s+([^\s\[]+(?:\[[^\]]*\])?)(?:\s+-\s.*)?$"
)


_SHORT_SUMMARY_HEADER_RE = re.compile(r"^=+\s*short test summary info\s*=+$")


def _short_summary_lines(lines):
    """Tail slice after the LAST short-summary header, or None if absent.

    pytest emits the top-level short-summary section AFTER every captured-output
    section, so a nested run's header (echoed inside a captured-stdout block) is
    always earlier in the stream. Anchoring on the last header therefore scopes
    parsing to the real summary. No header at all (a partial or non -rA capture)
    returns None and the caller falls back to scanning the whole text.
    """
    last = None
    for idx, line in enumerate(lines):
        if _SHORT_SUMMARY_HEADER_RE.match(line.strip()):
            last = idx
    return None if last is None else lines[last + 1:]


def parse_pytest_report(text: str) -> tuple[set, set]:
    """Parse `pytest -rA` short-summary stdout → (passed, failed_or_error).

    SOLE node-id identity source. PASSED → passed; FAILED/ERROR → failed_or_error.
    Parsing is scoped to the text after the last short-summary header (if any) so
    that a nested pytest run's captured stdout can't leak PASSED/FAILED lines into
    the outer run's identity set.
    """
    passed: set = set()
    failed_or_error: set = set()
    lines = text.splitlines()
    scoped = _short_summary_lines(lines)
    if scoped is not None:
        lines = scoped
    for line in lines:
        m = _REPORT_LINE_RE.match(line.strip())
        if not m:
            continue
        outcome, nodeid = m.group(1), m.group(2)
        if outcome == "PASSED":
            passed.add(nodeid)
        else:  # FAILED or ERROR
            failed_or_error.add(nodeid)
    return passed, failed_or_error


def parse_junit_count(xml_text: str) -> int:
    """COUNT ORACLE only: sum failures+errors over ALL <testsuite> elements.

    Correct whether the root is pytest's real `<testsuites>` wrapper (attrs on
    the child `<testsuite>`) or a bare `<testsuite>` root. Never walks
    `<testcase>` and never reconstructs a node-id. Malformed XML raises
    `ParseError` (the caller treats the oracle as unavailable for that run).
    """
    root = ET.fromstring(xml_text)
    total = 0
    for ts in root.iter("testsuite"):
        total += int(ts.get("failures", 0)) + int(ts.get("errors", 0))
    return total


def _file_prefix(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _selector_parts(s: str) -> tuple:
    """Path-component tuple for a selector or node-id file-prefix.

    Normalizes to POSIX components so an absolute selector (e.g. emitted by
    `affected_tests.py`) and a repo-relative node-id can be compared by
    trailing-component suffix instead of requiring literal path equality.
    """
    return PurePosixPath(Path(_file_prefix(s)).as_posix()).parts


def _is_component_suffix(short: tuple, long: tuple) -> bool:
    """True iff `short` is a whole-component tail of `long` (never a string prefix)."""
    return len(short) <= len(long) and long[len(long) - len(short):] == short


def _selector_matches(node_parts: tuple, sel_parts: tuple, sel_is_dir: bool) -> bool:
    """True iff a node-id's path parts fall under a selector's path parts.

    File selectors match by mutual component-suffix (either side may be the
    absolute one). Directory selectors match iff `sel_parts` appears as a
    contiguous window within the node-id's directory components.
    """
    if not sel_parts:
        return False
    if sel_is_dir:
        window = node_parts[:-1]
        n = len(sel_parts)
        return any(
            window[i:i + n] == sel_parts for i in range(len(window) - n + 1)
        )
    return _is_component_suffix(sel_parts, node_parts) or _is_component_suffix(
        node_parts, sel_parts
    )


def apply_selector_filter(passed, failed_or_error, selectors) -> tuple[set, set]:
    """Restrict both sets to node-ids whose file-prefix falls under `selectors`.

    Selector and node-id may be absolute or repo-relative on either side —
    matching is by path-component suffix, not literal string equality, so an
    absolute selector from `affected_tests.py` matches repo-relative node-ids
    parsed from pytest output (IVG-254).
    """
    prepared = []
    for s in selectors:
        parts = _selector_parts(s)
        is_dir = bool(parts) and not parts[-1].endswith(".py")
        prepared.append((parts, is_dir))

    def _keep(nodeid: str) -> bool:
        node_parts = _selector_parts(nodeid)
        return any(_selector_matches(node_parts, p, d) for (p, d) in prepared)

    p = {n for n in passed if _keep(n)}
    f = {n for n in failed_or_error if _keep(n)}
    return p, f


# ---------------------------------------------------------------------------
# T-06 — staleness counter (full-suite-only, dedup)
# ---------------------------------------------------------------------------


def _counter_path(project_root=None, counter_file=None) -> Path:
    if counter_file is not None:
        return Path(counter_file)
    root = Path(project_root) if project_root is not None else Path.cwd()
    return root / ".workflow_artifacts" / "cache" / "known-red-runs.json"


def load_counter(path) -> dict:
    """Load the staleness counter; absent or corrupt → {} (advisory cache)."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_counter(path, counter: dict) -> None:
    """Atomically write the staleness counter."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(counter, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _stale_threshold() -> int:
    raw = os.environ.get("QUOIN_KNOWN_RED_STALE_RUNS", "3")
    try:
        return int(raw)
    except ValueError:
        return 3


def _entry_outcome(entry, passed, failed_or_error) -> str:
    """Classify an entry's outcome this run: 'pass' | 'fail' | 'neither'.

    Whole-file entries aggregate: a pass requires ≥1 of its nodeids to appear
    AND none of the appearing nodeids to be in failed_or_error (skipped/out-of-
    scope nodeids are UNTOUCHED — 'not-failed' is never inferred as 'passed').
    """
    entry_id = entry["id"]
    if "::" in entry_id:
        if entry_id in failed_or_error:
            return "fail"
        if entry_id in passed:
            return "pass"
        return "neither"
    # whole-file entry
    appearing_failed = {n for n in failed_or_error if _file_prefix(n) == entry_id}
    appearing_passed = {n for n in passed if _file_prefix(n) == entry_id}
    if appearing_failed:
        return "fail"
    if appearing_passed:
        return "pass"
    return "neither"


def update_staleness(entries, passed, failed_or_error, run_token, counter_path) -> list[dict]:
    """Update the per-entry consecutive-pass counter and return stale entries.

    D-07: per-entry pass → +1; fail → reset 0; neither → untouched. Per-run-token
    dedup (one task/session's multiple consults = one run). Threshold N (default 3
    via QUOIN_KNOWN_RED_STALE_RUNS): consecutive_pass >= N emits a staleness WARN.
    """
    counter = load_counter(counter_path)
    threshold = _stale_threshold()
    for entry in entries:
        entry_id = entry["id"]
        rec = counter.get(entry_id, {"consecutive_pass": 0, "last_run_token": None})
        outcome = _entry_outcome(entry, passed, failed_or_error)
        if outcome == "neither":
            counter[entry_id] = rec
            continue
        # dedup: same run-token already recorded → do not re-apply
        if run_token is not None and rec.get("last_run_token") == run_token:
            counter[entry_id] = rec
            continue
        if outcome == "pass":
            rec = {
                "consecutive_pass": int(rec.get("consecutive_pass", 0)) + 1,
                "last_run_token": run_token,
            }
        else:  # fail
            rec = {"consecutive_pass": 0, "last_run_token": run_token}
        counter[entry_id] = rec
    save_counter(counter_path, counter)
    stale = [
        {"id": entry["id"], "consecutive_pass": int(counter[entry["id"]]["consecutive_pass"])}
        for entry in entries
        if int(counter.get(entry["id"], {}).get("consecutive_pass", 0)) >= threshold
    ]
    stale.sort(key=lambda s: s["id"])
    return stale


# ---------------------------------------------------------------------------
# T-07 — CLI + reconciliation + output
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_NET_NEW = 1
EXIT_MALFORMED = 2
EXIT_UNRECONCILED = 3
EXIT_USAGE = 64


_MANIFEST_REL = Path("quoin") / "dev" / "tests" / "known-red.toml"
# Any project other than quoin itself keeps its manifest with the rest of the
# workflow state, so it can never leak into that project's commits.
_PROJECT_MANIFEST_REL = Path(".workflow_artifacts") / "known-red.toml"


def _default_manifest(project_root) -> Path:
    """Resolve the manifest against the git root, not the outer project root.

    A direct hit at `<project_root>/<_MANIFEST_REL>` wins unconditionally, so a
    single-repo project behaves exactly as before. Otherwise scan `project_root`'s
    immediate subdirectories that look like a git checkout — a `.git` ENTRY, not
    necessarily a directory, since a worktree's `.git` is a file — in sorted order
    for determinism, and return the first one whose manifest path exists. Absent
    everywhere → `<project_root>/.workflow_artifacts/known-red.toml` (the
    quoin-repo layout path is never used as a default in other projects), and
    `load_manifest` returns `[]` when that file does not exist either, so the
    fail-closed spine holds.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    direct = root / _MANIFEST_REL
    if direct.exists():
        return direct
    fallback = root / _PROJECT_MANIFEST_REL
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return fallback
    for sub in candidates:
        if (sub / ".git").exists():
            candidate = sub / _MANIFEST_REL
            if candidate.exists():
                return candidate
    return fallback


def drop_phantom_failures(observed_rc, failed_or_error, junit_count):
    """Clear parsed failures when BOTH independent oracles report green.

    The caller's observed rc and the junit failure/error counts are independent
    of report text. When both say zero, any surviving parsed node-id can only be
    an artifact of arbitrary text in captured stdout, never a real failure.
    """
    if observed_rc == 0 and junit_count == 0 and failed_or_error:
        return set()
    return failed_or_error


def _reconcile(observed_rc, failed_or_error, reconcile_ok) -> bool:
    """D-08 reconciliation rule."""
    if observed_rc == 0 and failed_or_error == set():
        return True
    if failed_or_error != set() and reconcile_ok:
        return True
    return False


_GENERIC_UNRECONCILED_REASON = (
    "the supplied report could not be reconciled with observed pytest rc={rc}"
)


def _unreconciled_reason(observed_rc, failed_or_error, reconcile_ok) -> str:
    if observed_rc != 0 and not failed_or_error:
        return (
            f"observed pytest rc={observed_rc} indicates a red run but no "
            "failures/errors could be parsed from the supplied report"
        )
    if failed_or_error and not reconcile_ok:
        return (
            f"the report's parsed failure count ({len(failed_or_error)}) disagrees "
            "with the independent junit count"
        )
    return _GENERIC_UNRECONCILED_REASON.format(rc=observed_rc)


def _render_text(payload, observed_rc, reason=None) -> str:
    lines: list[str] = []
    if payload["known_red"]:
        lines.append("## Known-baseline (downgraded)")
        for e in payload["known_red"]:
            lines.append(f"- {e['id']} — {e['reason']} (added {e['added']})")
    if payload["net_new"]:
        lines.append("## Net-new failures (blocking)")
        for nid in payload["net_new"]:
            lines.append(f"- {nid}")
    if payload["stale"]:
        lines.append("## Staleness")
        for s in payload["stale"]:
            lines.append(
                f"- {s['id']}: passed {s['consecutive_pass']} consecutive runs — recommend removal"
            )
    if not payload["reconciled"]:
        lines.append("## Reconciliation")
        fallback = _GENERIC_UNRECONCILED_REASON.format(rc=observed_rc)
        lines.append(
            f"UNRECONCILED: {reason or fallback} — treating as blocking (fail-closed)"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="known_red.py",
        description="known-red manifest reader/matcher (IVG-144).",
    )
    p.add_argument("--pytest-output", help="pytest -rA stdout file (PRIMARY node-id identity source)")
    p.add_argument("--junit", help="junit-xml file (OPTIONAL count oracle for reconciliation)")
    p.add_argument("--observed-rc", type=int, help="caller's observed pytest return code")
    p.add_argument("--selectors", nargs="*", default=None, help="file-prefix filter over the parsed sets")
    p.add_argument("--full-suite", action="store_true", help="enable staleness counting")
    p.add_argument("--run-token", default=None, help="per-session dedup token for staleness")
    p.add_argument("--project-root", default=None, help="project root for manifest/counter defaults")
    p.add_argument("--counter-file", default=None, help="override staleness counter path")
    p.add_argument("--manifest", default=None, help="override manifest path")
    p.add_argument("--format", choices=("json", "text"), default="json")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    report_flags = (
        args.junit is not None
        or args.observed_rc is not None
        or args.full_suite
        or args.selectors is not None
    )

    # Usage errors (exit 64) — validated before anything else.
    if args.pytest_output is not None and args.observed_rc is None:
        print("usage error: --pytest-output requires --observed-rc", file=sys.stderr)
        return EXIT_USAGE
    if args.junit is not None and args.pytest_output is None:
        print("usage error: --junit requires --pytest-output (junit alone cannot establish identity)", file=sys.stderr)
        return EXIT_USAGE
    if report_flags and args.pytest_output is None:
        print("usage error: a report-taking mode requires --pytest-output", file=sys.stderr)
        return EXIT_USAGE

    # Load manifest (fail-closed on malformed → exit 2, before reconciliation).
    manifest_path = args.manifest or _default_manifest(args.project_root)
    try:
        entries = load_manifest(manifest_path)
    except MalformedManifest as exc:
        print(f"malformed manifest: {exc}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps({"malformed": True}))
        return EXIT_MALFORMED

    # No report supplied → nothing to evaluate (manifest-only validation mode).
    if args.pytest_output is None:
        payload = {
            "downgrade": False,
            "reconciled": True,
            "known_red": [],
            "net_new": [],
            "stale": [],
            "malformed": False,
        }
        _emit(payload, args.format, args.observed_rc)
        return EXIT_OK

    report_text = Path(args.pytest_output).read_text(encoding="utf-8")
    passed, failed_or_error = parse_pytest_report(report_text)
    if args.selectors is not None:
        passed, failed_or_error = apply_selector_filter(passed, failed_or_error, args.selectors)

    # junit count oracle (reconciliation only).
    reconcile_ok = True
    if args.junit is not None:
        try:
            junit_count = parse_junit_count(Path(args.junit).read_text(encoding="utf-8"))
            failed_or_error = drop_phantom_failures(args.observed_rc, failed_or_error, junit_count)
            reconcile_ok = len(failed_or_error) == junit_count
        except ET.ParseError:
            reconcile_ok = True  # oracle unavailable → trust the -rA parse at face value

    reconciled = _reconcile(args.observed_rc, failed_or_error, reconcile_ok)

    if not reconciled:
        reason = _unreconciled_reason(args.observed_rc, failed_or_error, reconcile_ok)
        payload = {
            "downgrade": False,
            "reconciled": False,
            "known_red": [],
            "net_new": [],
            "stale": [],
            "malformed": False,
        }
        _emit(payload, args.format, args.observed_rc, reason)
        return EXIT_UNRECONCILED

    known_red, net_new = match_failures(failed_or_error, entries)

    stale: list[dict] = []
    if args.full_suite:
        counter_path = _counter_path(args.project_root, args.counter_file)
        stale = update_staleness(entries, passed, failed_or_error, args.run_token, counter_path)

    downgrade = downgrade_ok(failed_or_error, net_new)
    payload = {
        "downgrade": bool(downgrade),
        "reconciled": True,
        "known_red": known_red,
        "net_new": net_new,
        "stale": stale,
        "malformed": False,
    }
    _emit(payload, args.format, args.observed_rc)
    return EXIT_NET_NEW if net_new else EXIT_OK


def _emit(payload, fmt, observed_rc, reason=None) -> None:
    if fmt == "json":
        print(json.dumps(payload))
    else:
        sys.stdout.write(_render_text(payload, observed_rc, reason))


if __name__ == "__main__":
    sys.exit(main())
