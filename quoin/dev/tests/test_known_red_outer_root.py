"""known_red.py when pytest runs from the outer project root.

The gate's full-suite recipe runs `pytest quoin/` from the directory above the
repo. pytest still uses the repo as rootdir, but its `-rA` lines carry the
child folder as a leading segment (`quoin/quoin/dev/tests/...`), while the
manifest is written repo-relative (`quoin/dev/tests/...`). These tests pin the
normalization that makes the two meet, and that the affected-area run (pytest
started inside the repo, same outer `--project-root`) is left untouched.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_CORE_PATH = Path(__file__).resolve().parents[2] / "core" / "scripts" / "known_red.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("_quoin_core_known_red_outer", _CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kr = _load_core()

_TEST_SRC = '''
def test_known():
    assert False

def test_new():
    assert False

def test_ok():
    assert True
'''


def _make_layout(tmp_path, manifest_ids, test_src=_TEST_SRC):
    """outer/ (project root) containing quoin/ (git repo) with a quoin/ package."""
    outer = tmp_path / "outer"
    repo = outer / "quoin"
    tests = repo / "quoin" / "dev" / "tests"
    tests.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"-p no:cacheprovider\"\n", encoding="utf-8"
    )
    (tests / "test_a.py").write_text(test_src, encoding="utf-8")
    body = "".join(
        f'[[entry]]\nid = "{i}"\nreason = "baseline"\nadded = "2026-11-05"\n\n'
        for i in manifest_ids
    )
    (tests / "known-red.toml").write_text(body, encoding="utf-8")
    return outer, repo


def _junit(failures):
    return (
        '<?xml version="1.0"?><testsuites><testsuite name="pytest" '
        f'errors="0" failures="{failures}" tests="3"></testsuite></testsuites>'
    )


def _report(lines):
    return "=== short test summary info ===\n" + "\n".join(lines) + "\n"


def _run(capsys, outer, report_lines, failures, extra=()):
    rep = outer / "ra.txt"
    rep.write_text(_report(report_lines), encoding="utf-8")
    junit = outer / "j.xml"
    junit.write_text(_junit(failures), encoding="utf-8")
    rc = kr.main([
        "--pytest-output", str(rep), "--junit", str(junit),
        "--observed-rc", "1" if failures else "0",
        "--project-root", str(outer),
        "--counter-file", str(outer / "counter.json"),
        *extra,
    ])
    return rc, json.loads(capsys.readouterr().out)


KNOWN = "quoin/dev/tests/test_a.py::test_known"
NEW = "quoin/dev/tests/test_a.py::test_new"
OK = "quoin/dev/tests/test_a.py::test_ok"


class TestOuterRootCapturedOutput:
    def test_listed_failure_downgraded_unlisted_stays_net_new(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, [KNOWN])
        rc, payload = _run(capsys, outer, [
            "PASSED quoin/" + OK,
            "FAILED quoin/" + KNOWN + " - assert False",
            "FAILED quoin/" + NEW + " - assert False",
        ], failures=2)
        assert rc == kr.EXIT_NET_NEW
        assert [e["id"] for e in payload["known_red"]] == [KNOWN]
        assert payload["net_new"] == [NEW]
        assert payload["downgrade"] is False

    def test_only_listed_failure_downgrades(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, [KNOWN])
        rc, payload = _run(capsys, outer, [
            "PASSED quoin/" + OK,
            "PASSED quoin/" + NEW,
            "FAILED quoin/" + KNOWN + " - assert False",
        ], failures=1, extra=["--full-suite"])
        assert rc == kr.EXIT_OK
        assert payload["downgrade"] is True
        assert payload["net_new"] == []

    def test_whole_file_entry_matches_prefixed_ids(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, ["quoin/dev/tests/test_a.py"])
        rc, payload = _run(capsys, outer, [
            "FAILED quoin/" + KNOWN, "FAILED quoin/" + NEW,
        ], failures=2)
        assert rc == kr.EXIT_OK and payload["downgrade"] is True

    def test_repo_relative_report_with_outer_project_root_untouched(self, tmp_path, capsys):
        # Affected-area run: pytest started inside the repo, ids already
        # repo-relative, and they also begin with `quoin/`. Must not be stripped.
        outer, _ = _make_layout(tmp_path, [KNOWN])
        rc, payload = _run(capsys, outer, ["FAILED " + KNOWN, "FAILED " + NEW], failures=2)
        assert rc == kr.EXIT_NET_NEW
        assert [e["id"] for e in payload["known_red"]] == [KNOWN]
        assert payload["net_new"] == [NEW]

    def test_manifest_written_in_prefixed_form_still_matches(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, ["quoin/" + KNOWN])
        rc, payload = _run(capsys, outer, ["FAILED quoin/" + KNOWN], failures=1)
        assert rc == kr.EXIT_OK
        assert [e["id"] for e in payload["known_red"]] == ["quoin/" + KNOWN]

    def test_staleness_counts_prefixed_pass(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, [KNOWN])
        for token in ("r1", "r2", "r3"):
            _run(capsys, outer, ["PASSED quoin/" + KNOWN], failures=0,
                 extra=["--full-suite", "--run-token", token])
        counter = json.loads((outer / "counter.json").read_text(encoding="utf-8"))
        assert counter[KNOWN]["consecutive_pass"] == 3

    @pytest.mark.parametrize("sel", [
        "quoin/quoin/dev/tests/test_a.py",
        "quoin/dev/tests/test_a.py",
        "quoin/quoin/dev/tests/",
    ])
    def test_selector_filter_accepts_either_form(self, tmp_path, capsys, sel):
        outer, _ = _make_layout(tmp_path, [KNOWN])
        rc, payload = _run(capsys, outer, ["FAILED quoin/" + KNOWN], failures=1,
                           extra=["--selectors", sel])
        assert rc == kr.EXIT_OK and payload["downgrade"] is True

    def test_selector_for_other_file_excludes(self, tmp_path, capsys):
        outer, repo = _make_layout(tmp_path, [KNOWN])
        (repo / "quoin" / "dev" / "tests" / "test_b.py").write_text("", encoding="utf-8")
        rep = outer / "ra.txt"
        rep.write_text(_report(["FAILED quoin/" + KNOWN]), encoding="utf-8")
        rc = kr.main([
            "--pytest-output", str(rep), "--observed-rc", "0",
            "--project-root", str(outer),
            "--selectors", "quoin/quoin/dev/tests/test_b.py",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kr.EXIT_OK and payload["downgrade"] is False


class TestNormalizerUnit:
    def test_no_repo_root_for_project_manifest(self, tmp_path):
        assert kr._manifest_repo_root(tmp_path / ".workflow_artifacts" / "known-red.toml") is None

    def test_repo_outside_invocation_dir_is_noop(self, tmp_path):
        outer, repo = _make_layout(tmp_path, [])
        norm = kr.NodeIdNormalizer(repo, tmp_path / "elsewhere")
        assert norm.nodeid("quoin/" + KNOWN) == "quoin/" + KNOWN

    def test_invocation_dir_is_repo_is_noop(self, tmp_path):
        _, repo = _make_layout(tmp_path, [])
        norm = kr.NodeIdNormalizer(repo, repo)
        assert norm.nodeid("quoin/" + KNOWN) == "quoin/" + KNOWN

    def test_strips_prefix_with_param_brackets(self, tmp_path):
        outer, repo = _make_layout(tmp_path, [])
        norm = kr.NodeIdNormalizer(repo, outer)
        assert norm.nodeid("quoin/" + KNOWN + "[a/b - c]") == KNOWN + "[a/b - c]"

    def test_absolute_selector_untouched(self, tmp_path):
        outer, repo = _make_layout(tmp_path, [])
        norm = kr.NodeIdNormalizer(repo, outer)
        sel = str(repo / "quoin" / "dev" / "tests" / "test_a.py")
        assert norm.selector(sel) == sel


class TestOuterRootEndToEnd:
    def test_real_pytest_from_outer_root(self, tmp_path, capsys):
        outer, _ = _make_layout(tmp_path, [KNOWN])
        junit = outer / "j.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-rA", f"--junitxml={junit}", "quoin/"],
            cwd=outer, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        # The bug's precondition: ids come out with the child folder prefixed.
        assert "FAILED quoin/" + KNOWN in proc.stdout
        rep = outer / "ra.txt"
        rep.write_text(proc.stdout, encoding="utf-8")
        rc = kr.main([
            "--pytest-output", str(rep), "--junit", str(junit),
            "--observed-rc", str(proc.returncode), "--full-suite", "--run-token", "t",
            "--project-root", str(outer), "--counter-file", str(outer / "c.json"),
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == kr.EXIT_NET_NEW
        assert payload["reconciled"] is True
        assert [e["id"] for e in payload["known_red"]] == [KNOWN]
        assert payload["net_new"] == [NEW]
