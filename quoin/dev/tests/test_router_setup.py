"""Tests for quoin router setup (IVG-64 Stage 1).

All tests run in CI with NO network/npm access.
- _install_ccr is monkeypatched; presence is faked via shutil.which/_npm_major
  rather than a version query (`ccr -v`/`ccr version` are never invoked).
- Temp HOME (tmp_path) isolates filesystem side-effects.
- No subprocess calls to npm or ccr in the test suite.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.ccr_config import (  # noqa: E402
    CcrConfigError,
    assert_no_secret_in,
    backup_config,
    ccr_config_path,
    load_config,
    merge_openrouter_provider,
    merge_router_keys,
    probe_service,
    read_openrouter_key,
    write_config,
)
from quoin.router import (  # noqa: E402
    CCR_PINNED_VERSION,
    DEFAULT_MODELS,
    MIN_NODE_MAJOR,
    ROUTER_MAP,
    CcrVersion,
    _cmd_router_setup,
    _cmd_router_status,
    quoin_models_path,
    seed_models_file_if_absent,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_args(dry_run: bool = False, home: Path | None = None) -> argparse.Namespace:
    args = argparse.Namespace(dry_run=dry_run)
    if home is not None:
        args._home_override = home
    return args


def _setup_env(monkeypatch, api_key: str = "sk-or-SENTINEL-KEY") -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", api_key)


# ── ccr_config.py unit tests ───────────────────────────────────────────────────

class TestCcrConfigPath:
    def test_default_path(self) -> None:
        path = ccr_config_path()
        assert path.name == "config.json"
        assert path.parent.name == ".claude-code-router"

    def test_home_override(self, tmp_path: Path) -> None:
        path = ccr_config_path(home=tmp_path)
        assert path == tmp_path / ".claude-code-router" / "config.json"


class TestReadOpenrouterKey:
    def test_reads_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abc123")
        assert read_openrouter_key() == "sk-or-abc123"

    def test_missing_key_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(CcrConfigError, match="OPENROUTER_API_KEY"):
            read_openrouter_key()

    def test_empty_key_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
        with pytest.raises(CcrConfigError, match="OPENROUTER_API_KEY"):
            read_openrouter_key()


class TestBackupConfig:
    def test_no_backup_if_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        result = backup_config(path)
        assert result is None

    def test_creates_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text('{"existing": true}', encoding="utf-8")
        backup = backup_config(path)
        assert backup is not None
        assert backup.exists()
        assert backup.name.startswith("config.json.bak-")
        assert json.loads(backup.read_text())["existing"] is True


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        assert load_config(path) == {}

    def test_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text('{"Providers": []}', encoding="utf-8")
        assert load_config(path) == {"Providers": []}

    def test_malformed_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("NOT JSON", encoding="utf-8")
        result = load_config(path)
        assert result == {}


class TestMergeOpenrouterProvider:
    def test_fresh_empty_config(self) -> None:
        cfg, changes = merge_openrouter_provider({}, "sk-key", ["model1", "model2"])
        assert "Providers" in cfg
        assert len(cfg["Providers"]) == 1
        assert cfg["Providers"][0]["name"] == "openrouter"
        assert cfg["Providers"][0]["models"] == ["model1", "model2"]
        assert "added" in changes[0]

    def test_no_keyerror_on_empty(self) -> None:
        # MAJ-2 coverage: must not raise KeyError on fresh {}
        cfg, _ = merge_openrouter_provider({}, "sk-key", ["m"])
        assert cfg["Providers"][0]["api_key"] == "sk-key"

    def test_preserves_other_providers(self) -> None:
        existing_cfg = {"Providers": [{"name": "other-provider", "models": ["x"]}]}
        cfg, _ = merge_openrouter_provider(existing_cfg, "sk-key", ["m"])
        names = [p["name"] for p in cfg["Providers"]]
        assert "other-provider" in names
        assert "openrouter" in names

    def test_update_existing_no_duplicate(self) -> None:
        cfg0 = {"Providers": [{"name": "openrouter", "api_key": "old-key", "models": []}]}
        cfg, changes = merge_openrouter_provider(cfg0, "new-key", ["m"])
        assert len(cfg["Providers"]) == 1  # no duplicate
        assert cfg["Providers"][0]["api_key"] == "new-key"
        assert "updated" in changes[0]

    def test_non_list_providers_treated_as_empty(self) -> None:
        cfg, _ = merge_openrouter_provider({"Providers": "broken"}, "sk-key", ["m"])
        assert isinstance(cfg["Providers"], list)

    def test_models_list_coercion(self) -> None:
        # dict_values must be coerced to list before JSON serialization
        models_values = DEFAULT_MODELS.values()
        cfg, _ = merge_openrouter_provider({}, "sk-key", list(models_values))
        assert isinstance(cfg["Providers"][0]["models"], list)


class TestMergeRouterKeys:
    def test_fresh_empty_config(self) -> None:
        cfg, changes, warnings = merge_router_keys({}, ROUTER_MAP)
        assert "Router" in cfg
        assert "default" in cfg["Router"]
        assert not warnings  # no foreign keys on fresh config

    def test_no_keyerror_on_empty(self) -> None:
        # MAJ-2 coverage
        cfg, changes, _ = merge_router_keys({}, ROUTER_MAP)
        assert cfg["Router"]["background"] == ROUTER_MAP["background"]

    def test_router_values_are_provider_model_format(self) -> None:
        # Round-2 MIN-1: values must be "provider,model" not bare slugs
        for val in ROUTER_MAP.values():
            assert "," in val, f"Router value {val!r} is not 'provider,model' format"
            assert val.startswith("openrouter,"), f"Router value {val!r} does not start with 'openrouter,'"

    def test_non_clobber_foreign_router_default(self) -> None:
        # D-05: if Router.default points at something else, preserve and warn
        cfg0 = {"Router": {"default": "my-provider,my-model"}}
        cfg, changes, warnings = merge_router_keys(cfg0, ROUTER_MAP)
        assert cfg["Router"]["default"] == "my-provider,my-model"
        assert any("default" in w for w in warnings)

    def test_updates_existing_openrouter_key(self) -> None:
        cfg0 = {"Router": {"default": "openrouter,old-model"}}
        cfg, changes, warnings = merge_router_keys(cfg0, ROUTER_MAP)
        assert cfg["Router"]["default"] == ROUTER_MAP["default"]
        assert not any("default" in w for w in warnings)

    def test_preserves_foreign_keys(self) -> None:
        cfg0 = {"Router": {"longContextThreshold": 80000, "webSearch": "some-model"}}
        cfg, _, _ = merge_router_keys(cfg0, ROUTER_MAP)
        assert cfg["Router"]["longContextThreshold"] == 80000
        assert cfg["Router"]["webSearch"] == "some-model"

    def test_never_sets_non_interactive_mode(self) -> None:
        cfg, _, _ = merge_router_keys({}, ROUTER_MAP)
        assert "NON_INTERACTIVE_MODE" not in cfg["Router"]


class TestProbeService:
    def test_returns_false_when_nothing_listening(self) -> None:
        # Port 3456 is very unlikely to be open in CI
        result = probe_service(port=3456, timeout=0.1)
        # We can't assert False because in theory something could listen there,
        # but we verify the function doesn't crash and returns a bool.
        assert isinstance(result, bool)

    def test_returns_true_with_real_listener(self, tmp_path: Path) -> None:
        import socket
        import threading
        # Spin up a real listener on a random port and probe it
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        _, port = server.getsockname()

        def _accept_and_close() -> None:
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                pass
            finally:
                server.close()

        t = threading.Thread(target=_accept_and_close, daemon=True)
        t.start()
        try:
            assert probe_service(port=port, timeout=1.0) is True
        finally:
            t.join(timeout=2)


class TestSeedModelsFile:
    def test_seeds_if_absent(self, tmp_path: Path) -> None:
        path = tmp_path / ".config" / "quoin" / "models.json"
        result = seed_models_file_if_absent(path, DEFAULT_MODELS)
        assert result is True
        assert json.loads(path.read_text()) == DEFAULT_MODELS

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        path = tmp_path / ".config" / "quoin" / "models.json"
        path.parent.mkdir(parents=True)
        user_data = {"haiku": "some/other-model"}
        path.write_text(json.dumps(user_data), encoding="utf-8")
        result = seed_models_file_if_absent(path, DEFAULT_MODELS)
        assert result is False
        assert json.loads(path.read_text()) == user_data  # untouched


# ── Integration tests for _cmd_router_setup ────────────────────────────────────

class TestCmdRouterSetup:
    """End-to-end handler tests — no real npm, no real ccr binary."""

    def _run_setup(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        node_present: bool = True,
        node_major: int | None = None,
        ccr_initially_present: bool = False,
        install_rc: int = 0,
        api_key: str = "sk-or-SENTINEL-KEY",
        dry_run: bool = False,
        existing_cfg: dict | None = None,
    ) -> int:
        # Patch injectable seams
        monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
        monkeypatch.setattr("quoin.router._node_present", lambda: node_present)
        monkeypatch.setattr("quoin.router._node_major", lambda: node_major)
        # Gate detect_ccr to "unknown" so the install-branch decision comes
        # from the patched shutil.which() below (belt-and-braces over the
        # conftest npm-isolation flag; see T-06 group (g)).
        monkeypatch.setattr(
            "quoin.router.detect_ccr",
            lambda home=None, npm_major=None: CcrVersion(0, None, "none"),
        )
        # This host has a real global `ccr` on PATH (claude-code-router 2.0.0
        # via Homebrew npm), so the unknown-major fallback's
        # `bool(shutil.which("ccr"))` half would otherwise silently override
        # the `ccr_initially_present` fixture below on every developer
        # machine that has CCR installed. Neutralize it so the presence
        # signal rests solely on `ccr_initially_present`, matching every
        # test's actual intent. None of the callers of this helper exercise
        # the fresh-install-succeeds path, so a static (not post-install
        # stateful) fake is sufficient here.
        monkeypatch.setattr(
            "quoin.router.shutil.which",
            lambda cmd: "/usr/bin/ccr" if (cmd == "ccr" and ccr_initially_present) else None,
        )
        monkeypatch.setattr("quoin.router._install_ccr", lambda: install_rc)

        # Optionally pre-seed a config
        config_path = ccr_config_path(home=tmp_path)
        if existing_cfg is not None:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            write_config(config_path, existing_cfg)

        args = _make_args(dry_run=dry_run, home=tmp_path)
        return _cmd_router_setup(args)

    def test_happy_path_creates_config(self, monkeypatch, tmp_path: Path) -> None:
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True)
        assert rc == 0
        config_path = ccr_config_path(home=tmp_path)
        assert config_path.exists()
        cfg = json.loads(config_path.read_text())
        assert any(p["name"] == "openrouter" for p in cfg["Providers"])
        assert "default" in cfg["Router"]
        assert "NON_INTERACTIVE_MODE" not in cfg.get("Router", {})

    def test_node_absent_returns_1_writes_nothing(self, monkeypatch, tmp_path: Path, capsys) -> None:
        rc = self._run_setup(monkeypatch, tmp_path, node_present=False)
        assert rc == 1
        assert not ccr_config_path(home=tmp_path).exists()
        captured = capsys.readouterr()
        assert_no_secret_in(captured.out, "sk-or-SENTINEL-KEY")
        assert_no_secret_in(captured.err, "sk-or-SENTINEL-KEY")

    def test_missing_key_returns_1_writes_nothing(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )
        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        assert rc == 1
        assert not ccr_config_path(home=tmp_path).exists()

    def test_install_failure_returns_nonzero_writes_nothing(self, monkeypatch, tmp_path: Path) -> None:
        """Install-failure propagation is a property of the install path
        itself, independent of whether a v3 writer exists — bypass the
        pre-install v3 refusal (which would otherwise return 0 before
        `_install_ccr()` is ever reached) so this actually exercises it."""
        monkeypatch.setattr("quoin.router._HAS_V3_WRITER", True)
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=False, install_rc=1)
        assert rc != 0
        assert not ccr_config_path(home=tmp_path).exists()

    def test_dry_run_writes_nothing(self, monkeypatch, tmp_path: Path) -> None:
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True, dry_run=True)
        assert rc == 0
        assert not ccr_config_path(home=tmp_path).exists()

    def test_return_code_is_int_not_systemexit(self, monkeypatch, tmp_path: Path) -> None:
        # D-07: handler must return int, never call sys.exit
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True)
        assert isinstance(rc, int)

    def test_idempotent_skips_install_on_rerun(self, monkeypatch, tmp_path: Path) -> None:
        """D-06: probe-first — _install_ccr must NOT be called when CCR is already present."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        # The unknown-major fallback rests on PATH alone; supply the
        # presence signal explicitly so the test is deterministic on a
        # host with no real `ccr` on PATH.
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)
        _cmd_router_setup(args)  # second run
        assert install_call_count["n"] == 0, "_install_ccr should not be called when CCR already present"

    # ── T-06(e): Node gate (AC-13, user-visible half) ──────────────────────────

    def test_node_below_min_does_not_install(self, monkeypatch, tmp_path: Path, capsys) -> None:
        install_call_count = {"n": 0}
        sentinel = "sk-or-SENTINEL"

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._node_major", lambda: 21)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)

        assert rc != 0
        assert install_call_count["n"] == 0
        captured = capsys.readouterr()
        assert_no_secret_in(captured.out, sentinel)
        assert_no_secret_in(captured.err, sentinel)

    def test_node_below_min_names_required_version(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._node_major", lambda: 21)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)
        captured = capsys.readouterr()
        assert "22" in captured.err

    def test_node_at_min_installs(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Node exactly at MIN_NODE_MAJOR (22) must not trip the version
        gate. The property this pins is "the handler proceeds past the
        Node check", not "install runs" — with no v3 writer available yet,
        the handler now refuses before ever spawning install (the hoisted
        v3 refusal, a separate property covered elsewhere), so asserting an
        install call here would pin that unrelated behaviour instead of the
        one this test is named for."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._node_major", lambda: 22)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert "requires Node" not in captured.err
        assert install_call_count["n"] == 0
        assert rc == 0

    # ── T-06(f): reinstall-skip (AC-14) and the `unknown` fallback ─────────────

    def test_detected_v2_skips_install(self, monkeypatch, tmp_path: Path) -> None:
        """The real AC-14 evidence: skip rests on the store plus a live
        presence signal, never on a version query."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".claude-code-router" / "config.json").write_text(
            "{}", encoding="utf-8"
        )

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)

        assert install_call_count["n"] == 0
        assert rc == 0

    def test_detected_v3_skips_install(self, monkeypatch, tmp_path: Path) -> None:
        """Stable half only — no return-code or message assertion, so this
        survives the sibling stage's supersession of the v3 refusal."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = store_dir / "config.sqlite"
        sqlite_path.write_bytes(b"")
        before = sqlite_path.read_bytes()

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        assert install_call_count["n"] == 0
        assert sqlite_path.read_bytes() == before

    def test_npm_presence_signal_skips_install_without_which(self, monkeypatch, tmp_path: Path) -> None:
        """The npm half of the presence signal (`_npm_major() is not None`
        when `which` is absent) is real production code, but the
        conftest-wide npm isolation flag meant no test exercised it — stub
        the npm seam to a readable major with `which` returning None and
        pin that the install is still skipped."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr("quoin.router._npm_major", lambda: 2)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)

        assert install_call_count["n"] == 0
        assert rc == 0

    def test_config_json_present_without_binary_installs(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A leftover config.json is quoin's own artifact and must not stand
        in for a live package. With neither `ccr` nor a readable npm
        package present, presence is False and — since quoin has no v3
        writer yet — the handler now refuses before ever spawning install,
        rather than installing and then refusing (the wasted-install
        this hoist avoids). Only the seams are stubbed here — the detector
        itself is never monkeypatched, so this exercises the real
        classification path."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / ".claude-code-router" / "config.json"
        config_path.write_text("{}", encoding="utf-8")

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert install_call_count["n"] == 0
        assert rc == 0
        assert "quoin's v3 configuration writer is not available yet" in captured.out
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert_no_secret_in(config_path.read_text(encoding="utf-8"), "sk-or-SENTINEL")

    def test_config_sqlite_present_without_binary_reaches_not_on_path(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Same fix, v3-shaped store: a `config.sqlite` left behind by an
        uninstalled package must not skip the install, and (once a v3
        writer exists to make the install worth attempting) must reach the
        pre-existing 'not on PATH' diagnostic when the install itself still
        cannot put `ccr` on PATH — the merge-base return-1 path this
        branch's store-based skip had made unreachable for this case.
        `_HAS_V3_WRITER` is bypassed here because that is exactly the
        condition under which this diagnostic runs at all; the no-writer
        case (refuse before installing) is covered elsewhere."""
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.sqlite").write_bytes(b"")

        monkeypatch.setattr("quoin.router._HAS_V3_WRITER", True)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 1
        assert "not on PATH" in captured.err

    # ── Stale config.json guard (refuses before writing beside a newer npm package) ──

    def test_json_store_with_npm_v3_or_v4_refuses_writes_nothing(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The primary migration path: an existing quoin user upgrades CCR
        (npm now carries a v3+ package) and re-runs setup with their old
        config.json still on disk. detect_ccr's json branch never consults
        npm, so without the stale-store guard this would classify as v2 and
        write the API key into a file v3 never reads. Exercised for both a
        pinned-major package (3) and a not-yet-recognised one (4) — the
        guard must catch both, across the four machine cells this covers
        (npm 3 and npm 4, `ccr` on PATH and absent)."""
        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / ".claude-code-router" / "config.json"

        for npm_major in (3, 4):
            for ccr_on_path in (False, True):
                config_path.write_text("{}", encoding="utf-8")
                monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
                monkeypatch.setattr("quoin.router._node_present", lambda: True)
                monkeypatch.setattr(
                    "quoin.router._install_ccr",
                    lambda: (_ for _ in ()).throw(AssertionError("must not install")),
                )
                monkeypatch.setattr("quoin.router._npm_major", lambda m=npm_major: m)
                monkeypatch.setattr(
                    "quoin.router.shutil.which",
                    lambda cmd, p=ccr_on_path: "/usr/bin/ccr" if (cmd == "ccr" and p) else None,
                )

                args = _make_args(home=tmp_path)
                rc = _cmd_router_setup(args)
                captured = capsys.readouterr()

                assert rc == 0, (npm_major, ccr_on_path)
                assert "quoin's v3 configuration writer is not available yet" in captured.out
                assert config_path.read_text(encoding="utf-8") == "{}"
                assert_no_secret_in(captured.out, "sk-or-SENTINEL")
                assert_no_secret_in(config_path.read_text(encoding="utf-8"), "sk-or-SENTINEL")

    def test_json_store_with_npm_v2_still_writes(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Regression guard for the stale-store guard's threshold: a
        genuine v2 machine (config.json, npm reporting major 2) must not be
        caught by the new >=3 check — the v2 write path must not regress."""
        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".claude-code-router" / "config.json").write_text("{}", encoding="utf-8")

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr(
            "quoin.router._install_ccr",
            lambda: (_ for _ in ()).throw(AssertionError("must not install")),
        )
        monkeypatch.setattr("quoin.router._npm_major", lambda: 2)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)

        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert any(p["name"] == "openrouter" for p in cfg["Providers"])

    # ── npm-capped detection at handler level ───────────────────────────────────

    def test_capped_npm_with_sqlite_store_refuses_without_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A `config.sqlite` store beside an npm package newer than quoin
        recognises (`store:sqlite-npm-capped`) must be treated as installed
        — never reinstalling the older pinned version over it — and must
        refuse rather than write a v2 config.json beside the sqlite store,
        with or without `ccr` on PATH."""
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = store_dir / "config.sqlite"
        sqlite_path.write_bytes(b"")
        before = sqlite_path.read_bytes()

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr(
            "quoin.router._install_ccr",
            lambda: (_ for _ in ()).throw(AssertionError("must not install")),
        )
        monkeypatch.setattr("quoin.router._npm_major", lambda: 4)  # beyond CCR_KNOWN_MAJOR_MAX
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)  # `ccr` not on PATH

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 0
        assert "quoin's v3 configuration writer is not available yet" in captured.out
        assert sqlite_path.read_bytes() == before
        assert not (store_dir / "config.json").exists()
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_capped_npm_with_no_store_refuses_without_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The no-store sibling of the sqlite case above: a globally
        installed npm package newer than quoin recognises, with no CCR
        store on disk at all, must also refuse rather than scaffold a
        fresh `config.json` and write the API key into it. Swept across
        both `ccr` PATH states, since the bug this pins reproduced in
        both: a capped detection's major is the deliberate `0` and its
        store is `None`, satisfying neither half of a refusal keyed on
        `major == 3 or store == "sqlite"` alone."""
        for ccr_on_path in (False, True):
            home = tmp_path / f"home-{ccr_on_path}"
            monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
            monkeypatch.setattr("quoin.router._node_present", lambda: True)
            monkeypatch.setattr(
                "quoin.router._install_ccr",
                lambda: (_ for _ in ()).throw(AssertionError("must not install")),
            )
            monkeypatch.setattr("quoin.router._npm_major", lambda: 4)  # beyond CCR_KNOWN_MAJOR_MAX
            monkeypatch.setattr(
                "quoin.router.shutil.which",
                lambda cmd, p=ccr_on_path: "/usr/bin/ccr" if (cmd == "ccr" and p) else None,
            )

            args = _make_args(home=home)
            rc = _cmd_router_setup(args)
            captured = capsys.readouterr()

            assert rc == 0, ccr_on_path
            assert "quoin's v3 configuration writer is not available yet" in captured.out
            assert "already installed" not in captured.out
            assert "installed successfully" not in captured.out
            assert not (home / ".claude-code-router" / "config.json").exists()
            assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_json_store_refusal_survives_known_major_ceiling_bump(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The stale-config.json guard must key on the invariant it means
        to express (a `config.json` store is only ever live for npm major
        2), not on `CCR_KNOWN_MAJOR_MAX` — bumping that ceiling, which
        happens the moment quoin learns to recognise a new CCR major, must
        not silently re-admit an already-stale `config.json` write for a
        machine running the major the bump just added."""
        monkeypatch.setattr("quoin.router.CCR_KNOWN_MAJOR_MAX", 4)
        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / ".claude-code-router" / "config.json"
        config_path.write_text("{}", encoding="utf-8")

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr(
            "quoin.router._install_ccr",
            lambda: (_ for _ in ()).throw(AssertionError("must not install")),
        )
        monkeypatch.setattr("quoin.router._npm_major", lambda: 3)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 0
        assert "quoin's v3 configuration writer is not available yet" in captured.out
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_has_v3_writer_flag_reopens_every_refusal_site(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """`_HAS_V3_WRITER` must gate all three refusal sites, not only the
        pre-install one, so flipping it is genuinely the single switch a
        future writer needs: the stale-config.json guard (a) and the
        installed-and-v3-shaped guard (b) must both stop refusing once a
        writer is available, the same way the pre-install hoist already
        does."""
        monkeypatch.setattr("quoin.router._HAS_V3_WRITER", True)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router._npm_major", lambda: 3)

        # (a) stale-config.json guard: config.json + npm major 3.
        home_a = tmp_path / "a"
        (home_a / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (home_a / ".claude-code-router" / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        rc = _cmd_router_setup(_make_args(home=home_a))
        captured = capsys.readouterr()
        assert rc == 0
        assert "quoin's v3 configuration writer is not available yet" not in captured.out

        # (b) installed-and-v3-shaped guard: config.sqlite + `ccr` on PATH.
        home_b = tmp_path / "b"
        (home_b / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (home_b / ".claude-code-router" / "config.sqlite").write_bytes(b"")
        monkeypatch.setattr(
            "quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None
        )

        rc = _cmd_router_setup(_make_args(home=home_b))
        captured = capsys.readouterr()
        assert rc == 0
        assert "quoin's v3 configuration writer is not available yet" not in captured.out

    def test_npm_spawn_census_per_machine_state(self, monkeypatch, tmp_path: Path) -> None:
        """Regression guard for the npm-read threading: pins the number of
        `_npm_major()` reads per machine state, so a future edit that
        reintroduces a duplicate spawn (or drops the thread-through
        entirely) fails here rather than only being caught by inspection."""
        call_count = {"n": 0}

        def _counting_npm_major():
            call_count["n"] += 1
            return 2

        monkeypatch.setattr("quoin.router._npm_major", _counting_npm_major)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: (_ for _ in ()).throw(
            AssertionError("must not install when already present")
        ))

        # (a) a lone config.json with `ccr` live on PATH: the stale-store
        # guard now reads npm once regardless of `which`, because the bug
        # it closes (a newer npm package beside a stale config.json) fires
        # whether or not `ccr` resolves on PATH — that same read is then
        # threaded into the presence check below it, so it costs one spawn,
        # not two.
        call_count["n"] = 0
        (tmp_path / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".claude-code-router" / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        rc = _cmd_router_setup(_make_args(home=tmp_path))
        assert rc == 0
        assert call_count["n"] == 1

        # (b) a lone config.sqlite with `ccr` live on PATH: the sqlite
        # branch reads npm once (to resolve the residual-rule cap), and
        # that same read is threaded into the presence check — one spawn.
        call_count["n"] = 0
        tmp_path2 = tmp_path.parent / (tmp_path.name + "-b")
        (tmp_path2 / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (tmp_path2 / ".claude-code-router" / "config.sqlite").write_bytes(b"")
        rc = _cmd_router_setup(_make_args(home=tmp_path2))
        assert rc == 0
        assert call_count["n"] == 1

    def test_v3_refusal_is_s1_local_contract(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """S-1-local contract, isolated from the stable skip test above so the
        sibling stage that adds the v3 writer can delete just this test when
        it replaces the early return with the store-emission path."""
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.sqlite").write_bytes(b"")

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 0
        assert "Nothing was changed" in captured.out

    def test_unknown_major_with_which_preserves_skip(self, monkeypatch, tmp_path: Path) -> None:
        """R-03: on the unknown-major fallback, `which` is the load-bearing
        half that preserves idempotence on a machine class where a version
        query is known-inert (`ccr -v` exits 1 on a healthy v3 install —
        which is exactly why this path never uses one)."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        assert install_call_count["n"] == 0

    def test_idempotent_config_no_duplicates(self, monkeypatch, tmp_path: Path) -> None:
        """Second run: backup created, no duplicate providers/keys, models.json untouched."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        # Mutate models.json to simulate user edit
        mp = quoin_models_path(home=tmp_path)
        mp.write_text('{"haiku":"user-edited-model"}', encoding="utf-8")

        _cmd_router_setup(args)

        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        # No duplicate openrouter providers
        openrouter_entries = [p for p in cfg["Providers"] if p.get("name") == "openrouter"]
        assert len(openrouter_entries) == 1

        # models.json user edit preserved
        assert json.loads(mp.read_text()) == {"haiku": "user-edited-model"}

        # The second setup run must APPLY the user edit, not silently revert it
        # to the compiled-in default (IVG-243 regression guard). Fails on
        # unfixed main because Step 4 used the frozen DEFAULT_MODELS/ROUTER_MAP
        # instead of reading models.json.
        assert cfg["Router"]["background"] == "openrouter,user-edited-model"
        openrouter_models = openrouter_entries[0].get("models", [])
        assert "user-edited-model" in openrouter_models

    # ── Fresh-machine v3 refusal: hoisted before install, never a version-query ──

    def test_fresh_install_with_v3_detected_refuses_before_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A fresh machine with no v3 writer available must not fall through
        to a v2 config write, and — per the round-4 hoist — must not run
        the install at all: quoin already knows installing would pin
        CCR_PINNED_VERSION (major 3) and that it has no writer for that
        major, so it refuses before spawning npm rather than installing
        and then refusing."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr("quoin.router._npm_major", lambda: None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert install_call_count["n"] == 0
        assert rc == 0
        assert "would pin" in captured.out
        assert "not available yet" in captured.out
        assert "Nothing was changed" not in captured.out
        assert not ccr_config_path(home=tmp_path).exists()

    def test_fresh_install_with_no_store_real_detection_refuses_before_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Same guarantee through the real (unstubbed) detector: an empty
        home with no readable npm package resolves to no store at all, and
        the hoisted refusal fires before `_install_ccr()` is ever called —
        nothing is written to the CCR config."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr("quoin.router._npm_major", lambda: None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert install_call_count["n"] == 0
        assert rc == 0
        assert "would pin" in captured.out
        assert "not available yet" in captured.out
        assert not ccr_config_path(home=tmp_path).exists()

    def test_setup_never_invokes_ccr_version_query(self, monkeypatch, tmp_path: Path) -> None:
        """Regression guard: nothing on the setup path may shell out to
        `ccr -v` / `ccr version` — both exit 1 on a healthy v3.1.0 install
        and migrate-and-delete the user's v2 config.json."""
        recorded_argv: list[list[str]] = []

        class _Result:
            def __init__(self, returncode: int = 0, stdout: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout

        def _spy_run(argv, *a, **kw):
            recorded_argv.append(list(argv))
            return _Result(returncode=0, stdout="/fake/prefix\n")

        monkeypatch.setattr("quoin.router.subprocess.run", _spy_run)
        monkeypatch.setattr("quoin.router._npm_query_enabled", True)
        monkeypatch.setattr("quoin.router._ccr_package_json", lambda prefix: None)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._node_major", lambda: 22)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        assert recorded_argv, "expected the install/npm-probe path to spawn at least once"
        for argv in recorded_argv:
            assert not (
                argv and argv[0] == "ccr" and any(a in ("-v", "version") for a in argv[1:])
            ), f"ccr invoked with a version query: {argv}"

    def test_dry_run_before_install_never_calls_install(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """--dry-run must short-circuit before any install is attempted —
        not just before the config write — so it can never spawn a real
        npm install, let alone delete or mutate anything."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr("quoin.router._npm_major", lambda: None)

        args = _make_args(home=tmp_path, dry_run=True)
        rc = _cmd_router_setup(args)

        assert install_call_count["n"] == 0
        assert rc == 0
        assert not ccr_config_path(home=tmp_path).exists()

    def test_secret_not_in_stdout(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """R-03: OPENROUTER_API_KEY must never appear in stdout."""
        sentinel = "sk-or-SENTINEL-KEY-DO-NOT-PRINT"
        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        captured = capsys.readouterr()
        assert_no_secret_in(captured.out, sentinel)
        assert_no_secret_in(captured.err, sentinel)

    def test_secret_not_in_stdout_on_v3_refusal(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """A detected-v3 machine returns before the key is read at all; pin
        that the refusal path carries no secret now, ahead of the sibling
        stage's v3 writer that will actually read the key on this branch."""
        sentinel = "sk-or-SENTINEL-KEY-DO-NOT-PRINT"
        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(3, "sqlite", "store:sqlite")
        )

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 0
        assert "Nothing was changed" in captured.out
        assert_no_secret_in(captured.out, sentinel)
        assert_no_secret_in(captured.err, sentinel)

    def test_secret_not_in_models_file(self, monkeypatch, tmp_path: Path) -> None:
        """R-03: models.json must contain slugs only, never the API key."""
        sentinel = "sk-or-SENTINEL-KEY-MODELS-FILE"
        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        args = _make_args(home=tmp_path)
        _cmd_router_setup(args)

        mp = quoin_models_path(home=tmp_path)
        assert mp.exists()
        assert_no_secret_in(mp.read_text(), sentinel)

    def test_backup_created_on_existing_config(self, monkeypatch, tmp_path: Path) -> None:
        """R-04: existing config gets a .bak-<ts> backup before writing."""
        existing = {"Providers": [{"name": "other", "models": []}], "Router": {}}
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True, existing_cfg=existing)
        assert rc == 0
        cfg_dir = ccr_config_path(home=tmp_path).parent
        backups = list(cfg_dir.glob("config.json.bak-*"))
        assert len(backups) == 1

    def test_foreign_provider_preserved(self, monkeypatch, tmp_path: Path) -> None:
        """R-04 / D-05: existing non-openrouter providers must be preserved."""
        existing = {
            "Providers": [{"name": "other-provider", "api_key": "x", "models": ["m"]}],
            "Router": {},
        }
        self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True, existing_cfg=existing)
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        names = [p["name"] for p in cfg["Providers"]]
        assert "other-provider" in names
        assert "openrouter" in names

    def test_no_non_interactive_mode_in_config(self, monkeypatch, tmp_path: Path) -> None:
        """PTY constraint: NON_INTERACTIVE_MODE must never appear in the written config."""
        self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True)
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert "NON_INTERACTIVE_MODE" not in cfg
        assert "NON_INTERACTIVE_MODE" not in cfg.get("Router", {})


# ── quoin router setup honors models.json (IVG-243 regression coverage) ────────

class TestSetupHonorsModelsJson:
    """T-03: `quoin router setup` must read the effective (models.json-merged)
    table instead of the frozen DEFAULT_MODELS/ROUTER_MAP constants (IVG-243)."""

    def _run_setup(self, monkeypatch, tmp_path: Path, *, dry_run: bool = False) -> int:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )
        args = _make_args(dry_run=dry_run, home=tmp_path)
        return _cmd_router_setup(args)

    def test_full_opus_override_applied(self, monkeypatch, tmp_path: Path) -> None:
        """(a) Full opus override: Router.think and provider models reflect it.

        Deviation from the plan's literal T-01 ack: the ack used
        `z-ai/glm-5.2` as the override value, but T-02 (same task) bumps
        DEFAULT_MODELS['opus'] to that same slug, which would make the
        override indistinguishable from the default. A distinct
        non-default slug is used here so the assertion actually proves
        the override mechanism, not coincidental equality with default.
        """
        mp = quoin_models_path(home=tmp_path)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"opus": "custom-vendor/opus-override"}), encoding="utf-8")

        rc = self._run_setup(monkeypatch, tmp_path)
        assert rc == 0

        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["think"] == "openrouter,custom-vendor/opus-override"
        openrouter_entry = next(p for p in cfg["Providers"] if p["name"] == "openrouter")
        assert "custom-vendor/opus-override" in openrouter_entry["models"]
        assert DEFAULT_MODELS["opus"] not in openrouter_entry["models"]

    def test_partial_override_other_tiers_default(self, monkeypatch, tmp_path: Path) -> None:
        """(b) Partial override: untouched tiers still resolve to their default slug."""
        mp = quoin_models_path(home=tmp_path)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"haiku": "custom-vendor/haiku-override"}), encoding="utf-8")

        rc = self._run_setup(monkeypatch, tmp_path)
        assert rc == 0

        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["background"] == "openrouter,custom-vendor/haiku-override"
        assert cfg["Router"]["think"] == f"openrouter,{DEFAULT_MODELS['opus']}"
        assert cfg["Router"]["default"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"
        assert cfg["Router"]["longContext"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"

    def test_malformed_models_json_falls_back_to_defaults(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """(c) Malformed models.json: setup falls back to defaults, returns 0, warns (fail-open)."""
        mp = quoin_models_path(home=tmp_path)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text("{not valid json", encoding="utf-8")

        rc = self._run_setup(monkeypatch, tmp_path)
        assert rc == 0

        captured = capsys.readouterr()
        assert "models.json parse error" in captured.err

        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["think"] == f"openrouter,{DEFAULT_MODELS['opus']}"
        assert cfg["Router"]["background"] == f"openrouter,{DEFAULT_MODELS['haiku']}"
        assert cfg["Router"]["default"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"

    def test_no_models_json_matches_default_behavior(self, monkeypatch, tmp_path: Path) -> None:
        """(d) No models.json: byte-identical to today — defaults written, file seeded."""
        mp = quoin_models_path(home=tmp_path)
        assert not mp.exists()

        rc = self._run_setup(monkeypatch, tmp_path)
        assert rc == 0

        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["think"] == f"openrouter,{DEFAULT_MODELS['opus']}"
        assert cfg["Router"]["background"] == f"openrouter,{DEFAULT_MODELS['haiku']}"
        assert cfg["Router"]["default"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"
        assert mp.exists()
        assert json.loads(mp.read_text()) == DEFAULT_MODELS


# ── Import-order regression (D-01 pin) ──────────────────────────────────────────

class TestImportOrderRegression:
    def test_router_module_imports_standalone(self) -> None:
        """D-01: quoin.router and quoin.models must import cleanly in either
        order, in a fresh subprocess with a clean sys.modules. A module-level
        back-import in either file raises a circular ImportError; this pins
        the function-local-import fix so a future refactor cannot silently
        reintroduce the cycle."""
        src_path = str(REPO_ROOT / "src")
        for first_module in ("quoin.router", "quoin.models"):
            script = (
                f"import sys; sys.path.insert(0, {src_path!r}); "
                f"import importlib; importlib.import_module({first_module!r})"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"import_module({first_module!r}) failed in a clean subprocess:\n"
                f"{result.stderr}"
            )


# ── Integration tests for _cmd_router_status ──────────────────────────────────

class TestCmdRouterStatus:
    def _run_status(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        ccr_installed: bool = False,
        cfg_present: bool = False,
        live: bool = False,
        key_set: bool = False,
    ) -> tuple[int, str]:
        import io
        monkeypatch.setattr(
            "quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if (cmd == "ccr" and ccr_installed) else None
        )
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: live)
        monkeypatch.setattr("quoin.router.probe_service", lambda **kw: live)
        # Forward-looking gate: _cmd_router_status does not call detect_ccr in
        # this stage, but this pins the class against host store state before
        # a later stage wires detection into status.
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )

        if key_set:
            monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        else:
            monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        config_path = ccr_config_path(home=tmp_path)
        if cfg_present:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            write_config(config_path, {"Providers": [], "Router": {}})

        args = argparse.Namespace(_home_override=tmp_path)
        buf = io.StringIO()

        import builtins
        orig_print = builtins.print

        def capturing_print(*a, **kw):
            if kw.get("file") in (None, sys.stdout):
                buf.write(" ".join(str(x) for x in a) + "\n")
            else:
                orig_print(*a, **kw)

        with patch("builtins.print", side_effect=capturing_print):
            rc = _cmd_router_status(args)
        return rc, buf.getvalue()

    def test_returns_0_always(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run_status(monkeypatch, tmp_path)
        assert rc == 0

    def test_return_code_is_int(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run_status(monkeypatch, tmp_path)
        assert isinstance(rc, int)

    def test_no_config_reports_native(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run_status(monkeypatch, tmp_path, live=False, cfg_present=False)
        assert "native" in out.lower()

    def test_config_present_but_service_down_reports_native(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run_status(monkeypatch, tmp_path, cfg_present=True, live=False)
        assert "native" in out.lower()
        assert "open via" not in out.lower()

    def test_live_and_config_reports_open(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run_status(monkeypatch, tmp_path, cfg_present=True, live=True)
        assert "open" in out.lower()

    def test_key_shown_as_set_unset_never_value(self, monkeypatch, tmp_path: Path, capsys) -> None:
        sentinel = "sk-or-SECRET-STATUS-LEAK"
        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        monkeypatch.setattr("quoin.router.probe_service", lambda **kw: False)
        monkeypatch.setattr(
            "quoin.router.detect_ccr", lambda home=None, **kw: CcrVersion(0, None, "none")
        )
        args = argparse.Namespace(_home_override=tmp_path)
        _cmd_router_status(args)
        out = capsys.readouterr().out
        assert_no_secret_in(out, sentinel)


# ── Opt-in isolation test ──────────────────────────────────────────────────────

class TestOptInIsolation:
    def test_install_path_does_not_import_router(self) -> None:
        """R-11 / D-01: quoin.installer must not import quoin.router or quoin.ccr_config."""
        import importlib
        import importlib.util

        # Load installer source without executing it
        spec = importlib.util.spec_from_file_location(
            "quoin.installer",
            str(REPO_ROOT / "src" / "quoin" / "installer.py"),
        )
        assert spec is not None
        source = Path(REPO_ROOT / "src" / "quoin" / "installer.py").read_text()
        assert "router" not in source.split("import")[-1] or "from quoin import router" not in source
        assert "ccr_config" not in source

    def test_cli_top_level_does_not_import_router(self) -> None:
        """Lazy import: router/ccr_config must not appear as top-level imports in cli.py."""
        source = Path(REPO_ROOT / "src" / "quoin" / "cli.py").read_text()
        # Top-level imports are before the first `def ` line
        top_level = source.split("def _cmd_")[0]
        assert "from quoin import router" not in top_level
        assert "from quoin import ccr_config" not in top_level
        assert "from quoin.router" not in top_level
        assert "from quoin.ccr_config" not in top_level
