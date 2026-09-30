"""Tests for the personal-profile import preview: the pure proposal, the
guarded write and the `config import-preview` command."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin import cli
from quoin.opencode_adapter import config, import_preview as ip, paths
from quoin.opencode_adapter.errors import ConfigErrors
from quoin.opencode_adapter.jsonio import UnsafeDirectoryError

SRC = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter" / "import_preview.py"
LARGE, MEDIUM, SMALL = "vendor-a/model-large", "vendor-a/model-medium", "vendor-b/model-small"
THREE = {"opus": LARGE, "sonnet": MEDIUM, "haiku": SMALL}


def opencode_env(home):
    return {"XDG_CONFIG_HOME": str(home / "xdgcfg"), "XDG_STATE_HOME": str(home / "xdgstate")}


# --------------------------------------------------------------- propose


def test_three_distinct_slugs_make_three_models():
    proposal = ip.propose(THREE)
    doc = proposal.document
    assert set(doc["models"]) == {"or-opus", "or-sonnet", "or-haiku"}
    assert doc["models"]["or-sonnet"] == {
        "provider": "openrouter", "model_id": MEDIUM, "qualification_ref": "local:or-sonnet",
    }
    assert doc["default_model"] == "or-sonnet" and doc["auxiliary_model"] == "or-haiku"
    roles = {role: entry["model"] for role, entry in doc["roles"].items()}
    assert roles == {
        "architect": "or-opus", "planner": "or-opus", "critic": "or-opus", "reviewer": "or-opus",
        "coordinator": "or-sonnet", "investigator": "or-sonnet", "implementer": "or-sonnet",
        "gate": "or-haiku",
    }
    assert all(set(entry) == {"model"} for entry in doc["roles"].values())
    assert proposal.model_ids == tuple(sorted({LARGE, MEDIUM, SMALL}))
    assert doc["classification"] == "personal" and doc["profile"] == "personal"
    assert doc["providers"]["openrouter"]["credential_ref"] == "env:OPENROUTER_API_KEY"


def test_equal_slugs_collapse_into_the_first_tier_name():
    doc = ip.propose({"opus": LARGE, "sonnet": MEDIUM, "haiku": MEDIUM}).document
    assert set(doc["models"]) == {"or-opus", "or-sonnet"}
    assert doc["auxiliary_model"] == "or-sonnet" and doc["roles"]["gate"]["model"] == "or-sonnet"


def test_all_equal_slugs_make_one_model():
    proposal = ip.propose({"opus": LARGE, "sonnet": LARGE, "haiku": LARGE})
    assert set(proposal.document["models"]) == {"or-opus"}
    assert {e["model"] for e in proposal.document["roles"].values()} == {"or-opus"}
    assert proposal.document["default_model"] == "or-opus"
    assert proposal.model_ids == (LARGE,)


def test_the_proposal_validates_and_round_trips():
    proposal = ip.propose(THREE, profile_name="mine")
    layer = config.validate_layer_data(
        json.loads(proposal.text), "profile", file_label="profiles/mine.json", expected_profile="mine"
    )
    assert layer.data == proposal.document
    assert proposal.text.endswith("\n") and json.loads(proposal.text) == proposal.document


@pytest.mark.parametrize("slug", ["{env:SOME_KEY}", "REPLACE_WITH_MODEL"])
def test_placeholder_slugs_are_configuration_errors(slug):
    with pytest.raises(ConfigErrors):
        ip.propose({**THREE, "sonnet": slug})


@pytest.mark.parametrize("slug", ["vendor a/model", "vendor/model\n", "vendor/\tmodel", " ", ""])
def test_slugs_with_whitespace_or_nothing_are_refused_with_fixed_text(slug):
    with pytest.raises(ValueError) as info:
        ip.propose({**THREE, "sonnet": slug})
    assert slug.strip() not in str(info.value) or slug.strip() == ""


def test_the_slug_rule_is_the_one_the_compiler_uses():
    from quoin.opencode_adapter import compiler, errors

    assert compiler.BAD_MODEL_ID_RE is errors.BAD_MODEL_ID_RE


@pytest.mark.parametrize("name", ["", "Bad Name", "../x", "a" * 80, None])
def test_bad_profile_names(name):
    with pytest.raises(ValueError):
        ip.propose(THREE, profile_name=name)


def test_missing_tiers_are_refused():
    with pytest.raises(ValueError):
        ip.propose({"opus": LARGE, "sonnet": MEDIUM})


# ----------------------------------------------------------- read_source


def _write_models(home, data):
    target = home / ".config" / "quoin" / "models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")


def test_read_source_from_a_models_file(tmp_path):
    _write_models(tmp_path, THREE)
    tiers, from_file = ip.read_source(tmp_path)
    assert from_file is True and tiers == THREE


def test_read_source_falls_back_to_the_built_in_defaults(tmp_path):
    tiers, from_file = ip.read_source(tmp_path)
    assert from_file is False and set(tiers) == {"opus", "sonnet", "haiku"}
    assert all(isinstance(v, str) and v for v in tiers.values())


# ----------------------------------------------------------------- apply


def test_apply_writes_a_private_profile_that_loads(tmp_path):
    env = opencode_env(tmp_path)
    proposal = ip.propose(THREE)
    target = ip.apply(proposal, confirmed=list(proposal.model_ids), force=False, env=env, home=tmp_path)
    assert target == paths.profile_path("personal", env, tmp_path)
    assert target.read_text(encoding="utf-8") == proposal.text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert config.load_profile("personal", env=env, home=tmp_path).data == proposal.document


def test_apply_refuses_an_open_profiles_directory(tmp_path):
    env = opencode_env(tmp_path)
    directory = paths.profiles_dir(env, tmp_path)
    directory.mkdir(parents=True)
    os.chmod(directory, 0o777)
    proposal = ip.propose(THREE)
    with pytest.raises(UnsafeDirectoryError):
        ip.apply(proposal, confirmed=list(proposal.model_ids), force=False, env=env, home=tmp_path)
    assert list(directory.iterdir()) == []


def _refused(tmp_path, confirmed, **kw):
    env = opencode_env(tmp_path)
    proposal = ip.propose(THREE)
    with pytest.raises(ip.ImportRefused) as info:
        ip.apply(proposal, confirmed=confirmed, force=kw.get("force", False), env=env, home=tmp_path)
    assert not paths.profile_path("personal", env, tmp_path).exists()
    return info.value


def test_a_missing_confirmation_refuses(tmp_path):
    exc = _refused(tmp_path, [LARGE, MEDIUM])
    assert exc.code == "unconfirmed" and exc.missing == (SMALL,) and exc.extra_count == 0
    assert SMALL in str(exc)


def test_an_extra_confirmation_refuses_and_is_only_counted(tmp_path):
    exc = _refused(tmp_path, [LARGE, MEDIUM, SMALL, "vendor-c/other-secret-shaped"])
    assert exc.code == "unconfirmed" and exc.missing == () and exc.extra_count == 1
    assert "vendor-c" not in str(exc) and "1 confirmed" in str(exc)


def test_the_logical_name_does_not_stand_in_for_the_slug(tmp_path):
    exc = _refused(tmp_path, [LARGE, "or-sonnet", SMALL])
    assert exc.missing == (MEDIUM,) and exc.extra_count == 1


def test_an_existing_profile_needs_force(tmp_path):
    env = opencode_env(tmp_path)
    target = paths.profile_path("personal", env, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    proposal = ip.propose(THREE)
    with pytest.raises(ip.ImportRefused) as info:
        ip.apply(proposal, confirmed=list(proposal.model_ids), force=False, env=env, home=tmp_path)
    assert info.value.code == "exists" and target.read_bytes() == b"old"
    ip.apply(proposal, confirmed=list(proposal.model_ids), force=True, env=env, home=tmp_path)
    assert target.read_text(encoding="utf-8") == proposal.text


def test_a_symlink_at_the_target_counts_as_existing(tmp_path):
    env = opencode_env(tmp_path)
    target = paths.profile_path("personal", env, tmp_path)
    target.parent.mkdir(parents=True)
    other = tmp_path / "elsewhere.json"
    other.write_bytes(b"keep")
    target.symlink_to(other)
    proposal = ip.propose(THREE)
    with pytest.raises(ip.ImportRefused):
        ip.apply(proposal, confirmed=list(proposal.model_ids), force=False, env=env, home=tmp_path)
    assert other.read_bytes() == b"keep"


# ------------------------------------------------------------ module rules


def test_module_does_not_reach_router_or_ccr_code():
    tree = ast.parse(SRC.read_text(encoding="utf-8"), feature_version=(3, 10))
    top_level = {id(n) for n in tree.body}
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "") + "." + a.name if node.level == 0 else node.module or "" for a in node.names]
        for name in names:
            assert not name.startswith(("quoin.router", "quoin.ccr_store", "quoin.ccr_config")), name
            if id(node) in top_level:
                assert not name.startswith("quoin.models") and name != "quoin.models", name
        if isinstance(node, ast.Attribute) and node.attr == "write_models":
            raise AssertionError("the preview must never write the models file")
        assert not (isinstance(node, ast.Name) and node.id == "write_models")


def test_importing_the_module_loads_neither_models_nor_router():
    code = (
        "import sys, quoin.opencode_adapter.import_preview\n"
        "bad = [m for m in ('quoin.models', 'quoin.router', 'quoin.ccr_store', 'quoin.ccr_config') if m in sys.modules]\n"
        "print(bad)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# ----------------------------------------------------------------- the CLI


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        out[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), digest, info.st_mtime_ns)
    return out


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdgcfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdgstate"))
    monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    _write_models(home, THREE)
    ccr = home / ".claude-code-router"
    ccr.mkdir()
    (ccr / "config.json").write_text('{"Providers": []}', encoding="utf-8")
    (ccr / "config.sqlite").write_bytes(b"not really a database")
    return home


def run(capsys, *argv):
    code = cli.main(["opencode", "config", "import-preview", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def confirm_args(*ids):
    args = ["--apply"]
    for one in ids:
        args += ["--confirm-model-id", one]
    return args


def test_preview_prints_the_proposal_and_writes_nothing(home, capsys):
    before = snapshot(home)
    code, out, err = run(capsys)
    assert code == 0 and err == ""
    assert "source: models.json (read only)" in out
    assert "target: " + str(paths.profile_path("personal", opencode_env(home), home)) in out
    assert json.dumps(MEDIUM) in out and "or-sonnet" in out
    assert "not the or-* names" in out
    assert "quoin opencode probe --profile personal --synthetic-only --model MODEL" in out
    assert snapshot(home) == before


def test_preview_names_the_built_in_defaults_source(home, capsys):
    (home / ".config" / "quoin" / "models.json").unlink()
    before = snapshot(home)
    code, out, _ = run(capsys)
    assert code == 0 and "source: built-in defaults (no models.json)" in out
    assert snapshot(home) == before


def test_apply_with_a_missing_id_writes_nothing(home, capsys):
    before = snapshot(home)
    code, out, err = run(capsys, *confirm_args(LARGE, MEDIUM))
    assert code == 2 and SMALL in err and out == ""
    assert snapshot(home) == before


def test_apply_with_an_extra_id_writes_nothing(home, capsys):
    before = snapshot(home)
    code, _, err = run(capsys, *confirm_args(LARGE, MEDIUM, SMALL, "vendor-z/extra"))
    assert code == 2 and "vendor-z" not in err
    assert snapshot(home) == before


def test_apply_with_exact_confirmation_adds_only_the_profile(home, capsys):
    before = snapshot(home)
    code, out, err = run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))
    assert code == 0 and err == ""
    target = paths.profile_path("personal", opencode_env(home), home)
    assert out.strip().splitlines()[-1] == "written: %s" % target
    after = snapshot(home)
    added = set(after) - set(before)
    rel = lambda p: str(p.relative_to(home))  # noqa: E731
    created = {rel(target)} | {rel(p) for p in target.parents if p != home and home in p.parents}
    assert added == created and len(created) == 5
    assert {k: v for k, v in after.items() if k in before} == before
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_existing_profile_is_kept_without_force(home, capsys):
    assert run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))[0] == 0
    before = snapshot(home)
    code, _, err = run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))
    assert code == 2 and "--force" in err
    assert snapshot(home) == before
    assert run(capsys, *confirm_args(LARGE, MEDIUM, SMALL), "--force")[0] == 0


def test_confirm_and_force_need_apply(home, capsys):
    before = snapshot(home)
    for extra in (["--confirm-model-id", LARGE], ["--force"]):
        code, out, err = run(capsys, *extra)
        assert code == 2 and "--apply" in err and out == ""
    assert snapshot(home) == before


def test_a_bad_profile_name_is_refused(home, capsys):
    code, _, err = run(capsys, "--profile-name", "Bad Name")
    assert code == 2 and "profile name" in err


def test_a_bad_models_file_slug_is_refused_without_echoing_it(home, capsys):
    _write_models(home, {**THREE, "sonnet": "vendor a/secret-ish"})
    code, out, err = run(capsys)
    assert code == 2 and out == "" and "secret-ish" not in err


def test_an_open_profiles_directory_is_refused(home, capsys):
    directory = paths.profiles_dir(opencode_env(home), home)
    directory.mkdir(parents=True)
    os.chmod(directory, 0o777)
    code, _, err = run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))
    assert code == 2 and "not private enough" in err and str(directory) not in err
    assert list(directory.iterdir()) == []


def test_filesystem_errors_print_fixed_text(home, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("/hidden/path")

    monkeypatch.setattr(ip, "apply", boom)
    code, _, err = run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))
    assert code == 2 and "/hidden/path" not in err and err.strip()


def test_the_written_profile_reaches_the_qualification_step(home, capsys):
    assert run(capsys, *confirm_args(LARGE, MEDIUM, SMALL))[0] == 0
    project = home / "project"
    (project / ".quoin").mkdir(parents=True)
    (project / ".quoin" / "runtime.json").write_text(
        json.dumps({"schema_version": 1, "runtime": "opencode", "profile": "personal", "classification": "personal"}),
        encoding="utf-8",
    )
    code = cli.main(["opencode", "config", "explain", "--profile", "personal", "--project-root", str(project)])
    out = capsys.readouterr()
    assert code == 1  # blocked only by missing qualification records
    assert "qualification-missing" in out.out + out.err
    assert "invalid" not in out.err
