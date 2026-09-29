"""Unit tests for the opt-in autocompact env-var delegation (IVG-258 stage 7).

Covers the `_merge_env`/`_clear_env` helpers in `quoin.installer` and the
`_validate_autocompact_args`/`_autocompact_window_type` helpers in `quoin.cli`.
These tests run in CI (no `claude` or `npx` dependency).

Eleven cases, lettered per the plan: (a), (b), (b2), (c), (d), (e), (f), (g),
(h), (i), (j).
"""
import argparse
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"

from quoin.cli import _autocompact_window_type, _validate_autocompact_args  # noqa: E402
from quoin.installer import (  # noqa: E402
    _clear_env,
    _merge_env,
    deploy_hooks,
)


def _make_args(pct=None, window=None, clear=False) -> argparse.Namespace:
    return argparse.Namespace(
        autocompact_pct=pct,
        autocompact_window=window,
        clear_autocompact_env=clear,
    )


class TestAutocompactEnvInstall:

    # (a) no env key by default
    def test_default_install_writes_no_env_key(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest)
        settings = json.loads((dest / "settings.json").read_text())
        assert "env" not in settings, (
            f"Expected no 'env' key on a default install, got {settings.get('env')!r}"
        )

    def test_merge_env_itself_writes_nothing_for_two_none_args(self, tmp_path):
        """Directly exercises the real `quoin.installer._merge_env` (not a stub).

        review-1.md MIN-2: the previous version of this test defined and called a
        local stub that never touched `quoin.installer`, so it would have kept
        passing even if the real `_merge_env` were deleted — it discriminated
        against nothing. Routing a broken `_merge_env` through `deploy_hooks`'s
        default-install path can't discriminate either: `deploy_hooks` only calls
        `_merge_env` at all when `pct is not None or window is not None`
        (installer.py's own outer guard), so a default install (both None) never
        reaches `_merge_env` regardless of its correctness — the outer guard, not
        `_merge_env`, is what protects case (a) end-to-end. What `_merge_env`
        itself is responsible for is its own documented contract ("Writes ONLY
        the keys whose value is not None") — this test calls the real function
        directly with pct=None, window=None and asserts that contract holds,
        independent of the outer guard. A regression here (e.g. `_merge_env`
        writing unconditionally) would be caught by this test even if the outer
        guard were ever loosened.
        """
        from quoin.installer import _merge_env  # noqa: PLC0415

        settings: dict = {}
        changed = _merge_env(settings, pct=None, window=None)
        assert changed == 0
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in settings.get("env", {})
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in settings.get("env", {})

    # (b) opt-in with both values writes exactly the two keys, as strings (D-05)
    def test_opt_in_both_values_written_as_strings(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75, autocompact_window=500000)
        settings = json.loads((dest / "settings.json").read_text())
        env = settings["env"]
        assert env == {
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
        }
        assert isinstance(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str)
        assert isinstance(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], str)

    # (b2) each flag alone writes only its own key (D-02 independence)
    def test_pct_only_leaves_window_key_absent(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        env = json.loads((dest / "settings.json").read_text())["env"]
        assert env == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env

    def test_window_only_leaves_pct_key_absent(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_window=500000)
        env = json.loads((dest / "settings.json").read_text())["env"]
        assert env == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"}
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env

    # (c) idempotency with the opt-in
    def test_opt_in_idempotent_on_reinstall(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75, autocompact_window=500000)
        first = json.loads((dest / "settings.json").read_text())["env"]
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75, autocompact_window=500000)
        second = json.loads((dest / "settings.json").read_text())["env"]
        assert first == second
        assert len(second) == 2

    # (d) idempotency without the opt-in — a plain reinstall preserves an existing opt-in
    def test_default_reinstall_stays_env_free(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest)
        deploy_hooks(QUOIN_SRC, dest)
        settings = json.loads((dest / "settings.json").read_text())
        assert "env" not in settings

    def test_default_reinstall_after_opt_in_preserves_it(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        deploy_hooks(QUOIN_SRC, dest)  # plain reinstall, no autocompact flags
        env = json.loads((dest / "settings.json").read_text())["env"]
        assert env == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}

    # (e) unrelated pre-existing env keys survive both the opt-in write and the clear
    def test_unrelated_env_keys_survive_opt_in_write(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        (dest / "settings.json").write_text(json.dumps({"env": {"FOO": "bar"}}))
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        env = json.loads((dest / "settings.json").read_text())["env"]
        assert env["FOO"] == "bar"
        assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "75"

    def test_unrelated_env_keys_survive_clear(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        (dest / "settings.json").write_text(
            json.dumps({"env": {"FOO": "bar", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"}})
        )
        deploy_hooks(QUOIN_SRC, dest, clear_autocompact_env=True)
        env = json.loads((dest / "settings.json").read_text())["env"]
        assert env == {"FOO": "bar"}

    # (f) --clear removes only the two quoin keys, drops env dict when empty
    def test_clear_removes_only_quoin_keys(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75, autocompact_window=500000)
        deploy_hooks(QUOIN_SRC, dest, clear_autocompact_env=True)
        settings = json.loads((dest / "settings.json").read_text())
        assert "env" not in settings  # emptied dict is dropped entirely

    # (g) range validation
    def test_validate_autocompact_args_rejects_out_of_range_window(self):
        for bad in (99999, 1000001, 0):
            with pytest.raises(ValueError):
                _validate_autocompact_args(_make_args(window=bad))

    def test_validate_autocompact_args_rejects_out_of_range_pct(self):
        for bad in (0, 101):
            with pytest.raises(ValueError):
                _validate_autocompact_args(_make_args(pct=bad))

    def test_validate_autocompact_args_accepts_boundary_values(self):
        assert _validate_autocompact_args(_make_args(window=100000)) == (None, 100000, False)
        assert _validate_autocompact_args(_make_args(window=1000000)) == (None, 1000000, False)
        assert _validate_autocompact_args(_make_args(pct=1)) == (1, None, False)
        assert _validate_autocompact_args(_make_args(pct=100)) == (100, None, False)

    def test_autocompact_window_type_rejects_suffixed_and_non_integer(self):
        for bad in ("500k", "abc", ""):
            with pytest.raises(argparse.ArgumentTypeError):
                _autocompact_window_type(bad)

    # (h) _merge_env raises ValueError rather than writing an out-of-range value
    def test_merge_env_raises_on_out_of_range_pct(self):
        settings = {}
        with pytest.raises(ValueError):
            _merge_env(settings, pct=0, window=None)
        assert "env" not in settings

    def test_merge_env_raises_on_out_of_range_window(self):
        settings = {}
        with pytest.raises(ValueError):
            _merge_env(settings, pct=None, window=99999)
        assert "env" not in settings

    # (i) whole-file regression: env merge is additive and touches nothing else
    def test_env_merge_does_not_disturb_other_sections(self, tmp_path):
        dest = tmp_path / ".claude"
        dest.mkdir()
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        settings = json.loads((dest / "settings.json").read_text())
        stanza_count = sum(len(v) for v in settings["hooks"].values())
        assert stanza_count == 9
        assert "skillOverrides" in settings
        assert "deny" in settings["permissions"]

    # (j) non-dict env tolerance
    def test_opt_in_tolerates_null_env_and_warns(self, tmp_path, capsys):
        dest = tmp_path / ".claude"
        dest.mkdir()
        (dest / "settings.json").write_text(json.dumps({"env": None}))
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        settings = json.loads((dest / "settings.json").read_text())
        assert settings["env"] is None  # byte-unchanged value
        err = capsys.readouterr().err
        assert "env" in err.lower()

    def test_opt_in_tolerates_string_env_and_warns(self, tmp_path, capsys):
        dest = tmp_path / ".claude"
        dest.mkdir()
        (dest / "settings.json").write_text(json.dumps({"env": "FOO=bar"}))
        deploy_hooks(QUOIN_SRC, dest, autocompact_pct=75)
        settings = json.loads((dest / "settings.json").read_text())
        assert settings["env"] == "FOO=bar"
        err = capsys.readouterr().err
        assert "env" in err.lower()

    def test_clear_env_against_non_dict_returns_zero_and_changes_nothing(self):
        settings = {"env": None}
        assert _clear_env(settings) == 0
        assert settings["env"] is None

        settings2 = {"env": "FOO=bar"}
        assert _clear_env(settings2) == 0
        assert settings2["env"] == "FOO=bar"
