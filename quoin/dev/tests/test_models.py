"""Tests for quoin models command (IVG-65 Stage 2).

All tests run in CI with NO network/ccr/npm access.
- ccr_config.probe_service is monkeypatched for deterministic liveness.
- router._verify_ccr and shutil.which are monkeypatched for CCR install state.
- Temp HOME (tmp_path) isolates filesystem side-effects.

Import style note (load-bearing):
  models.py uses `import quoin.ccr_config as ccr_config` (module-qualified).
  A SINGLE `monkeypatch.setattr('quoin.ccr_config.probe_service', ...)` intercepts
  all calls — no second stub at 'quoin.models.probe_service' needed.
  This avoids Stage-1's double-stub foot-gun (test_router_setup.py:452-453).
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from quoin.ccr_config import (  # noqa: E402
    assert_no_secret_in,
    backup_config,
    ccr_config_path,
    write_config,
)
from quoin.router import DEFAULT_MODELS, quoin_models_path  # noqa: E402
from quoin.models import (  # noqa: E402
    FRIENDLY_ALIASES,
    KNOWN_SLUGS,
    TIER_KEYS,
    SlugRejected,
    _cmd_models_preset,
    _cmd_models_reset,
    _cmd_models_set,
    _cmd_models_show,
    build_router_map,
    read_effective_models,
    set_provider_models_inplace,
    validate_slug,
    write_models,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_args(home: Path | None = None, **kwargs: Any) -> argparse.Namespace:
    args = argparse.Namespace(**kwargs)
    if home is not None:
        args._home_override = home
    return args


def _write_ccr_config(
    tmp_path: Path,
    *,
    api_key: str = "sk-or-SENTINEL-KEY",
    extra_models: list[str] | None = None,
    extra_router_keys: dict[str, str] | None = None,
) -> Path:
    """Write a minimal CCR config with an openrouter provider."""
    models_list = list(DEFAULT_MODELS.values())
    if extra_models:
        models_list = extra_models + models_list
    cfg: dict[str, Any] = {
        "Providers": [
            {
                "name": "openrouter",
                "api_base_url": "https://openrouter.ai/api/v1/chat/completions",
                "api_key": api_key,
                "models": models_list,
                "transformer": {"use": ["openrouter"]},
            }
        ],
        "Router": {
            "default": f"openrouter,{DEFAULT_MODELS['sonnet']}",
            "background": f"openrouter,{DEFAULT_MODELS['haiku']}",
            "think": f"openrouter,{DEFAULT_MODELS['opus']}",
            "longContext": f"openrouter,{DEFAULT_MODELS['sonnet']}",
        },
    }
    if extra_router_keys:
        cfg["Router"].update(extra_router_keys)
    path = ccr_config_path(home=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_config(path, cfg)
    return path


def _capture_output(fn, args, monkeypatch, *, ccr_live: bool = False) -> tuple[int, str]:
    """Run a command handler, capturing print output. Returns (rc, stdout_str)."""
    monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: ccr_live)
    buf = io.StringIO()
    import builtins
    orig = builtins.print

    def fake_print(*a, file=None, **kw2):
        if file is None or file is sys.stdout:
            orig(*a, file=buf, **kw2)
        else:
            orig(*a, file=file, **kw2)

    monkeypatch.setattr(builtins, "print", fake_print)
    rc = fn(args)
    return rc, buf.getvalue()


# ── read_effective_models ─────────────────────────────────────────────────────

class TestReadEffectiveModels:
    def test_no_user_file_returns_defaults(self, tmp_path: Path) -> None:
        result = read_effective_models(home=tmp_path)
        assert result == dict(DEFAULT_MODELS)

    def test_partial_file_merges_per_key(self, tmp_path: Path) -> None:
        path = quoin_models_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"opus": "x/custom-model"}) + "\n")
        result = read_effective_models(home=tmp_path)
        assert result["opus"] == "x/custom-model"
        assert result["haiku"] == DEFAULT_MODELS["haiku"]
        assert result["sonnet"] == DEFAULT_MODELS["sonnet"]

    def test_malformed_json_warns_and_falls_back(
        self, tmp_path: Path, capsys
    ) -> None:
        path = quoin_models_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not valid json }")
        result = read_effective_models(home=tmp_path)
        assert result == dict(DEFAULT_MODELS)
        captured = capsys.readouterr()
        assert "parse error" in captured.err or "parse error" in captured.out

    def test_stray_key_ignored_and_warned(self, tmp_path: Path, capsys) -> None:
        path = quoin_models_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"haiku": "a/b", "unknown_tier": "x/y"}) + "\n")
        result = read_effective_models(home=tmp_path)
        assert result["haiku"] == "a/b"
        assert "unknown_tier" not in result
        captured = capsys.readouterr()
        assert "unknown" in captured.err.lower() or "unknown" in captured.out.lower()

    def test_full_override_all_three_keys(self, tmp_path: Path) -> None:
        path = quoin_models_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"haiku": "a/haiku", "sonnet": "b/sonnet", "opus": "c/opus"}
        path.write_text(json.dumps(data) + "\n")
        result = read_effective_models(home=tmp_path)
        assert result == data


# ── build_router_map ──────────────────────────────────────────────────────────

class TestBuildRouterMap:
    def test_all_values_start_with_openrouter(self) -> None:
        rmap = build_router_map(dict(DEFAULT_MODELS))
        for key, value in rmap.items():
            assert value.startswith("openrouter,"), (
                f"Router.{key} = {value!r} does not start with 'openrouter,'"
            )

    def test_maps_tiers_correctly(self) -> None:
        models = {"haiku": "a/haiku", "sonnet": "b/sonnet", "opus": "c/opus"}
        rmap = build_router_map(models)
        assert rmap["default"] == "openrouter,b/sonnet"
        assert rmap["background"] == "openrouter,a/haiku"
        assert rmap["think"] == "openrouter,c/opus"
        assert rmap["longContext"] == "openrouter,b/sonnet"

    def test_round_trips_through_merge_router_keys_no_spurious_warning(
        self,
    ) -> None:
        """merge_router_keys should not warn when updating owned openrouter keys."""
        from quoin.ccr_config import merge_router_keys

        cfg: dict[str, Any] = {
            "Router": {
                "default": f"openrouter,{DEFAULT_MODELS['sonnet']}",
                "background": f"openrouter,{DEFAULT_MODELS['haiku']}",
                "think": f"openrouter,{DEFAULT_MODELS['opus']}",
                "longContext": f"openrouter,{DEFAULT_MODELS['sonnet']}",
            }
        }
        new_models = {"haiku": "x/haiku", "sonnet": "x/sonnet", "opus": "x/opus"}
        rmap = build_router_map(new_models)
        _, changes, warnings = merge_router_keys(cfg, rmap)
        # No spurious non-clobber warnings — all existing keys point at openrouter.
        assert warnings == [], f"Unexpected warnings: {warnings}"
        assert len(changes) == 4


# ── set_provider_models_inplace ───────────────────────────────────────────────

class TestSetProviderModelsInplace:
    def test_absent_providers_returns_false(self) -> None:
        cfg: dict[str, Any] = {}
        result = set_provider_models_inplace(cfg, list(DEFAULT_MODELS.values()))
        assert result is False
        # After the call, Providers is set to [] (type guard ran).
        assert cfg.get("Providers") == []

    def test_providers_non_list_warns_and_returns_false(
        self, capsys
    ) -> None:
        cfg: dict[str, Any] = {"Providers": "not-a-list"}
        result = set_provider_models_inplace(cfg, list(DEFAULT_MODELS.values()))
        assert result is False
        captured = capsys.readouterr()
        assert "not a list" in captured.err

    def test_no_openrouter_provider_returns_false(self) -> None:
        cfg: dict[str, Any] = {
            "Providers": [{"name": "anthropic", "api_key": "x"}]
        }
        result = set_provider_models_inplace(cfg, list(DEFAULT_MODELS.values()))
        assert result is False

    def test_updates_models_field_only(self) -> None:
        sentinel_key = "sk-or-SENTINEL-DO-NOT-TOUCH"
        cfg: dict[str, Any] = {
            "Providers": [
                {
                    "name": "openrouter",
                    "api_key": sentinel_key,
                    "models": list(DEFAULT_MODELS.values()),
                    "transformer": {"use": ["openrouter"]},
                }
            ]
        }
        new_models = ["x/haiku", "x/sonnet", "x/opus"]
        result = set_provider_models_inplace(cfg, new_models)
        assert result is True
        provider = cfg["Providers"][0]
        # api_key must be byte-unchanged.
        assert provider["api_key"] == sentinel_key
        # transformer must be unchanged.
        assert provider["transformer"] == {"use": ["openrouter"]}
        # models list must contain our new slugs.
        for slug in new_models:
            assert slug in provider["models"]

    def test_extra_user_model_preserved(self) -> None:
        """Union semantics: extra user-added models survive a set call (D-06)."""
        user_extra = "my-provider/custom-model"
        cfg: dict[str, Any] = {
            "Providers": [
                {
                    "name": "openrouter",
                    "api_key": "sk-test",
                    "models": [user_extra] + list(DEFAULT_MODELS.values()),
                }
            ]
        }
        new_models = ["a/haiku", "a/sonnet", "a/opus"]
        result = set_provider_models_inplace(cfg, new_models)
        assert result is True
        final_models = cfg["Providers"][0]["models"]
        assert user_extra in final_models
        for slug in new_models:
            assert slug in final_models

    def test_returns_true_on_success(self) -> None:
        cfg: dict[str, Any] = {
            "Providers": [{"name": "openrouter", "api_key": "k", "models": []}]
        }
        result = set_provider_models_inplace(cfg, ["a/b"])
        assert result is True


# ── validate_slug ─────────────────────────────────────────────────────────────

class TestValidateSlug:
    def test_known_slug_accepts_no_warning(self) -> None:
        slug = "deepseek/deepseek-v4-pro"
        resolved, warnings = validate_slug(slug)
        assert resolved == slug
        assert warnings == []

    def test_unknown_but_plausible_accepts_one_warning(self) -> None:
        slug = "foo/bar-99"
        resolved, warnings = validate_slug(slug)
        assert resolved == slug
        assert len(warnings) == 1
        assert "unknown slug" in warnings[0]

    def test_implausible_no_slash_raises(self) -> None:
        with pytest.raises(SlugRejected):
            validate_slug("garbage")

    def test_implausible_trailing_slash_raises(self) -> None:
        with pytest.raises(SlugRejected):
            validate_slug("a/")

    def test_implausible_leading_slash_raises(self) -> None:
        with pytest.raises(SlugRejected):
            validate_slug("/b")

    def test_implausible_whitespace_raises(self) -> None:
        with pytest.raises(SlugRejected):
            validate_slug("a b")

    def test_friendly_alias_flash(self) -> None:
        resolved, warnings = validate_slug("flash")
        assert resolved == FRIENDLY_ALIASES["flash"]
        assert warnings == []

    def test_friendly_alias_pro(self) -> None:
        resolved, warnings = validate_slug("pro")
        assert resolved == FRIENDLY_ALIASES["pro"]
        assert warnings == []

    def test_friendly_alias_glm(self) -> None:
        resolved, warnings = validate_slug("glm")
        assert resolved == FRIENDLY_ALIASES["glm"]
        assert warnings == []

    def test_multiple_slashes_raises(self) -> None:
        with pytest.raises(SlugRejected):
            validate_slug("a/b/c")

    def test_glm_alias_resolves_to_current_default_warning_free(self) -> None:
        """IVG-243 T-02: 'glm' must resolve to the (post-bump) blessed slug
        with no advisory warning — i.e. the alias target is always a member
        of KNOWN_SLUGS, not just structurally plausible."""
        resolved, warnings = validate_slug("glm")
        assert resolved == FRIENDLY_ALIASES["glm"] == DEFAULT_MODELS["opus"]
        assert warnings == []


# ── Slug table consistency (IVG-243 T-02) ───────────────────────────────────────

class TestSlugTableConsistency:
    """Every default/alias target must be a member of the advisory allowlist —
    otherwise a freshly-installed user immediately gets an 'unknown slug'
    warning from their own blessed defaults."""

    def test_default_models_are_known_slugs(self) -> None:
        assert set(DEFAULT_MODELS.values()) <= KNOWN_SLUGS

    def test_friendly_aliases_are_known_slugs(self) -> None:
        assert set(FRIENDLY_ALIASES.values()) <= KNOWN_SLUGS

    def test_default_models_tier_assignment(self) -> None:
        """CRIT-1 guard: the pre-existing consistency check above passes even
        when tiers are transposed, because the additive allowlist contains
        all three slugs regardless of which tier holds which. This is the
        only test that pins *which slug goes in which tier*."""
        assert DEFAULT_MODELS["haiku"] == "z-ai/glm-5.3-flash"
        assert DEFAULT_MODELS["sonnet"] == "deepseek/deepseek-v4.1-flash"
        assert DEFAULT_MODELS["opus"] == "z-ai/glm-5.3"

    def test_friendly_alias_tier_mapping(self) -> None:
        """The non-tautological remainder of AC-36 under derivation: the
        alias→tier mapping (flash→haiku, pro→sonnet, glm→opus) is the new,
        meaningful literal content and can be wrong even though the slug
        values themselves cannot drift from DEFAULT_MODELS."""
        assert FRIENDLY_ALIASES["flash"] is DEFAULT_MODELS["haiku"]
        assert FRIENDLY_ALIASES["pro"] is DEFAULT_MODELS["sonnet"]
        assert FRIENDLY_ALIASES["glm"] is DEFAULT_MODELS["opus"]


class TestOriginReclassification:
    """R-06: correcting DEFAULT_MODELS legitimately reclassifies a
    models.json pin of a now-superseded slug from 'default' to 'user'."""

    def test_pinned_superseded_slug_reports_user(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.setattr("quoin.router._verify_ccr", lambda: False)
        monkeypatch.setattr("shutil.which", lambda name: None)

        mp = quoin_models_path(home=tmp_path)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"opus": "z-ai/glm-5.2"}), encoding="utf-8")

        args = _make_args(home=tmp_path)
        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = _cmd_models_show(args)
        out = buf.getvalue()

        assert rc == 0
        assert "opus" in out
        opus_line = next(line for line in out.splitlines() if line.strip().startswith("opus"))
        assert "(user)" in opus_line
        for tier in ("haiku", "sonnet"):
            tier_line = next(line for line in out.splitlines() if line.strip().startswith(tier))
            assert "(default)" in tier_line


# ── quoin models show ─────────────────────────────────────────────────────────

class TestCmdModelsShow:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        live: bool = False,
        cfg_present: bool = False,
        ccr_installed: bool = False,
    ) -> tuple[int, str]:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: live)
        monkeypatch.setattr("quoin.router._verify_ccr", lambda: ccr_installed)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ccr" if (name == "ccr" and ccr_installed) else None)

        if cfg_present:
            _write_ccr_config(tmp_path)

        args = _make_args(home=tmp_path)
        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = _cmd_models_show(args)
        return rc, buf.getvalue()

    def test_returns_zero(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path)
        assert rc == 0

    def test_live_and_config_reports_open(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run(monkeypatch, tmp_path, live=True, cfg_present=True)
        assert "open via CCR" in out

    def test_config_present_but_down_reports_native(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _, out = self._run(monkeypatch, tmp_path, live=False, cfg_present=True)
        assert "native" in out
        assert "open via CCR" not in out

    def test_no_config_reports_native(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run(monkeypatch, tmp_path, live=False, cfg_present=False)
        assert "native" in out

    def test_no_config_no_ccr_shows_install_hint(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _, out = self._run(
            monkeypatch, tmp_path, live=False, cfg_present=False, ccr_installed=False
        )
        assert "quoin router setup" in out

    def test_never_prints_api_key(self, monkeypatch, tmp_path: Path) -> None:
        """R-03: show must never leak OPENROUTER_API_KEY."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SENTINEL-SHOULD-NOT-APPEAR")
        _, out = self._run(monkeypatch, tmp_path, live=True, cfg_present=True)
        assert "SENTINEL-SHOULD-NOT-APPEAR" not in out

    def test_shows_all_three_tiers(self, monkeypatch, tmp_path: Path) -> None:
        _, out = self._run(monkeypatch, tmp_path)
        for tier in TIER_KEYS:
            assert tier in out


# ── quoin models set ──────────────────────────────────────────────────────────

class TestCmdModelsSet:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        tier: str,
        model: str,
        *,
        with_ccr_config: bool = True,
        api_key: str = "sk-or-SENTINEL",
        extra_user_model: str | None = None,
        extra_router_keys: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        if with_ccr_config:
            extra_models = [extra_user_model] if extra_user_model else None
            _write_ccr_config(
                tmp_path,
                api_key=api_key,
                extra_models=extra_models,
                extra_router_keys=extra_router_keys,
            )

        args = _make_args(home=tmp_path, tier=tier, model=model)
        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = _cmd_models_set(args)
        return rc, buf.getvalue()

    def test_unknown_tier_returns_1_writes_nothing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "ultra", "x/y", with_ccr_config=False)
        assert rc == 1
        assert not quoin_models_path(home=tmp_path).exists()

    def test_implausible_slug_returns_1_writes_nothing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "opus", "garbage", with_ccr_config=False)
        assert rc == 1
        assert not quoin_models_path(home=tmp_path).exists()

    def test_writes_one_tier_to_models_json(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "opus", "foo/bar", with_ccr_config=False)
        assert rc == 0
        data = json.loads(quoin_models_path(home=tmp_path).read_text())
        assert data["opus"] == "foo/bar"
        assert data["haiku"] == DEFAULT_MODELS["haiku"]
        assert data["sonnet"] == DEFAULT_MODELS["sonnet"]

    def test_re_points_matching_router_key(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "opus", "x/new-model")
        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["think"] == "openrouter,x/new-model"

    def test_works_without_api_key_set(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """R-03: set must work even when OPENROUTER_API_KEY is unset."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        rc, _ = self._run(monkeypatch, tmp_path, "haiku", "deepseek/deepseek-v4-flash")
        assert rc == 0

    def test_api_key_byte_unchanged(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """R-03: provider api_key must be byte-for-byte unchanged after set."""
        sentinel = "sk-or-SENTINEL-PRESERVED"
        rc, _ = self._run(monkeypatch, tmp_path, "opus", "x/model", api_key=sentinel)
        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        provider = next(p for p in cfg["Providers"] if p.get("name") == "openrouter")
        assert provider["api_key"] == sentinel

    def test_backup_created(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "haiku", "x/model")
        assert rc == 0
        bak_files = list(
            ccr_config_path(home=tmp_path).parent.glob("config.json.bak-*")
        )
        assert len(bak_files) == 1

    def test_extra_user_model_preserved(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """D-06: user-added extra model in provider list must survive set."""
        extra = "my-provider/custom-model"
        rc, _ = self._run(
            monkeypatch, tmp_path, "opus", "x/new-opus", extra_user_model=extra
        )
        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        provider = next(p for p in cfg["Providers"] if p.get("name") == "openrouter")
        assert extra in provider["models"]

    def test_differing_non_openrouter_router_key_preserved(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """R-04: a Router key pointing at a non-openrouter value must be preserved + warned."""
        rc, out = self._run(
            monkeypatch,
            tmp_path,
            "sonnet",
            "x/model",
            extra_router_keys={"default": "anthropic/claude-3-sonnet"},
        )
        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["default"] == "anthropic/claude-3-sonnet"

    def test_no_ccr_config_writes_models_json_only(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, out = self._run(
            monkeypatch, tmp_path, "opus", "x/model", with_ccr_config=False
        )
        assert rc == 0
        assert quoin_models_path(home=tmp_path).exists()
        assert not ccr_config_path(home=tmp_path).exists()
        assert "quoin router setup" in out

    def test_providers_absent_returns_int_no_raise(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """set against a config with no Providers must return int, not raise."""
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        # Write a CCR config without a Providers key.
        path = ccr_config_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_config(path, {"Router": {}})

        args = _make_args(home=tmp_path, tier="opus", model="x/model")
        rc = _cmd_models_set(args)
        assert isinstance(rc, int)

    def test_secret_not_leaked_to_stdout_or_models_json(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """R-03: SECRET-NO-LEAK — sentinel api_key must never appear in stdout or models.json."""
        sentinel = "sk-or-SENTINEL-MUST-NOT-LEAK"
        rc, out = self._run(
            monkeypatch, tmp_path, "sonnet", "x/model", api_key=sentinel
        )
        assert rc == 0
        # Must not appear in stdout.
        assert sentinel not in out
        # Must not appear in models.json.
        models_content = quoin_models_path(home=tmp_path).read_text()
        assert sentinel not in models_content
        # api_key in CCR config must be preserved byte-for-byte.
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        provider = next(p for p in cfg["Providers"] if p.get("name") == "openrouter")
        assert provider["api_key"] == sentinel

    def test_friendly_alias_resolved(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "opus", "glm")
        assert rc == 0
        data = json.loads(quoin_models_path(home=tmp_path).read_text())
        assert data["opus"] == FRIENDLY_ALIASES["glm"]


# ── quoin models preset ───────────────────────────────────────────────────────

class TestCmdModelsPreset:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        name: str,
        *,
        with_ccr_config: bool = True,
        api_key: str = "sk-or-SENTINEL",
    ) -> tuple[int, str]:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        if with_ccr_config:
            _write_ccr_config(tmp_path, api_key=api_key)

        args = _make_args(home=tmp_path, name=name)
        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = _cmd_models_preset(args)
        return rc, buf.getvalue()

    def test_unknown_preset_returns_1(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "closed", with_ccr_config=False)
        assert rc == 1

    def test_open_preset_writes_all_three_defaults(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "open")
        assert rc == 0
        data = json.loads(quoin_models_path(home=tmp_path).read_text())
        for tier in TIER_KEYS:
            assert data[tier] == DEFAULT_MODELS[tier]

    def test_open_preset_re_points_all_four_router_keys(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "open")
        assert rc == 0
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        assert cfg["Router"]["default"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"
        assert cfg["Router"]["background"] == f"openrouter,{DEFAULT_MODELS['haiku']}"
        assert cfg["Router"]["think"] == f"openrouter,{DEFAULT_MODELS['opus']}"
        assert cfg["Router"]["longContext"] == f"openrouter,{DEFAULT_MODELS['sonnet']}"

    def test_backup_created(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "open")
        assert rc == 0
        bak_files = list(
            ccr_config_path(home=tmp_path).parent.glob("config.json.bak-*")
        )
        assert len(bak_files) == 1

    def test_secret_not_leaked(self, monkeypatch, tmp_path: Path) -> None:
        """R-03: SECRET-NO-LEAK for preset path."""
        sentinel = "sk-or-SENTINEL-PRESET-MUST-NOT-LEAK"
        rc, out = self._run(monkeypatch, tmp_path, "open", api_key=sentinel)
        assert rc == 0
        assert sentinel not in out
        models_content = quoin_models_path(home=tmp_path).read_text()
        assert sentinel not in models_content
        cfg = json.loads(ccr_config_path(home=tmp_path).read_text())
        provider = next(p for p in cfg["Providers"] if p.get("name") == "openrouter")
        assert provider["api_key"] == sentinel

    def test_no_ccr_config_writes_models_json_only(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, out = self._run(monkeypatch, tmp_path, "open", with_ccr_config=False)
        assert rc == 0
        assert quoin_models_path(home=tmp_path).exists()
        assert not ccr_config_path(home=tmp_path).exists()

    def test_returns_int(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path, "open")
        assert isinstance(rc, int)


# ── quoin models reset ────────────────────────────────────────────────────────

class TestCmdModelsReset:
    def _run(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        with_ccr_config: bool = True,
        native_flag: bool = False,
    ) -> tuple[int, str]:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)

        if with_ccr_config:
            _write_ccr_config(tmp_path)

        args = _make_args(home=tmp_path, native=native_flag)
        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = _cmd_models_reset(args)
        return rc, buf.getvalue()

    def test_config_byte_identical_after_reset(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """R-04: reset must leave the CCR config byte-identical (only .bak added)."""
        # Pre-create the CCR config so we can capture its content before reset.
        _write_ccr_config(tmp_path)
        config_path = ccr_config_path(home=tmp_path)
        before = config_path.read_text()
        rc, _ = self._run(monkeypatch, tmp_path)
        assert rc == 0
        after = config_path.read_text()
        assert before == after

    def test_backup_created(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path)
        assert rc == 0
        bak_files = list(
            ccr_config_path(home=tmp_path).parent.glob("config.json.bak-*")
        )
        assert len(bak_files) == 1

    def test_models_json_untouched(self, monkeypatch, tmp_path: Path) -> None:
        # Pre-seed a models.json.
        path = quoin_models_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"opus": "x/custom"}) + "\n")
        before = path.read_text()

        rc, _ = self._run(monkeypatch, tmp_path)
        assert rc == 0
        assert path.read_text() == before

    def test_native_flag_identical_to_bare_reset(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # Both should produce the same return code and create a backup.
        config_path = ccr_config_path(home=tmp_path)

        def run_with_flag(flag: bool) -> tuple[int, str]:
            # Reset the CCR config to same state before each run.
            _write_ccr_config(tmp_path)
            # Remove prior backups.
            for bak in config_path.parent.glob("*.bak-*"):
                bak.unlink()
            return self._run(monkeypatch, tmp_path, native_flag=flag)

        rc_bare, _ = run_with_flag(False)
        rc_native, _ = run_with_flag(True)
        assert rc_bare == rc_native == 0

    def test_no_config_returns_zero_with_message(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, out = self._run(monkeypatch, tmp_path, with_ccr_config=False)
        assert rc == 0
        assert "native" in out.lower() or "no ccr config" in out.lower()

    def test_prints_native_launch_instructions(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        rc, out = self._run(monkeypatch, tmp_path)
        assert rc == 0
        assert "claude" in out

    def test_returns_int(self, monkeypatch, tmp_path: Path) -> None:
        rc, _ = self._run(monkeypatch, tmp_path)
        assert isinstance(rc, int)


# ── Import isolation (R-05) ───────────────────────────────────────────────────

class TestImportIsolation:
    def test_importing_installer_leaves_models_out_of_sys_modules(self) -> None:
        """R-05: quoin.installer import must not pull in quoin.models."""
        # Remove models from sys.modules if already loaded (from earlier tests).
        sys.modules.pop("quoin.models", None)
        # Re-import installer.
        import quoin.installer  # noqa: F401 (side-effect import)
        importlib.reload(quoin.installer)
        assert "quoin.models" not in sys.modules

    def test_cli_has_no_top_level_models_import(self) -> None:
        """cli.py must not import quoin.models at module level (lazy import only)."""
        # Check the source text for a top-level import.
        cli_src = (REPO_ROOT / "src" / "quoin" / "cli.py").read_text()
        # Ensure there's no top-level 'import quoin.models' or 'from quoin import models'.
        lines = cli_src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Allow imports inside function bodies (indented).
            if line.startswith((" ", "\t")):
                continue
            if "quoin.models" in stripped or (
                "from quoin import" in stripped and "models" in stripped
            ):
                pytest.fail(
                    f"cli.py has a top-level models import at line {i + 1}: {line!r}"
                )


# ── CCR not-installed path (all subcommands) ──────────────────────────────────

class TestCcrNotInstalledPath:
    """When there's no openrouter provider / no config, set/preset write models.json
    only and print a setup hint; none raise; all return int."""

    def test_set_no_provider_returns_int(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        # Write a CCR config with no openrouter provider.
        path = ccr_config_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_config(path, {"Providers": [{"name": "other"}], "Router": {}})

        args = _make_args(home=tmp_path, tier="opus", model="x/model")
        rc = _cmd_models_set(args)
        assert isinstance(rc, int)
        assert rc == 0  # writes models.json only + hint

    def test_preset_no_provider_returns_int(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        path = ccr_config_path(home=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_config(path, {"Providers": [], "Router": {}})

        args = _make_args(home=tmp_path, name="open")
        rc = _cmd_models_preset(args)
        assert isinstance(rc, int)
        assert rc == 0

    def test_show_no_config_no_ccr_returns_int(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.setattr("quoin.router._verify_ccr", lambda: False)
        monkeypatch.setattr("shutil.which", lambda name: None)
        args = _make_args(home=tmp_path)
        rc = _cmd_models_show(args)
        assert isinstance(rc, int)
        assert rc == 0

    def test_reset_no_config_returns_int(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        args = _make_args(home=tmp_path, native=False)
        rc = _cmd_models_reset(args)
        assert isinstance(rc, int)
        assert rc == 0


# ── CLI wiring via quoin.cli ──────────────────────────────────────────────────

class TestCliWiring:
    """Verify that 'quoin models' sub-commands are wired in cli.py."""

    def _run_cli(self, monkeypatch, tmp_path: Path, argv: list[str]) -> tuple[int, str]:
        monkeypatch.setattr("quoin.ccr_config.probe_service", lambda **kw: False)
        monkeypatch.setattr("quoin.router._verify_ccr", lambda: False)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        from quoin.cli import main

        buf = io.StringIO()
        import builtins
        orig = builtins.print

        def fake_print(*a, file=None, **kw2):
            if file is None or file is sys.stdout:
                orig(*a, file=buf, **kw2)
            else:
                orig(*a, file=file, **kw2)

        monkeypatch.setattr(builtins, "print", fake_print)
        rc = main(argv)
        return rc, buf.getvalue()

    def test_bare_quoin_models_runs_show(self, monkeypatch, tmp_path: Path) -> None:
        """bare 'quoin models' runs the show handler (returns 0, prints tiers)."""
        rc, out = self._run_cli(
            monkeypatch, tmp_path, ["models"]
        )
        assert rc == 0
        # Show prints all three tiers.
        for tier in TIER_KEYS:
            assert tier in out

    def test_quoin_models_set_wired(self, monkeypatch, tmp_path: Path) -> None:
        """'quoin models set' is dispatched to _cmd_models_set."""
        # Use a known-good slug with a home override via _home_override.
        # We can't pass _home_override via argv, so we monkeypatch models_path.
        import quoin.models as _m
        monkeypatch.setattr(
            _m, "read_effective_models", lambda home=None: dict(DEFAULT_MODELS)
        )
        called_with: list[Any] = []

        def fake_write(models, home=None):
            called_with.append(models)
            return quoin_models_path(home=tmp_path)

        monkeypatch.setattr(_m, "write_models", fake_write)
        monkeypatch.setattr(
            "quoin.ccr_config.ccr_config_path",
            lambda home=None: ccr_config_path(home=tmp_path),
        )

        from quoin.cli import main
        rc = main(["models", "set", "haiku", "deepseek/deepseek-v4-flash"])
        assert rc == 0
        assert called_with, "write_models was not called"

    def test_quoin_models_preset_wired(self, monkeypatch, tmp_path: Path) -> None:
        """'quoin models preset open' is dispatched to _cmd_models_preset."""
        import quoin.models as _m
        called: list[bool] = []

        def fake_write(models, home=None):
            called.append(True)
            return quoin_models_path(home=tmp_path)

        monkeypatch.setattr(_m, "write_models", fake_write)
        monkeypatch.setattr(
            "quoin.ccr_config.ccr_config_path",
            lambda home=None: ccr_config_path(home=tmp_path),
        )

        from quoin.cli import main
        rc = main(["models", "preset", "open"])
        assert rc == 0
        assert called

    def test_quoin_models_reset_wired(self, monkeypatch, tmp_path: Path) -> None:
        """'quoin models reset' is dispatched to _cmd_models_reset."""
        monkeypatch.setattr(
            "quoin.ccr_config.ccr_config_path",
            lambda home=None: ccr_config_path(home=tmp_path),
        )

        from quoin.cli import main
        rc = main(["models", "reset"])
        # No config → returns 0 + message.
        assert rc == 0
