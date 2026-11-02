"""Generated workctl integration skills (quoin-jira-context, quoin-slack-context,
quoin-mail-context), the MCP evaluation stub, and broker auth isolation."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from quoin.opencode_adapter import generate, launch_env, manifest
from quoin.workctl import mcp, operations

SOURCE = Path(__file__).resolve().parents[2]  # the repo's quoin/ source dir
REPO = SOURCE.parent
SKILLS = ("quoin-jira-context", "quoin-mail-context", "quoin-slack-context")


@pytest.fixture(scope="module")
def rendered():
    return generate.render_source_dir(SOURCE)


def test_integration_skills_are_generated(rendered):
    for name in SKILLS:
        rf = rendered[".opencode/skills/%s/SKILL.md" % name]
        assert rf.kind == "integration-skill"
        text = rf.content.decode()
        assert text.startswith("---\nname: \"%s\"" % name)
        assert "canonical_id: \"integration:%s\"" % name[len("quoin-"):] in text


@pytest.mark.parametrize("name,service", [("quoin-jira-context", "jira"), ("quoin-slack-context", "slack"), ("quoin-mail-context", "mail")])
def test_skill_teaches_syntax_pagination_provenance_and_draft_first(rendered, name, service):
    text = rendered[".opencode/skills/%s/SKILL.md" % name].content.decode()
    for op in (o for o in operations.OPERATIONS.values() if o.service == service):
        assert "workctl %s %s" % (service, op.name.split(".")[1]) in text
    for needle in ("--query-file", "--cursor", "pagination.cursor", "provenance", "untrusted", "local draft",
                   "Summarize", "POLICY_DENIED", "retry_after"):
        assert needle in text, needle


def test_skills_carry_no_credentials_or_provider_settings(rendered):
    for name in SKILLS:
        text = rendered[".opencode/skills/%s/SKILL.md" % name].content.decode()
        assert "keychain:" not in text and "xox" not in text and "Bearer " not in text
        assert "api_key" not in text.lower() and "provider" not in text.split("## Retrieved text")[0].lower()


def test_generated_config_enables_no_mcp_server(rendered):
    config = rendered[".opencode/opencode.jsonc"].content.decode()
    assert '"mcp"' not in config


def test_manifest_integration_rows_check_clean():
    data = manifest.load_manifest(SOURCE)
    errs = manifest.check_manifest(data, manifest.load_catalog(SOURCE), manifest.read_pinned_version(SOURCE), REPO)
    assert errs == []
    assert [r["skill"] for r in data["integration_skills"]] == list(SKILLS)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda rows: rows[0].update(skill="quoin-other"), "skill is"),
        (lambda rows: rows[0].update(service="email"), "service"),
        (lambda rows: rows[0].update(source="../etc/passwd.md"), "source"),
        (lambda rows: rows.reverse(), "not sorted"),
        (lambda rows: rows[0].update(id="architect", skill="quoin-architect"), "produced by both"),
    ],
)
def test_manifest_integration_rows_are_validated(mutate, fragment):
    data = copy.deepcopy(manifest.load_manifest(SOURCE))
    mutate(data["integration_skills"])
    errs = manifest.check_manifest(data, manifest.load_catalog(SOURCE), manifest.read_pinned_version(SOURCE), REPO)
    assert any(fragment in e.lower() for e in errs), errs


def test_install_records_integration_skills_and_uninstall_removes_them(tmp_path):
    import io

    from quoin.opencode_adapter import install

    project = tmp_path / "project"
    project.mkdir()
    out, err = io.StringIO(), io.StringIO()
    assert install.run_install(project, SOURCE, "default", False, out, err) == 0, err.getvalue()
    meta = json.loads((project / ".quoin" / "opencode-install.json").read_text())
    for name in SKILLS:
        rel = ".opencode/skills/%s/SKILL.md" % name
        assert (project / rel).is_file()
        # recorded with a kind older installs understand
        assert meta["owned"][rel]["kind"] == "skill" and meta["owned"][rel]["id"] == name[len("quoin-"):]
    assert install.run_uninstall(project, False, io.StringIO(), io.StringIO()) == 0
    assert not any((project / ".opencode" / "skills" / n).exists() for n in SKILLS)


def test_render_refuses_integration_rows_without_their_template():
    inputs = generate.load_inputs(SOURCE)
    inputs.integration_template = None
    with pytest.raises(generate.GenerationError, match="integration-skill"):
        generate.render(inputs)


def test_mcp_tool_schemas_match_the_operation_parameters():
    by_name = {t["name"]: t for t in mcp.tools()}
    assert set(by_name) == {mcp.tool_name(op) for op in operations.OPERATIONS}
    for op_name in operations.OPERATIONS:
        tool = by_name[mcp.tool_name(op_name)]
        assert set(tool["inputSchema"]["properties"]) == operations.ALLOWED_PARAMS[op_name] - {"query_file"}
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is True


def _write_broker_config(home):
    path = home / ".config" / "quoin" / "workctl.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "services": {
        "jira": {"backend": "jira-cloud", "tenant": "acme", "base_url": "https://jira.example.invalid",
                 "credential": "keychain:quoin-workctl-jira/reader"}}}))
    os.chmod(path, 0o600)


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("workctl_jira_issue", {"key": "eng-1; rm -rf /"}),
        ("workctl_jira_search", {"query": "x" * 5000}),
        ("workctl_jira_search", {"query": "x", "cursor": "bad cursor!"}),
        ("workctl_jira_search", {"query": "x", "expand": "attachments"}),
    ],
)
def test_mcp_calls_get_the_brokers_validation(tmp_path, tool, arguments):
    home = tmp_path / "home"
    home.mkdir()
    _write_broker_config(home)
    env = mcp.call_tool(tool, arguments, env={}, home=home)
    assert env["error"]["code"] == "INVALID_INPUT" and len(env["request_id"]) == 32


def test_mcp_stub_refuses_unknown_tools_query_files_and_unconfigured_services(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert mcp.call_tool("workctl_jira_issue", {"key": "ENG-1"}, env={}, home=home)["error"]["code"] == "POLICY_DENIED"
    assert mcp.call_tool("workctl_slack_post", {}, env={}, home=home)["error"]["code"] == "INVALID_INPUT"
    bad = mcp.call_tool("workctl_mail_search", {"query_file": "/etc/passwd"}, env={}, home=home)
    assert bad["error"]["code"] == "INVALID_INPUT"


def test_broker_drops_caller_ca_and_proxy_variables_for_every_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/evil-ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    home = tmp_path / "home"
    home.mkdir()
    mcp.call_tool("workctl_jira_issue", {"key": "ENG-1"}, env={}, home=home)
    assert "SSL_CERT_FILE" not in os.environ and "HTTPS_PROXY" not in os.environ


def test_agent_launch_environment_carries_no_broker_credentials(tmp_path):
    """The OpenCode launch environment is an allowlist: broker-style
    credentials present in the launching shell never reach the agent, and
    the broker itself has no way to read credentials from an environment."""
    from quoin.opencode_adapter.secrets import SecretResolutionError
    from quoin.workctl import broker

    ambient = {
        "PATH": "/bin",
        "HOME": "/h",
        "SLACK_TOKEN": "xoxp-ambient-123456789",
        "JIRA_API_TOKEN": "jira-ambient-123456789",
        "GMAIL_ACCESS_TOKEN": "ya29.ambient-123456789",
    }
    env = launch_env.build_env(
        ambient=ambient,
        compile_sidecar={"credential_env": {}},
        providers=[],
        resolver=None,
        data_dir=tmp_path / "d",
        config_path=tmp_path / "c.json",
    ).materialize()
    for name in ("SLACK_TOKEN", "JIRA_API_TOKEN", "GMAIL_ACCESS_TOKEN"):
        assert name not in env
    assert not any("ambient-123456789" in v for v in env.values())
    with pytest.raises(SecretResolutionError) as exc:
        broker.default_resolver("darwin").resolve("env:SLACK_TOKEN")
    assert exc.value.code == "backend-unavailable"
