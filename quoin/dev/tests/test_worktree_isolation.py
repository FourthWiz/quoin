"""Unit tests for quoin/core/scripts/worktree_isolation.py (IVG-116, T-12).

Sibling of test_dispatch_config.py — mirrors its structure/idioms because
worktree_isolation.py mirrors dispatch_config.py's config → file → sentinel → default
precedence design (D-03).

Tests cover:
- Config precedence matrix (env QUOIN_WORKTREE_ISOLATION > ~/.config/quoin/dispatch.json > None)
- config_verdict() truth table (on → attempt, off → skip, else unset)
- Probe sentinel layer: read_probe (works/broken/malformed/missing/no-root → unknown),
  write_probe (atomic rename, idempotency, overwrite, invalid no-op, no-root/error silent)
- decide() truth table (config × probe → attempt/skip + reason)
- main() CLI: --decide (+ --verbose), --write-probe, blanket fail-OPEN → "skip"
- Dual-list installed-import smoke (DEPLOYED_SCRIPTS + CORE_SCRIPTS)

Import idiom: importlib loader (lesson 2026-06-17 — direct package import raises
ModuleNotFoundError for core scripts).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Load the core module via importlib (lesson 2026-06-17)
# ---------------------------------------------------------------------------

_CORE_PATH = (
    Path(__file__).resolve().parents[3]
    / "quoin"
    / "core"
    / "scripts"
    / "worktree_isolation.py"
)

_MODULE_NAME = "_quoin_core_worktree_isolation_test"
_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _CORE_PATH)
assert _SPEC is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = _MOD
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

read_config = _MOD.read_config
config_verdict = _MOD.config_verdict
find_project_root = _MOD.find_project_root
probe_path = _MOD.probe_path
read_probe = _MOD.read_probe
write_probe = _MOD.write_probe
decide = _MOD.decide
main = _MOD.main


# ---------------------------------------------------------------------------
# Config layer — read_config() + config_verdict()
# ---------------------------------------------------------------------------


class TestReadConfig:
    """Config precedence: env QUOIN_WORKTREE_ISOLATION > dispatch.json:worktree_isolation > None.

    All tests use mock.patch exclusively for expanduser redirection (never
    monkeypatch.setattr on os.path.expanduser) — mixing the two leaks a stale Mock
    into subsequent tests via teardown-ordering (see test_dispatch_config.py note).
    """

    def test_env_on_overrides_file(self, tmp_path, monkeypatch):
        """QUOIN_WORKTREE_ISOLATION env takes precedence over the file value."""
        cfg_file = tmp_path / "dispatch.json"
        cfg_file.write_text(json.dumps({"worktree_isolation": "off"}))
        monkeypatch.setenv("QUOIN_WORKTREE_ISOLATION", "on")
        with _patch_cfg_path(cfg_file):
            assert read_config() == "on"

    def test_file_used_when_env_absent(self, tmp_path, monkeypatch):
        """dispatch.json value used when env var is unset."""
        monkeypatch.delenv("QUOIN_WORKTREE_ISOLATION", raising=False)
        cfg_file = tmp_path / "dispatch.json"
        cfg_file.write_text(json.dumps({"worktree_isolation": "off"}))
        with _patch_cfg_path(cfg_file):
            assert read_config() == "off"

    def test_empty_env_falls_through_to_file(self, tmp_path, monkeypatch):
        """Empty env string is treated as unset → the file value is used."""
        monkeypatch.setenv("QUOIN_WORKTREE_ISOLATION", "")
        cfg_file = tmp_path / "dispatch.json"
        cfg_file.write_text(json.dumps({"worktree_isolation": "on"}))
        with _patch_cfg_path(cfg_file):
            assert read_config() == "on"

    def test_none_when_both_absent(self, tmp_path, monkeypatch):
        """Both env and file absent → None."""
        monkeypatch.delenv("QUOIN_WORKTREE_ISOLATION", raising=False)
        nonexistent = tmp_path / "dispatch.json"
        with _patch_cfg_path(nonexistent):
            assert read_config() is None

    def test_missing_key_is_none(self, tmp_path, monkeypatch):
        """File present but without the worktree_isolation key → None."""
        monkeypatch.delenv("QUOIN_WORKTREE_ISOLATION", raising=False)
        cfg_file = tmp_path / "dispatch.json"
        cfg_file.write_text(json.dumps({"one_m_dispatch": "on"}))
        with _patch_cfg_path(cfg_file):
            assert read_config() is None

    def test_malformed_json_falls_through(self, tmp_path, monkeypatch):
        """Malformed JSON in dispatch.json → treated as absent (fail-OPEN → None)."""
        monkeypatch.delenv("QUOIN_WORKTREE_ISOLATION", raising=False)
        cfg_file = tmp_path / "dispatch.json"
        cfg_file.write_text("not json {{")
        with _patch_cfg_path(cfg_file):
            assert read_config() is None


class TestConfigVerdict:
    """config_verdict() truth table: on → attempt, off → skip, else unset."""

    def test_on_returns_attempt(self):
        assert config_verdict("on") == "attempt"

    def test_off_returns_skip(self):
        assert config_verdict("off") == "skip"

    def test_none_returns_unset(self):
        assert config_verdict(None) == "unset"

    def test_empty_string_returns_unset(self):
        assert config_verdict("") == "unset"

    def test_case_insensitive_on(self):
        assert config_verdict("ON") == "attempt"
        assert config_verdict("On") == "attempt"

    def test_case_insensitive_off(self):
        assert config_verdict("OFF") == "skip"

    def test_whitespace_stripped(self):
        assert config_verdict("  on  ") == "attempt"
        assert config_verdict("  off  ") == "skip"

    def test_garbage_value_returns_unset(self):
        assert config_verdict("maybe") == "unset"
        assert config_verdict("   ") == "unset"


# ---------------------------------------------------------------------------
# Project-root resolution
# ---------------------------------------------------------------------------


class TestFindProjectRoot:
    def test_no_workflow_artifacts_returns_none(self, tmp_path):
        assert find_project_root(tmp_path) is None

    def test_finds_parent(self, tmp_path):
        (tmp_path / ".workflow_artifacts").mkdir()
        child = tmp_path / "sub" / "dir"
        child.mkdir(parents=True)
        assert find_project_root(child) == tmp_path

    def test_finds_self(self, tmp_path):
        (tmp_path / ".workflow_artifacts").mkdir()
        assert find_project_root(tmp_path) == tmp_path


class TestProbePath:
    def test_probe_path_shape(self, tmp_path):
        assert probe_path(tmp_path) == (
            tmp_path / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        )


# ---------------------------------------------------------------------------
# Probe sentinel layer — read_probe()
# ---------------------------------------------------------------------------


class TestReadProbe:
    def _write_sentinel(self, root: Path, content: str) -> None:
        p = root / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_works_sentinel(self, tmp_path):
        self._write_sentinel(tmp_path, "works")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "works"

    def test_broken_sentinel(self, tmp_path):
        self._write_sentinel(tmp_path, "broken")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "broken"

    def test_works_sentinel_trailing_whitespace_stripped(self, tmp_path):
        self._write_sentinel(tmp_path, "works\n")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "works"

    def test_missing_sentinel_is_unknown(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "unknown"

    def test_malformed_sentinel_is_unknown(self, tmp_path):
        self._write_sentinel(tmp_path, "works broken junk")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "unknown"

    def test_empty_sentinel_is_unknown(self, tmp_path):
        self._write_sentinel(tmp_path, "")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert read_probe() == "unknown"

    def test_no_project_root_is_unknown(self):
        with mock.patch.object(_MOD, "find_project_root", return_value=None):
            assert read_probe() == "unknown"

    def test_read_error_is_unknown(self, tmp_path):
        self._write_sentinel(tmp_path, "works")
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            with mock.patch.object(Path, "read_text", side_effect=OSError("io error")):
                assert read_probe() == "unknown"


# ---------------------------------------------------------------------------
# Probe sentinel layer — write_probe()
# ---------------------------------------------------------------------------


class TestWriteProbe:
    def _sentinel(self, root: Path) -> Path:
        return root / ".workflow_artifacts" / "memory" / "worktree-probe.txt"

    def test_writes_works(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            write_probe("works")
        assert self._sentinel(tmp_path).read_text() == "works"

    def test_writes_broken(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            write_probe("broken")
        assert self._sentinel(tmp_path).read_text() == "broken"

    def test_idempotent_same_value(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            write_probe("works")
            write_probe("works")
        assert self._sentinel(tmp_path).read_text() == "works"

    def test_overwrite_flips_value(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            write_probe("works")
            write_probe("broken")
        assert self._sentinel(tmp_path).read_text() == "broken"

    def test_invalid_result_is_noop(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            write_probe("bad-value")
        assert not self._sentinel(tmp_path).exists()

    def test_no_project_root_is_silent(self):
        with mock.patch.object(_MOD, "find_project_root", return_value=None):
            write_probe("works")  # must not raise

    def test_write_error_is_silent(self, tmp_path):
        """Write failure → silent skip (fail-OPEN)."""
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                write_probe("works")  # must not raise

    def test_atomic_rename_used(self, tmp_path):
        """Writes via a .tmp file then os.replace (whole-file atomic overwrite)."""
        replaced = []
        real_replace = os.replace

        def capturing_replace(src, dst):
            replaced.append((src, dst))
            real_replace(src, dst)

        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            with mock.patch("os.replace", side_effect=capturing_replace):
                write_probe("works")

        assert len(replaced) == 1
        src, dst = replaced[0]
        assert str(src).endswith(".tmp")
        assert not str(dst).endswith(".tmp")


# ---------------------------------------------------------------------------
# decide() truth table
# ---------------------------------------------------------------------------


class TestDecide:
    """decide() covers all (config × probe) combinations. Default = skip (D-04)."""

    def _decide_with(self, cfg_val, probe_state: str):
        with mock.patch.object(_MOD, "read_config", return_value=cfg_val):
            with mock.patch.object(_MOD, "read_probe", return_value=probe_state):
                return decide()

    # --- config=on → attempt regardless of probe ---

    def test_config_on_any_probe_returns_attempt_config(self):
        for probe_state in ("works", "broken", "unknown"):
            v, r = self._decide_with("on", probe_state)
            assert v == "attempt", f"probe={probe_state}"
            assert r == "config", f"probe={probe_state}"

    # --- config=off → skip regardless of probe ---

    def test_config_off_any_probe_returns_skip_config(self):
        for probe_state in ("works", "broken", "unknown"):
            v, r = self._decide_with("off", probe_state)
            assert v == "skip", f"probe={probe_state}"
            assert r == "config", f"probe={probe_state}"

    # --- config=unset + probe=works → attempt ---

    def test_unset_config_probe_works_returns_attempt_probe(self):
        v, r = self._decide_with(None, "works")
        assert v == "attempt"
        assert r == "probe"

    # --- config=unset + probe=broken/unknown → default-skip ---

    def test_unset_config_probe_broken_returns_skip_default(self):
        v, r = self._decide_with(None, "broken")
        assert v == "skip"
        assert r == "default"

    def test_unset_config_probe_unknown_returns_skip_default(self):
        """Common case (no config, no sentinel) → skip = isolation opt-in (D-04)."""
        v, r = self._decide_with(None, "unknown")
        assert v == "skip"
        assert r == "default"

    def test_empty_config_falls_through_to_probe(self):
        """Empty-string config → unset → falls through to the probe layer."""
        v, r = self._decide_with("", "works")
        assert v == "attempt"
        assert r == "probe"


# ---------------------------------------------------------------------------
# main() CLI + blanket fail-OPEN
# ---------------------------------------------------------------------------


class TestMainDecide:
    """--decide prints verdict on line 1; --verbose adds reason on line 2."""

    def test_default_decide_prints_skip(self, tmp_path, capsys, monkeypatch):
        """No config, no sentinel → 'skip' (default; the guaranteed-safe verdict)."""
        monkeypatch.delenv("QUOIN_WORKTREE_ISOLATION", raising=False)
        with mock.patch.object(_MOD, "read_config", return_value=None):
            with mock.patch.object(_MOD, "read_probe", return_value="unknown"):
                rc = main(["--decide"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "skip"

    def test_decide_attempt_via_config(self, capsys):
        with mock.patch.object(_MOD, "read_config", return_value="on"):
            rc = main(["--decide"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "attempt"

    def test_verbose_default_reason_on_second_line(self, capsys):
        with mock.patch.object(_MOD, "read_config", return_value=None):
            with mock.patch.object(_MOD, "read_probe", return_value="unknown"):
                rc = main(["--decide", "--verbose"])
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "skip"
        assert lines[1] == "default"

    def test_verbose_config_reason(self, capsys):
        with mock.patch.object(_MOD, "read_config", return_value="off"):
            main(["--decide", "--verbose"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "skip"
        assert lines[1] == "config"

    def test_verbose_probe_reason(self, capsys):
        with mock.patch.object(_MOD, "read_config", return_value=None):
            with mock.patch.object(_MOD, "read_probe", return_value="works"):
                main(["--decide", "--verbose"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "attempt"
        assert lines[1] == "probe"


class TestMainFailOpen:
    """Every error path for --decide must produce 'skip' (never raise, never attempt)."""

    def test_decide_config_exception_failopen(self, capsys):
        with mock.patch.object(_MOD, "read_config", side_effect=Exception("disk error")):
            rc = main(["--decide"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "skip"

    def test_decide_probe_exception_failopen(self, capsys):
        with mock.patch.object(_MOD, "read_config", return_value=None):
            with mock.patch.object(_MOD, "read_probe", side_effect=Exception("io error")):
                rc = main(["--decide"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "skip"

    def test_decide_never_raises_on_config_error(self):
        with mock.patch.object(_MOD, "read_config", side_effect=RuntimeError("boom")):
            assert main(["--decide"]) == 0

    def test_unknown_flag_with_decide_prints_skip(self, capsys):
        """argparse error while --decide is present → fail-OPEN prints 'skip'."""
        rc = main(["--decide", "--bogus-flag"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "skip"

    def test_no_args_exits_0(self):
        assert main([]) == 0


class TestMainWriteProbe:
    def test_write_probe_works_flips_subsequent_decide(self, tmp_path):
        """--write-probe --result works → a later --decide returns 'attempt' (probe)."""
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert main(["--write-probe", "--result", "works"]) == 0
            # env/file config must be unset for the probe layer to be consulted
            with mock.patch.object(_MOD, "read_config", return_value=None):
                v, r = decide()
        assert v == "attempt"
        assert r == "probe"
        sentinel = tmp_path / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        assert sentinel.read_text() == "works"

    def test_write_probe_broken(self, tmp_path):
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert main(["--write-probe", "--result", "broken"]) == 0
        sentinel = tmp_path / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        assert sentinel.read_text() == "broken"

    def test_write_probe_missing_result_exits_0(self, tmp_path):
        """--write-probe without --result → silent no-op, exit 0, no sentinel written."""
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert main(["--write-probe"]) == 0
        sentinel = tmp_path / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        assert not sentinel.exists()

    def test_write_probe_invalid_result_rejected_by_argparse(self, tmp_path):
        """--result only accepts works|broken; anything else is an argparse error → exit 0, no write."""
        with mock.patch.object(_MOD, "find_project_root", return_value=tmp_path):
            assert main(["--write-probe", "--result", "maybe"]) == 0
        sentinel = tmp_path / ".workflow_artifacts" / "memory" / "worktree-probe.txt"
        assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Dual-list installer smoke (DEPLOYED_SCRIPTS + CORE_SCRIPTS)
# ---------------------------------------------------------------------------


class TestInstallerDualListSmoke:
    """worktree_isolation.py must appear in BOTH installer lists.

    If either list is missing it, the wrapper's parents[1]/core/scripts loader
    will fail at runtime (NameError / FileNotFoundError) on a fresh install
    (lesson 2026-05-31).
    """

    @pytest.fixture(scope="class")
    def installer(self):
        installer_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "quoin"
            / "installer.py"
        )
        spec = importlib.util.spec_from_file_location("_quoin_installer_wt_test", installer_path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod


    def test_wrapper_importable_via_deployed_path(self):
        """Smoke-import the WRAPPER (not the core) via the wrapper's own importlib chain."""
        wrapper_path = (
            Path(__file__).resolve().parents[3]
            / "quoin"
            / "scripts"
            / "worktree_isolation.py"
        )
        spec = importlib.util.spec_from_file_location("_wt_wrapper_smoke_test", wrapper_path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        # Core functions should be re-exported via the globals() loop in the wrapper.
        assert hasattr(mod, "main"), "main not exported from wrapper"
        assert hasattr(mod, "decide"), "decide not exported from wrapper"
        assert hasattr(mod, "write_probe"), "write_probe not exported from wrapper"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patch_cfg_path(cfg_file: Path):
    """Redirect os.path.expanduser('~/.config/quoin/dispatch.json') to cfg_file.

    Uses mock.patch ONLY (no monkeypatch) to avoid the mock.patch-vs-monkeypatch
    teardown ordering hazard (see test_dispatch_config.py note). Intercepts only the
    specific dispatch.json path; all other expanduser calls pass through unchanged.
    """
    original_expanduser = os.path.expanduser

    def patched_expanduser(p):
        if p == "~/.config/quoin/dispatch.json":
            return str(cfg_file)
        return original_expanduser(p)

    with mock.patch("os.path.expanduser", side_effect=patched_expanduser):
        yield
