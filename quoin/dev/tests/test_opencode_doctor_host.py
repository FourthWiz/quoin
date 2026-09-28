"""Host-environment checks for the OpenCode adapter doctor.

Covers `run_host`: install state, manifest drift, config reading and
subagent-depth detection, the Claude-rules fallback, per-flag truthiness,
and the quoin/opencode PATH and version checks. Skill-discovery census
findings are covered in a separate test module.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _opencode_helpers as helpers

from quoin.opencode_adapter import doctor, generate, install, manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


def _which_none(_name):
    return None


def _which_quoin_only(name):
    return "/usr/bin/quoin" if name == "quoin" else None


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    """A fake HOME plus a git-rooted project directory, source dir is a
    subset copy (fast) unless a test needs the real tree."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    source_dir = helpers.copy_source_subset(tmp_path / "src")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "which", _which_none)
    env = {"HOME": str(home)}
    return home, project, source_dir, env


def _install(project, source_dir, env):
    rendered = generate.render_source_dir(source_dir)
    out, err = [], []
    code = install.run_install(
        project,
        source_dir,
        None,
        False,
        _Sink(out),
        _Sink(err),
        rendered=rendered,
    )
    assert code == 0, "".join(err)
    return rendered


class _Sink:
    def __init__(self, buf):
        self._buf = buf

    def write(self, text):
        self._buf.append(text)


def _run_host(project, source_dir, env, home, which=_which_none, version_runner=None):
    return doctor.run_host(project, source_dir, env, home, which, version_runner)


def test_clean_install_has_no_install_state_warnings_and_reports_current(fixture):
    home, project, source_dir, env = fixture
    rendered = _install(project, source_dir, env)
    findings = _run_host(project, source_dir, env, home)
    ids = {f.id for f in findings}
    assert not ids & {"owned-missing", "owned-modified", "owned-stale", "owned-not-installed", "owned-unreadable"}
    current = [f for f in findings if f.id == "install-current"]
    assert len(current) == 1
    assert current[0].message == "the install matches a fresh render (%d file(s))" % len(rendered)


def test_one_edited_file_triggers_exactly_one_owned_modified(fixture):
    home, project, source_dir, env = fixture
    rendered = _install(project, source_dir, env)
    relpath = next(iter(rendered))
    target = project / relpath
    target.write_bytes(target.read_bytes() + b"\nedited\n")
    findings = _run_host(project, source_dir, env, home)
    modified = [f for f in findings if f.id == "owned-modified"]
    assert len(modified) == 1
    assert modified[0].path == "./" + relpath


def test_one_deleted_file_triggers_exactly_one_owned_missing(fixture):
    home, project, source_dir, env = fixture
    rendered = _install(project, source_dir, env)
    relpath = next(iter(rendered))
    (project / relpath).unlink()
    findings = _run_host(project, source_dir, env, home)
    missing = [f for f in findings if f.id == "owned-missing"]
    assert len(missing) == 1
    assert missing[0].path == "./" + relpath


def test_invalid_metadata_yields_error_and_exit_1(fixture):
    home, project, source_dir, env = fixture
    _install(project, source_dir, env)
    meta_path = project / install.METADATA_RELPATH
    meta_path.write_text("{not json", encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.severity == "error"] and any(
        f.id == "install-metadata-invalid" for f in findings
    )
    invalid = [f for f in findings if f.id == "install-metadata-invalid"][0]
    assert "not json" not in invalid.message
    status = doctor.report_status(findings)
    assert doctor.exit_code(status) == 1


def test_no_install_record_yields_install_absent(fixture):
    home, project, source_dir, env = fixture
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "install-absent"]


def test_owned_unreadable_on_chmod_000_and_run_completes(fixture):
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("requires a non-root POSIX user to enforce chmod 000")
    home, project, source_dir, env = fixture
    rendered = _install(project, source_dir, env)
    relpath = next(iter(rendered))
    target = project / relpath
    os.chmod(target, 0)
    try:
        findings = _run_host(project, source_dir, env, home)
    finally:
        os.chmod(target, 0o644)
    assert [f.id for f in findings if f.id == "owned-unreadable" and f.path == "./" + relpath]


def test_install_version_differs_redacts_non_version_metadata(fixture):
    """Minor 1 regression: `install.py` only checks `isinstance(str)` before
    persisting `quoin_version`/`opencode_version`, so a corrupted or
    hand-edited metadata file can carry an arbitrary string. The doctor
    must never interpolate it verbatim into a finding message."""
    home, project, source_dir, env = fixture
    _install(project, source_dir, env)
    meta_path = project / install.METADATA_RELPATH
    obj = json.loads(meta_path.read_text(encoding="utf-8"))
    obj["quoin_version"] = "v-" + helpers.SEEDED_SECRET
    obj["opencode_version"] = "\x1b[31m1.2.3\x1b[0m"
    meta_path.write_text(json.dumps(obj), encoding="utf-8")

    findings = _run_host(project, source_dir, env, home)
    differs = [f for f in findings if f.id == "install-version-differs"]
    assert len(differs) == 1
    assert helpers.SEEDED_SECRET not in differs[0].message
    assert "\x1b" not in differs[0].message
    assert "unknown" in differs[0].message


def test_home_opencode_dir_reads_its_own_opencode_json_directly(fixture):
    """Major 3 regression: `~/.opencode` already ends in `.opencode`, so it
    must be read directly (`~/.opencode/opencode.json`), never with another
    `.opencode` appended (`~/.opencode/.opencode/opencode.json`, which
    OpenCode itself never reads)."""
    home, project, source_dir, env = fixture
    home_ocd = home / ".opencode"
    home_ocd.mkdir()
    (home_ocd / "opencode.json").write_text(json.dumps({"subagent_depth": 4}), encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    raised = [f for f in findings if f.id == "subagent-depth-raised"]
    assert len(raised) == 1 and "4" in raised[0].message
    assert not [f.id for f in findings if f.id == "config-unreadable"]


def test_home_opencode_dir_jsonc_variant_is_also_read(fixture):
    home, project, source_dir, env = fixture
    home_ocd = home / ".opencode"
    home_ocd.mkdir()
    (home_ocd / "opencode.jsonc").write_text(
        "{\n  // a comment\n  \"subagent_depth\": 2,\n}\n", encoding="utf-8"
    )
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "subagent-depth-raised"]
    assert not [f.id for f in findings if f.id == "config-unreadable"]


def test_manifest_drift_reports_count(fixture, monkeypatch):
    home, project, source_dir, env = fixture
    monkeypatch.setattr(manifest, "check_source_dir", lambda _sd: ["a", "b", "c"])
    findings = _run_host(project, source_dir, env, home)
    drift = [f for f in findings if f.id == "manifest-drift"]
    assert len(drift) == 1
    assert "3" in drift[0].message


def test_manifest_unreadable_on_recursion_error(fixture, monkeypatch):
    home, project, source_dir, env = fixture

    def _boom(_sd):
        raise RecursionError("boom")

    monkeypatch.setattr(manifest, "check_source_dir", _boom)
    findings = _run_host(project, source_dir, env, home)
    unreadable = [f for f in findings if f.id == "manifest-unreadable"]
    assert len(unreadable) == 1
    assert "boom" not in unreadable[0].message


def test_global_claude_md_with_no_agents_md_warns(fixture):
    home, project, source_dir, env = fixture
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "rules-global-claude-md"]


def test_global_claude_md_warn_suppressed_by_prompt_flag(fixture):
    home, project, source_dir, env = fixture
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    env = dict(env, OPENCODE_DISABLE_CLAUDE_CODE_PROMPT="1")
    findings = _run_host(project, source_dir, env, home)
    assert not [f.id for f in findings if f.id == "rules-global-claude-md"]


def test_project_claude_md_without_agents_md_warns_and_agents_md_is_info_only(fixture):
    home, project, source_dir, env = fixture
    (project / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "rules-project-claude-md"]

    (project / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert not [f.id for f in findings if f.id == "rules-project-claude-md"]
    assert [f.id for f in findings if f.id == "rules-agents-md-present"]


def test_project_opencode_json_subagent_depth_warns_with_number(fixture):
    home, project, source_dir, env = fixture
    (project / "opencode.json").write_text(json.dumps({"subagent_depth": 3}), encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    raised = [f for f in findings if f.id == "subagent-depth-raised"]
    assert len(raised) == 1
    assert "3" in raised[0].message


def test_top_level_one_with_raised_experimental_still_warns_with_no_in_effect_claim(fixture):
    home, project, source_dir, env = fixture
    (project / "opencode.json").write_text(
        json.dumps({"subagent_depth": 1, "experimental": {"subagent_depth": 4}}), encoding="utf-8"
    )
    findings = _run_host(project, source_dir, env, home)
    raised = [f for f in findings if f.id == "subagent-depth-raised"]
    assert len(raised) == 1
    assert "4" in raised[0].message
    assert "in effect" not in raised[0].message.lower()
    assert raised[0].remediation and "in effect" not in raised[0].remediation.lower()
    assert "effective depth is" not in raised[0].message.lower()


def test_jsonc_with_comments_and_trailing_comma_parses(fixture):
    home, project, source_dir, env = fixture
    text = "{\n  // a comment\n  \"subagent_depth\": 2,\n}\n"
    (project / "opencode.jsonc").write_text(text, encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "subagent-depth-raised"]
    assert not [f.id for f in findings if f.id == "config-unreadable"]


def test_jsonc_trailing_comma_regex_does_not_touch_a_string_value(fixture):
    """Minor 6 regression: a `,}`/`,]` substring inside a JSON string value
    must survive; only a genuine trailing comma before a closing brace or
    bracket, outside any string literal, is stripped."""
    home, project, source_dir, env = fixture
    text = json.dumps({"subagent_depth": 2, "instructions": ["a value with a ,} inside it"]})
    (project / "opencode.json").write_text(text, encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "subagent-depth-raised"]
    assert not [f.id for f in findings if f.id == "config-unreadable"]


def test_strip_jsonc_leaves_a_string_bearing_trailing_comma_syntax_intact():
    text = '{\n  "a": "x,}",\n  "b": 1,\n}\n'
    stripped = doctor._strip_jsonc(text)
    payload = json.loads(stripped)
    assert payload == {"a": "x,}", "b": 1}


def test_big_int_subagent_depth_does_not_crash_and_is_config_unreadable(fixture):
    """Major 2 regression: a 5000-digit integer literal trips Python's
    int-string-conversion limit inside `json.loads` with a bare `ValueError`
    (not `json.JSONDecodeError`), on Python 3.14+. The doctor must degrade
    to a `config-unreadable` finding, not crash."""
    home, project, source_dir, env = fixture
    text = '{"subagent_depth": %s}' % ("9" * 5000)
    (project / "opencode.json").write_text(text, encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "config-unreadable"]
    assert not [f.id for f in findings if f.id == "subagent-depth-raised"]


class _OutSink:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def getvalue(self):
        return "".join(self.parts)


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom"),
        OSError("boom"),
        RecursionError("boom"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "boom"),
        json.JSONDecodeError("boom", "doc", 0),
    ],
    ids=["ValueError", "OSError", "RecursionError", "UnicodeDecodeError", "JSONDecodeError"],
)
def test_run_doctor_boundary_catches_every_documented_exception_class(fixture, monkeypatch, exc):
    """Major 2 regression: `run_doctor` must catch each documented
    exception class raised anywhere in the host-mode path and emit a
    redacted finding with a non-zero exit, never let a traceback (which
    could carry a raw path or config value) escape to the caller."""
    home, project, source_dir, env = fixture

    def _boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(doctor, "run_host", _boom)

    for as_json in (False, True):
        out, err = _OutSink(), _OutSink()
        code = doctor.run_doctor(project, source_dir, False, as_json, out, err, env=env, home=home, which=_which_none)
        assert code != 0
        combined = out.getvalue() + err.getvalue()
        assert "Traceback" not in combined
        assert "boom" not in combined
        assert str(home) not in combined
        assert str(project) not in combined
        assert "doctor-internal-error" in combined
        if as_json:
            payload = json.loads(out.getvalue())
            assert {f["id"] for f in payload["findings"]} == {"doctor-internal-error"}


def test_garbage_config_yields_config_unreadable(fixture):
    home, project, source_dir, env = fixture
    (project / "opencode.json").write_text("not json at all", encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "config-unreadable"]


def test_opencode_config_dir_reads_its_own_opencode_json_and_its_config_json_is_not_read(fixture):
    home, project, source_dir, env = fixture
    ocd = home / "custom-ocd"
    ocd.mkdir()
    (ocd / "opencode.json").write_text(json.dumps({"subagent_depth": 2}), encoding="utf-8")
    (ocd / "config.json").write_text("not json", encoding="utf-8")
    env = dict(env, OPENCODE_CONFIG_DIR=str(ocd))
    findings = _run_host(project, source_dir, env, home)
    assert [f.id for f in findings if f.id == "subagent-depth-raised"]
    assert not [f.id for f in findings if f.id == "config-unreadable"]


def test_opencode_config_dir_global_agents_md_lookup(fixture):
    home, project, source_dir, env = fixture
    ocd = home / "custom-ocd"
    ocd.mkdir()
    (ocd / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    env = dict(env, OPENCODE_CONFIG_DIR=str(ocd))
    findings = _run_host(project, source_dir, env, home)
    assert not [f.id for f in findings if f.id == "rules-global-claude-md"]


def test_opencode_config_env_var_subagent_depth_and_config_env_set(fixture):
    home, project, source_dir, env = fixture
    cfg = home / "custom.json"
    cfg.write_text(json.dumps({"subagent_depth": 5}), encoding="utf-8")
    env = dict(env, OPENCODE_CONFIG=str(cfg))
    findings = _run_host(project, source_dir, env, home)
    raised = [f for f in findings if f.id == "subagent-depth-raised"]
    assert len(raised) == 1 and "5" in raised[0].message
    names_finding = [f for f in findings if f.id == "config-env-set"][0]
    assert "OPENCODE_CONFIG" in names_finding.message


def test_disable_project_config_true_and_1_are_errors_yes_is_not(fixture):
    home, project, source_dir, env = fixture
    (project / "CLAUDE.md").write_text("rules\n", encoding="utf-8")

    for value in ("true", "1"):
        findings = _run_host(project, source_dir, dict(env, OPENCODE_DISABLE_PROJECT_CONFIG=value), home)
        assert [f.id for f in findings if f.id == "project-config-disabled"], value
        assert not [f.id for f in findings if f.id == "rules-project-claude-md"], value

    findings = _run_host(project, source_dir, dict(env, OPENCODE_DISABLE_PROJECT_CONFIG="yes"), home)
    assert not [f.id for f in findings if f.id == "project-config-disabled"]
    assert [f.id for f in findings if f.id == "rules-project-claude-md"]


def test_disable_claude_code_prompt_yes_suppresses_rules_warning(fixture):
    home, project, source_dir, env = fixture
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    findings = _run_host(project, source_dir, dict(env, OPENCODE_DISABLE_CLAUDE_CODE_PROMPT="yes"), home)
    assert not [f.id for f in findings if f.id == "rules-global-claude-md"]


def test_flags_set_names_only_set_flags(fixture):
    home, project, source_dir, env = fixture
    env = dict(env, OPENCODE_DISABLE_EXTERNAL_SKILLS="1", OPENCODE_DISABLE_CLAUDE_CODE_SKILLS="yes")
    findings = _run_host(project, source_dir, env, home)
    flags = [f for f in findings if f.id == "flags-set"][0]
    assert "OPENCODE_DISABLE_EXTERNAL_SKILLS" in flags.message
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS" in flags.message
    assert "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT" not in flags.message


def test_quoin_not_on_path_warns_when_which_returns_none(fixture):
    home, project, source_dir, env = fixture
    findings = _run_host(project, source_dir, env, home, which=_which_none)
    assert [f.id for f in findings if f.id == "quoin-not-on-path"]
    findings = _run_host(project, source_dir, env, home, which=_which_quoin_only)
    assert not [f.id for f in findings if f.id == "quoin-not-on-path"]


def test_opencode_binary_absent_is_info(fixture):
    home, project, source_dir, env = fixture
    findings = _run_host(project, source_dir, env, home, which=_which_none)
    assert [f.id for f in findings if f.id == "opencode-binary-absent"]


def test_version_runner_match_differ_timeout_garbage(fixture):
    home, project, source_dir, env = fixture

    def which_opencode(name):
        return "/usr/bin/opencode" if name == "opencode" else None

    pinned = manifest.read_pinned_version(source_dir)

    findings = _run_host(project, source_dir, env, home, which=which_opencode, version_runner=lambda _p: pinned)
    versioned = [f for f in findings if f.id == "opencode-version"][0]
    assert pinned in versioned.message

    findings = _run_host(project, source_dir, env, home, which=which_opencode, version_runner=lambda _p: "9.9.9")
    versioned = [f for f in findings if f.id == "opencode-version"][0]
    assert "9.9.9" in versioned.message and pinned in versioned.message

    findings = _run_host(project, source_dir, env, home, which=which_opencode, version_runner=lambda _p: None)
    assert [f.id for f in findings if f.id == "opencode-version-unknown"]

    findings = _run_host(
        project, source_dir, env, home, which=which_opencode,
        version_runner=lambda _p: "garbage " + helpers.SEEDED_SECRET,
    )
    unknown = [f for f in findings if f.id == "opencode-version-unknown"]
    assert unknown
    assert helpers.SEEDED_SECRET not in doctor.render_text(findings, doctor.report_status(findings))


def test_default_version_runner_passes_scratch_xdg_dirs(fixture, monkeypatch):
    home, project, source_dir, env = fixture
    captured = {}
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_run([sys.executable, "-c", "print('opencode 1.2.3')"], capture_output=True)

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = doctor._default_version_runner("opencode")
    assert "1.2.3" in output
    passed_env = captured["env"]
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert passed_env[var] != os.environ.get(var)


def test_exit_codes_warnings_error_and_clean(fixture):
    home, project, source_dir, env = fixture
    _install(project, source_dir, env)
    findings = doctor.run_host(project, source_dir, env, home, _which_quoin_only, lambda _p: None)
    assert doctor.exit_code(doctor.report_status(findings)) == 0

    (project / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    findings = doctor.run_host(project, source_dir, env, home, _which_quoin_only, lambda _p: None)
    assert doctor.report_status(findings) == "warnings"
    assert doctor.exit_code("warnings") == 4

    env_err = dict(env, OPENCODE_DISABLE_PROJECT_CONFIG="1")
    findings = doctor.run_host(project, source_dir, env_err, home, _which_quoin_only, lambda _p: None)
    assert any(f.severity == "error" for f in findings)
    assert doctor.exit_code(doctor.report_status(findings)) == 1
