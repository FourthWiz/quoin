"""Skill-discovery census checks for the OpenCode adapter doctor.

Covers `read_skill_name` (the census's own tolerant frontmatter-name
reader), `census`/`_census_findings` (duplicate, legacy-discovery and
outside-project findings), `_permission_loosened_findings`, and the
redaction of every secret-bearing channel the host census reads.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import _opencode_helpers as helpers

from quoin.opencode_adapter import doctor, generate, install

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
REAL_CLAUDE_SKILLS_DIR = SOURCE_DIR / "adapters" / "claude" / "skills"


def _which_none(_name):
    return None


class _Sink:
    def __init__(self):
        self.buf = []

    def write(self, text):
        self.buf.append(text)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
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


def _run_host(project, source_dir, env, home):
    return doctor.run_host(project, source_dir, env, home, _which_none, None)


def _write_skill(path: Path, name: str, body: str = "content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: %s\n---\n\n# %s\n\n%s\n" % (name, name, body), encoding="utf-8")


# --- read_skill_name -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_kind,expected_name",
    [
        ("---\nname: plan\n---\nbody", "named", "plan"),
        ("---\nname: plan  # a comment\n---\n", "named", "plan"),
        ("---\nname: 'it''s-fine'\n---\n", "named", "it's-fine"),
        ('---\nname: "double quoted"\n---\n', "named", "double quoted"),
        ("---\r\nname: plan\r\n---\r\nbody", "named", "plan"),
        ("﻿---\nname: plan\n---\n", "named", "plan"),
        ("description: >\n  block\nname: plan\n---\n", "absent", None),
        ("no fence at all", "absent", None),
        ("---\nno name key here\n---\n", "absent", None),
        ("---\nname: >\n  block scalar\n---\n", "unverified", None),
        ("---\nname: [a, b]\n---\n", "unverified", None),
        ("---\nname: {a: 1}\n---\n", "unverified", None),
        ("---\nname: true\n---\n", "unverified", None),
        ("---\nname: 123\n---\n", "unverified", None),
        ("---\nname: foo\nname: bar\n---\n", "unverified", None),
        ("---\nname: plan\nno closing fence", "unverified", None),
        ("---\n  name: indented\n---\n", "absent", None),
        ("---\nname: \n---\n", "unverified", None),
    ],
)
def test_read_skill_name_cases(text, expected_kind, expected_name):
    kind, name = doctor.read_skill_name(text)
    assert kind == expected_kind
    assert name == expected_name


def test_read_skill_name_over_real_claude_skill_headers():
    """The census's own reader must read every real, unquoted Claude skill
    header with certainty; `frontmatter.parse` rejects at least one of
    them, which is why the census does not use it."""
    from quoin.opencode_adapter import frontmatter

    skill_files = sorted(REAL_CLAUDE_SKILLS_DIR.glob("*/SKILL.md"))
    assert skill_files, "fixture source tree is missing its Claude skills"

    rejected_by_frontmatter = 0
    for path in skill_files:
        text = path.read_text(encoding="utf-8")
        kind, name = doctor.read_skill_name(text)
        assert kind == "named", "expected named for %s, got %s" % (path, kind)
        assert name == path.parent.name
        try:
            frontmatter.parse(text)
        except frontmatter.FrontmatterError:
            rejected_by_frontmatter += 1
    assert rejected_by_frontmatter >= 1


# --- census: duplicates, aliases, legacy discovery ------------------------


def test_duplicate_across_claude_agents_and_project_opencode(fixture):
    home, project, source_dir, env = fixture
    _write_skill(home / ".claude" / "skills" / "dup" / "SKILL.md", "dup")
    _write_skill(home / ".agents" / "skills" / "dup" / "SKILL.md", "dup")
    _write_skill(project / ".opencode" / "skills" / "dup" / "SKILL.md", "dup")

    findings = _run_host(project, source_dir, env, home)
    dup = [f for f in findings if f.id in ("skill-duplicate", "skill-duplicate-unnamed")]
    assert len(dup) == 1
    assert dup[0].id == "skill-duplicate-unnamed"  # "dup" is not a catalog id or a generated quoin-* name
    assert dup[0].remediation == doctor._SKILL_DUP_REMEDIATION
    assert dup[0].message.count(",") == 2  # three locations, comma-separated


def test_symlink_alias_is_one_skill_not_a_duplicate(fixture):
    home, project, source_dir, env = fixture
    real_dir = home / ".agents" / "skills" / "x"
    real_dir.mkdir(parents=True)
    _write_skill(real_dir / "SKILL.md", "x")
    alias_parent = home / ".claude" / "skills"
    alias_parent.mkdir(parents=True)
    os.symlink(real_dir, alias_parent / "x")

    findings = _run_host(project, source_dir, env, home)
    dup = [f for f in findings if f.id in ("skill-duplicate", "skill-duplicate-unnamed")]
    assert not dup

    other_copy = project / ".opencode" / "skills" / "x"
    _write_skill(other_copy / "SKILL.md", "x")
    findings2 = _run_host(project, source_dir, env, home)
    dup2 = [f for f in findings2 if f.id in ("skill-duplicate", "skill-duplicate-unnamed")]
    assert len(dup2) == 1
    assert dup2[0].message.count(",") == 1  # exactly two locations now


def test_census_scan_order_is_deterministic(fixture):
    """Carried critic minor: the census walks each directory's children in
    sorted order, so two runs over the same tree agree exactly."""
    home, project, source_dir, env = fixture
    for letter in ("c", "a", "b"):
        _write_skill(home / ".agents" / "skills" / letter / "SKILL.md", letter)

    docs, _ = doctor._read_config_files(project, project, env, home, doctor._build_roots(project, env, home))
    census_map_1, _, _, _ = doctor.census(project, project, env, home, docs)
    census_map_2, _, _, _ = doctor.census(project, project, env, home, docs)
    assert list(census_map_1) == list(census_map_2)
    assert {name: [str(p) for p in locs] for name, locs in census_map_1.items()} == {
        name: [str(p) for p in locs] for name, locs in census_map_2.items()
    }


def test_unverified_file_is_excluded_from_duplicate_grouping(fixture):
    home, project, source_dir, env = fixture
    (home / ".agents" / "skills" / "u1").mkdir(parents=True)
    (home / ".agents" / "skills" / "u1" / "SKILL.md").write_text("---\nname: >\n---\n", encoding="utf-8")
    _write_skill(home / ".claude" / "skills" / "u2" / "SKILL.md", "u2")

    findings = _run_host(project, source_dir, env, home)
    unverified = [f for f in findings if f.id == "census-unverified"]
    assert len(unverified) == 1
    assert unverified[0].message.startswith("1 ")
    dup = [f for f in findings if f.id in ("skill-duplicate", "skill-duplicate-unnamed")]
    assert not dup


def test_legacy_claude_skills_warns_and_downgrades_to_info_with_flag(fixture):
    home, project, source_dir, env = fixture
    _write_skill(home / ".claude" / "skills" / "plan" / "SKILL.md", "plan")
    _write_skill(home / ".claude" / "skills" / "review" / "SKILL.md", "review")

    findings = _run_host(project, source_dir, env, home)
    legacy = [f for f in findings if f.id == "legacy-claude-skills"]
    assert len(legacy) == 1
    assert legacy[0].severity == "warn"
    assert legacy[0].message.startswith("2 ")

    env2 = dict(env, OPENCODE_DISABLE_CLAUDE_CODE_SKILLS="1")
    findings2 = _run_host(project, source_dir, env2, home)
    legacy2 = [f for f in findings2 if f.id == "legacy-claude-skills"]
    assert len(legacy2) == 1
    assert legacy2[0].severity == "info"
    assert legacy2[0].message.startswith("2 ")


def test_quoin_skill_outside_project(fixture):
    home, project, source_dir, env = fixture
    _write_skill(home / ".agents" / "skills" / "quoin-plan" / "SKILL.md", "quoin-plan")

    findings = _run_host(project, source_dir, env, home)
    outside = [f for f in findings if f.id == "quoin-skill-outside-project"]
    assert len(outside) == 1
    assert outside[0].message == "a skill named 'quoin-plan' was found outside the project's own .opencode/skills directory"


def test_duplicate_remediation_names_no_location_and_adds_url_sentence_when_configured(fixture):
    home, project, source_dir, env = fixture
    _write_skill(home / ".claude" / "skills" / "plan" / "SKILL.md", "plan")
    project_opencode = project / ".opencode" / "skills" / "plan"
    _write_skill(project_opencode / "SKILL.md", "plan")

    (project / "opencode.json").write_text(
        json.dumps({"skills": {"urls": ["https://example.invalid/skill.tar"]}}), encoding="utf-8"
    )

    findings = _run_host(project, source_dir, env, home)
    dup = [f for f in findings if f.id == "skill-duplicate"]
    assert len(dup) == 1
    assert str(home) not in dup[0].remediation
    assert str(project) not in dup[0].remediation
    assert "URL" in dup[0].remediation


# --- permission-loosened ---------------------------------------------------


def test_permission_loosened_bare_string_names_every_generated_tool(fixture):
    home, project, source_dir, env = fixture
    (project / "opencode.json").write_text(json.dumps({"permission": "ask"}), encoding="utf-8")
    findings = _run_host(project, source_dir, env, home)
    loosened = [f for f in findings if f.id == "permission-loosened"]
    assert len(loosened) == 1
    named = loosened[0].message.split(": ", 1)[1]
    assert sorted(n.strip() for n in named.split(",")) == sorted(doctor._TOOLS_SOME_ROLE_ALLOWS)


def test_permission_loosened_per_tool_map(fixture):
    home, project, source_dir, env = fixture
    (project / "opencode.json").write_text(
        json.dumps({"permission": {"bash": "deny", "edit": {"*": "ask"}}}), encoding="utf-8"
    )
    findings = _run_host(project, source_dir, env, home)
    loosened = [f for f in findings if f.id == "permission-loosened"]
    assert len(loosened) == 1
    named = sorted(n.strip() for n in loosened[0].message.split(": ", 1)[1].split(","))
    assert named == ["bash", "edit"]


# --- mandatory redaction test ----------------------------------------------


def test_census_never_prints_a_secret(fixture, monkeypatch):
    """Seed a fake secret into every channel the host census reads, then
    assert it never appears in text or JSON output, in host or smoke mode.
    """
    home, project, source_dir, env = fixture
    secret = helpers.SEEDED_SECRET

    real_home = home.parent / ("home-" + secret)
    real_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: real_home)

    xdg = home.parent / ("xdg-" + secret)
    (xdg / "opencode").mkdir(parents=True)
    dup_a = xdg / "opencode" / "skills" / "dupsecret"
    _write_skill(dup_a / "SKILL.md", "dupsecret")

    project_opencode = project / ".opencode" / "skills" / "dupsecret"
    _write_skill(project_opencode / "SKILL.md", "dupsecret")

    (real_home / ".claude").mkdir(parents=True)
    _write_skill(real_home / ".claude" / "skills" / "plan" / "SKILL.md", "plan")

    ocd = home.parent / ("ocd-" + secret)
    ocd.mkdir()
    (ocd / "opencode.json").write_text(json.dumps({"subagent_depth": 3}), encoding="utf-8")

    env = {
        "HOME": str(real_home),
        "XDG_CONFIG_HOME": str(xdg),
        "OPENCODE_CONFIG_DIR": str(ocd),
        "OPENCODE_DISABLE_CLAUDE_CODE": secret,
        "OPENCODE_CONFIG_CONTENT": secret,
    }

    def version_runner(_path):
        return "garbage output containing %s" % secret

    def which(name):
        return "/usr/bin/%s" % name if name == "opencode" else None

    findings = doctor.run_host(project, source_dir, env, real_home, which, version_runner)
    assert findings  # the fixture must actually produce findings
    text_out = doctor.render_text(findings, doctor.report_status(findings))
    json_out = doctor.render_json(findings, doctor.report_status(findings))
    for form in helpers.secret_forms(secret) + [secret.lower()]:
        assert form not in text_out
        assert form not in json_out


def test_census_mutation_checks_are_caught_by_the_redaction_test(fixture, monkeypatch):
    """Each of these deliberate breaks must fail a redaction assertion, so
    the passing state above is not an accident of the fixture."""
    home, project, source_dir, env = fixture
    secret = helpers.SEEDED_SECRET
    xdg = home.parent / ("xdg-" + secret)
    (xdg / "opencode").mkdir(parents=True)

    # (1) a message template interpolates a config value directly
    monkeypatch.setitem(doctor.MESSAGES, "config-env-set", "leaked value: " + secret)
    env_with_content = {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg), "OPENCODE_CONFIG_CONTENT": "x"}
    findings = doctor.run_host(project, source_dir, env_with_content, home, _which_none, None)
    text_out = doctor.render_text(findings, doctor.report_status(findings))
    assert secret in text_out
    monkeypatch.undo()

    # (2) display_path returns the raw absolute path instead of a rendered root name
    def raw_display_path(p, roots):
        return str(p)

    home2 = home.parent / ("home2-" + secret)
    (home2 / ".claude").mkdir(parents=True)
    (home2 / ".claude" / "CLAUDE.md").write_text("rules", encoding="utf-8")
    monkeypatch.setattr(doctor, "display_path", raw_display_path)
    findings2 = doctor.run_host(project, source_dir, {"HOME": str(home2)}, home2, _which_none, None)
    assert any(secret in (f.path or "") for f in findings2)
    monkeypatch.undo()

    # (3) skill-duplicate prints the raw skill name for every duplicate,
    # even one that is not a catalog id or a generated quoin-* name
    monkeypatch.setattr(
        doctor,
        "make_finding",
        lambda id, severity, path=None, remediation=None, **fields: doctor.Finding(
            id=id,
            severity=severity,
            message=(fields.get("names") or "") + secret if id == "skill-duplicate-unnamed" else (
                doctor.MESSAGES[id] % fields if fields else doctor.MESSAGES[id]
            ),
            path=path,
            remediation=remediation,
        ),
    )
    home3 = home.parent / ("home3-" + secret)
    home3.mkdir()
    _write_skill(home3 / ".claude" / "skills" / "dup" / "SKILL.md", "dup")
    _write_skill(home3 / ".agents" / "skills" / "dup" / "SKILL.md", "dup")
    findings3 = doctor.run_host(project, source_dir, {"HOME": str(home3)}, home3, _which_none, None)
    dup3 = [f for f in findings3 if f.id == "skill-duplicate-unnamed"]
    assert dup3 and secret in dup3[0].message
    monkeypatch.undo()
