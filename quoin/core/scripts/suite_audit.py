#!/usr/bin/env python3
"""Deterministic helpers for test-suite audits; stdlib only, reads files tools produce.

Subcommands
  durations <junit.xml> [--top N]
      JSON with test count, skipped count, total and summed time, slowest tests, per-file totals.
  static <tests-root> [--min-fixture-users K]
      JSON hints from the test sources: sleeps, assert-free tests, unregistered markers and
      function-scoped fixtures that do I/O and are used by at least K tests (default 5).
  redundancy <coverage.json> [--durations junit.xml] [--exclude-ids FILE] [--all-tests FILE]
             [--source-root DIR]
      Reads `coverage json --show-contexts` output. Reports per-test reasons (duplicate-of,
      dominated-by, union-covered, covers-nothing) and one jointly removable set: removing the
      whole set leaves every covered line covered. Tests with no covered source line are never
      removable. Coverage of test modules and conftest files is ignored.
  compare <before.json> <after.json> [--expected-removed N]
      Snapshot shape {"count", "runtime", "line_coverage", "survivors", "unstable"}. Exit 0 when
      the count dropped by exactly N, coverage did not fall and no new stable survivor appeared.
  deselect-plugin <ids-file> <out-dir>
      Writes quoin_deselect.py, a pytest plugin that drops listed node ids (and reverses order
      when asked) so removal sets travel through PYTEST_ADDOPTS instead of long command lines.
      Ids file: {"deselect": [node ids], "reverse": false}.

Exit codes: 0 ok, 1 comparison failed, 2 unreadable or malformed input.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

BUILTIN_MARKERS = {"skip", "skipif", "xfail", "parametrize", "usefixtures", "filterwarnings"}
IO_HINTS = re.compile(
    r"\bopen\(|subprocess|Popen|tmp_path|tmpdir|write_text|write_bytes|read_text|shutil|"
    r"socket|requests\.|urlopen|sleep\(|mkdtemp|sqlite3\.connect"
)


class InputError(Exception):
    pass


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _load_json(path: str):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read JSON {path}: {exc}") from exc


# ----------------------------------------------------------------------- durations

def _classname_to_path(classname: str) -> tuple[str, str]:
    """Split a junit classname into (file path, class chain) by stopping at the first class-like part."""
    parts = classname.split(".")
    k = len(parts)
    for i, part in enumerate(parts):
        if i > 0 and part[:1].isupper():
            k = i
            break
    return "/".join(parts[:k]) + ".py", "::".join(parts[k:])


def junit_node_id(classname: str, name: str) -> str:
    path, cls = _classname_to_path(classname)
    return f"{path}::{cls}::{name}" if cls else f"{path}::{name}"


def _parse_junit(path: str):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise InputError(f"malformed junit XML {path}: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = 0.0
    for s in suites:
        try:
            total += float(s.get("time", 0) or 0)
        except ValueError:
            pass
    cases = []
    for tc in root.iter("testcase"):
        try:
            t = float(tc.get("time", 0) or 0)
        except ValueError:
            t = 0.0
        skipped = tc.find("skipped") is not None
        cases.append((junit_node_id(tc.get("classname", ""), tc.get("name", "")), t, skipped))
    return total, cases


def cmd_durations(args) -> int:
    total, cases = _parse_junit(args.junit)
    ran = [(i, t) for i, t, s in cases if not s]
    per_file: dict[str, float] = defaultdict(float)
    for i, t in ran:
        per_file[i.split("::", 1)[0]] += t
    summed = sum(t for _, t in ran)
    slowest = sorted(ran, key=lambda x: (-x[1], x[0]))[: args.top]
    _emit({
        "count": len(ran),
        "skipped": sum(1 for *_, s in cases if s),
        "total_time": total or summed,
        "summed_time": summed,
        "slowest": [{"id": i, "time": t} for i, t in slowest],
        "per_file": {k: round(v, 6) for k, v in sorted(per_file.items())},
    })
    return 0


# ----------------------------------------------------------------------- static

def _registered_markers(tests_root: Path) -> set[str]:
    names: set[str] = set()

    def add(entries):
        for e in entries:
            m = re.match(r"\s*([A-Za-z_][\w-]*)", e)
            if m:
                names.add(m.group(1))

    for d in [tests_root, *tests_root.resolve().parents]:
        pyproject, ini, cfg = d / "pyproject.toml", d / "pytest.ini", d / "setup.cfg"
        if not (pyproject.exists() or ini.exists() or cfg.exists()):
            continue
        if pyproject.exists():
            try:
                import tomllib
                data = tomllib.loads(pyproject.read_text())
                add(data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", []))
            except ImportError:
                text = pyproject.read_text()
                m = re.search(r"markers\s*=\s*\[(.*?)\]", text, re.S)
                if m:
                    add(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))
            except ValueError:
                pass
        for f in (ini, cfg):
            if f.exists():
                text = f.read_text()
                m = re.search(r"^markers\s*=\s*(.*?)(?=^\S|\Z)", text, re.S | re.M)
                if m:
                    add([ln for ln in m.group(1).splitlines() if ln.strip()])
        break
    return names


def _decorator_names(node) -> list[str]:
    out = []
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        out.append(ast.unparse(target))
    return out


def _is_fixture(node) -> tuple[bool, bool]:
    """Return (is_fixture, function_scoped)."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if ast.unparse(target) in {"pytest.fixture", "fixture"}:
            scope = "function"
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                        scope = str(kw.value.value)
            return True, scope == "function"
    return False, False


def _has_assertion(fn) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name.startswith("assert") or name in {"raises", "warns", "fail"}:
                return True
    return False


def cmd_static(args) -> int:
    root = Path(args.tests_root)
    if not root.is_dir():
        raise InputError(f"not a directory: {root}")
    registered = _registered_markers(root)
    sleeps, assert_free, used_markers = [], [], defaultdict(list)
    fixtures: dict[str, dict] = {}
    test_params: list[tuple[str, set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_fix, func_scoped = _is_fixture(node)
            if is_fix:
                body_src = ast.get_source_segment(path.read_text(), node) or ""
                fixtures[node.name] = {"file": rel, "line": node.lineno, "function": func_scoped,
                                       "io": bool(IO_HINTS.search(body_src))}
                continue
            if not node.name.startswith("test"):
                continue
            test_params.append((rel, {a.arg for a in node.args.args}))
            for dec in _decorator_names(node):
                m = re.match(r"pytest\.mark\.(\w+)", dec)
                if m:
                    used_markers[m.group(1)].append(f"{rel}:{node.lineno}")
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    f = n.func
                    if (isinstance(f, ast.Attribute) and f.attr == "sleep") or (
                            isinstance(f, ast.Name) and f.id == "sleep"):
                        sleeps.append({"file": rel, "line": n.lineno, "function": node.name})
            if not _has_assertion(node):
                assert_free.append({"file": rel, "line": node.lineno, "function": node.name})
    unregistered = sorted(
        ({"marker": m, "uses": u[:5]} for m, u in used_markers.items()
         if m not in registered and m not in BUILTIN_MARKERS),
        key=lambda x: x["marker"])
    heavy = []
    for name, info in sorted(fixtures.items()):
        if not (info["function"] and info["io"]):
            continue
        users = sum(1 for _, params in test_params if name in params)
        if users >= args.min_fixture_users:
            heavy.append({"fixture": name, "file": info["file"], "line": info["line"], "users": users})
    _emit({"sleeps": sleeps, "assert_free": assert_free, "unregistered_markers": unregistered,
           "heavy_function_fixtures": heavy})
    return 0


# ----------------------------------------------------------------------- redundancy

def _is_test_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"


def _test_line_sets(cov: dict) -> dict[str, set[tuple[str, int]]]:
    sets: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for fname, info in cov.get("files", {}).items():
        if _is_test_file(fname):
            for ctxs in info.get("contexts", {}).values():
                for c in ctxs:
                    if c:
                        sets.setdefault(c.rsplit("|", 1)[0] if "|" in c else c, set())
            continue
        for line, ctxs in info.get("contexts", {}).items():
            for c in ctxs:
                if not c:
                    continue
                node = c.rsplit("|", 1)[0] if "|" in c else c
                sets[node].add((fname, int(line)))
    return sets


def _functions_for(source_root: Path, lines: set[tuple[str, int]]) -> list[str]:
    by_file: dict[str, set[int]] = defaultdict(set)
    for f, ln in lines:
        by_file[f].add(ln)
    names = set()
    for f, nums in by_file.items():
        try:
            tree = ast.parse((source_root / f).read_text())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    n.lineno <= x <= (n.end_lineno or n.lineno) for x in nums):
                names.add(f"{f}::{n.name}")
    return sorted(names)


def cmd_redundancy(args) -> int:
    cov = _load_json(args.coverage)
    sets = _test_line_sets(cov)
    runtime: dict[str, float] = {}
    known = set(sets)
    if args.durations:
        _, cases = _parse_junit(args.durations)
        for i, t, skipped in cases:
            runtime[i] = runtime.get(i, 0.0) + t
            if not skipped:
                known.add(i)
    if args.all_tests:
        known |= {ln.strip() for ln in Path(args.all_tests).read_text().splitlines() if ln.strip()}
    excluded = set()
    if args.exclude_ids:
        excluded = {ln.strip() for ln in Path(args.exclude_ids).read_text().splitlines() if ln.strip()}
    for t in known:
        sets.setdefault(t, set())

    reasons: dict[str, list[str]] = defaultdict(list)
    nonempty = {t: frozenset(s) for t, s in sets.items() if s}
    for t, s in sets.items():
        if not s:
            reasons[t].append("covers-nothing")
    groups: dict[frozenset, list[str]] = defaultdict(list)
    for t, s in nonempty.items():
        if t not in excluded:
            groups[s].append(t)

    def rank(i: str):
        return (len(i), i)

    removed: set[str] = set()
    for s, members in groups.items():
        rep = min(members, key=rank)
        for m in members:
            if m != rep:
                removed.add(m)
                reasons[m].append(f"duplicate-of:{rep}")
    for t, s in nonempty.items():
        supers = [o for o, os_ in nonempty.items() if o != t and s < os_]
        if supers:
            reasons[t].append(f"dominated-by:{min(supers, key=rank)}")
    covering: dict[tuple[str, int], int] = defaultdict(int)
    for s in nonempty.values():
        for ln in s:
            covering[ln] += 1
    for t, s in nonempty.items():
        if all(covering[ln] >= 2 for ln in s):
            reasons[t].append("union-covered")
    count: dict[tuple[str, int], int] = defaultdict(int)
    for t, s in nonempty.items():
        if t not in removed:
            for ln in s:
                count[ln] += 1
    reps = [min(m, key=rank) for m in groups.values()]
    if runtime:
        order = sorted(reps, key=lambda i: (-runtime.get(i, 0.0), i))
    else:
        order = sorted(reps)
    for t in order:
        if all(count[ln] >= 2 for ln in nonempty[t]):
            removed.add(t)
            for ln in nonempty[t]:
                count[ln] -= 1
    src_root = Path(args.source_root) if args.source_root else None
    candidates = {}
    for t in sorted(removed):
        files = sorted({f for f, _ in nonempty[t]})
        entry = {"files": files}
        if src_root:
            entry["functions"] = _functions_for(src_root, set(nonempty[t]))
        candidates[t] = entry
    _emit({
        "reasons": {t: sorted(set(r)) for t, r in sorted(reasons.items())},
        "jointly_removable": sorted(removed),
        "candidates": candidates,
        "excluded": sorted(excluded),
    })
    return 0


# ----------------------------------------------------------------------- compare

def cmd_compare(args) -> int:
    before, after = _load_json(args.before), _load_json(args.after)
    try:
        failures = []
        want = before["count"] - args.expected_removed
        if after["count"] != want:
            failures.append(f"test count: expected {want}, got {after['count']}")
        if after["line_coverage"] < before["line_coverage"]:
            failures.append(f"line coverage fell from {before['line_coverage']} to {after['line_coverage']}")
        unstable = set(before.get("unstable", [])) | set(after.get("unstable", []))
        new = (set(after.get("survivors", [])) - unstable) - set(before.get("survivors", []))
        if new:
            failures.append(f"new surviving mutants: {sorted(new)[:5]}")
    except (KeyError, TypeError) as exc:
        raise InputError(f"snapshot missing field {exc}") from exc
    for f in failures:
        print("FAIL", f)
    print("compare: " + ("OK" if not failures else f"{len(failures)} regressions"))
    return 1 if failures else 0


# ----------------------------------------------------------------------- deselect plugin

PLUGIN_TEMPLATE = '''"""Drops listed node ids from collection and optionally reverses the order."""
import json

_IDS_FILE = {ids_file!r}


def pytest_collection_modifyitems(config, items):
    with open(_IDS_FILE) as fh:
        data = json.load(fh)
    drop = set(data.get("deselect", []))
    kept = [i for i in items if i.nodeid not in drop]
    gone = [i for i in items if i.nodeid in drop]
    if gone:
        config.hook.pytest_deselected(items=gone)
    items[:] = kept
    if data.get("reverse"):
        items.reverse()
'''


def cmd_deselect_plugin(args) -> int:
    ids_file = Path(args.ids_file).resolve()
    if not ids_file.is_file():
        raise InputError(f"ids file not found: {ids_file}")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "quoin_deselect.py").write_text(PLUGIN_TEMPLATE.format(ids_file=str(ids_file)))
    print("PYTEST_ADDOPTS=-p quoin_deselect")
    print(f"PYTHONPATH_PREPEND={out.resolve()}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("durations", help="summarise a junit XML")
    d.add_argument("junit")
    d.add_argument("--top", type=int, default=20)
    s = sub.add_parser("static", help="static hints from test sources")
    s.add_argument("tests_root")
    s.add_argument("--min-fixture-users", type=int, default=5)
    r = sub.add_parser("redundancy", help="coverage-based redundancy analysis")
    r.add_argument("coverage")
    r.add_argument("--durations")
    r.add_argument("--exclude-ids")
    r.add_argument("--all-tests")
    r.add_argument("--source-root")
    c = sub.add_parser("compare", help="compare before and after snapshots")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--expected-removed", type=int, default=0)
    g = sub.add_parser("deselect-plugin", help="write the deselection pytest plugin")
    g.add_argument("ids_file")
    g.add_argument("out_dir")
    args = p.parse_args(argv)
    handlers = {"durations": cmd_durations, "static": cmd_static, "redundancy": cmd_redundancy,
                "compare": cmd_compare, "deselect-plugin": cmd_deselect_plugin}
    try:
        return handlers[args.cmd](args) or 0
    except InputError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
