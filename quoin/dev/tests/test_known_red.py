"""Unit tests for quoin.core.scripts.known_red (IVG-144 S-02).

Covers T-03..T-07 plus the round-1/2/3 regression cases:
  - TestLoad: tomllib path + FORCED internal-parser (3.10) path; absent → [];
    malformed grammar / missing key / non-str value → MalformedManifest.
  - TestMatch: node-id exact; whole-file matches all file nodeids; anchor-guard
    (no substring); net-new-blocks-while-known-downgrades same set; empty → no downgrade.
  - TestReportParse: space-bearing parametrized FAILED + ERROR line; the round-3
    MIN-1 internal-hyphen bracket case (test[a - b]); class-based FAILED line;
    FAILED-only → empty passed; garbage-tolerant.
  - TestJunitCount: real <testsuites><testsuite> wrapper; multiple children;
    tampered attrs reflect declared count; collection-error; malformed → ParseError;
    never returns a node-id.
  - TestSelectorFilter: absolute selectors match repo-relative node-ids and vice
    versa; bracket-aware file-prefix split; directory-shaped selectors; component-
    boundary guard against sibling-stem matches; empty-selector fail-closed pin.
  - TestStructuralGuard: a REAL pytest run over throwaway fixtures asserts the
    -rA-parsed node-ids round-trip to real pytest node-ids and the junit count
    equals the -rA-parsed failure count.
  - TestStaleness: pass increments; fail resets; skipped/out-of-scope untouched;
    run-token dedup; threshold N default 3 + env override; no --full-suite → no change.
  - TestReconcile: rc!=0 + empty parse → NOT reconciled; rc==0 + empty → reconciled;
    junit-count disagreement → NOT reconciled; rc!=0 + parsed failures, no junit → reconciled.
  - TestCliExit: full exit-code matrix incl. exit 3 (CRIT-1) and exit 64 usage cases;
    plus absolute-selector downgrade/net-new coverage (IVG-254).
  - TestManifestResolution: manifest resolves against the nested git root, not the
    outer project root (IVG-254) — direct hit, nested layout, no-.git guard,
    absent-everywhere, deterministic ordering, worktree `.git` file, iterdir OSError.
  - TestHumanBlock: text output lists downgrade + stale + reconciliation line.
  - TestNodeIdEndToEnd: real emitted node-id for test_sleep_scoring.py == manifest id.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the core module from its canonical source path (importlib loader idiom).
# ---------------------------------------------------------------------------

_CORE_PATH = Path(__file__).resolve().parents[2] / "core" / "scripts" / "known_red.py"
_MANIFEST = Path(__file__).resolve().parents[0] / "known-red.toml"


def _load_core():
    spec = importlib.util.spec_from_file_location("_quoin_core_known_red_test", _CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


kr = _load_core()


VALID_TOML = """\
[[entry]]
id = "quoin/dev/tests/test_sleep_scoring.py"
reason = "flaky clock"
added = "2026-07-29"
issue = "IVG-116"

[[entry]]
id = "quoin/dev/tests/test_foo.py::test_bar"
reason = "known"
added = "2026-07-01"
"""


def _write(tmp_path, text) -> Path:
    p = tmp_path / "known-red.toml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestLoad
# ---------------------------------------------------------------------------


class TestLoad:
    def test_tomllib_path(self, tmp_path):
        entries = kr.load_manifest(_write(tmp_path, VALID_TOML))
        assert len(entries) == 2
        assert entries[0]["id"] == "quoin/dev/tests/test_sleep_scoring.py"

    def test_internal_parser_path_equals_tomllib(self, tmp_path, monkeypatch):
        via_tomllib = kr.load_manifest(_write(tmp_path, VALID_TOML))
        # Force the 3.10 (tomllib-absent) branch.
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "tomllib":
                raise ModuleNotFoundError("No module named 'tomllib'")
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", fake_import)
        via_internal = kr.load_manifest(_write(tmp_path, VALID_TOML))
        assert via_internal == via_tomllib

    def test_committed_manifest_seed(self):
        entries = kr.load_manifest(_MANIFEST)
        ids = {e["id"] for e in entries}
        assert "quoin/dev/tests/test_sleep_scoring.py" in ids
        seed = next(e for e in entries if e["id"] == "quoin/dev/tests/test_sleep_scoring.py")
        assert "::" not in seed["id"]  # whole-file grain
        assert seed["reason"] and seed["added"]

    def test_absent_path_returns_empty(self, tmp_path):
        assert kr.load_manifest(tmp_path / "nope.toml") == []

    @pytest.mark.parametrize(
        "bad",
        [
            '[server]\nid="x"\n',  # nested / non-entry table
            '[[entry]]\nid=["a"]\nreason="r"\nadded="d"\n',  # array value
            '[[entry]]\nid=3\nreason="r"\nadded="d"\n',  # numeric value
            '[[entry]]\nid="oops\nreason="r"\nadded="d"\n',  # unterminated quote
            '[[entry]]\nid=bare\nreason="r"\nadded="d"\n',  # bare value
            '[[entry]]\nreason="r"\nadded="d"\n',  # missing required key id
        ],
    )
    def test_malformed_raises_internal(self, tmp_path, monkeypatch, bad):
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "tomllib":
                raise ModuleNotFoundError
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", fake_import)
        with pytest.raises(kr.MalformedManifest):
            kr.load_manifest(_write(tmp_path, bad))

    def test_missing_required_key_raises_tomllib(self, tmp_path):
        with pytest.raises(kr.MalformedManifest):
            kr.load_manifest(_write(tmp_path, '[[entry]]\nid="x"\nreason="r"\n'))  # no added

    def test_non_str_value_raises_tomllib(self, tmp_path):
        # valid TOML but wrong type → schema validation must reject
        with pytest.raises(kr.MalformedManifest):
            kr.load_manifest(_write(tmp_path, '[[entry]]\nid="x"\nreason="r"\nadded=3\n'))


# ---------------------------------------------------------------------------
# TestMatch
# ---------------------------------------------------------------------------


class TestMatch:
    ENTRIES = [
        {"id": "quoin/dev/tests/test_foo.py::test_bar", "reason": "r1", "added": "d1"},
        {"id": "quoin/dev/tests/test_sleep_scoring.py", "reason": "r2", "added": "d2"},
    ]

    def test_nodeid_exact(self):
        kr_, net = kr.match_failures({"quoin/dev/tests/test_foo.py::test_bar"}, self.ENTRIES)
        assert net == []
        assert kr_[0]["id"] == "quoin/dev/tests/test_foo.py::test_bar"

    def test_wholefile_matches_all_file_nodeids(self):
        failed = {
            "quoin/dev/tests/test_sleep_scoring.py::test_a",
            "quoin/dev/tests/test_sleep_scoring.py::test_b",
        }
        kr_, net = kr.match_failures(failed, self.ENTRIES)
        assert net == []
        assert len(kr_) == 1  # deduped to the one file entry
        assert kr_[0]["id"] == "quoin/dev/tests/test_sleep_scoring.py"

    def test_anchor_guard_no_substring(self):
        # a foo.py entry must NOT match foobar.py::t
        entries = [{"id": "quoin/dev/tests/foo.py", "reason": "r", "added": "d"}]
        kr_, net = kr.match_failures({"quoin/dev/tests/foobar.py::t"}, entries)
        assert kr_ == []
        assert net == ["quoin/dev/tests/foobar.py::t"]

    def test_net_new_blocks_while_known_downgrades_same_set(self):
        failed = {
            "quoin/dev/tests/test_foo.py::test_bar",  # known
            "quoin/dev/tests/test_new.py::test_x",  # net-new
        }
        kr_, net = kr.match_failures(failed, self.ENTRIES)
        assert net == ["quoin/dev/tests/test_new.py::test_x"]
        assert len(kr_) == 1
        assert kr.downgrade_ok(failed, net) is False

    def test_empty_failed_no_downgrade(self):
        kr_, net = kr.match_failures(set(), self.ENTRIES)
        assert kr.downgrade_ok(set(), net) is False

    def test_all_known_downgrades(self):
        failed = {"quoin/dev/tests/test_foo.py::test_bar"}
        _, net = kr.match_failures(failed, self.ENTRIES)
        assert kr.downgrade_ok(failed, net) is True


# ---------------------------------------------------------------------------
# TestReportParse
# ---------------------------------------------------------------------------


class TestReportParse:
    def test_space_bearing_param_and_error_line(self):
        text = (
            "PASSED quoin/dev/tests/test_x.py::test_ok\n"
            "FAILED quoin/dev/tests/test_x.py::test_p[a b] - assert 0\n"
            "ERROR quoin/dev/tests/test_y.py::test_collect\n"
        )
        passed, failed = kr.parse_pytest_report(text)
        assert passed == {"quoin/dev/tests/test_x.py::test_ok"}
        assert failed == {
            "quoin/dev/tests/test_x.py::test_p[a b]",
            "quoin/dev/tests/test_y.py::test_collect",
        }

    def test_internal_hyphen_in_param_not_truncated(self):
        # round-3 MIN-1: an internal ' - ' inside the bracket must NOT truncate.
        text = "FAILED quoin/dev/tests/test_x.py::test_p[a - b] - AssertionError\n"
        _, failed = kr.parse_pytest_report(text)
        assert failed == {"quoin/dev/tests/test_x.py::test_p[a - b]"}

    def test_class_based_failed_line_exact(self):
        text = "FAILED quoin/dev/tests/test_x.py::TestClass::test_method - boom\n"
        _, failed = kr.parse_pytest_report(text)
        assert failed == {"quoin/dev/tests/test_x.py::TestClass::test_method"}

    def test_failed_only_empty_passed(self):
        passed, failed = kr.parse_pytest_report("FAILED a::b\n")
        assert passed == set()
        assert failed == {"a::b"}

    def test_garbage_tolerant(self):
        text = "random log line\n= 3 failed in 0.1s =\nPASSED a::b\n"
        passed, failed = kr.parse_pytest_report(text)
        assert passed == {"a::b"}
        assert failed == set()


# ---------------------------------------------------------------------------
# TestJunitCount
# ---------------------------------------------------------------------------


class TestJunitCount:
    def test_real_pytest_wrapper_shape(self):
        xml = (
            '<testsuites name="pytest tests">'
            '<testsuite name="pytest" tests="5" failures="2" errors="1" skipped="0">'
            "</testsuite></testsuites>"
        )
        assert kr.parse_junit_count(xml) == 3

    def test_multiple_testsuite_children_sum(self):
        xml = (
            "<testsuites>"
            '<testsuite failures="1" errors="0"></testsuite>'
            '<testsuite failures="2" errors="1"></testsuite>'
            "</testsuites>"
        )
        assert kr.parse_junit_count(xml) == 4

    def test_bare_testsuite_root(self):
        xml = '<testsuite failures="1" errors="1"></testsuite>'
        assert kr.parse_junit_count(xml) == 2

    def test_tampered_attrs_reflect_declared_not_recount(self):
        # declared failures=5 despite one <failure> child → count reflects the attr
        xml = (
            '<testsuite failures="5" errors="0">'
            '<testcase name="t"><failure/></testcase>'
            "</testsuite>"
        )
        assert kr.parse_junit_count(xml) == 5

    def test_collection_error_already_in_errors_attr(self):
        xml = (
            '<testsuite failures="0" errors="1">'
            '<testcase classname="quoin.dev.tests.test_x" name="test_x">'
            "<error>collection failed</error></testcase>"
            "</testsuite>"
        )
        assert kr.parse_junit_count(xml) == 1

    def test_malformed_xml_raises_parseerror(self):
        with pytest.raises(ET.ParseError):
            kr.parse_junit_count("<not-closed")

    def test_returns_int_never_nodeid(self):
        xml = '<testsuite failures="1" errors="0"><testcase classname="a.b.c" name="t"/></testsuite>'
        result = kr.parse_junit_count(xml)
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# TestSelectorFilter — IVG-254 selector-shape mismatch regression
# ---------------------------------------------------------------------------


class TestSelectorFilter:
    def test_absolute_selectors_match_relative_nodeids(self):
        # the IVG-254 repro shape: affected_tests.py emits absolute selectors,
        # -rA parsing yields repo-relative node-ids
        passed, failed = kr.apply_selector_filter(
            {"quoin/dev/tests/test_a.py::test_x"},
            set(),
            ["/repo-root/quoin/dev/tests/test_a.py"],
        )
        assert passed == {"quoin/dev/tests/test_a.py::test_x"}
        assert failed == set()

    def test_relative_selectors_match_absolute_nodeids(self):
        # the inverse shape
        passed, failed = kr.apply_selector_filter(
            {"/repo-root/quoin/dev/tests/test_a.py::test_x"},
            set(),
            ["quoin/dev/tests/test_a.py"],
        )
        assert passed == {"/repo-root/quoin/dev/tests/test_a.py::test_x"}
        assert failed == set()

    def test_repo_relative_selectors_still_match(self):
        # pre-fix behavior preserved: both sides repo-relative
        passed, failed = kr.apply_selector_filter(
            {"quoin/dev/tests/test_a.py::test_x"},
            set(),
            ["quoin/dev/tests/test_a.py"],
        )
        assert passed == {"quoin/dev/tests/test_a.py::test_x"}
        assert failed == set()

    def test_out_of_scope_nodeids_are_dropped(self):
        # DISCRIMINATOR: a selector list that omits one file must drop that
        # file's node-ids. A no-op `return passed, failed_or_error` passes
        # tests 1-3 above but fails this one.
        _, failed = kr.apply_selector_filter(
            set(),
            {
                "quoin/dev/tests/test_a.py::test_x",
                "quoin/dev/tests/test_b.py::test_y",
            },
            ["quoin/dev/tests/test_a.py"],
        )
        assert failed == {"quoin/dev/tests/test_a.py::test_x"}

    def test_parametrized_bracket_nodeid_matches(self):
        # guards the bracket-aware `_file_prefix` split (lesson 2026-07-30):
        # an internal " - " inside the param must not truncate the node-id
        passed, _ = kr.apply_selector_filter(
            {"quoin/dev/tests/test_p.py::test_v[a - b]"},
            set(),
            ["/repo-root/quoin/dev/tests/test_p.py"],
        )
        assert passed == {"quoin/dev/tests/test_p.py::test_v[a - b]"}

    def test_nodeid_shaped_selector_selects_whole_file(self):
        # a selector carrying `::` is reduced by `_file_prefix` before
        # matching, so it selects every node-id in that file
        passed, _ = kr.apply_selector_filter(
            {
                "quoin/dev/tests/test_a.py::test_x",
                "quoin/dev/tests/test_a.py::test_y",
            },
            set(),
            ["/repo-root/quoin/dev/tests/test_a.py::test_x"],
        )
        assert passed == {
            "quoin/dev/tests/test_a.py::test_x",
            "quoin/dev/tests/test_a.py::test_y",
        }

    def test_directory_selector_matches_contained_nodeids(self):
        # a directory-shaped selector (last component has no `.py` suffix)
        # matches node-ids beneath it
        passed, _ = kr.apply_selector_filter(
            {"quoin/dev/tests/test_a.py::test_x"},
            set(),
            ["quoin/dev/tests"],
        )
        assert passed == {"quoin/dev/tests/test_a.py::test_x"}

    def test_sibling_stem_does_not_match(self):
        # component boundary, not string prefix: test_a.py must not match
        # test_ab.py
        passed, _ = kr.apply_selector_filter(
            {"quoin/dev/tests/test_ab.py::test_x"},
            set(),
            ["quoin/dev/tests/test_a.py"],
        )
        assert passed == set()

    def test_empty_selector_list_filters_everything(self):
        # pins the existing fail-closed semantics: no selectors → nothing kept
        passed, failed = kr.apply_selector_filter(
            {"quoin/dev/tests/test_a.py::test_x"},
            {"quoin/dev/tests/test_b.py::test_y"},
            [],
        )
        assert passed == set()
        assert failed == set()


# ---------------------------------------------------------------------------
# TestStructuralGuard — live pytest invariant (round 3)
# ---------------------------------------------------------------------------

_FIXTURE_PARAM = '''\
import pytest
@pytest.mark.parametrize("v", ["a - b"])
def test_p(v):
    assert False
'''

_FIXTURE_CLASS = '''\
class TestThing:
    def test_method(self):
        assert False
'''


class TestStructuralGuard:
    def test_node_id_roundtrip_and_count(self, tmp_path):
        (tmp_path / "test_param_fixture.py").write_text(_FIXTURE_PARAM, encoding="utf-8")
        (tmp_path / "test_class_fixture.py").write_text(_FIXTURE_CLASS, encoding="utf-8")
        junit = tmp_path / "out.junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-rA", f"--junitxml={junit}",
             "-p", "no:cacheprovider", str(tmp_path)],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        )
        passed, failed = kr.parse_pytest_report(proc.stdout)
        # (a) real pytest node-ids (collected independently) must all be parseable.
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", str(tmp_path)],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        )
        real_ids = {
            ln.strip() for ln in collect.stdout.splitlines()
            if "::" in ln and not ln.startswith(" ")
        }
        assert real_ids, f"no collected ids parsed from: {collect.stdout!r}"
        # every real failing id was captured by parse_pytest_report from -rA
        assert real_ids.issubset(failed), (
            f"parse_pytest_report missed real node-ids.\nreal={real_ids}\nparsed_failed={failed}"
        )
        # the internal-hyphen param id survived untruncated
        assert any("[a - b]" in nid for nid in failed), failed
        # (b) junit count oracle equals the -rA-parsed failure count.
        assert kr.parse_junit_count(junit.read_text(encoding="utf-8")) == len(failed)


# ---------------------------------------------------------------------------
# TestStaleness
# ---------------------------------------------------------------------------


class TestStaleness:
    ENTRIES = [{"id": "quoin/dev/tests/test_sleep_scoring.py", "reason": "r", "added": "d"}]

    def _counter(self, tmp_path):
        return tmp_path / "known-red-runs.json"

    def test_pass_increments(self, tmp_path):
        cf = self._counter(tmp_path)
        passed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        kr.update_staleness(self.ENTRIES, passed, set(), "tok1", cf)
        kr.update_staleness(self.ENTRIES, passed, set(), "tok2", cf)
        data = json.loads(cf.read_text())
        assert data["quoin/dev/tests/test_sleep_scoring.py"]["consecutive_pass"] == 2

    def test_fail_resets(self, tmp_path):
        cf = self._counter(tmp_path)
        passed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        kr.update_staleness(self.ENTRIES, passed, set(), "tok1", cf)
        failed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        kr.update_staleness(self.ENTRIES, set(), failed, "tok2", cf)
        data = json.loads(cf.read_text())
        assert data["quoin/dev/tests/test_sleep_scoring.py"]["consecutive_pass"] == 0

    def test_skipped_out_of_scope_untouched(self, tmp_path):
        cf = self._counter(tmp_path)
        # entry file does not appear in either set (skipped / out of scope)
        kr.update_staleness(self.ENTRIES, {"other.py::t"}, set(), "tok1", cf)
        data = json.loads(cf.read_text())
        assert data["quoin/dev/tests/test_sleep_scoring.py"]["consecutive_pass"] == 0
        assert data["quoin/dev/tests/test_sleep_scoring.py"]["last_run_token"] is None

    def test_run_token_dedup(self, tmp_path):
        cf = self._counter(tmp_path)
        passed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        kr.update_staleness(self.ENTRIES, passed, set(), "same", cf)
        kr.update_staleness(self.ENTRIES, passed, set(), "same", cf)  # dedup
        data = json.loads(cf.read_text())
        assert data["quoin/dev/tests/test_sleep_scoring.py"]["consecutive_pass"] == 1

    def test_threshold_default_3(self, tmp_path):
        cf = self._counter(tmp_path)
        passed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        stale = []
        for i in range(3):
            stale = kr.update_staleness(self.ENTRIES, passed, set(), f"t{i}", cf)
        assert any(s["id"] == "quoin/dev/tests/test_sleep_scoring.py" for s in stale)

    def test_threshold_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_KNOWN_RED_STALE_RUNS", "2")
        cf = self._counter(tmp_path)
        passed = {"quoin/dev/tests/test_sleep_scoring.py::test_a"}
        kr.update_staleness(self.ENTRIES, passed, set(), "t0", cf)
        stale = kr.update_staleness(self.ENTRIES, passed, set(), "t1", cf)
        assert any(s["id"] == "quoin/dev/tests/test_sleep_scoring.py" for s in stale)

    def test_absent_counter_no_crash(self, tmp_path):
        cf = tmp_path / "nested" / "known-red-runs.json"
        stale = kr.update_staleness(self.ENTRIES, set(), set(), "t0", cf)
        assert stale == []


# ---------------------------------------------------------------------------
# TestReconcile
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_rc_nonzero_empty_parse_not_reconciled(self):
        assert kr._reconcile(1, set(), True) is False

    def test_rc_zero_empty_parse_reconciled(self):
        assert kr._reconcile(0, set(), True) is True

    def test_junit_count_disagreement_not_reconciled(self):
        # failed set nonempty but reconcile_ok False (count mismatch) → not reconciled
        assert kr._reconcile(1, {"a::b"}, False) is False

    def test_rc_nonzero_parsed_failures_no_junit_reconciled(self):
        # primary path default: reconcile_ok True absent a count oracle
        assert kr._reconcile(1, {"a::b"}, True) is True


class TestUnreconciledReason:
    def test_rc_zero_count_disagreement_never_mentions_red_run(self):
        reason = kr._unreconciled_reason(0, {"a::b"}, False)
        assert "indicates a red run" not in reason
        assert "disagrees" in reason

    def test_rc_zero_generic_fallback_never_mentions_red_run(self):
        reason = kr._unreconciled_reason(0, set(), True)
        assert "indicates a red run" not in reason
        assert "could not be reconciled" in reason

    def test_rc_nonzero_no_parsed_failures_keeps_existing_wording(self):
        reason = kr._unreconciled_reason(1, set(), True)
        assert "rc=1 indicates a red run" in reason


# ---------------------------------------------------------------------------
# TestPhantomFailureLines
# ---------------------------------------------------------------------------

# Reproduces a real `pytest -rA` capture where a passing test's own captured
# stdout happens to contain literal short-summary-shaped lines (e.g. a test
# that exercises this module's own fixture strings). The nested header and
# FAILED line appear BEFORE the real top-level short-summary section, which
# pytest always emits last.
_PHANTOM_CAPTURE = (
    "==================================== PASSES ====================================\n"
    "__________________________ test_prints_fixture_lines ___________________________\n"
    "----------------------------- Captured stdout call -----------------------------\n"
    "=========================== short test summary info ============================\n"
    "FAILED test_src.py::test_src_fails - AssertionError: deliberate failure\n"
    "=========================== 1 failed in 0.01s ============================\n"
    "- generated xml file: ... -\n"
    "=========================== short test summary info ============================\n"
    "PASSED test_outer.py::test_prints_fixture_lines\n"
    "PASSED test_outer.py::test_plain_pass\n"
    "============================== 2 passed in 0.01s ===============================\n"
)


class TestPhantomFailureLines:
    def _manifest(self, tmp_path):
        return tmp_path / "absent.toml"  # absent → [] entries; irrelevant to these cases

    def _write(self, tmp_path, name, text):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_anchored_parse_ignores_pre_summary_failed_lines(self):
        passed, failed = kr.parse_pytest_report(_PHANTOM_CAPTURE)
        assert failed == set()
        assert passed == {
            "test_outer.py::test_prints_fixture_lines",
            "test_outer.py::test_plain_pass",
        }

    def test_green_capture_with_fixture_lines_exit0_with_junit(self, tmp_path, capsys):
        ra = self._write(tmp_path, "ra.txt", _PHANTOM_CAPTURE)
        junit = self._write(
            tmp_path, "j.xml",
            '<testsuites><testsuite tests="2" failures="0" errors="0"/></testsuites>',
        )
        rc = kr.main([
            "--manifest", str(self._manifest(tmp_path)), "--pytest-output", str(ra),
            "--junit", str(junit), "--observed-rc", "0", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["reconciled"] is True
        assert out["downgrade"] is False
        assert out["net_new"] == []

    def test_green_capture_with_fixture_lines_exit0_without_junit(self, tmp_path, capsys):
        ra = self._write(tmp_path, "ra.txt", _PHANTOM_CAPTURE)
        rc = kr.main([
            "--manifest", str(self._manifest(tmp_path)), "--pytest-output", str(ra),
            "--observed-rc", "0", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["net_new"] == []

    def test_no_summary_header_falls_back_to_whole_text(self):
        passed, failed = kr.parse_pytest_report("FAILED a::b\n")
        assert passed == set()
        assert failed == {"a::b"}

    def test_junit_zero_backstop_without_anchor(self, tmp_path, capsys):
        # Phantom FAILED line with NO header at all — the header anchor can't help
        # here, so this proves the junit-zero backstop fires independently.
        ra = self._write(
            tmp_path, "ra.txt",
            "FAILED test_src.py::test_src_fails - AssertionError: deliberate failure\n",
        )
        junit = self._write(
            tmp_path, "j.xml",
            '<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>',
        )
        rc = kr.main([
            "--manifest", str(self._manifest(tmp_path)), "--pytest-output", str(ra),
            "--junit", str(junit), "--observed-rc", "0", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["reconciled"] is True
        assert out["downgrade"] is False

    def test_caller_green_but_junit_red_still_blocks(self, tmp_path, capsys):
        # Two REAL failures inside the (last) summary section, rc==0, junit==2 — the
        # junit-zero backstop must not fire, and the failures remain net-new.
        ra = self._write(
            tmp_path, "ra.txt",
            "=========================== short test summary info ============================\n"
            "FAILED test_a.py::test_1 - AssertionError\n"
            "FAILED test_a.py::test_2 - AssertionError\n"
            "============================== 2 failed in 0.01s ===============================\n",
        )
        junit = self._write(
            tmp_path, "j.xml",
            '<testsuites><testsuite tests="2" failures="2" errors="0"/></testsuites>',
        )
        rc = kr.main([
            "--manifest", str(self._manifest(tmp_path)), "--pytest-output", str(ra),
            "--junit", str(junit), "--observed-rc", "0", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["net_new"] == ["test_a.py::test_1", "test_a.py::test_2"]

    def test_live_green_run_with_fixture_output_reconciles(self, tmp_path, capsys):
        # Live guard mirroring TestStructuralGuard: a real pytest run whose PASSING
        # test prints short-summary-shaped fixture lines to stdout. Pins the
        # section-ordering assumption against real pytest behavior, not a frozen
        # string.
        (tmp_path / "test_fixture_printer.py").write_text(
            "def test_prints_fixture_lines():\n"
            "    print('=========================== short test summary info ===========================')\n"
            "    print('FAILED test_src.py::test_src_fails - AssertionError: deliberate failure')\n"
            "    print('=========================== 1 failed in 0.01s ===========================')\n",
            encoding="utf-8",
        )
        junit = tmp_path / "live.junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-rA", f"--junitxml={junit}",
             "-p", "no:cacheprovider", str(tmp_path)],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        # Pin the section-ordering property itself, independent of the junit-zero
        # backstop: the capture must carry more than one short-summary header (the
        # printed phantom plus the real trailing one) so the anchor is actually
        # exercised, the phantom node-id must not survive anchored parsing, and a
        # real passing node-id must.
        assert proc.stdout.count("short test summary info") > 1
        live_passed, live_failed = kr.parse_pytest_report(proc.stdout)
        assert "test_src.py::test_src_fails" not in live_failed
        assert live_passed
        assert any(n.endswith("::test_prints_fixture_lines") for n in live_passed)

        ra = self._write(tmp_path, "live.ra.txt", proc.stdout)
        rc = kr.main([
            "--manifest", str(self._manifest(tmp_path)), "--pytest-output", str(ra),
            "--junit", str(junit), "--observed-rc", str(proc.returncode), "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["reconciled"] is True
        assert out["downgrade"] is False


# ---------------------------------------------------------------------------
# TestCliExit
# ---------------------------------------------------------------------------


class TestCliExit:
    ENTRIES_TOML = """\
[[entry]]
id = "quoin/dev/tests/test_sleep_scoring.py"
reason = "flaky"
added = "2026-07-29"

[[entry]]
id = "quoin/dev/tests/test_cls.py::TestC::test_m"
reason = "class known"
added = "2026-07-10"
"""

    def _manifest(self, tmp_path, text=None):
        p = tmp_path / "known-red.toml"
        p.write_text(text if text is not None else self.ENTRIES_TOML, encoding="utf-8")
        return p

    def _ra(self, tmp_path, lines):
        p = tmp_path / "ra.txt"
        p.write_text("".join(l if l.endswith("\n") else l + "\n" for l in lines), encoding="utf-8")
        return p

    def _run(self, argv):
        return kr.main(argv)

    def test_exit0_all_known_red_downgrade_true(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - x"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["downgrade"] is True
        assert out["known_red"][0]["id"] == "quoin/dev/tests/test_sleep_scoring.py"

    def test_exit0_clean_downgrade_false(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["PASSED quoin/dev/tests/test_sleep_scoring.py::test_a"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "0", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["downgrade"] is False

    def test_class_based_known_red_downgrades(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_cls.py::TestC::test_m - boom"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["downgrade"] is True
        assert out["known_red"][0]["id"] == "quoin/dev/tests/test_cls.py::TestC::test_m"

    def test_exit1_net_new(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_new.py::test_x - x"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["net_new"] == ["quoin/dev/tests/test_new.py::test_x"]
        assert out["downgrade"] is False

    def test_exit2_malformed(self, tmp_path, capsys):
        man = self._manifest(tmp_path, text='[[entry]]\nid=3\nreason="r"\nadded="d"\n')
        ra = self._ra(tmp_path, ["FAILED a::b - x"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        cap = capsys.readouterr()
        assert rc == 2
        assert "malformed" in cap.err.lower()
        out = json.loads(cap.out)
        assert "downgrade" not in out  # downgrade absent on malformed

    def test_exit3_unreconciled_crit1(self, tmp_path, capsys):
        # observed rc=1 but the -rA report parsed an EMPTY failed set → CRIT-1 hole
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["some log with no FAILED lines"])
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 3
        assert out["reconciled"] is False
        assert out["downgrade"] is False

    def test_exit3_junit_count_disagreement(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - x"])
        junit = tmp_path / "j.xml"
        junit.write_text('<testsuites><testsuite failures="3" errors="0"/></testsuites>', encoding="utf-8")
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra), "--junit", str(junit),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 3
        assert out["reconciled"] is False

    def test_exit64_pytest_output_without_rc(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED a::b - x"])
        rc = self._run(["--manifest", str(man), "--pytest-output", str(ra)])
        assert rc == 64

    def test_exit64_junit_without_pytest_output(self, tmp_path):
        man = self._manifest(tmp_path)
        junit = tmp_path / "j.xml"
        junit.write_text('<testsuite failures="1" errors="0"/>', encoding="utf-8")
        rc = self._run(["--manifest", str(man), "--junit", str(junit), "--observed-rc", "1"])
        assert rc == 64

    def test_absent_manifest_failure_exit1(self, tmp_path, capsys):
        # absent manifest → every failure net-new → exit 1 (AC-7)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - x"])
        rc = self._run([
            "--manifest", str(tmp_path / "absent.toml"), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["net_new"]

    def test_junit_reconciled_agreement_downgrades(self, tmp_path, capsys):
        man = self._manifest(tmp_path)
        ra = self._ra(tmp_path, ["FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - x"])
        junit = tmp_path / "j.xml"
        junit.write_text('<testsuites><testsuite failures="1" errors="0"/></testsuites>', encoding="utf-8")
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra), "--junit", str(junit),
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["downgrade"] is True

    def test_absolute_selectors_downgrade_instead_of_exit3(self, tmp_path, capsys):
        # IVG-254: pre-fix, an absolute --selectors path filters out every
        # repo-relative node-id, leaving an empty failed set that fails
        # reconciliation and exits 3. Post-fix it downgrades normally.
        man = self._manifest(tmp_path)
        ra = self._ra(
            tmp_path,
            ["FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - boom"],
        )
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--selectors", "/some/other/root/quoin/dev/tests/test_sleep_scoring.py",
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["downgrade"] is True
        assert out["reconciled"] is True
        assert out["known_red"][0]["id"] == "quoin/dev/tests/test_sleep_scoring.py"

    def test_net_new_under_absolute_selectors_still_exits_1(self, tmp_path, capsys):
        # guards against the selector-shape fix turning the filter into a blanket pass: an
        # unlisted node-id must still block even under absolute selectors
        man = self._manifest(tmp_path)
        ra = self._ra(
            tmp_path,
            ["FAILED quoin/dev/tests/test_new.py::test_x - boom"],
        )
        rc = self._run([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--selectors", "/some/other/root/quoin/dev/tests/test_new.py",
            "--observed-rc", "1", "--format", "json",
        ])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert "quoin/dev/tests/test_new.py::test_x" in out["net_new"]


# ---------------------------------------------------------------------------
# TestManifestResolution — IVG-254, manifest-root resolution
# ---------------------------------------------------------------------------

# The outer project root two levels above the git repo root, derived the same
# way `_default_manifest` scans for a nested checkout — used only by the
# skip-guarded live-layout test below.
_LIVE_PROJECT_ROOT = _CORE_PATH.resolve().parents[4]


class TestManifestResolution:
    def test_direct_hit_wins(self, tmp_path):
        # single-repo projects behave exactly as before
        direct = tmp_path / "quoin" / "dev" / "tests" / "known-red.toml"
        direct.parent.mkdir(parents=True)
        direct.write_text("", encoding="utf-8")
        assert kr._default_manifest(tmp_path) == direct

    def test_nested_repo_layout_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "quoin" / "dev" / "tests" / "known-red.toml"
        nested.parent.mkdir(parents=True)
        nested.write_text("", encoding="utf-8")
        assert kr._default_manifest(tmp_path) == nested

    def test_subdirectory_without_git_never_searched(self, tmp_path):
        sub = tmp_path / "not-a-repo"
        nested = sub / "quoin" / "dev" / "tests" / "known-red.toml"
        nested.parent.mkdir(parents=True)
        nested.write_text("", encoding="utf-8")
        fallback = tmp_path / ".workflow_artifacts" / "known-red.toml"
        assert kr._default_manifest(tmp_path) == fallback
        assert not fallback.exists()

    def test_absent_everywhere_returns_workflow_artifacts_path(self, tmp_path):
        # never default to quoin's own repo layout inside another project
        fallback = tmp_path / ".workflow_artifacts" / "known-red.toml"
        assert kr._default_manifest(tmp_path) == fallback
        assert not fallback.exists()
        assert not (tmp_path / "quoin").exists()

    def test_deterministic_sorted_first_hit_wins(self, tmp_path):
        for name in ("repo-b", "repo-a"):
            repo = tmp_path / name
            (repo / ".git").mkdir(parents=True)
            nested = repo / "quoin" / "dev" / "tests" / "known-red.toml"
            nested.parent.mkdir(parents=True)
            nested.write_text("", encoding="utf-8")
        expected = tmp_path / "repo-a" / "quoin" / "dev" / "tests" / "known-red.toml"
        assert kr._default_manifest(tmp_path) == expected

    def test_worktree_git_file_is_recognized(self, tmp_path):
        # a git worktree's `.git` is a FILE, not a directory
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").write_text(
            "gitdir: /elsewhere/.git/worktrees/repo\n", encoding="utf-8"
        )
        nested = repo / "quoin" / "dev" / "tests" / "known-red.toml"
        nested.parent.mkdir(parents=True)
        nested.write_text("", encoding="utf-8")
        assert kr._default_manifest(tmp_path) == nested

    def test_oserror_from_iterdir_falls_back_to_workflow_artifacts(self, tmp_path, monkeypatch):
        fallback = tmp_path / ".workflow_artifacts" / "known-red.toml"
        real_iterdir = Path.iterdir

        def _boom(self):
            if self == tmp_path:
                raise OSError("permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert kr._default_manifest(tmp_path) == fallback

    @pytest.mark.skipif(
        not (_LIVE_PROJECT_ROOT / "quoin" / ".git").exists(),
        reason="nested git-root layout not present (bare checkout)",
    )
    def test_live_nested_layout_resolves_to_committed_manifest(self):
        assert kr._default_manifest(_LIVE_PROJECT_ROOT) == _MANIFEST


# ---------------------------------------------------------------------------
# TestHumanBlock
# ---------------------------------------------------------------------------


class TestHumanBlock:
    def test_text_lists_downgrade(self, tmp_path, capsys):
        man = tmp_path / "known-red.toml"
        man.write_text(
            '[[entry]]\nid = "quoin/dev/tests/test_sleep_scoring.py"\n'
            'reason = "flaky clock"\nadded = "2026-07-29"\n',
            encoding="utf-8",
        )
        ra = tmp_path / "ra.txt"
        ra.write_text("FAILED quoin/dev/tests/test_sleep_scoring.py::test_a - x\n", encoding="utf-8")
        rc = kr.main([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "text",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Known-baseline (downgraded)" in out
        assert "test_sleep_scoring.py" in out
        assert "flaky clock" in out
        assert "2026-07-29" in out

    def test_text_reconciliation_line_on_exit3(self, tmp_path, capsys):
        man = tmp_path / "known-red.toml"
        man.write_text(
            '[[entry]]\nid = "x"\nreason = "r"\nadded = "d"\n', encoding="utf-8"
        )
        ra = tmp_path / "ra.txt"
        ra.write_text("no failed lines here\n", encoding="utf-8")
        rc = kr.main([
            "--manifest", str(man), "--pytest-output", str(ra),
            "--observed-rc", "1", "--format", "text",
        ])
        out = capsys.readouterr().out
        assert rc == 3
        assert "## Reconciliation" in out
        assert "UNRECONCILED" in out

    def test_text_lists_stale(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("QUOIN_KNOWN_RED_STALE_RUNS", "1")
        man = tmp_path / "known-red.toml"
        man.write_text(
            '[[entry]]\nid = "quoin/dev/tests/test_sleep_scoring.py"\n'
            'reason = "flaky"\nadded = "d"\n', encoding="utf-8"
        )
        ra = tmp_path / "ra.txt"
        ra.write_text("PASSED quoin/dev/tests/test_sleep_scoring.py::test_a\n", encoding="utf-8")
        cf = tmp_path / "counter.json"
        rc = kr.main([
            "--manifest", str(man), "--pytest-output", str(ra), "--observed-rc", "0",
            "--full-suite", "--run-token", "t0", "--counter-file", str(cf), "--format", "text",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "## Staleness" in out
        assert "recommend removal" in out


# ---------------------------------------------------------------------------
# TestNodeIdEndToEnd
# ---------------------------------------------------------------------------


class TestNodeIdEndToEnd:
    def test_real_sleep_scoring_nodeid_prefix_matches_manifest(self):
        repo_root = Path(__file__).resolve().parents[3]  # <proj>/quoin
        target = "quoin/dev/tests/test_sleep_scoring.py"
        # Run the file with -rA from the repo root and parse the ACTUAL emitted
        # node-ids via parse_pytest_report — the same identity surface production
        # uses. This asserts the real emitted node-id FORM (repo-root-relative,
        # 'quoin/'-prefixed) matches the whole-file manifest id, independent of
        # whether the file currently passes or fails.
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-rA", "-p", "no:cacheprovider", target],
            capture_output=True, text=True, cwd=str(repo_root), timeout=120,
        )
        passed, failed = kr.parse_pytest_report(run.stdout)
        ids = passed | failed
        assert ids, f"no node-ids parsed: {run.stdout[-800:]!r}\n{run.stderr[-400:]!r}"
        manifest = kr.load_manifest(_MANIFEST)
        seed = next(e for e in manifest if e["id"] == target)
        assert all(nid.split("::", 1)[0] == seed["id"] for nid in ids), ids
