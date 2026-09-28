"""Offline smoke checks for the OpenCode adapter doctor.

Host-environment checks (`run_host`) are tested in `test_opencode_doctor_host.py`;
this file covers only `run_smoke` and the report-rendering primitives
(`Finding`, `make_finding`, `display_path`, `render_text`, `render_json`,
`report_status`, `exit_code`).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import doctor, generate

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


def _real_files():
    return generate.render(generate.load_inputs(SOURCE_DIR))


def test_smoke_on_real_source_dir_is_clean_and_counts_every_file():
    findings = doctor.run_smoke(SOURCE_DIR)
    assert [f.id for f in findings] == ["smoke-ok"]
    files = _real_files()
    assert findings[0].message == "offline smoke checks passed for %d rendered files" % len(files)
    assert doctor.report_status(findings) == "healthy"
    assert doctor.exit_code(doctor.report_status(findings)) == 0


def test_smoke_never_touches_the_network_or_requires_a_binary(monkeypatch):
    def _blocked(*_a, **_k):
        raise AssertionError("run_smoke must not touch the network")

    monkeypatch.setattr("socket.socket.connect", _blocked)
    monkeypatch.setattr("socket.create_connection", _blocked)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    findings = doctor.run_smoke(SOURCE_DIR)
    assert [f.id for f in findings] == ["smoke-ok"]


def test_smoke_render_error_on_a_broken_manifest(tmp_path, monkeypatch):
    def _raise_load_inputs(_source_dir):
        raise generate.GenerationError("boom")

    monkeypatch.setattr(generate, "load_inputs", _raise_load_inputs)
    findings = doctor.run_smoke(tmp_path)
    assert [f.id for f in findings] == ["smoke-render"]
    assert findings[0].severity == "error"
    assert findings[0].remediation == "run `quoin install --runtime opencode --check` to see the generation error"


def _mutate_and_check(monkeypatch, mutate, expected_id):
    files = _real_files()
    mutated = dict(files)
    mutate(mutated)

    def _fake_render(_inputs):
        return mutated

    monkeypatch.setattr(generate, "render", _fake_render)
    findings = doctor.run_smoke(SOURCE_DIR)
    ids = {f.id for f in findings}
    assert expected_id in ids, "expected %r among %r" % (expected_id, ids)


def _first_of_kind(files, kind):
    for relpath, rf in files.items():
        if rf.kind == kind:
            return relpath, rf
    raise AssertionError("no rendered file of kind %r" % kind)


def test_mutated_skill_dir_name_mismatch_triggers_smoke_names(monkeypatch):
    def mutate(files):
        relpath, rf = _first_of_kind(files, "skill")
        wrong = ".opencode/skills/Not_A_Valid_Name/SKILL.md"
        files.pop(relpath)
        files[wrong] = generate.RenderedFile(
            relpath=wrong, content=rf.content, kind=rf.kind, source_id=rf.source_id, source_digest=rf.source_digest
        )

    _mutate_and_check(monkeypatch, mutate, "smoke-names")


def test_mutated_unparseable_frontmatter_triggers_smoke_frontmatter(monkeypatch):
    def mutate(files):
        relpath, rf = _first_of_kind(files, "command")
        broken = b"---\nbroken: [unterminated\n---\nbody\n"
        files[relpath] = generate.RenderedFile(
            relpath=relpath, content=broken, kind=rf.kind, source_id=rf.source_id, source_digest=rf.source_digest
        )

    _mutate_and_check(monkeypatch, mutate, "smoke-frontmatter")


def test_mutated_skill_digest_triggers_smoke_digests(monkeypatch):
    def mutate(files):
        relpath, rf = _first_of_kind(files, "skill")
        text = rf.content.decode("utf-8")
        broken = text.replace(rf.source_digest, "0" * len(rf.source_digest))
        files[relpath] = generate.RenderedFile(
            relpath=relpath, content=broken.encode("utf-8"), kind=rf.kind, source_id=rf.source_id, source_digest=rf.source_digest
        )

    _mutate_and_check(monkeypatch, mutate, "smoke-digests")


def test_missing_command_triggers_smoke_bundle(monkeypatch):
    def mutate(files):
        relpath, _rf = _first_of_kind(files, "command")
        del files[relpath]

    _mutate_and_check(monkeypatch, mutate, "smoke-bundle")


def test_critic_map_without_leading_deny_triggers_smoke_read_only_roles(monkeypatch):
    def mutate(files):
        for relpath, rf in list(files.items()):
            if rf.kind == "agent" and rf.source_id == "critic":
                text = rf.content.decode("utf-8")
                fields, body = frontmatter_roundtrip(text)
                fields["permission"] = {"read": {"*": "allow"}, "edit": "allow"}
                new_text = reemit(fields, body)
                files[relpath] = generate.RenderedFile(
                    relpath=relpath,
                    content=new_text.encode("utf-8"),
                    kind=rf.kind,
                    source_id=rf.source_id,
                    source_digest=rf.source_digest,
                )
                return
        raise AssertionError("no critic agent file found")

    _mutate_and_check(monkeypatch, mutate, "smoke-read-only-roles")


def test_subagent_with_a_task_allow_triggers_smoke_task_graph(monkeypatch):
    def mutate(files):
        for relpath, rf in list(files.items()):
            if rf.kind != "agent":
                continue
            text = rf.content.decode("utf-8")
            fields, _body = frontmatter_roundtrip(text)
            if fields.get("mode") == "subagent" and isinstance(fields.get("permission"), dict):
                fields["permission"] = dict(fields["permission"])
                fields["permission"]["task"] = {"*": "deny", "quoin-implementer": "allow"}
                new_text = reemit(fields, _body)
                files[relpath] = generate.RenderedFile(
                    relpath=relpath,
                    content=new_text.encode("utf-8"),
                    kind=rf.kind,
                    source_id=rf.source_id,
                    source_digest=rf.source_digest,
                )
                return
        raise AssertionError("no subagent-mode agent file found")

    _mutate_and_check(monkeypatch, mutate, "smoke-task-graph")


def test_reference_to_unknown_script_triggers_smoke_script_refs(monkeypatch):
    def mutate(files):
        relpath, rf = _first_of_kind(files, "instructions")
        text = rf.content.decode("utf-8") + "\n`quoin opencode script totally_unknown_script`\n"
        files[relpath] = generate.RenderedFile(
            relpath=relpath, content=text.encode("utf-8"), kind=rf.kind, source_id=rf.source_id, source_digest=rf.source_digest
        )

    _mutate_and_check(monkeypatch, mutate, "smoke-script-refs")


def test_config_with_extra_key_triggers_smoke_config(monkeypatch):
    def mutate(files):
        relpath, rf = _first_of_kind(files, "config")
        text = rf.content.decode("utf-8")
        stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
        payload = json.loads(stripped)
        payload["extra"] = "nope"
        files[relpath] = generate.RenderedFile(
            relpath=relpath,
            content=json.dumps(payload).encode("utf-8"),
            kind=rf.kind,
            source_id=rf.source_id,
            source_digest=rf.source_digest,
        )

    _mutate_and_check(monkeypatch, mutate, "smoke-config")


def frontmatter_roundtrip(text):
    from quoin.opencode_adapter import frontmatter as fm

    return fm.parse(text)


def reemit(fields, body):
    from quoin.opencode_adapter import frontmatter as fm

    return fm.emit(fields) + body


# --- report primitives -----------------------------------------------------


def test_render_json_output_parses_has_four_top_keys_and_is_byte_identical_across_two_runs():
    findings = [
        doctor.make_finding("smoke-roundtrip", "error", path="a/b.md"),
        doctor.make_finding("smoke-ok", "ok", count=3),
    ]
    status = doctor.report_status(findings)
    one = doctor.render_json(findings, status)
    two = doctor.render_json(list(reversed(findings)), status)
    assert one == two
    payload = json.loads(one)
    assert set(payload) == {"schema_version", "runtime", "status", "findings"}
    assert payload["schema_version"] == 1
    assert payload["runtime"] == "opencode"


def test_make_finding_rejects_unknown_id_and_unknown_field():
    with pytest.raises(ValueError):
        doctor.make_finding("not-a-real-id", "error")
    with pytest.raises(ValueError):
        doctor.make_finding("smoke-ok", "ok", count=1, bogus_field="x")


def test_two_findings_same_id_one_with_path_one_without_render_in_stable_order():
    findings = [
        doctor.make_finding("smoke-roundtrip", "error", path="z.md"),
        doctor.make_finding("smoke-roundtrip", "error"),
        doctor.make_finding("smoke-roundtrip", "error", path="a.md"),
    ]
    status = doctor.report_status(findings)
    text = doctor.render_text(findings, status)
    j = json.loads(doctor.render_json(findings, status))
    # No path sorts before any path (empty string sorts first).
    paths_in_order = [f.get("path", "") for f in j["findings"]]
    assert paths_in_order == sorted(paths_in_order)
    assert text  # renders without error


def test_render_text_groups_and_counts():
    findings = [
        doctor.make_finding("smoke-render", "error"),
        doctor.make_finding("smoke-bundle", "error"),
    ]
    status = doctor.report_status(findings)
    text = doctor.render_text(findings, status)
    assert text.strip().endswith("opencode doctor: errors (errors=2, warnings=0, info=0)")


def test_exit_code_mapping():
    assert doctor.exit_code("healthy") == 0
    assert doctor.exit_code("warnings") == 4
    assert doctor.exit_code("errors") == 1


# --- display_path ------------------------------------------------------


def test_display_path_each_root_kind_renders_by_name():
    roots = [("/t/proj", "."), ("/t/ocd", "$OPENCODE_CONFIG_DIR"), ("/home/u", "~")]
    assert doctor.display_path("/t/proj/a.json", roots) == "./a.json"
    assert doctor.display_path("/t/ocd/opencode.json", roots) == "$OPENCODE_CONFIG_DIR/opencode.json"
    assert doctor.display_path("/home/u/.claude/CLAUDE.md", roots) == "~/.claude/CLAUDE.md"


def test_display_path_longest_root_wins_when_roots_nest():
    roots = [("/t/proj", "."), ("/t/proj/sub", "skills.paths[0] of ./opencode.json")]
    assert doctor.display_path("/t/proj/sub/SKILL.md", roots) == "skills.paths[0] of ./opencode.json/SKILL.md"


def test_display_path_no_root_prints_absolute():
    roots = [("/t/proj", ".")]
    assert doctor.display_path("/other/place/f.json", roots) == "/other/place/f.json"


def test_display_path_sibling_with_shared_prefix_does_not_match():
    roots = [("/t/ocd", "$OPENCODE_CONFIG_DIR")]
    assert doctor.display_path("/t/ocd-x/opencode.json", roots) == "/t/ocd-x/opencode.json"


def test_display_path_trailing_slash_and_dotdot_still_match():
    roots = [("/t/ocd/", "$OPENCODE_CONFIG_DIR")]
    assert doctor.display_path("/t/ocd/../ocd/opencode.json", roots) == "$OPENCODE_CONFIG_DIR/opencode.json"
