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
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.ccr_config import (  # noqa: E402
    CcrConfigError,
    UNKNOWN_LAUNCH_NOTE,
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
from conftest import make_v3_store, store_value_snapshot  # noqa: E402

from quoin.ccr_store import read_v3_config  # noqa: E402
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

    def test_merge_router_keys_message_split_preserved(self) -> None:
        # Pins owned_key_is_writable's `was_absent` reconstruction: a fresh
        # config yields "set to" and a pre-existing openrouter key yields
        # "updated to", the one distinction the two AC-47 tests don't assert.
        cfg, changes, _ = merge_router_keys({}, ROUTER_MAP)
        assert changes
        assert all("set to" in c for c in changes)

        cfg0 = {"Router": {"default": "openrouter,old-model"}}
        cfg, changes, warnings = merge_router_keys(cfg0, ROUTER_MAP)
        default_change = next(c for c in changes if c.startswith("Router.default"))
        assert "updated to" in default_change
        assert not any("default" in w for w in warnings)


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

    def test_setup_print_names_no_v2_command_on_unknown_version(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # The launch print receives a real detected major. On a machine with
        # no signal at all that major is 0, so the unknown-branch note is
        # what renders — no command is named either way.
        rc = self._run_setup(monkeypatch, tmp_path, ccr_initially_present=True)
        assert rc == 0
        captured = capsys.readouterr()
        assert "ccr code" not in captured.out
        assert "ccr default-claude-code" not in captured.out
        assert UNKNOWN_LAUNCH_NOTE.split(";")[0] in captured.out

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
        """Install-failure propagation: a no-signal machine routes to v2 and
        takes the install branch, so a failing `_install_ccr()` must surface
        its exit code and leave no config behind."""
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
        gate: the handler proceeds past the Node check and runs the install.
        `shutil.which` stays None afterwards, so the run ends at the
        not-on-PATH diagnostic — the install itself is what this pins."""
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
        assert install_call_count["n"] == 1
        assert rc == 1
        assert "not on PATH" in captured.err

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
        """A sqlite store plus `ccr` on PATH is a live v3 machine: the skip
        this test is named for still holds (`_install_ccr` is never called),
        and the run now merges into the store instead of declining."""
        install_call_count = {"n": 0}

        def _install():
            install_call_count["n"] += 1
            return 0

        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path = make_v3_store(store_dir, {})

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", _install)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)

        assert install_call_count["n"] == 0
        assert rc == 0
        cfg = read_v3_config(store_path).config
        assert any(p["name"] == "openrouter" for p in cfg["Providers"])
        assert not (store_dir / "config.json").exists()

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

    def test_config_json_present_without_binary_installs_then_not_on_path(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A leftover config.json is quoin's own artifact and must not stand
        in for a live package. With neither `ccr` nor a readable npm package
        present, presence is False, the install runs, and — because the stub
        cannot put `ccr` on PATH — the run ends at the not-on-PATH
        diagnostic. The leftover config.json must survive untouched, since
        the handler returns before the key is ever read. Only the seams are
        stubbed here — the detector itself is never monkeypatched, so this
        exercises the real classification path."""
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

        assert install_call_count["n"] == 1
        assert rc == 1
        assert "not on PATH" in captured.err
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert_no_secret_in(config_path.read_text(encoding="utf-8"), "sk-or-SENTINEL")

    def test_config_sqlite_present_without_binary_reaches_not_on_path(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Same fix, v3-shaped store: a `config.sqlite` left behind by an
        uninstalled package must not skip the install, and must reach the
        pre-existing 'not on PATH' diagnostic when the install itself still
        cannot put `ccr` on PATH — the merge-base return-1 path this
        branch's store-based skip had made unreachable for this case. The
        post-install re-dispatch is not reached: `shutil.which` is None for
        every command, so the PATH check returns first."""
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.sqlite").write_bytes(b"")

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

    def test_json_store_with_npm_v3_or_v4_declines_writes_nothing(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The primary migration path: an existing quoin user upgrades CCR
        (npm now carries a v3+ package) and re-runs setup with their old
        config.json still on disk. detect_ccr's json branch never consults
        npm, so without the store dispatch this would classify as v2 and
        write the API key into a file v3 never reads. npm 3 lands on the
        store-absent decline (v3 is installed but has made no store yet);
        npm 4 lands on the unknown-version decline. Both return 2 and write
        nothing, with `ccr` on PATH and absent alike."""
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

                assert rc == 2, (npm_major, ccr_on_path)
                assert "quoin declined to write to CCR" in captured.out
                assert config_path.read_text(encoding="utf-8") == "{}"
                assert_no_secret_in(captured.out, "sk-or-SENTINEL")
                assert_no_secret_in(config_path.read_text(encoding="utf-8"), "sk-or-SENTINEL")
                # The message must name the version it was actually
                # handed, not a hardcoded "3.x"/"v3" — a CCR 4 machine must
                # never be told it is on 3.
                if npm_major == 3:
                    # The store-absent decline prints no `Detected:` line;
                    # its major reaches the user through this sentence.
                    assert "CCR v3 is installed" in captured.out
                    assert "has not created its config store yet" in captured.out
                else:
                    assert "v4" in captured.out
                    assert "Detected:  v4 (store: json, via npm)" in captured.out
                    assert "3.x" not in captured.out

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

    def test_capped_npm_with_sqlite_store_declines_without_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A `config.sqlite` store beside an npm package newer than quoin
        recognises (`store:sqlite-npm-capped`) must be treated as installed
        — never reinstalling the older pinned version over it — and must
        decline rather than write anything into or beside the sqlite store.
        The store is seeded in WAL mode, the shape v3 actually ships, and
        the no-write oracle reads the stored value rather than the file's
        bytes: a committed WAL write is invisible in the main file."""
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = make_v3_store(store_dir, {"Providers": []}, wal=True)
        before = store_value_snapshot(sqlite_path)

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

        assert rc == 2
        assert "a version newer than quoin recognises" in captured.out
        assert "quoin declined to write to CCR" in captured.out
        assert store_value_snapshot(sqlite_path) == before
        assert not list(store_dir.glob("config.sqlite.value-bak-*.json"))
        assert not (store_dir / "config.json").exists()
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_capped_npm_with_no_store_declines_without_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The no-store sibling of the sqlite case above: a globally
        installed npm package newer than quoin recognises, with no CCR
        store on disk at all, must also decline rather than scaffold a
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

            assert rc == 2, ccr_on_path
            assert "a version newer than quoin recognises" in captured.out
            assert "quoin declined to write to CCR" in captured.out
            assert "already installed" not in captured.out
            assert "installed successfully" not in captured.out
            assert not (home / ".claude-code-router" / "config.json").exists()
            assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_json_store_decline_survives_known_major_ceiling_bump(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The stale-config.json rule must key on the invariant it means
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

        assert rc == 2
        assert "CCR v3 is installed" in captured.out
        assert "quoin declined to write to CCR" in captured.out
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

    def test_refusal_message_names_the_detected_version(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The decline text must be derived from the `CcrVersion` it was
        handed, not hardcoded to "3.x"/"v3". Four machines: a real v3
        (sqlite store), which now writes rather than declines; a real v4
        (stale config.json beside an npm 4 package); a sqlite store beside
        an npm 4 package, which carries the no-write oracle; and a capped
        cell with no store at all, whose major is the sentinel `0`. The
        capped cell must not claim "v3" either, even though that is the
        only major this stage is pinned to install."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr(
            "quoin.router._install_ccr",
            lambda: (_ for _ in ()).throw(AssertionError("must not install")),
        )

        # v3: config.sqlite store, npm major 3, `ccr` on PATH — there is no
        # decline here any more, so the assertion is on the write itself.
        home_v3 = tmp_path / "v3"
        store_dir_v3 = home_v3 / ".claude-code-router"
        store_dir_v3.mkdir(parents=True, exist_ok=True)
        store_v3 = make_v3_store(store_dir_v3, {})
        monkeypatch.setattr("quoin.router._npm_major", lambda: 3)
        monkeypatch.setattr(
            "quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None
        )
        rc = _cmd_router_setup(_make_args(home=home_v3))
        capsys.readouterr()
        assert rc == 0
        cfg_v3 = read_v3_config(store_v3).config
        assert any(p["name"] == "openrouter" for p in cfg_v3["Providers"])
        assert not (store_dir_v3 / "config.json").exists()

        # v4: stale config.json beside an npm 4 package (the primary
        # migration path, driven through the store dispatch).
        home_v4 = tmp_path / "v4"
        (home_v4 / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        (home_v4 / ".claude-code-router" / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr("quoin.router._npm_major", lambda: 4)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        rc = _cmd_router_setup(_make_args(home=home_v4))
        out = capsys.readouterr().out
        assert rc == 2
        assert "claude-code-router 4.x is installed" in out
        assert "Detected:  v4 (store: json, via npm)" in out
        assert "3.x" not in out

        # sqlite store beside an npm 4 package: the `Detected:` line a
        # sqlite machine still gets, and the no-write oracle for it.
        home_sqlite4 = tmp_path / "sqlite4"
        store_dir_s4 = home_sqlite4 / ".claude-code-router"
        store_dir_s4.mkdir(parents=True, exist_ok=True)
        store_s4 = make_v3_store(store_dir_s4, {"Providers": []}, wal=True)
        before_s4 = store_value_snapshot(store_s4)
        monkeypatch.setattr("quoin.router._npm_major", lambda: 4)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        rc = _cmd_router_setup(_make_args(home=home_sqlite4))
        out = capsys.readouterr().out
        assert rc == 2
        assert "Detected:  unrecognised (store: sqlite, via store:sqlite-npm-capped)" in out
        assert store_value_snapshot(store_s4) == before_s4
        assert not (store_dir_s4 / "config.json").exists()

        # Capped: no store at all, npm major beyond CCR_KNOWN_MAJOR_MAX,
        # `ccr` not on PATH — detected.major is the deliberate sentinel 0.
        home_capped = tmp_path / "capped"
        monkeypatch.setattr("quoin.router._npm_major", lambda: 5)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
        rc = _cmd_router_setup(_make_args(home=home_capped))
        out = capsys.readouterr().out
        assert rc == 2
        assert "a version newer than quoin recognises" in out
        assert "Detected:  unrecognised" in out
        # What must not appear is the capped major being *claimed* as a
        # detected version.
        assert "3.x is installed" not in out
        assert "Detected:  v0" not in out
        assert "Detected:  v3" not in out

    def test_every_former_flag_gated_site_now_dispatches(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The three machine shapes that a single capability flag used to
        decide between now each reach a named outcome: (a) a leftover
        config.json beside a v3 package and (c) a v3 package with no store
        at all both land on the store-absent decline, while (b) a live
        sqlite store is merged into. No config.json is ever created on any
        of the three."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._install_ccr", lambda: 0)
        monkeypatch.setattr("quoin.router._npm_major", lambda: 3)

        # (a) leftover config.json beside an npm 3 package.
        home_a = tmp_path / "a"
        (home_a / ".claude-code-router").mkdir(parents=True, exist_ok=True)
        config_a = home_a / ".claude-code-router" / "config.json"
        config_a.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        rc = _cmd_router_setup(_make_args(home=home_a))
        captured = capsys.readouterr()
        assert rc == 2
        assert "has not created its config store yet" in captured.out
        assert config_a.read_text(encoding="utf-8") == "{}"
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

        # (b) a live sqlite store with `ccr` on PATH: the write path. The
        # sidecar must carry the pre-write blob, not the merged one.
        home_b = tmp_path / "b"
        store_dir_b = home_b / ".claude-code-router"
        store_dir_b.mkdir(parents=True, exist_ok=True)
        seeded = {"Providers": [{"name": "keepme"}]}
        store_b = make_v3_store(store_dir_b, seeded, wal=True)
        monkeypatch.setattr(
            "quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None
        )

        rc = _cmd_router_setup(_make_args(home=home_b))
        captured = capsys.readouterr()
        assert rc == 0
        cfg_b = read_v3_config(store_b).config
        assert any(p["name"] == "openrouter" for p in cfg_b["Providers"])
        assert not (store_dir_b / "config.json").exists()
        sidecars = list(store_dir_b.glob("config.sqlite.value-bak-*.json"))
        assert len(sidecars) == 1
        assert json.loads(sidecars[0].read_text(encoding="utf-8")) == seeded
        assert_no_secret_in(captured.out, "sk-or-SENTINEL")

        # (c) npm 3 with no store on disk at all.
        home_c = tmp_path / "c"
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)

        rc = _cmd_router_setup(_make_args(home=home_c))
        captured = capsys.readouterr()
        assert rc == 2
        assert "has not created its config store yet" in captured.out
        assert not (home_c / ".claude-code-router" / "config.json").exists()

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

    def test_fresh_install_does_not_fall_through_to_v2_write(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A fresh machine installs the pinned package, then finds `ccr`
        still off PATH — the stubbed `shutil.which` returns None for every
        command — so the run ends at the not-on-PATH diagnostic before the
        post-install re-dispatch is reached. The load-bearing property is
        that no v2 config is written on the way out."""
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

        assert install_call_count["n"] == 1
        assert rc == 1
        assert "not on PATH" in captured.err
        assert not ccr_config_path(home=tmp_path).exists()

    def test_fresh_install_with_no_store_real_detection_writes_no_v2_config(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Same guarantee through the real (unstubbed) detector: an empty
        home with no readable npm package resolves to no store at all, the
        install runs, and the not-on-PATH check returns before anything is
        written to the CCR config."""
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

        assert install_call_count["n"] == 1
        assert rc == 1
        assert "not on PATH" in captured.err
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
        # npm resolves (so the npm-probe path actually spawns); ccr does not
        # (so the ccr-presence check takes the "absent" branch, same as
        # before F-08's shutil.which(npm)-then-spawn-resolved-path fix).
        monkeypatch.setattr(
            "quoin.router.shutil.which",
            lambda cmd: "/usr/local/bin/npm" if cmd == "npm" else None,
        )

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

    def test_secret_not_in_stdout_on_v3_write(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """The v3 write path reads the key and puts it in the store, and
        must never echo it. The sidecar holds pre-write bytes, so a key the
        store did not already carry must not appear there either."""
        sentinel = "sk-or-SENTINEL-KEY-DO-NOT-PRINT"
        store_dir = tmp_path / ".claude-code-router"
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path = make_v3_store(store_dir, {})

        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None)

        args = _make_args(home=tmp_path)
        rc = _cmd_router_setup(args)
        captured = capsys.readouterr()

        assert rc == 0
        assert_no_secret_in(captured.out, sentinel)
        assert_no_secret_in(captured.err, sentinel)
        cfg = read_v3_config(store_path).config
        provider = next(p for p in cfg["Providers"] if p["name"] == "openrouter")
        assert provider["api_key"] == sentinel
        for sidecar in store_dir.glob("config.sqlite.value-bak-*.json"):
            assert_no_secret_in(sidecar.read_text(encoding="utf-8"), sentinel)

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

    def test_ccr_store_imports_first_without_a_cycle(self) -> None:
        """router -> ccr_store -> router must not close into a cycle."""
        src_path = str(REPO_ROOT / "src")
        script = (
            f"import sys; sys.path.insert(0, {src_path!r}); "
            "import quoin.ccr_store; import quoin.router; import quoin.models; "
            "assert quoin.router.ccr_store is quoin.ccr_store"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_ccr_store_imports_only_the_standard_library(self) -> None:
        """AC-25: the store module stays free of quoin's own install path."""
        source = (REPO_ROOT / "src" / "quoin" / "ccr_store.py").read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "quoin" in stripped:
                raise AssertionError(f"non-stdlib import in ccr_store.py: {stripped}")
            if stripped.startswith(("import ", "from ")) and "installer" in stripped:
                raise AssertionError(f"install-path import in ccr_store.py: {stripped}")


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


# ── Production wiring of the three v3 writers ─────────────────────────────────

@pytest.mark.parametrize("writer", ["router setup", "models set", "models preset"])
def test_every_v3_writer_backs_up_into_the_store_directory(
    monkeypatch, tmp_path: Path, writer: str
) -> None:
    """The sidecar lands beside the store, on all three write paths.

    The unit-level backup cells deliberately decouple backup_dir from the
    store directory so a read-only-directory failure is reachable, which
    leaves the production call sites free to drift. This is the assertion
    that pins them.
    """
    import quoin.ccr_store as ccr_store
    from quoin.ccr_config import ccr_store_path
    from quoin.models import _cmd_models_preset, _cmd_models_set
    from quoin.router import ccr_store_dir

    store_dir = ccr_store_dir(tmp_path)
    store_dir.mkdir(parents=True, exist_ok=True)
    make_v3_store(
        store_dir,
        {
            "Providers": [
                {
                    "name": "openrouter",
                    "api_base_url": "https://openrouter.ai/api/v1/chat/completions",
                    "api_key": "sk-or-EXISTING",
                    "models": ["old/model"],
                    "transformer": {"use": ["openrouter"]},
                }
            ]
        },
    )

    seen: dict[str, Path] = {}
    real_update = ccr_store.update_v3_config

    def spy(path, mutate, *, backup_dir, dry_run=False):
        seen["path"] = path
        seen["backup_dir"] = backup_dir
        return real_update(path, mutate, backup_dir=backup_dir, dry_run=dry_run)

    monkeypatch.setattr("quoin.ccr_store.update_v3_config", spy)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-WIRING")
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None,
    )

    if writer == "router setup":
        rc = _cmd_router_setup(_make_args(home=tmp_path))
    elif writer == "models set":
        rc = _cmd_models_set(
            argparse.Namespace(_home_override=tmp_path, tier="opus", model="x/model")
        )
    else:
        rc = _cmd_models_preset(argparse.Namespace(_home_override=tmp_path, name="open"))

    assert rc == 0
    assert seen["path"] == ccr_store_path(home=tmp_path)
    assert seen["backup_dir"] == store_dir
    assert list(store_dir.glob("config.sqlite.value-bak-*.json"))


# ── dispatch_store: the whole store x npm cross product ───────────────────────

_DETECTED_BY_CELL = {
    ("sqlite", None): CcrVersion(3, "sqlite", "store:sqlite"),
    ("sqlite", 2): CcrVersion(3, "sqlite", "store:sqlite"),
    ("sqlite", 3): CcrVersion(3, "sqlite", "store:sqlite"),
    ("sqlite", 4): CcrVersion(0, "sqlite", "store:sqlite-npm-capped"),
    ("json", None): CcrVersion(2, "json", "store:json"),
    ("json", 2): CcrVersion(2, "json", "store:json"),
    ("json", 3): CcrVersion(2, "json", "store:json"),
    ("json", 4): CcrVersion(2, "json", "store:json"),
    ("none", None): CcrVersion(0, None, "none"),
    ("none", 2): CcrVersion(2, None, "npm"),
    ("none", 3): CcrVersion(3, None, "npm"),
    ("none", 4): CcrVersion(0, None, "npm-capped"),
}

_EXPECTED_ROUTE_BY_CELL = {
    ("sqlite", None): "v3",
    ("sqlite", 2): "v3",
    ("sqlite", 3): "v3",
    ("sqlite", 4): "unknown",
    ("json", None): "v2",
    ("json", 2): "v2",
    ("json", 3): "v3-store-absent",
    ("json", 4): "unknown",
    ("none", None): "v2",
    ("none", 2): "v2",
    ("none", 3): "v3-store-absent",
    ("none", 4): "unknown",
}


@pytest.mark.parametrize(
    "cell",
    [(store, npm) for store in ("sqlite", "json", "none") for npm in (None, 2, 3, 4)],
)
def test_dispatch_store_cross_product(cell) -> None:
    """Pure function, every machine state: no handler, no IO."""
    from quoin.router import dispatch_store

    assert dispatch_store(_DETECTED_BY_CELL[cell], cell[1]) == _EXPECTED_ROUTE_BY_CELL[cell]


def test_dispatch_store_table_covers_the_whole_cross_product() -> None:
    """The count is derived from the enumeration, never asserted beside it."""
    expected = {
        (store, npm) for store in ("sqlite", "json", "none") for npm in (None, 2, 3, 4)
    }
    assert set(_EXPECTED_ROUTE_BY_CELL) == expected
    assert set(_DETECTED_BY_CELL) == expected


# ── Empty-sqlite reachability: major 3 always wins, never store-absent ────────

@pytest.mark.parametrize(
    "npm_major, json_present",
    [(None, False), (None, True), (3, False), (3, True)],
)
def test_empty_sqlite_at_major_3_always_dispatches_v3(
    monkeypatch, tmp_path: Path, npm_major, json_present
) -> None:
    """Reachability pin for `_decline_store_absent`: a zero-byte
    `config.sqlite` whose npm major is unreadable or 3 never reaches the
    store-absent decline — `dispatch_store` routes every such cell to "v3"
    unconditionally on `major`, because the store == "sqlite" test runs
    before any major-keyed test. A future reordering that let major == 3
    reach the decline would fail this table first."""
    from quoin.router import ccr_store_dir, detect_ccr, dispatch_store

    store_dir = ccr_store_dir(tmp_path)
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "config.sqlite").touch()
    if json_present:
        (store_dir / "config.json").write_text("{}")
    monkeypatch.setattr("quoin.router._npm_major", lambda: npm_major)

    detected = detect_ccr(home=tmp_path, npm_major=npm_major)
    assert dispatch_store(detected, npm_major) == "v3"


# ── resolve_ccr_route: the assume_installed_major surface ─────────────────────

class TestAssumeInstalledMajor:
    def _route(self, tmp_path: Path, *, assume: int | None):
        from quoin.router import resolve_ccr_route

        if assume is None:
            return resolve_ccr_route(tmp_path)
        return resolve_ccr_route(tmp_path, assume_installed_major=assume)

    def test_default_leaves_no_signal_on_v2(self, tmp_path: Path) -> None:
        assert self._route(tmp_path, assume=None).route == "v2"

    def test_no_store_and_unreadable_npm_becomes_store_absent(self, tmp_path: Path) -> None:
        from quoin.router import CCR_PINNED_MAJOR

        route = self._route(tmp_path, assume=CCR_PINNED_MAJOR)
        assert route.route == "v3-store-absent"

    def test_stale_json_and_unreadable_npm_becomes_store_absent(
        self, tmp_path: Path
    ) -> None:
        from quoin.router import CCR_PINNED_MAJOR, ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.json").write_text("{}")
        assert self._route(tmp_path, assume=None).route == "v2"
        assert self._route(tmp_path, assume=CCR_PINNED_MAJOR).route == "v3-store-absent"

    def test_stale_json_with_readable_npm_reaches_store_absent_without_assuming(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The stale-json rule, not the substitution, is what decides here."""
        from quoin.router import CCR_PINNED_MAJOR, ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.json").write_text("{}")
        monkeypatch.setattr("quoin.router._npm_major", lambda: 3)
        route = self._route(tmp_path, assume=CCR_PINNED_MAJOR)
        assert route.route == "v3-store-absent"
        assert route.version.source != "just-installed"

    def test_capped_source_is_never_substituted(self, monkeypatch, tmp_path: Path) -> None:
        from quoin.router import CCR_PINNED_MAJOR

        monkeypatch.setattr("quoin.router._npm_major", lambda: 9)
        assert self._route(tmp_path, assume=CCR_PINNED_MAJOR).route == "unknown"

    def test_sqlite_store_still_routes_to_v3(self, tmp_path: Path) -> None:
        from quoin.router import CCR_PINNED_MAJOR, ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        make_v3_store(store_dir, {})
        assert self._route(tmp_path, assume=CCR_PINNED_MAJOR).route == "v3"


# ── The post-install re-dispatch ──────────────────────────────────────────────

class TestPostInstallRedispatch:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        npm_after: int | None = None,
        seed_json: bool = False,
        seed_empty_sqlite: bool = False,
    ) -> tuple[int, int]:
        from quoin.router import ccr_store_dir

        state = {"installed": False, "calls": 0}

        def which(cmd: str):
            if cmd in ("node", "npx"):
                return "/usr/bin/node"
            if cmd == "ccr":
                return "/usr/bin/ccr" if state["installed"] else None
            return None

        def install() -> int:
            state["calls"] += 1
            state["installed"] = True
            return 0

        monkeypatch.setattr("quoin.router.shutil.which", which)
        monkeypatch.setattr("quoin.router._install_ccr", install)
        monkeypatch.setattr("quoin.router._node_present", lambda: True)
        monkeypatch.setattr("quoin.router._node_major", lambda: MIN_NODE_MAJOR)
        monkeypatch.setattr(
            "quoin.router._npm_major",
            lambda: npm_after if state["installed"] else None,
        )
        if seed_json or seed_empty_sqlite:
            store_dir = ccr_store_dir(tmp_path)
            store_dir.mkdir(parents=True, exist_ok=True)
            if seed_json:
                (store_dir / "config.json").write_text("{}")
            if seed_empty_sqlite:
                (store_dir / "config.sqlite").touch()

        rc = _cmd_router_setup(_make_args(home=tmp_path))
        return rc, state["calls"]

    def test_unreadable_npm_declines_store_absent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        rc, calls = self._run(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert calls == 1
        assert rc == 2
        assert "it has not created its config store yet" in out
        assert not ccr_config_path(home=tmp_path).exists()

    def test_readable_npm_reaches_the_same_outcome_without_assuming(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        rc, calls = self._run(monkeypatch, tmp_path, npm_after=3)
        out = capsys.readouterr().out
        assert calls == 1
        assert rc == 2
        assert "it has not created its config store yet" in out
        assert not ccr_config_path(home=tmp_path).exists()

    def test_stale_config_json_is_not_overwritten(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        rc, calls = self._run(monkeypatch, tmp_path, seed_json=True)
        out = capsys.readouterr().out
        assert calls == 1
        assert rc == 2
        assert "it has not created its config store yet" in out
        assert ccr_config_path(home=tmp_path).read_text() == "{}"

    def test_stray_empty_sqlite_with_leftover_json_declines_as_stray(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The one reachable stub_present cell: a zero-byte config.sqlite
        beside a leftover config.json, with npm reporting major 1. Renders
        the stray wording (not "not yet written"), declines, and leaves
        both pre-existing files byte-unchanged."""
        from quoin.router import ccr_store_dir

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL")
        rc, calls = self._run(
            monkeypatch, tmp_path, npm_after=1, seed_json=True, seed_empty_sqlite=True
        )
        out = capsys.readouterr().out
        assert calls == 1
        assert rc == 2
        assert "stray, empty config.sqlite exists" in out
        assert "not yet written" not in out
        assert "nothing has been written into it yet" not in out
        assert_no_secret_in(out, "sk-or-SENTINEL")
        store_dir = ccr_store_dir(tmp_path)
        assert (store_dir / "config.json").read_text() == "{}"
        assert (store_dir / "config.sqlite").stat().st_size == 0

    def test_already_installed_line_is_absent_after_a_real_install(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        _rc, calls = self._run(monkeypatch, tmp_path)
        captured = capsys.readouterr()
        assert calls == 1
        assert "already installed — skipping npm install." not in (
            captured.out + captured.err
        )


# ── v3 writes from router setup ───────────────────────────────────────────────

def _seed_store(tmp_path: Path, blob, *, wal: bool = False):
    from quoin.router import ccr_store_dir

    store_dir = ccr_store_dir(tmp_path)
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir, make_v3_store(store_dir, blob, wal=wal)


def _run_setup_on_store(
    monkeypatch, tmp_path: Path, *, dry_run: bool = False, key: str = "sk-or-KEY"
) -> int:
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None,
    )
    return _cmd_router_setup(_make_args(dry_run=dry_run, home=tmp_path))


class TestSetupV3Writes:
    def test_happy_path_writes_provider_and_built_in_route(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        _store_dir, path = _seed_store(tmp_path, {})
        rc = _run_setup_on_store(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        snap = store_value_snapshot(path)
        assert snap is not None
        blob = json.loads(snap[0])
        provider = next(p for p in blob["Providers"] if p.get("name") == "openrouter")
        assert provider["api_key"] == "sk-or-KEY"
        assert blob["Router"]["builtInRules"]["claude-code"]["enabled"] is True
        for v2_key in ("default", "background", "think", "longContext"):
            assert v2_key not in blob["Router"]
        assert "rules" not in blob["Router"]
        assert "NON_INTERACTIVE_MODE" not in json.dumps(blob)
        assert "NON_INTERACTIVE_MODE" not in blob.get("Router", {})
        assert "no v3 equivalent" in out
        assert not ccr_config_path(home=tmp_path).exists()

    def test_table_absent_store_is_populated_by_the_write(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """AC-24: a store quoin did not make must not crash it."""
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / "config.sqlite"
        con = sqlite3.connect(str(path))
        con.close()
        rc = _run_setup_on_store(monkeypatch, tmp_path)
        assert rc == 0
        snap = store_value_snapshot(path)
        assert snap is not None
        blob = json.loads(snap[0])
        assert any(p.get("name") == "openrouter" for p in blob["Providers"])

    def test_re_run_is_idempotent(self, monkeypatch, tmp_path: Path) -> None:
        _store_dir, path = _seed_store(tmp_path, {})
        assert _run_setup_on_store(monkeypatch, tmp_path) == 0
        first = store_value_snapshot(path)
        assert _run_setup_on_store(monkeypatch, tmp_path) == 0
        assert store_value_snapshot(path) == first

    def test_dry_run_writes_nothing_but_reports_everything(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        store_dir, path = _seed_store(tmp_path, {}, wal=True)
        before = store_value_snapshot(path)
        rc = _run_setup_on_store(monkeypatch, tmp_path, dry_run=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "quoin router setup — changes:" in out
        assert "no v3 equivalent" in out
        assert "[dry-run] No files written." in out
        assert store_value_snapshot(path) == before
        assert list(store_dir.glob("config.sqlite.value-bak-*.json")) == []

    def test_malformed_blob_backs_up_and_fails(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / "config.sqlite"
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO app_config VALUES ('default', '{not json', '2026-01-01 00:00:00')"
        )
        con.commit()
        con.close()
        rc = _run_setup_on_store(monkeypatch, tmp_path)
        err = capsys.readouterr().err
        assert rc == 1
        assert "malformed" in err
        assert list(store_dir.glob("config.sqlite.value-bak-*.json"))

    def test_locked_store_reports_a_store_error_not_a_raw_sqlite_error(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        _store_dir, path = _seed_store(tmp_path, {})
        holder = sqlite3.connect(str(path), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            rc = _run_setup_on_store(monkeypatch, tmp_path)
            err = capsys.readouterr().err
            assert rc == 1
            assert "failed to write CCR v3 store" in err
            assert "running ccr process" in err
        finally:
            holder.execute("ROLLBACK")
            holder.close()


class TestUpgradeLossReport:
    def _seed_upgrade_machine(self, tmp_path: Path, *, leftover_json: bool = True):
        store_dir, path = _seed_store(tmp_path, {})
        if leftover_json:
            (store_dir / "config.json").write_text("{}")
        return path

    def test_setup_reports_the_loss_and_says_it_rebuilt(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._seed_upgrade_machine(tmp_path)
        rc = _run_setup_on_store(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "v2 -> v3 upgrade that lost your configuration" in out
        assert "quoin has just rebuilt them from `models.json`" in out
        assert "re-run `quoin router setup` with OPENROUTER_API_KEY" not in out

    def test_dry_run_reports_the_loss_without_claiming_a_rebuild(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        path = self._seed_upgrade_machine(tmp_path)
        before = store_value_snapshot(path)
        rc = _run_setup_on_store(monkeypatch, tmp_path, dry_run=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "v2 -> v3 upgrade that lost your configuration" in out
        assert "re-run `quoin router setup` with OPENROUTER_API_KEY" in out
        assert "quoin has just rebuilt them" not in out
        assert store_value_snapshot(path) == before

    def test_no_leftover_config_json_means_no_report(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._seed_upgrade_machine(tmp_path, leftover_json=False)
        rc = _run_setup_on_store(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert "v2 -> v3 upgrade that lost your configuration" not in out


# ── router status on a v3 machine ─────────────────────────────────────────────

class TestCmdRouterStatusV3:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        capsys,
        *,
        blob=None,
        on_path: bool = True,
        live: bool = False,
        leftover_json: bool = False,
        npm_major: int | None = None,
    ) -> str:
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        if blob is not None:
            make_v3_store(store_dir, blob)
        if leftover_json:
            (store_dir / "config.json").write_text("{}")
        monkeypatch.setattr(
            "quoin.router.shutil.which",
            lambda cmd: "/usr/bin/ccr" if (cmd == "ccr" and on_path) else None,
        )
        monkeypatch.setattr("quoin.router.probe_service", lambda **kw: live)
        if npm_major is not None:
            monkeypatch.setattr("quoin.router._npm_major", lambda: npm_major)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert _cmd_router_status(_make_args(home=tmp_path)) == 0
        return capsys.readouterr().out

    def test_populated_store_reports_populated_and_omits_config_present(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(
            monkeypatch,
            tmp_path,
            capsys,
            blob={"Providers": [{"name": "openrouter", "models": ["a/b"]}]},
        )
        assert "Store populated: yes" in out
        assert "Config present:" not in out
        assert "CCR version:     v3 (via store:sqlite)" in out
        assert "(authoritative for v3)" in out

    def test_empty_store_reports_not_populated(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(monkeypatch, tmp_path, capsys, blob={})
        assert "Store populated: no  (no providers, no API key)" in out
        assert "Active mode:     native" in out

    def test_malformed_store_degrades_to_unknown(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / "config.sqlite"
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO app_config VALUES ('default', '{not json', '2026-01-01 00:00:00')"
        )
        con.commit()
        con.close()
        out = self._run(monkeypatch, tmp_path, capsys)
        assert "Store populated: unknown  (" in out

    def test_locked_store_degrades_to_unknown(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        make_v3_store(store_dir, {})
        holder = sqlite3.connect(str(store_dir / "config.sqlite"), isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            out = self._run(monkeypatch, tmp_path, capsys)
            assert "Store populated: unknown  (" in out
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_store_vouches_for_presence_when_path_does_not(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(
            monkeypatch,
            tmp_path,
            capsys,
            blob={"Providers": [{"name": "openrouter"}]},
            on_path=False,
        )
        assert "CCR installed:   yes  (store present; `ccr` not on PATH)" in out

    def test_populated_store_with_proxy_down_names_the_v3_command(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(
            monkeypatch,
            tmp_path,
            capsys,
            blob={"Providers": [{"name": "openrouter"}]},
        )
        assert "run `ccr default-claude-code` to start" in out
        assert "no `code` subcommand" in out
        assert "ccr code" not in out

    def test_upgrade_loss_report_is_forward_looking_from_status(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(monkeypatch, tmp_path, capsys, blob={}, leftover_json=True)
        assert "v2 -> v3 upgrade that lost your configuration" in out
        assert "re-run `quoin router setup` with OPENROUTER_API_KEY" in out
        assert "quoin has just rebuilt them" not in out

    def test_malformed_store_with_leftover_json_suppresses_loss_report(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """F-02: a store quoin could not fully parse must not ALSO be
        reported as an upgrade that lost the user's configuration — the
        two claims ("unknown" and "lost") contradict each other, and the
        second is a confident false claim about a store that may be fully
        populated."""
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        path = store_dir / "config.sqlite"
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO app_config VALUES ('default', '{not json', '2026-01-01 00:00:00')"
        )
        con.commit()
        con.close()

        out = self._run(monkeypatch, tmp_path, capsys, leftover_json=True)
        assert "Store populated: unknown  (" in out
        assert "v2 -> v3 upgrade that lost your configuration" not in out

    def test_unreadable_store_with_leftover_json_suppresses_loss_report(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """F-02, the unreadable-store variant (genuine corruption, or a
        rollback-journal store under lock — reproduced separately from the
        malformed-JSON cell above)."""
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.sqlite").write_bytes(b"not a sqlite database, just bytes")

        out = self._run(monkeypatch, tmp_path, capsys, leftover_json=True)
        assert "Store populated: unknown  (" in out
        assert "v2 -> v3 upgrade that lost your configuration" not in out

    def test_store_absent_route_points_at_ccr_start_not_router_setup(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """F-03: on the leftover-config.json-with-no-store machine, status
        and setup must agree on the next step — both point at `ccr start`
        first, rather than status sending the user into a `quoin router
        setup` that will just decline again on this exact machine."""
        out = self._run(monkeypatch, tmp_path, capsys, leftover_json=True, npm_major=3)
        assert "v2 -> v3 upgrade that lost your configuration" in out
        assert "ccr start" in out

    def test_no_signal_machine_reads_not_detected(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(monkeypatch, tmp_path, capsys, on_path=False)
        assert "CCR version:    not detected" in out
        assert "(via" not in out.split("CCR version:")[1].split("\n")[0]

    def test_capped_machine_reads_unrecognised(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run(monkeypatch, tmp_path, capsys, npm_major=9)
        assert "CCR version:    unrecognised (via npm-capped)" in out

    def test_zero_byte_store_on_a_genuine_v2_machine_reads_v2(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A zero-byte config.sqlite satisfies sqlite's own definition of a
        valid empty database, so bare existence is not proof CCR ever
        created a v3 store here. On a machine npm confirms is v2, with a
        populated config.json, an unrelated zero-byte config.sqlite must
        not flip the machine to v3 and report an upgrade loss that never
        happened."""
        from quoin.router import ccr_store_dir

        store_dir = ccr_store_dir(tmp_path)
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "config.sqlite").write_bytes(b"")
        write_config(
            ccr_config_path(home=tmp_path),
            {"Providers": [{"name": "openrouter", "models": ["a/b"]}], "Router": {}},
        )
        out = self._run(monkeypatch, tmp_path, capsys, npm_major=2)
        assert "CCR version:    v2 (via store:json)" in out
        assert "Config present: yes" in out
        assert "v2 -> v3 upgrade that lost your configuration" not in out


# ── The effective version, at each render site ────────────────────────────────

def _v3_by_npm_fixture(monkeypatch, tmp_path: Path, npm_major: int) -> None:
    """A leftover config.json beside a newer package, and no config.sqlite."""
    from quoin.router import ccr_store_dir

    store_dir = ccr_store_dir(tmp_path)
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "config.json").write_text("{}")
    monkeypatch.setattr("quoin.router._npm_major", lambda: npm_major)
    monkeypatch.setattr(
        "quoin.router.shutil.which",
        lambda cmd: "/usr/bin/ccr" if cmd == "ccr" else None,
    )
    monkeypatch.setattr("quoin.router.probe_service", lambda **kw: False)
    monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)


def test_effective_version_on_router_setup(monkeypatch, tmp_path: Path, capsys) -> None:
    _v3_by_npm_fixture(monkeypatch, tmp_path, 3)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-KEY")
    assert _cmd_router_setup(_make_args(home=tmp_path)) == 2
    out = capsys.readouterr().out
    assert "CCR v3 is installed" in out
    assert "CCR v2" not in out


def test_effective_version_on_models_set(monkeypatch, tmp_path: Path, capsys) -> None:
    from quoin.models import _cmd_models_set

    _v3_by_npm_fixture(monkeypatch, tmp_path, 3)
    rc = _cmd_models_set(
        argparse.Namespace(_home_override=tmp_path, tier="opus", model="x/model")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "CCR v3 is installed" in out
    assert "CCR v2" not in out


def test_effective_version_on_models_show(monkeypatch, tmp_path: Path, capsys) -> None:
    from quoin.models import _cmd_models_show

    _v3_by_npm_fixture(monkeypatch, tmp_path, 3)
    assert _cmd_models_show(argparse.Namespace(_home_override=tmp_path)) == 0
    out = capsys.readouterr().out
    # No store exists, so nothing is configured and no launch clause is
    # offered — but the v2 command must never appear on a v3 machine.
    assert "ccr code" not in out
    assert "active mode: native" in out


def test_effective_version_on_router_status(monkeypatch, tmp_path: Path, capsys) -> None:
    _v3_by_npm_fixture(monkeypatch, tmp_path, 3)
    assert _cmd_router_status(_make_args(home=tmp_path)) == 0
    out = capsys.readouterr().out
    version_line = next(
        line for line in out.splitlines() if line.strip().startswith("CCR version:")
    )
    assert version_line.strip() == "CCR version:    v3 (via npm)"
    assert "v2" not in version_line
    assert "store:json" not in version_line
    assert "Config present: yes" not in out


def test_effective_version_npm_four_variant_names_v4(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from quoin.models import _cmd_models_set

    _v3_by_npm_fixture(monkeypatch, tmp_path, 4)
    rc = _cmd_models_set(
        argparse.Namespace(_home_override=tmp_path, tier="opus", model="x/model")
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "Detected:  v4" in out
    assert "v2" not in out.split("Detected:")[1].split("\n")[0]


def test_genuine_v2_machine_keeps_the_v2_rendering(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The identity half of the same fix: a real v2 machine still reads v2."""
    from quoin.models import _cmd_models_show

    _v3_by_npm_fixture(monkeypatch, tmp_path, 2)
    write_config(
        ccr_config_path(home=tmp_path),
        {"Providers": [{"name": "openrouter", "models": []}], "Router": {}},
    )
    assert _cmd_router_status(_make_args(home=tmp_path)) == 0
    status_out = capsys.readouterr().out
    assert "CCR version:    v2 (via store:json)" in status_out
    assert "Config present: yes" in status_out

    assert _cmd_models_show(argparse.Namespace(_home_override=tmp_path)) == 0
    show_out = capsys.readouterr().out
    assert "run `ccr code`" in show_out


# ── F-20: npm spawn census, widened past router setup ─────────────────────────

def test_npm_spawn_census_covers_status_and_models_surfaces(
    monkeypatch, tmp_path: Path
) -> None:
    """The round-6 effective-version wiring added a `resolve_ccr_route`
    call to five surfaces `router setup`'s own census guard never covered.
    Pins the same one-read-per-invocation property there, so a future edit
    reintroducing a duplicate spawn on any of them fails here rather than
    only being caught by inspection."""
    from quoin.models import (
        _cmd_models_preset,
        _cmd_models_reset,
        _cmd_models_set,
        _cmd_models_show,
    )
    from quoin.router import _cmd_router_status

    write_config(
        ccr_config_path(home=tmp_path),
        {"Providers": [{"name": "openrouter", "models": []}], "Router": {}},
    )

    call_count = {"n": 0}

    def _counting_npm_major():
        call_count["n"] += 1
        return 2

    monkeypatch.setattr("quoin.router._npm_major", _counting_npm_major)
    monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
    monkeypatch.setattr("quoin.router.probe_service", lambda **kw: False)
    monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)

    surfaces = {
        "router status": lambda: _cmd_router_status(_make_args(home=tmp_path)),
        "models show": lambda: _cmd_models_show(
            argparse.Namespace(_home_override=tmp_path)
        ),
        "models set": lambda: _cmd_models_set(
            argparse.Namespace(_home_override=tmp_path, tier="opus", model="x/model")
        ),
        "models preset": lambda: _cmd_models_preset(
            argparse.Namespace(_home_override=tmp_path, name="open")
        ),
        "models reset": lambda: _cmd_models_reset(
            argparse.Namespace(_home_override=tmp_path)
        ),
    }
    for label, call in surfaces.items():
        call_count["n"] = 0
        call()
        assert call_count["n"] <= 1, f"{label} spawned npm more than once"


# ── Store-open census beside the npm one ───────────────────────────────────────

def test_router_status_store_open_census(monkeypatch, tmp_path: Path) -> None:
    """`router status` opens the v3 store once to render `Store populated:`,
    and a second time — for `v3_api_key_row_count` — only when a leftover
    `config.json` also makes the upgrade-loss report reachable. Pins the
    laziness directly: re-hoisting the row count back to an eager
    positional argument would restore the second open on the dominant
    (no-leftover-`config.json`) shape with no test failing without this."""
    import quoin.ccr_store as ccr_store
    from quoin.router import ccr_store_dir

    open_count = {"n": 0}
    real_read = ccr_store.read_v3_config
    real_row_count = ccr_store.v3_api_key_row_count

    def _counting_read(path):
        open_count["n"] += 1
        return real_read(path)

    def _counting_row_count(path):
        open_count["n"] += 1
        return real_row_count(path)

    monkeypatch.setattr("quoin.router.ccr_store.read_v3_config", _counting_read)
    monkeypatch.setattr("quoin.router.ccr_store.v3_api_key_row_count", _counting_row_count)
    monkeypatch.setattr("quoin.router.shutil.which", lambda cmd: None)
    monkeypatch.setattr("quoin.router.probe_service", lambda **kw: False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # (a) a v3 home with no leftover config.json: one open.
    store_dir_a = ccr_store_dir(tmp_path)
    store_dir_a.mkdir(parents=True, exist_ok=True)
    make_v3_store(store_dir_a, {})
    open_count["n"] = 0
    assert _cmd_router_status(_make_args(home=tmp_path)) == 0
    assert open_count["n"] == 1

    # (b) the same store, plus a leftover config.json: two opens.
    (store_dir_a / "config.json").write_text("{}", encoding="utf-8")
    open_count["n"] = 0
    assert _cmd_router_status(_make_args(home=tmp_path)) == 0
    assert open_count["n"] == 2
