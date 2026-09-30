"""Launch environment, redaction, file verification and configuration-layer scan."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from quoin.opencode_adapter import compiler, doctor, install, launch_env
from quoin.opencode_adapter import secrets as credential_refs
from quoin.opencode_adapter.launch_env import LaunchRefused
from quoin.opencode_adapter.merge import ProviderView

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
SECRET = "sk-test-SEEDED-SECRET-0000"


# ------------------------------------------------------------ fixtures


class _Resolver:
    def __init__(self, table):
        self.table = table

    def resolve(self, ref):
        if ref not in self.table:
            raise credential_refs.SecretResolutionError("env-missing", credential_refs.parse(ref))
        return credential_refs.SecretValue(self.table[ref])


def _view(pid="prov", env_name="PROV_KEY", proxy=False):
    return ProviderView(
        id=pid, kind="openai-compatible", endpoint_family="x", host_key="h",
        credential_env=env_name, use_env_proxy=proxy, credential_ref="env:" + env_name,
    )


def _sidecar(env_name="PROV_KEY", pid="prov"):
    return {"credential_env": {env_name: pid}}


def _compiled_doc():
    return {
        "$schema": compiler.CONFIG_SCHEMA_URL,
        "model": "prov/m",
        "small_model": "prov/m",
        "agent": {"quoin-plan": {"model": "prov/m"}},
        "provider": {"prov": {}},
        "share": "disabled",
    }


@pytest.fixture(scope="module")
def installed_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed")
    out, err = io.StringIO(), io.StringIO()
    code = install.run_install(root, SOURCE_DIR, None, False, out, err)
    assert code == 0, err.getvalue()
    return root


def _owned(root, kind):
    meta = install.load_metadata(root)
    return {rel: rec["sha256"] for rel, rec in meta.owned.items() if rec["kind"] == kind}


class Project:
    """A git-worktree project (installed or bare), a home and a child env."""

    def __init__(self, tmp_path, template=None):
        self.tmp = tmp_path
        self.root = tmp_path / "project"
        if template is not None:
            shutil.copytree(template, self.root)
        else:
            self.root.mkdir()
        (self.root / ".git").mkdir(exist_ok=True)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.xdg = tmp_path / "xdg"
        self.xdg.mkdir()
        self.compiled = tmp_path / "out" / "opencode.json"
        self.compiled.parent.mkdir()
        self.compiled.write_text("{}", encoding="utf-8")
        self.env = {"XDG_CONFIG_HOME": str(self.xdg), "OPENCODE_CONFIG": str(self.compiled)}
        self.managed = tmp_path / "managed"

    def write(self, rel, text, base=None):
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, cwd=None, **kw):
        kw.setdefault("owned_agents", _owned(self.root, "agent") if (self.root / ".quoin").exists() else {})
        kw.setdefault("owned_commands", _owned(self.root, "command") if (self.root / ".quoin").exists() else {})
        kw.setdefault("managed_dir", self.managed)
        kw.setdefault("managed_prefs", [])
        return launch_env.check_config_layers(
            cwd=cwd or self.root, env=self.env, home=self.home, compiled_doc=_compiled_doc(), **kw
        )


def _refused(project, code, category=None, **kw):
    with pytest.raises(LaunchRefused) as info:
        project.check(**kw)
    assert info.value.code == code, info.value.message
    if category:
        assert info.value.category == category
    return info.value


@pytest.fixture
def bare(tmp_path):
    return Project(tmp_path)


@pytest.fixture
def fresh(tmp_path, installed_template):
    return Project(tmp_path, installed_template)


# ---------------------------------------------------------- environment


def test_build_env_allowlists_ambient_and_exports_credentials(tmp_path):
    ambient = {
        "PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "LANG": "en", "TERM": "xterm",
        "OPENROUTER_API_KEY": "leak-1234567890", "OPENCODE_CONFIG_CONTENT": "{}",
        "OPENCODE_PERMISSION": "{}", "OPENCODE_CONFIG_DIR": "/x", "HTTPS_PROXY": "http://p",
        "PROV_KEY": "ambient-value-1", "NODE_EXTRA_CA_CERTS": "/ca.pem",
    }
    env = launch_env.build_env(
        ambient=ambient, compile_sidecar=_sidecar(), providers=[_view()],
        resolver=_Resolver({"env:PROV_KEY": SECRET}), data_dir=tmp_path / "d", config_path=tmp_path / "c.json",
    )
    values = env.materialize()
    assert values["PROV_KEY"] == SECRET
    assert values["OPENCODE_CONFIG"] == str(tmp_path / "c.json")
    assert values["XDG_DATA_HOME"] == str(tmp_path / "d")
    assert values["LC_ALL"] == "C" and values["NODE_EXTRA_CA_CERTS"] == "/ca.pem"
    for dropped in ("OPENROUTER_API_KEY", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION",
                    "OPENCODE_CONFIG_DIR", "HTTPS_PROXY"):
        assert dropped not in values
    assert SECRET not in repr(env)
    assert "PROV_KEY" in repr(env)
    assert env.redactor("token %s here" % SECRET) == "token <redacted> here"


def test_proxy_variables_only_with_use_env_proxy(tmp_path):
    ambient = {"HTTPS_PROXY": "http://p", "no_proxy": "x", "PATH": "/bin"}

    def build(proxy):
        return launch_env.build_env(
            ambient=ambient, compile_sidecar=_sidecar(), providers=[_view(proxy=proxy)],
            resolver=_Resolver({"env:PROV_KEY": SECRET}), data_dir=tmp_path, config_path=tmp_path / "c",
        ).materialize()

    assert "HTTPS_PROXY" not in build(False)
    on = build(True)
    assert on["HTTPS_PROXY"] == "http://p" and on["no_proxy"] == "x"


@pytest.mark.parametrize("table", [{}, {"env:PROV_KEY": ""}])
def test_unresolved_or_empty_credential_is_refused_by_name_only(tmp_path, table):
    with pytest.raises(LaunchRefused) as info:
        launch_env.build_env(
            ambient={}, compile_sidecar=_sidecar(), providers=[_view()], resolver=_Resolver(table),
            data_dir=tmp_path, config_path=tmp_path / "c",
        )
    assert info.value.code == "credential-unresolved"
    assert info.value.category == "invalid-configuration"
    assert "PROV_KEY" in info.value.message


def test_data_dir_is_created_private(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    target = launch_env.data_dir(env, home, "work")
    assert target.is_dir()
    assert target == tmp_path / "state" / "quoin" / "opencode" / "work" / "data"
    assert (target.stat().st_mode & 0o077) == 0


def test_redactor_handles_overlap_shapes_and_short_values():
    r = launch_env.Redactor()
    r.add("abcdefgh")
    r.add("abcdefghij")
    r.add("short")
    assert r("x abcdefghij y abcdefgh") == "x <redacted> y <redacted>"
    assert r("keep short") == "keep short"
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in r("sk-abcdefghijklmnopqrstuvwxyz0123")
    assert r(None) == "None"


# ------------------------------------------------------ file verification


def test_verify_owned_file_and_compiled(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"data")
    sha = hashlib.sha256(b"data").hexdigest()
    assert launch_env.verify_owned_file(tmp_path, "f.md", sha)
    assert not launch_env.verify_owned_file(tmp_path, "f.md", "0" * 64)
    assert not launch_env.verify_owned_file(tmp_path, "absent.md", sha)
    (tmp_path / "link.md").symlink_to(target)
    assert not launch_env.verify_owned_file(tmp_path, "link.md", sha)
    launch_env.verify_compiled(target, sha)
    for bad in (target, tmp_path / "absent", tmp_path / "link.md"):
        with pytest.raises(LaunchRefused) as info:
            launch_env.verify_compiled(bad, "0" * 64 if bad == target else sha)
        assert info.value.code == "compiled-file-changed"


# ------------------------------------------------------------ layer scan


def test_fresh_install_is_accepted(fresh):
    assert (fresh.root / ".opencode" / "opencode.jsonc").exists()
    fresh.check()


def test_model_added_to_owned_config_is_refused_by_key(fresh):
    path = fresh.root / ".opencode" / "opencode.jsonc"
    doc = json.loads(doctor._strip_jsonc(path.read_text(encoding="utf-8")))
    doc["model"] = "other/x"
    path.write_text(json.dumps(doc), encoding="utf-8")
    err = _refused(fresh, "protected-key-overridden", "policy-denial")
    assert "model" in err.message and "opencode.jsonc" in err.message


def test_schema_compared_by_value(bare):
    bare.write("opencode.json", json.dumps({"$schema": compiler.CONFIG_SCHEMA_URL}))
    bare.check()
    bare.write("opencode.json", json.dumps({"$schema": "https://example.com/other.json"}))
    err = _refused(bare, "protected-key-overridden")
    assert compiler.CONFIG_SCHEMA_URL in err.message
    assert "opencode.json" in err.message


@pytest.mark.parametrize("where", ["project", "dotopencode", "managed", "plist"])
def test_model_override_in_each_above_rank_layer_is_refused(bare, where):
    body = json.dumps({"model": "x/y"})
    kw = {}
    if where == "project":
        bare.write("opencode.json", body)
    elif where == "dotopencode":
        bare.write(".opencode/opencode.jsonc", body)
    elif where == "managed":
        bare.write("opencode.json", body, base=bare.managed)
    else:
        plist = bare.write("prefs.plist", "x")
        kw = {"managed_prefs": [plist], "plutil": lambda p: body}
    err = _refused(bare, "protected-key-overridden", **kw)
    assert str(bare.tmp) not in err.message or where in ("managed", "plist")
    if where in ("project", "dotopencode"):
        assert "opencode.json" in err.message


def test_unconvertible_plist_is_refused(bare):
    plist = bare.write("prefs.plist", "x")

    def boom(_path):
        raise OSError("no")

    _refused(bare, "config-layer-unreadable", managed_prefs=[plist], plutil=boom)


def test_global_layer_may_set_model_provider_agent(bare):
    bare.write("opencode.json", json.dumps({"model": "g/m", "provider": {"prov": {}}, "agent": {"quoin-plan": {}}}),
               base=bare.xdg / "opencode")
    bare.check()


def test_global_layer_still_refuses_loop_and_command_keys(bare):
    base = bare.xdg / "opencode"
    bare.write("opencode.json", json.dumps({"experimental": {"continue_loop_on_deny": True}}), base=base)
    _refused(bare, "continue-loop-on-deny", "invalid-configuration")
    bare.write("opencode.json", json.dumps({"command": {"quoin-plan": {"model": "x/y"}}}), base=base)
    _refused(bare, "command-key-overridden", "policy-denial")


def test_unrelated_agents_and_providers_are_allowed_but_compiled_names_are_not(bare):
    bare.write("opencode.json", json.dumps({"agent": {"reviewer": {}}, "provider": {"other": {}}}))
    bare.check()
    bare.write("opencode.json", json.dumps({"agent": {"quoin-plan": {"model": "x/y"}}}))
    err = _refused(bare, "protected-key-overridden")
    assert "agent.quoin-plan" in err.message
    bare.write("opencode.json", json.dumps({"provider": {"prov": {}}}))
    assert "provider.prov" in _refused(bare, "protected-key-overridden").message


def test_continue_loop_on_deny_refused_in_project_layer(bare):
    bare.write("opencode.json", json.dumps({"experimental": {"continue_loop_on_deny": False}}))
    _refused(bare, "continue-loop-on-deny", "invalid-configuration")


def test_json_command_key_refused_unrelated_allowed(bare):
    bare.write("opencode.json", json.dumps({"command": {"review": {"model": "x/y"}}}))
    bare.check()
    bare.write("opencode.json", json.dumps({"command": {"quoin-plan": {"model": "x/y"}}}))
    _refused(bare, "command-key-overridden", "policy-denial")


def test_unreadable_project_layer_is_refused(bare):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    path = bare.write("opencode.json", "{}")
    path.chmod(0)
    try:
        _refused(bare, "config-layer-unreadable", "invalid-configuration")
    finally:
        path.chmod(0o600)


def test_extra_and_global_agent_files_are_refused(fresh):
    fresh.write(".opencode/agents/quoin-extra.md", "x")
    _refused(fresh, "agent-file-overridden", "policy-denial")
    (fresh.root / ".opencode/agents/quoin-extra.md").unlink()
    fresh.check()
    fresh.write("agents/quoin-plan.md", "x", base=fresh.xdg / "opencode")
    _refused(fresh, "agent-file-overridden")


def test_project_mode_file_is_refused(bare):
    bare.write(".opencode/mode/quoin-x.md", "x")
    _refused(bare, "agent-file-overridden")


def test_owned_agent_and_command_drift(fresh):
    agent = next(iter(_owned(fresh.root, "agent")))
    with open(fresh.root / agent, "a", encoding="utf-8") as handle:
        handle.write("\npermission: loosened\n")
    _refused(fresh, "owned-file-drift", "workflow-validation")


def test_owned_command_drift(fresh):
    command = next(iter(_owned(fresh.root, "command")))
    with open(fresh.root / command, "a", encoding="utf-8") as handle:
        handle.write("\nmodel: x/y\n")
    _refused(fresh, "owned-file-drift", "workflow-validation")


@pytest.mark.parametrize(
    "base_kind,rel",
    [("home", ".opencode/commands/quoin-plan.md"), ("xdg", "commands/quoin-plan.md"),
     ("project", ".opencode/command/quoin-plan.md"), ("managed", "commands/quoin-plan.md")],
)
def test_command_shadows_are_refused(fresh, base_kind, rel):
    base = {"home": fresh.home, "xdg": fresh.xdg / "opencode", "project": fresh.root, "managed": fresh.managed}[base_kind]
    fresh.write(rel, "---\nmodel: x/y\n---\n", base=base)
    _refused(fresh, "command-file-overridden", "policy-denial")


def test_unrelated_markdown_is_ignored(fresh):
    fresh.write(".opencode/commands/review.md", "x")
    fresh.write("agents/helper.md", "x", base=fresh.xdg / "opencode")
    fresh.check()


def test_non_git_root_scans_every_ancestor(tmp_path):
    top = tmp_path / "a"
    cwd = top / "b" / "c"
    cwd.mkdir(parents=True)
    if doctor._worktree_root(cwd) != Path(cwd.anchor):
        pytest.skip("a git worktree encloses the temporary directory")
    p = Project(tmp_path)
    (p.root / ".git").rmdir()
    ancestor_config = p.write("opencode.json", json.dumps({"model": "x/y"}), base=top)
    _refused(p, "protected-key-overridden", cwd=cwd, owned_agents={}, owned_commands={})
    ancestor_config.unlink()
    p.check(cwd=cwd, owned_agents={}, owned_commands={})
    p.write(".opencode/agents/quoin-x.md", "x", base=top)
    _refused(p, "agent-file-overridden", cwd=cwd, owned_agents={}, owned_commands={})
    (top / ".opencode/agents/quoin-x.md").unlink()
    p.write(".opencode/commands/quoin-plan.md", "x", base=top)
    _refused(p, "command-file-overridden", cwd=cwd, owned_agents={}, owned_commands={})


def test_messages_carry_no_seeded_secret(bare):
    bare.write("opencode.json", json.dumps({"model": SECRET}))
    err = _refused(bare, "protected-key-overridden")
    assert SECRET not in err.message
