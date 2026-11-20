"""Tests for installer wiring of dashboard modules and assets (T-11..T-15).

T-11: CORE_SCRIPTS and DEPLOYED_SCRIPTS membership.
T-12: deploy_dashboard_assets — fresh dest, missing source asset abort.
T-13: assets have no __QUOIN_HOME__ placeholder; _QUOIN_DEPLOYED_SUBDIRS covers 'core'.
T-14: quoin doctor --scope project:<tmp> shows Assets section in BOTH modes.
T-15: installed smoke — import, asset presence, doctor exit 0, GET / byte-serving.
"""
from __future__ import annotations

import http.client
import importlib.util
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from quoin import installer
from quoin.cli import main as _cli_main

_REPO_ROOT = Path(__file__).resolve().parents[3]
_QUOIN_SRC = _REPO_ROOT / "quoin"  # package source dir (has skills/, scripts/, core/)
_SRC = _REPO_ROOT / "src"

_ASSETS_SRC = _QUOIN_SRC / "core" / "scripts" / "dashboard_assets"
_DASHBOARD_ASSETS = installer._DASHBOARD_ASSETS


# ---------------------------------------------------------------------------
# T-11: List membership
# ---------------------------------------------------------------------------

class TestListsRegistered:
    def test_dashboard_model_in_core_scripts(self):
        """dashboard_model.py must be in CORE_SCRIPTS (T-11, D-11)."""
        assert "dashboard_model.py" in installer.CORE_SCRIPTS

    def test_dashboard_cost_in_deployed_scripts(self):
        """dashboard_cost.py must be in DEPLOYED_SCRIPTS (T-11, D-11)."""
        assert "dashboard_cost.py" in installer.DEPLOYED_SCRIPTS

    def test_dashboard_server_in_deployed_scripts(self):
        """dashboard_server.py must be in DEPLOYED_SCRIPTS (T-11, D-11)."""
        assert "dashboard_server.py" in installer.DEPLOYED_SCRIPTS


# ---------------------------------------------------------------------------
# T-12: deploy_dashboard_assets
# ---------------------------------------------------------------------------

class TestDeployAssets:
    def test_deploy_assets_fresh_dest(self, tmp_path):
        """deploy_dashboard_assets creates dest dir and copies all four assets."""
        dest_root = tmp_path / "dest_claude"
        # dest_root/core/scripts/dashboard_assets does NOT exist yet (fresh install)
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)
        assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
        assert assets_dir.is_dir()
        for fname in _DASHBOARD_ASSETS:
            assert (assets_dir / fname).exists(), f"{fname} not deployed"

    def test_deploy_assets_idempotent(self, tmp_path):
        """deploy_dashboard_assets can be called twice without error."""
        dest_root = tmp_path / "dest_claude"
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)  # second call
        assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
        for fname in _DASHBOARD_ASSETS:
            assert (assets_dir / fname).exists()

    def test_deploy_assets_missing_source_exits_1(self, tmp_path):
        """Missing source asset causes SystemExit(1)."""
        # Create a fake source tree with an empty dashboard_assets dir
        fake_src = tmp_path / "fake_quoin"
        (fake_src / "core" / "scripts" / "dashboard_assets").mkdir(parents=True)
        # Leave all asset files absent
        dest_root = tmp_path / "dest_claude"

        with pytest.raises(SystemExit) as exc_info:
            installer.deploy_dashboard_assets(fake_src, dest_root)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# T-13: no __QUOIN_HOME__ placeholder in assets + 'core' in deployed subdirs
# ---------------------------------------------------------------------------

class TestAssetsNoPlaceholder:
    def test_core_in_deployed_subdirs(self):
        """_QUOIN_DEPLOYED_SUBDIRS must include 'core' (covers asset dir, T-13)."""
        assert "core" in installer._QUOIN_DEPLOYED_SUBDIRS

    def test_index_html_no_placeholder(self):
        content = (_ASSETS_SRC / "index.html").read_text(encoding="utf-8")
        assert installer.QUOIN_HOME_PLACEHOLDER not in content

    def test_dashboard_css_no_placeholder(self):
        content = (_ASSETS_SRC / "dashboard.css").read_text(encoding="utf-8")
        assert installer.QUOIN_HOME_PLACEHOLDER not in content

    def test_app_js_no_placeholder(self):
        content = (_ASSETS_SRC / "app.js").read_text(encoding="utf-8")
        assert installer.QUOIN_HOME_PLACEHOLDER not in content

    def test_memory_js_no_placeholder(self):
        content = (_ASSETS_SRC / "memory.js").read_text(encoding="utf-8")
        assert installer.QUOIN_HOME_PLACEHOLDER not in content


# ---------------------------------------------------------------------------
# T-02 (IVG-87): every local asset ref in index.html is in _DASHBOARD_ASSETS
# ---------------------------------------------------------------------------

class TestIndexHtmlAssetCoverage:
    """Guard: any <script src> / <link href> added to index.html but not the
    installer tuple must fail CI (prevents a repeat of the memory.js 404 bug)."""

    def _parse_local_asset_refs(self) -> list[str]:
        import re
        content = (_ASSETS_SRC / "index.html").read_text(encoding="utf-8")
        # Match src="..." and href="..." attribute values
        raw = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', content)
        result = []
        for ref in raw:
            if ref.startswith("data:"):
                continue  # skip base64 inline data URIs (favicon)
            if "://" in ref or ref.startswith("//"):
                continue  # skip external URLs
            # Keep only relative local refs (bare filename or ./filename)
            fname = ref.lstrip("./")
            if fname:
                result.append(fname)
        return result

    def test_all_index_html_refs_in_assets_tuple(self):
        """Every local asset ref in index.html must be listed in _DASHBOARD_ASSETS."""
        refs = self._parse_local_asset_refs()
        assert refs, "No local asset refs found in index.html — parser may be broken"
        for fname in refs:
            assert fname in _DASHBOARD_ASSETS, (
                f"index.html references '{fname}' but it is not in "
                f"installer._DASHBOARD_ASSETS. Add it to the tuple."
            )

    def test_all_index_html_refs_are_deployed(self, tmp_path):
        """After deploy, every local asset ref in index.html exists on disk."""
        refs = self._parse_local_asset_refs()
        dest_root = tmp_path / "dest"
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)
        assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
        for fname in refs:
            assert (assets_dir / fname).exists(), (
                f"index.html references '{fname}' but it was not deployed to {assets_dir}"
            )

    def test_data_uri_favicon_not_treated_as_asset(self):
        """data: URIs (inline base64 favicon) must not be treated as local asset refs."""
        refs = self._parse_local_asset_refs()
        for ref in refs:
            assert not ref.startswith("data:"), (
                f"data: URI leaked into asset refs: {ref!r}"
            )


# ---------------------------------------------------------------------------
# T-14: quoin doctor assets section in both user and project modes
# ---------------------------------------------------------------------------

class TestDoctorAssetCheck:
    def test_doctor_project_mode_shows_assets(self, tmp_path, capsys):
        """quoin doctor --scope project:<tmp> shows Assets section with four files."""
        # First install to tmp_path to create a valid dest
        dest_root = tmp_path / ".claude"
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)

        # Run doctor in project mode against this scope
        # We invoke _cli_main and capture stdout
        import io, contextlib
        buf = io.StringIO()
        scope = f"project:{tmp_path}"
        rc = None
        with contextlib.redirect_stdout(buf):
            try:
                rc = _cli_main(["doctor", "--scope", scope])
            except SystemExit as e:
                rc = e.code

        output = buf.getvalue()
        # Assets section must be present
        assert "Assets" in output or "dashboard_assets" in output
        # All four asset filenames must appear
        for fname in _DASHBOARD_ASSETS:
            assert fname in output

    def test_doctor_project_mode_missing_asset_exits_1(self, tmp_path):
        """Missing asset causes doctor to exit 1."""
        dest_root = tmp_path / ".claude"
        installer.deploy_dashboard_assets(_QUOIN_SRC, dest_root)
        # Remove one asset
        (dest_root / "core" / "scripts" / "dashboard_assets" / "app.js").unlink()

        import io, contextlib
        buf = io.StringIO()
        scope = f"project:{tmp_path}"
        rc = None
        with contextlib.redirect_stdout(buf):
            try:
                rc = _cli_main(["doctor", "--scope", scope])
            except SystemExit as e:
                rc = e.code

        assert rc == 1


# ---------------------------------------------------------------------------
# T-15: Installed smoke — import, asset presence, doctor exit 0, byte-serving
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestInstalledSmoke:
    def test_installed_smoke(self, tmp_path):
        """Install to tmp scope and verify: imports, assets, doctor, GET / byte-serving."""
        scope = f"project:{tmp_path}"

        # --- (a) Install to tmp scope ---
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rc = _cli_main([
                    "install",
                    "--scope", scope,
                    "--source-dir", str(_QUOIN_SRC),
                    "--allow-hook-merge",  # avoid home-hook conflict abort in project mode
                ])
            except SystemExit as e:
                rc = e.code if e.code is not None else 0

        assert rc == 0, f"install exited {rc}; output:\n{buf.getvalue()}"

        dest_root = tmp_path / ".claude"

        # --- (b) Spec-load deployed scripts and assert public symbols ---
        def _spec_load(key: str, path: Path):
            spec = importlib.util.spec_from_file_location(key, path)
            assert spec is not None, f"Cannot create spec for {path}"
            mod = importlib.util.module_from_spec(spec)
            sys.modules[key] = mod
            spec.loader.exec_module(mod)
            return mod

        model_path = dest_root / "core" / "scripts" / "dashboard_model.py"
        assert model_path.exists(), "dashboard_model.py not deployed"
        dm = _spec_load("_t15_dashboard_model", model_path)
        assert hasattr(dm, "scan_tasks"), "dashboard_model missing scan_tasks"
        assert hasattr(dm, "task_detail"), "dashboard_model missing task_detail"

        cost_path = dest_root / "scripts" / "dashboard_cost.py"
        assert cost_path.exists(), "dashboard_cost.py not deployed"
        dc = _spec_load("_t15_dashboard_cost", cost_path)
        assert hasattr(dc, "make_cost_provider"), "dashboard_cost missing make_cost_provider"

        server_path = dest_root / "scripts" / "dashboard_server.py"
        assert server_path.exists(), "dashboard_server.py not deployed"
        ds = _spec_load("_t15_dashboard_server", server_path)
        assert hasattr(ds, "scan_tasks"), "deployed dashboard_server doesn't bind scan_tasks"

        # --- (c) Assert four asset files exist ---
        assets_dir = dest_root / "core" / "scripts" / "dashboard_assets"
        for fname in _DASHBOARD_ASSETS:
            assert (assets_dir / fname).exists(), f"deployed asset missing: {fname}"

        # --- (d) Doctor exits 0 with Assets section ---
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            try:
                rc2 = _cli_main(["doctor", "--scope", scope])
            except SystemExit as e:
                rc2 = e.code if e.code is not None else 0

        doctor_out = buf2.getvalue()
        assert rc2 == 0, f"doctor exit {rc2}; output:\n{doctor_out}"
        assert "Assets" in doctor_out or "dashboard_assets" in doctor_out

        # --- (e) Start server on --port 0 and GET / for byte-serving smoke ---
        proj = tmp_path / "project"
        (proj / ".workflow_artifacts").mkdir(parents=True)

        proc = subprocess.Popen(
            [sys.executable, str(server_path),
             "--no-browser", "--port", "0",
             "--project-root", str(proj)],
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

            assert url_line is not None, "Deployed server did not print URL= line within 10s"
            port = int(url_line.split(":")[-1])

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()

            assert resp.status == 200, f"GET / returned {resp.status}"
            # Body must contain index.html bytes
            assert b"<!DOCTYPE html>" in body or b"<!doctype html>" in body, (
                "GET / body does not look like index.html"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
