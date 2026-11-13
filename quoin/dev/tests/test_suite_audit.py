"""Tests for suite_audit: durations, static, redundancy, compare and deselect-plugin."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "quoin" / "scripts" / "suite_audit.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "suite_audit"


def run(*args):
    return subprocess.run([sys.executable, str(WRAPPER), *map(str, args)], capture_output=True, text=True)


def run_json(*args):
    res = run(*args)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def cov_file(tmp_path: Path, mapping: dict[str, list[int]], name="cov.json") -> Path:
    """mapping: test context -> executed lines of src/mod.py."""
    contexts: dict[str, list[str]] = {}
    for ctx, lines in mapping.items():
        for ln in lines:
            contexts.setdefault(str(ln), []).append(ctx)
    data = {"files": {"src/mod.py": {"executed_lines": sorted(int(k) for k in contexts),
                                      "contexts": contexts}}}
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------- durations

def test_durations_fixture():
    out = run_json("durations", FIXTURES / "junit.xml", "--top", "2")
    assert set(out) == {"count", "skipped", "total_time", "summed_time", "slowest", "per_file"}
    assert out["count"] == 4
    assert out["skipped"] == 1
    assert out["total_time"] == 12.5
    assert out["summed_time"] == pytest.approx(8.01)
    assert out["slowest"] == [
        {"id": "tests/test_a.py::test_slow", "time": 5.0},
        {"id": "tests/test_b.py::TestGroup::test_method", "time": 2.0},
    ]
    assert out["per_file"] == {"tests/test_a.py": pytest.approx(5.01), "tests/test_b.py": 3.0}


def test_durations_malformed_xml_exits_2(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><testcase")
    res = run("durations", bad)
    assert res.returncode == 2
    assert "malformed junit XML" in res.stderr
    assert "Traceback" not in res.stderr


# ---------------------------------------------------------------- static

def _static_tree(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["slow: slow tests", "smoke"]\n')
    (tests / "test_x.py").write_text(
        "import time\nimport pytest\n\n\n"
        "@pytest.fixture\ndef heavy(tmp_path):\n    return open(tmp_path / 'f', 'w')\n\n\n"
        "@pytest.fixture(scope='module')\ndef shared(tmp_path_factory):\n    return open('x')\n\n\n"
        "@pytest.mark.slow\ndef test_sleeper(heavy):\n    time.sleep(1)\n    assert True\n\n\n"
        "@pytest.mark.mystery\ndef test_no_assert(heavy):\n    x = 1\n\n\n"
        "def test_raises(heavy):\n    with pytest.raises(ValueError):\n        int('a')\n\n\n"
        "def test_helper_assert(heavy, shared):\n    assertEqual(1, 1)\n\n\n"
        "def test_plain(heavy):\n    assert 1\n")
    return tests


def test_static_hints(tmp_path):
    tests = _static_tree(tmp_path)
    out = run_json("static", tests, "--min-fixture-users", "3")
    assert set(out) == {"sleeps", "assert_free", "unregistered_markers", "heavy_function_fixtures"}
    assert [(s["function"], s["file"]) for s in out["sleeps"]] == [("test_sleeper", "test_x.py")]
    assert [a["function"] for a in out["assert_free"]] == ["test_no_assert"]
    assert [m["marker"] for m in out["unregistered_markers"]] == ["mystery"]
    assert [(h["fixture"], h["users"]) for h in out["heavy_function_fixtures"]] == [("heavy", 5)]


def test_static_fixture_threshold(tmp_path):
    tests = _static_tree(tmp_path)
    out = run_json("static", tests, "--min-fixture-users", "6")
    assert out["heavy_function_fixtures"] == []


def test_static_bad_root_exits_2(tmp_path):
    res = run("static", tmp_path / "missing")
    assert res.returncode == 2


# ---------------------------------------------------------------- redundancy

def test_redundancy_basic_fixture():
    out = run_json("redundancy", FIXTURES / "coverage-basic.json",
                   "--all-tests", FIXTURES / "all-basic.txt")
    reasons = out["reasons"]
    assert "covers-nothing" in reasons["tests/test_a.py::test_textfile"]
    assert "tests/test_a.py::test_textfile" not in out["jointly_removable"]
    assert "duplicate-of:tests/test_a.py::test_one" in reasons["tests/test_a.py::test_one_copy"]
    assert any(r.startswith("dominated-by:") for r in reasons["tests/test_a.py::test_one"])
    removable = set(out["jointly_removable"])
    assert "tests/test_a.py::test_one_copy" in removable
    assert "tests/test_a.py::test_big" not in removable


def test_two_identical_tests_one_removable(tmp_path):
    p = cov_file(tmp_path, {"t::a|run": [1, 2], "t::bb|run": [1, 2]})
    out = run_json("redundancy", p)
    assert out["jointly_removable"] == ["t::bb"]


def test_cycle_of_union_covered_tests_removes_at_most_one(tmp_path):
    p = cov_file(tmp_path, {"t::a|run": [1, 2], "t::b|run": [2, 3], "t::c|run": [1, 3]})
    out = run_json("redundancy", p)
    assert len(out["jointly_removable"]) <= 1
    for t in out["reasons"]:
        assert "union-covered" in out["reasons"][t]


def test_removal_keeps_every_line_covered(tmp_path):
    mapping = {
        "t::a|run": [1, 2, 3], "t::b|run": [1, 2], "t::c|run": [3, 4], "t::d|run": [4, 5],
        "t::e|run": [5, 6, 1], "t::f|run": [1, 2, 3, 4, 5, 6],
    }
    p = cov_file(tmp_path, mapping)
    out = run_json("redundancy", p)
    kept = [t for t in mapping if f"{t.split('|')[0]}" not in out["jointly_removable"]]
    covered = {ln for t in kept for ln in mapping[t]}
    assert covered == {ln for lines in mapping.values() for ln in lines}


def test_setup_only_unique_line_not_removable():
    out = run_json("redundancy", FIXTURES / "coverage-setup.json")
    assert "tests/test_a.py::test_b" not in out["jointly_removable"]
    assert out["jointly_removable"] == ["tests/test_a.py::test_a"]


def test_phases_merge_per_node(tmp_path):
    p = cov_file(tmp_path, {"t::a|setup": [1], "t::a|run": [2], "t::a|teardown": [3],
                            "t::b|run": [1, 2, 3]})
    out = run_json("redundancy", p)
    assert out["jointly_removable"] == ["t::b"]
    assert "duplicate-of:t::a" in out["reasons"]["t::b"]


def test_zero_coverage_never_removable(tmp_path):
    p = cov_file(tmp_path, {"t::a|run": [1]})
    ids = tmp_path / "all.txt"
    ids.write_text("t::a\nt::zero\n")
    out = run_json("redundancy", p, "--all-tests", ids)
    assert "covers-nothing" in out["reasons"]["t::zero"]
    assert "t::zero" not in out["jointly_removable"]


def test_exclude_ids_never_removed(tmp_path):
    p = cov_file(tmp_path, {"t::a|run": [1], "t::bb|run": [1]})
    ex = tmp_path / "ex.txt"
    ex.write_text("t::bb\n")
    out = run_json("redundancy", p, "--exclude-ids", ex)
    assert "t::bb" not in out["jointly_removable"]


def test_runtime_orders_greedy_slow_first(tmp_path):
    p = cov_file(tmp_path, {"tests/test_a.py::x|run": [1, 2], "tests/test_a.py::y|run": [1, 2, 3],
                            "tests/test_a.py::z|run": [3]})
    junit = tmp_path / "j.xml"
    junit.write_text(
        '<testsuite time="9"><testcase classname="tests.test_a" name="x" time="1"/>'
        '<testcase classname="tests.test_a" name="y" time="5"/>'
        '<testcase classname="tests.test_a" name="z" time="1"/></testsuite>')
    out = run_json("redundancy", p, "--durations", junit)
    assert out["jointly_removable"] == ["tests/test_a.py::y"]
    lexical = run_json("redundancy", p)
    assert lexical["jointly_removable"] == ["tests/test_a.py::x", "tests/test_a.py::z"]


def test_source_root_functions(tmp_path):
    src = tmp_path / "srcroot" / "src"
    src.mkdir(parents=True)
    (src / "mod.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    p = cov_file(tmp_path, {"t::a|run": [1, 2], "t::bb|run": [1, 2]})
    out = run_json("redundancy", p, "--source-root", tmp_path / "srcroot")
    assert out["candidates"]["t::bb"]["functions"] == ["src/mod.py::f"]


# ---------------------------------------------------------------- compare

def snap(tmp_path, name, **kw):
    base = {"count": 10, "runtime": 5.0, "line_coverage": 90.0, "survivors": ["m1"], "unstable": []}
    base.update(kw)
    p = tmp_path / name
    p.write_text(json.dumps(base))
    return p


def test_compare_ok(tmp_path):
    b, a = snap(tmp_path, "b.json"), snap(tmp_path, "a.json", count=8, line_coverage=91.0)
    res = run("compare", b, a, "--expected-removed", "2")
    assert res.returncode == 0, res.stdout


@pytest.mark.parametrize("after,flag,msg", [
    ({"line_coverage": 89.0}, "0", "line coverage fell"),
    ({"count": 9}, "0", "test count"),
    ({"survivors": ["m1", "m2"]}, "0", "new surviving mutants"),
])
def test_compare_fails(tmp_path, after, flag, msg):
    b, a = snap(tmp_path, "b.json"), snap(tmp_path, "a.json", **after)
    res = run("compare", b, a, "--expected-removed", flag)
    assert res.returncode == 1
    assert msg in res.stdout


def test_compare_unstable_survivor_allowed(tmp_path):
    b = snap(tmp_path, "b.json")
    a = snap(tmp_path, "a.json", survivors=["m1", "m2"], unstable=["m2"])
    assert run("compare", b, a).returncode == 0


def test_compare_missing_field_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert run("compare", bad, bad).returncode == 2


# ---------------------------------------------------------------- deselect plugin

def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_p.py").write_text(
        "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n\n\ndef test_three():\n    pass\n")
    return proj


def _pytest(proj: Path, plug_dir: Path, *extra):
    env = dict(os.environ, PYTEST_ADDOPTS="-p quoin_deselect",
               PYTHONPATH=os.pathsep.join([str(plug_dir), os.environ.get("PYTHONPATH", "")]))
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", *extra],
                          cwd=proj, env=env, capture_output=True, text=True)


def test_deselect_plugin_and_reverse(tmp_path):
    proj = _project(tmp_path)
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps({"deselect": ["test_p.py::test_two"], "reverse": False}))
    out_dir = tmp_path / "plug"
    res = run("deselect-plugin", ids, out_dir)
    assert res.returncode == 0
    assert "PYTEST_ADDOPTS=-p quoin_deselect" in res.stdout
    assert (out_dir / "quoin_deselect.py").is_file()
    r1 = _pytest(proj, out_dir)
    assert "1 deselected" in r1.stdout and "2 passed" in r1.stdout, r1.stdout
    assert "test_two PASSED" not in r1.stdout
    ids.write_text(json.dumps({"deselect": [], "reverse": True}))
    r2 = _pytest(proj, out_dir)
    order = [ln.split("::")[1].split()[0] for ln in r2.stdout.splitlines() if "PASSED" in ln]
    assert order == ["test_three", "test_two", "test_one"]


# ---------------------------------------------------------------- CLI and mapping

def test_help_lists_subcommands():
    res = run("--help")
    for name in ("durations", "static", "redundancy", "compare", "deselect-plugin"):
        assert name in res.stdout


def test_affected_tests_maps_helper_to_this_file():
    path = REPO_ROOT / "quoin" / "core" / "scripts" / "affected_tests.py"
    spec = importlib.util.spec_from_file_location("_t_affected", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    selectors, unmatched, _ignored = mod.map_changed_to_tests(
        ["quoin/core/scripts/suite_audit.py"], REPO_ROOT)
    assert any(s.endswith("quoin/dev/tests/test_suite_audit.py") for s in selectors)
    assert not any("suite_audit.py" in u for u in unmatched)


def test_redundancy_strips_only_phase_suffixes(tmp_path):
    p = cov_file(tmp_path, {
        "t.py::test_a[x|y]|run": [1, 2],
        "t.py::test_a[x|z]|run": [1],
        "t.py::test_b|setup": [1],
    })
    out = run_json("redundancy", p)
    assert "t.py::test_a[x|y]" in out["reasons"] or "t.py::test_a[x|z]" in out["reasons"]
    assert "t.py::test_a[x|z]" in out["reasons"]
    assert any(r.startswith("dominated-by:t.py::test_a[x|y]") for r in out["reasons"]["t.py::test_a[x|z]"])
    assert "t.py::test_a[x" not in out["reasons"]


def test_redundancy_dominated_by_with_many_tests(tmp_path):
    mapping = {f"t.py::test_{i}": [i] for i in range(1, 200)}
    mapping["t.py::test_big"] = list(range(1, 200))
    out = run_json("redundancy", cov_file(tmp_path, mapping))
    assert all(
        any(r.startswith("dominated-by:t.py::test_big") for r in out["reasons"][f"t.py::test_{i}"])
        for i in range(1, 200))
