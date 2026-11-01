"""Tests for the 'quoin dashboard' subcommand (T-10).

Tests: --help lists flags, missing script exits 2, URL= on stdout (CLI smoke).

Note: invocations use `quoin.cli.main(...)` directly (not `python -m quoin`)
because the package has no __main__.py. The `quoin` binary is not used in tests
to keep them hermetic (no PATH dependency).
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Import the CLI main function directly
from quoin.cli import main as _cli_main

# Repo paths used in startup test
_REPO_ROOT = Path(__file__).resolve().parents[3]  # e.g. .../Codex_workflow/quoin
_QUOIN_SRC = _REPO_ROOT / "quoin"                  # source package dir
_SRC = _REPO_ROOT / "src"


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project with .workflow_artifacts/."""
    proj = tmp_path / "project"
    (proj / ".workflow_artifacts").mkdir(parents=True)
    return proj


class TestDashboardHelp:
    """--help output contains all four flags (T-10 ack)."""

    def _help_output(self) -> str:
        import io as _io
        import argparse
        # Capture help via argparse (parse_args(['dashboard', '--help']))
        # argparse raises SystemExit(0) on --help
        buf = _io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            try:
                _cli_main(["dashboard", "--help"])
            except SystemExit:
                pass
        return buf.getvalue()

    def test_help_lists_port(self):
        out = self._help_output()
        assert "--port" in out

    def test_help_lists_no_browser(self):
        out = self._help_output()
        assert "no-browser" in out

    def test_help_lists_project_root(self):
        out = self._help_output()
        assert "project-root" in out

    def test_help_lists_source_dir(self):
        out = self._help_output()
        assert "source-dir" in out


class TestDashboardMissingScript:
    def test_missing_dashboard_server_exits_2(self, tmp_path):
        """If dashboard_server.py is not found in the resolved source tree, exit 2."""
        fake_source = tmp_path / "fake_quoin"
        (fake_source / "adapters" / "claude" / "skills").mkdir(parents=True)
        (fake_source / "scripts").mkdir(parents=True)
        # dashboard_server.py intentionally absent

        with pytest.raises(SystemExit) as exc_info:
            _cli_main([
                "dashboard",
                "--source-dir", str(fake_source),
                "--no-browser", "--port", "0",
                "--project-root", ".",
            ])
        assert exc_info.value.code == 2


class TestDashboardStartup:
    def test_cli_dashboard_prints_url_line(self, tmp_path):
        """quoin dashboard --no-browser --port 0 prints URL= on stdout.

        Runs the server via subprocess (not in-process) to properly test
        the blocking server lifecycle including the URL= line print.
        Uses PYTHONPATH pointing to src/ so the package resolves.
        """
        proj = _make_project(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, str('" + str(_SRC) + "')); "
             "from quoin.cli import main; main(['dashboard', '--no-browser', '--port', '0', "
             "'--project-root', '" + str(proj) + "', "
             "'--source-dir', '" + str(_QUOIN_SRC) + "'])"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            url_line = None
            deadline = time.time() + 10
            while time.time() < deadline:
                line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("URL="):
                    url_line = line
                    break
            assert url_line is not None, "quoin dashboard did not print URL= line within 10s"
            assert url_line.startswith("URL=http://127.0.0.1:")
            port_str = url_line.split(":")[-1]
            assert int(port_str) > 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
