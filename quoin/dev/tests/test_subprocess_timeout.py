"""Unit tests for the QUOIN_SUBPROCESS_TIMEOUT central knob (IVG-116 T-02/T-07/T-13).

Covers every touched subprocess site:
  - quoin/core/scripts/branch_hygiene.py  (_run)
  - quoin/core/scripts/affected_tests.py  (_run, pytest subprocess)
  - quoin/scripts/build_preambles.py      (git_hash_object)
  - quoin/core/scripts/status_graph.py    (_git_diff_nonempty)
  - quoin/core/scripts/verify_claims.py   (_run_gh_pr_list)
  - quoin/core/scripts/generate_discovery_map.py (_read_git_head)

Assertions:
  - Every site passes an explicit `timeout` kwarg derived from
    QUOIN_SUBPROCESS_TIMEOUT (captured via monkeypatched subprocess.run).
  - QUOIN_SUBPROCESS_TIMEOUT=1 forces TimeoutExpired handling deterministically
    on the SHORT git subprocesses — no uncaught raise, correct fallback return.
  - The pytest subprocess in affected_tests is bounded by a value derived from
    the number of selected test files (clamp(12 s x files, 600, 3300), raised to
    QUOIN_SUBPROCESS_TIMEOUT when larger, replaced exactly by
    QUOIN_PYTEST_TIMEOUT) and maps TimeoutExpired to exit code 3 with
    exit_reason="pytest-timeout" (proc P-03) — NOT exit 1, NOT exit 0.
  - _subprocess_timeout() bad-value fallback: QUOIN_SUBPROCESS_TIMEOUT="abc"
    -> 30, asserted across every local copy of the helper (MIN-5 / D-06).

Import idiom: importlib loader for core/scripts modules (lesson 2026-06-17);
sys.path.insert for quoin/scripts/build_preambles.py (mirrors
test_preamble_freshness.py's existing idiom).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------

_CORE_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "core" / "scripts"
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load_core(name: str):
    core_path = _CORE_SCRIPTS_DIR / f"{name}.py"
    mod_name = f"_quoin_core_{name}_timeout_test"
    spec = importlib.util.spec_from_file_location(mod_name, core_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_bh = _load_core("branch_hygiene")
_at = _load_core("affected_tests")
_sg = _load_core("status_graph")
_vc = _load_core("verify_claims")
_gdm = _load_core("generate_discovery_map")

sys.path.insert(0, str(_SCRIPTS_DIR))
import build_preambles as _bp  # noqa: E402


# ---------------------------------------------------------------------------
# _subprocess_timeout() — default / override / bad-value fallback (MIN-5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mod",
    [_bh, _at, _bp, _sg, _vc, _gdm],
    ids=["branch_hygiene", "affected_tests", "build_preambles", "status_graph", "verify_claims", "generate_discovery_map"],
)
class TestHelperFallback:
    def test_default_is_30(self, mod, monkeypatch):
        monkeypatch.delenv("QUOIN_SUBPROCESS_TIMEOUT", raising=False)
        assert mod._subprocess_timeout() == 30

    def test_env_override_honored(self, mod, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "7")
        assert mod._subprocess_timeout() == 7

    def test_bad_value_falls_back_to_30(self, mod, monkeypatch):
        """MIN-5: a typo in one copy is caught here for every touched site."""
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "abc")
        assert mod._subprocess_timeout() == 30


# ---------------------------------------------------------------------------
# branch_hygiene._run — explicit timeout kwarg + graceful TimeoutExpired
# ---------------------------------------------------------------------------

class TestBranchHygieneRun:
    def test_run_passes_explicit_timeout(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_bh.subprocess, "run", fake_run)
        out, err, rc = _bh._run(["git", "status"])
        assert captured.get("timeout") == 1
        assert (out, err, rc) == ("", "timeout", 1)

    def test_run_timeout_expired_no_uncaught_raise(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(_bh.subprocess, "run", fake_run)
        # Must not raise — graceful fallback return.
        result = _bh._run(["git", "rev-parse", "HEAD"])
        assert result == ("", "timeout", 1)


# ---------------------------------------------------------------------------
# affected_tests._run — mirror of branch_hygiene._run
# ---------------------------------------------------------------------------

class TestAffectedTestsRun:
    def test_run_passes_explicit_timeout(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_at.subprocess, "run", fake_run)
        out, err, rc = _at._run(["git", "status"])
        assert captured.get("timeout") == 1
        assert (out, err, rc) == ("", "timeout", 1)

    def test_run_timeout_expired_no_uncaught_raise(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(_at.subprocess, "run", fake_run)
        result = _at._run(["git", "rev-parse", "HEAD"])
        assert result == ("", "timeout", 1)


# ---------------------------------------------------------------------------
# affected_tests pytest-subprocess — exit-3 pin + derived bound (MAJ-3/D-05)
# ---------------------------------------------------------------------------

class TestAffectedTestsPytestTimeout:
    @pytest.fixture(autouse=True)
    def _clean_knobs(self, monkeypatch):
        # An operator who exports these must not turn the suite red.
        monkeypatch.delenv("QUOIN_PYTEST_TIMEOUT", raising=False)
        monkeypatch.delenv("QUOIN_WAIT_BUDGET_SECS", raising=False)

    def _make_git_repo_with_one_change(self, tmp_path):
        """Hermetic repo: one committed .py source + matching test, no upstream,
        clean worktree — reaches Step 5 (pytest invocation) via base-branch-diff
        or worktree-diff fallback so main() gets far enough to hit the pytest
        subprocess.run call. Branch pinned to "main" (mirrors
        test_affected_tests.py's TestChangedFiles idiom) so base-branch
        resolution is deterministic regardless of git's init.defaultBranch."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "foo.py").write_text("def foo():\n    return 1\n")
        (tmp_path / "test_foo.py").write_text("import foo\ndef test_foo():\n    assert foo.foo() == 1\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
        # Second commit so there is a HEAD~1 to diff against via worktree fallback.
        (tmp_path / "foo.py").write_text("def foo():\n    return 1\n\n\n# touch\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        return tmp_path

    def test_pytest_timeout_maps_to_exit_3(self, tmp_path, monkeypatch, capsys):
        repo = self._make_git_repo_with_one_change(tmp_path)
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        monkeypatch.delenv("QUOIN_DISABLE_AFFECTED_TESTS", raising=False)

        real_run = subprocess.run
        captured_timeout = {}

        def fake_run(args, **kwargs):
            # Only intercept the pytest invocation (module + "-m" + "pytest").
            if "pytest" in args:
                captured_timeout["timeout"] = kwargs.get("timeout")
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
            return real_run(args, **kwargs)

        monkeypatch.setattr(_at.subprocess, "run", fake_run)

        rc = _at.main(["--project-root", str(repo)])

        assert rc == 3
        # env=1 does NOT trip the pytest bound: derived = max(600, 1) = 600 (MIN-4)
        assert captured_timeout.get("timeout") == 600

        out = capsys.readouterr().out
        assert '"exit_reason": "pytest-timeout"' in out
        assert '"ran_pytest": false' in out

    def test_pytest_timeout_derived_bound_respects_higher_env(self, tmp_path, monkeypatch):
        repo = self._make_git_repo_with_one_change(tmp_path)
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "900")

        real_run = subprocess.run
        captured_timeout = {}

        def fake_run(args, **kwargs):
            if "pytest" in args:
                captured_timeout["timeout"] = kwargs.get("timeout")
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
            return real_run(args, **kwargs)

        monkeypatch.setattr(_at.subprocess, "run", fake_run)
        rc = _at.main(["--project-root", str(repo)])

        assert rc == 3
        assert captured_timeout.get("timeout") == 900  # max(600, 900) == 900

    # -- derived bound ------------------------------------------------------

    def test_bound_176_files_at_least_1800(self):
        bound, basis = _at._pytest_timeout(176)
        assert bound == 2112 >= 1800
        assert basis["source"] == "derived"

    def test_bound_5_files_is_floor_600(self):
        assert _at._pytest_timeout(5)[0] == 600

    def test_bound_ceiling_3300(self):
        assert _at._pytest_timeout(1000)[0] == 3300

    def test_pytest_knob_exact_override(self, monkeypatch):
        monkeypatch.setenv("QUOIN_PYTEST_TIMEOUT", "77")
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "900")
        bound, basis = _at._pytest_timeout(176)
        assert bound == 77
        assert basis["source"] == "env-override"

    def test_subprocess_timeout_compat_raises_bound(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "2500")
        bound, basis = _at._pytest_timeout(176)
        assert bound == 2500
        assert basis["source"] == "subprocess-timeout-compat"

    @pytest.mark.parametrize("raw", ["abc", "0", "-5"])
    def test_pytest_knob_invalid_ignored(self, monkeypatch, raw):
        monkeypatch.setenv("QUOIN_PYTEST_TIMEOUT", raw)
        bound, basis = _at._pytest_timeout(176)
        assert bound == 2112
        assert basis["source"] == "derived"
        assert basis["knob_invalid"] == raw

    def test_git_subprocess_timeout_unchanged_by_pytest_knob(self, monkeypatch):
        monkeypatch.setenv("QUOIN_PYTEST_TIMEOUT", "5000")
        monkeypatch.delenv("QUOIN_SUBPROCESS_TIMEOUT", raising=False)
        assert _at._subprocess_timeout() == 30
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(_at.subprocess, "run", fake_run)
        _at._run(["git", "status"])
        assert captured["timeout"] == 30

    # -- CLI output ---------------------------------------------------------

    def _force_timeout(self, monkeypatch):
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "pytest" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
            return real_run(args, **kwargs)

        monkeypatch.setattr(_at.subprocess, "run", fake_run)

    def test_forced_timeout_output_names_bound_size_and_knob(self, tmp_path, monkeypatch, capsys):
        import json

        repo = self._make_git_repo_with_one_change(tmp_path)
        self._force_timeout(monkeypatch)
        rc = _at.main(["--project-root", str(repo)])
        assert rc == 3
        d = json.loads(capsys.readouterr().out)
        assert d["exit_reason"] == "pytest-timeout"
        assert d["pytest_timeout_secs"] == 600
        assert d["pytest_timeout_basis"]["selected_files"] == 1
        assert "600" in d["remedy"] and "1 selected" in d["remedy"]
        assert "QUOIN_PYTEST_TIMEOUT" in d["remedy"]

        rc = _at.main(["--project-root", str(repo), "--format", "text"])
        assert rc == 3
        text = capsys.readouterr().out
        assert "pytest_timeout: 600 s (derived, 1 selected files)" in text
        assert "remedy:" in text and "QUOIN_PYTEST_TIMEOUT" in text

    def test_env_override_remedy_wording(self, tmp_path, monkeypatch, capsys):
        import json

        repo = self._make_git_repo_with_one_change(tmp_path)
        monkeypatch.setenv("QUOIN_PYTEST_TIMEOUT", "42")
        self._force_timeout(monkeypatch)
        _at.main(["--project-root", str(repo)])
        d = json.loads(capsys.readouterr().out)
        assert d["pytest_timeout_secs"] == 42
        assert "set by QUOIN_PYTEST_TIMEOUT" in d["remedy"]

    def test_select_only_reports_bound(self, tmp_path, capsys):
        import json

        repo = self._make_git_repo_with_one_change(tmp_path)
        rc = _at.main(["--project-root", str(repo), "--select-only"])
        assert rc == 0
        d = json.loads(capsys.readouterr().out)
        assert d["pytest_timeout_secs"] == 600
        assert d["pytest_timeout_basis"]["selected_files"] == 1

    def test_budget_warning_when_bound_plus_margin_exceeds_budget(self, tmp_path, monkeypatch, capsys):
        import json

        repo = self._make_git_repo_with_one_change(tmp_path)
        monkeypatch.setenv("QUOIN_PYTEST_TIMEOUT", "3500")
        for fmt in ("json", "text"):
            _at.main(["--project-root", str(repo), "--select-only", "--format", fmt])
            out = capsys.readouterr().out
            assert "raise QUOIN_WAIT_BUDGET_SECS" in out
        monkeypatch.setenv("QUOIN_WAIT_BUDGET_SECS", "7200")
        for fmt in ("json", "text"):
            _at.main(["--project-root", str(repo), "--select-only", "--format", fmt])
            out = capsys.readouterr().out
            assert "pytest_timeout_warning" not in out
            assert "exceeds the headless" not in out


# ---------------------------------------------------------------------------
# build_preambles.git_hash_object — explicit timeout + clear TimeoutExpired wrap
# ---------------------------------------------------------------------------

class TestBuildPreamblesHashObject:
    def test_passes_explicit_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        captured = {}
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            return real_run(args, **kwargs)

        monkeypatch.setattr(_bp.subprocess, "run", fake_run)
        f = tmp_path / "x.txt"
        f.write_text("hello\n")
        sha = _bp.git_hash_object(f)
        assert captured.get("timeout") == 1
        assert isinstance(sha, str) and len(sha) == 40

    def test_timeout_expired_wrapped_clearly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_bp.subprocess, "run", fake_run)
        f = tmp_path / "x.txt"
        f.write_text("hello\n")
        with pytest.raises(RuntimeError, match="git hash-object timed out"):
            _bp.git_hash_object(f)


# ---------------------------------------------------------------------------
# status_graph._git_diff_nonempty — explicit timeout + graceful fallback
# ---------------------------------------------------------------------------

class TestStatusGraphTimeout:
    def test_passes_explicit_timeout_and_handles_expiry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        captured = []

        def fake_run(args, **kwargs):
            captured.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_sg.subprocess, "run", fake_run)
        result = _sg._git_diff_nonempty(tmp_path)
        assert result is False  # graceful fallback, no uncaught raise
        assert captured and all(t == 1 for t in captured)


# ---------------------------------------------------------------------------
# verify_claims._run_gh_pr_list — explicit timeout + graceful fallback (None)
# ---------------------------------------------------------------------------

class TestVerifyClaimsTimeout:
    def test_passes_explicit_timeout_and_handles_expiry(self, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        monkeypatch.setattr(_vc.shutil, "which", lambda _: "/usr/bin/gh")
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_vc.subprocess, "run", fake_run)
        result = _vc._run_gh_pr_list()
        assert result is None  # fail-open, no uncaught raise
        assert captured.get("timeout") == 1


# ---------------------------------------------------------------------------
# generate_discovery_map._read_git_head — explicit timeout + graceful fallback
# ---------------------------------------------------------------------------

class TestGenerateDiscoveryMapTimeout:
    def test_passes_explicit_timeout_and_falls_back_on_expiry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUOIN_SUBPROCESS_TIMEOUT", "1")
        repo_dir = tmp_path / "repo"
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "refs" / "heads").mkdir(parents=True)
        sha = "a" * 40
        (git_dir / "refs" / "heads" / "main").write_text(sha + "\n")

        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(_gdm.subprocess, "run", fake_run)
        result = _gdm._read_git_head(repo_dir)
        assert captured.get("timeout") == 1
        # Falls back to Strategy 2 (reading .git/HEAD directly) on TimeoutExpired.
        assert result == sha
