"""`OpenCodeDriver.probe` and `prepare`: every refusal, and the happy path."""
from __future__ import annotations

import io
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

import _opencode_driver_helpers as h
import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW, World
from quoin.opencode_adapter import driver, install, proctree, runstore
from quoin.opencode_adapter import secrets as credential_refs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SECRET = "sk-test-SEEDED-SECRET-0000"


class _Resolver:
    def resolve(self, ref):
        return credential_refs.SecretValue(SECRET)


class _EmptyResolver:
    def resolve(self, ref):
        return credential_refs.SecretValue("")


def _trap(*args, **kwargs):
    raise AssertionError("prepare must never spawn a process")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A git-worktree project with a fresh install, pinned home and binary."""
    return Setup(tmp_path, monkeypatch)


class Setup:
    def __init__(self, tmp_path, monkeypatch, root=None, git=True):
        self.tmp = tmp_path
        self.world = World(tmp_path, agents=False, root=root)
        self.root = self.world.root
        if git:
            (self.root / ".git").mkdir(exist_ok=True)
        code = install.run_install(str(self.root), helpers.SOURCE_DIR, None, False, io.StringIO(), io.StringIO())
        assert code == 0
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.world.home))
        fake_scenario = h.fake.write_scenario(tmp_path / "s.json", h.fake.SCENARIOS["record_only"]())
        self.shim = h.fake.write_shim(tmp_path / "bin", fake_scenario, tmp_path / "fake-state")
        self.which = lambda name: str(self.shim)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(
            driver, "subprocess",
            types.SimpleNamespace(
                Popen=_trap, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError,
            ),
        )
        self.kw = {}

    def driver(self, **overrides):
        kw = dict(
            env=dict(self.world.env, PATH=os.environ.get("PATH", ""), UNRELATED_SECRET="leak-1234567890"),
            home=self.world.home, which=self.which, version_runner=lambda path: "1.18.32",
            resolver_factory=lambda environ, platform: _Resolver(), clock=lambda: NOW.timestamp(),
        )
        kw.update(self.kw)
        kw.update(overrides)
        return driver.OpenCodeDriver(self.root, **kw)

    def request(self, **overrides):
        kw = dict(project_root=self.root, task="demo", stage=None, phase="plan", profile="work")
        kw.update(overrides)
        return driver.RunRequest(**kw)

    def refused(self, code, category=None, request=None, drv=None, **prep_kw):
        with pytest.raises(driver.PrepareRefused) as info:
            (drv or self.driver()).prepare(request or self.request(), **prep_kw)
        assert info.value.code == code, info.value.message
        if category:
            assert info.value.category == category
        assert SECRET not in info.value.message
        return info.value

    def write(self, rel, text, base=None):
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


# ------------------------------------------------------------------ probe


def test_probe_reports_the_pinned_binary(env):
    caps = env.driver().probe()
    assert caps.binary == env.shim and caps.version == "1.18.32" and caps.version_supported
    assert caps.native_stop_event == "absent" and caps.child_session_events == "filtered"
    assert caps.step_settling == ("verified" if driver.STEP_SETTLING_VERIFIED else "unverified")
    assert caps.process_groups == "supported" and caps.descendant_reaping in ("ps", "proc")


def test_probe_without_binary_or_with_other_version(env):
    assert env.driver(which=lambda n: None).probe().binary is None
    caps = env.driver(version_runner=lambda p: "0.0.1").probe()
    assert caps.version == "0.0.1" and not caps.version_supported
    off = env.driver(proc=types.SimpleNamespace(SUPPORTED=False)).probe()
    assert (off.process_groups, off.descendant_reaping) == ("unsupported", "unsupported")


# ------------------------------------------------------------------ happy


def test_happy_path_on_a_fresh_install(env):
    prepared = env.driver().prepare(env.request())
    assert prepared.role and prepared.effective_model and prepared.config_digest
    assert prepared.cwd == env.root
    assert prepared.argv[1:] == ("run", "--format", "json", "--command", "quoin-plan", "--", "demo")
    for flag in ("--auto", "--yolo", "--dangerously-skip-permissions", "--agent"):
        assert flag not in prepared.argv
    assert "OPENCODE_CONFIG" in prepared.env_names and "UNRELATED_SECRET" not in prepared.env_names
    assert SECRET not in repr(prepared) and SECRET not in " ".join(prepared.argv)
    directory = h.runstore.store_dir(env.root)
    record = runstore.load_record(directory, prepared.run_id)
    assert record["state"] == "prepared" and record["refusal"] is None
    assert record["prepared"]["command"] == "quoin-plan"
    assert record["input_hashes"] == dict(prepared.input_hashes)
    assert runstore.load_pointer(directory, "demo")["run_id"] == prepared.run_id
    assert prepared.retry is not None
    assert prepared.native_sha256 and prepared.config_path.is_file()


def test_stage_argument_and_phase_spellings(env):
    prepared = env.driver().prepare(env.request(stage="2"))
    assert prepared.argv[-1] == "stage 2 of demo"
    a = env.driver().prepare(env.request(phase="thorough_plan"))
    b = env.driver().prepare(env.request(phase="thorough-plan"))
    assert a.argv[5] == b.argv[5] == "quoin-thorough-plan"


# --------------------------------------------------------------- refusals


def test_invalid_names(env):
    env.refused("invalid-task-name", "workflow-validation", request=env.request(task="../x"))
    env.refused("invalid-stage", "workflow-validation", request=env.request(stage="abcd"))


def test_runtime_dir_unwritable(env):
    (env.root / ".workflow_artifacts").mkdir(exist_ok=True)
    (env.root / ".workflow_artifacts" / "memory").symlink_to(env.tmp)
    err = env.refused("sidecar-dir-unwritable", "workflow-validation")
    assert err.run_id is None


def test_non_posix_refused(env):
    env.refused("process-groups-unsupported", "workflow-validation", drv=env.driver(proc=types.SimpleNamespace(SUPPORTED=False)))


def test_phase_refusals(env):
    env.refused("whole-task-unavailable", "workflow-validation", request=env.request(phase="run"))
    env.refused("phase-unsupported", "workflow-validation", request=env.request(phase="capture_insight"))
    env.refused("phase-unsupported", "workflow-validation", request=env.request(phase="no_such_phase"))


def test_binary_and_version(env):
    env.refused("opencode-binary-absent", "missing-binary", drv=env.driver(which=lambda n: None))
    env.refused("opencode-version", "unsupported-version", drv=env.driver(version_runner=lambda p: "9.9.9"))
    env.refused("opencode-version", "unsupported-version", drv=env.driver(version_runner=lambda p: None))


def test_unknown_profile_is_an_invalid_configuration(env):
    err = env.refused("profile-not-found", "invalid-configuration", request=env.request(profile="nope"))
    assert err.run_id


def test_missing_qualification_refuses_as_unqualified_gateway(env):
    for path in list((env.world.tmp / "xdg" / "quoin" / "opencode" / "qualifications").glob("*.json")):
        path.unlink()
    env.refused("not-launchable", "unqualified-gateway")


def test_credential_unresolved(env):
    env.refused(
        "credential-unresolved", "invalid-configuration",
        drv=env.driver(resolver_factory=lambda environ, platform: _EmptyResolver()),
    )


def test_not_installed_and_bad_install_record(env, monkeypatch):
    def broken(root):
        raise install.InstallError("bad record with %s" % SECRET)

    monkeypatch.setattr(install, "load_metadata", broken)
    env.refused("install-record-invalid", "workflow-validation")
    monkeypatch.setattr(install, "load_metadata", lambda root: None)
    env.refused("not-installed", "workflow-validation")


def test_owned_file_drift_for_every_kind(env):
    meta = install.load_metadata(env.root)
    by_kind = {}
    for rel, rec in meta.owned.items():
        by_kind.setdefault(rec["kind"], rel)
    assert set(by_kind) >= {"agent", "command", "config", "skill"}
    for kind, rel in sorted(by_kind.items()):
        path = env.root / rel
        original = path.read_bytes()
        path.write_bytes(original + b"\n# local edit\n")
        try:
            env.refused("owned-file-drift", "workflow-validation")
        finally:
            path.write_bytes(original)
    env.driver().prepare(env.request())


def test_model_added_to_the_owned_config_is_drift(env):
    config = next(r for r, rec in install.load_metadata(env.root).owned.items() if rec["kind"] == "config")
    path = env.root / config
    path.write_text(path.read_text() .rstrip().rstrip("}") + ',\n  "model": "x/y"\n}\n', encoding="utf-8")
    env.refused("owned-file-drift", "workflow-validation")


def test_command_agent_must_be_primary(env):
    meta = install.load_metadata(env.root)
    rel = ".opencode/commands/quoin-plan.md"
    text = (env.root / rel).read_text()
    agent = [l for l in text.splitlines() if l.startswith("agent:")][0].split(":", 1)[1].strip().strip('"')
    agent_rel = ".opencode/agents/%s.md" % agent
    original = (env.root / agent_rel).read_text()
    changed = original.replace('mode: "primary"', 'mode: "subagent"').replace('mode: "all"', 'mode: "subagent"')
    assert changed != original
    import hashlib

    (env.root / agent_rel).write_text(changed, encoding="utf-8")
    meta.owned[agent_rel]["sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    env.monkeypatch.setattr(install, "load_metadata", lambda root: meta)
    env.refused("command-agent-not-primary", "workflow-validation")


# ------------------------------------------------------------- non-git root


def test_non_git_root_is_refused_only_while_discovery_is_unverified(tmp_path, monkeypatch):
    env = Setup(tmp_path, monkeypatch, git=False)
    monkeypatch.setattr(driver, "NON_GIT_DISCOVERY_VERIFIED", False)
    err = env.refused("non-git-root-unverified", "workflow-validation")
    assert "git" in err.message
    monkeypatch.setattr(driver, "NON_GIT_DISCOVERY_VERIFIED", True)
    prepared = env.driver().prepare(env.request())
    assert prepared.cwd == env.root


def test_a_dotfiles_git_directory_in_home_counts_as_a_worktree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git").mkdir()
    env = Setup(tmp_path, monkeypatch, root=home / "project", git=False)
    monkeypatch.setattr(driver, "NON_GIT_DISCOVERY_VERIFIED", False)
    assert env.driver().prepare(env.request()).cwd == env.root


# ---------------------------------------------------------------- layers


def test_shadow_command_files_and_keys_refuse_the_launch(env):
    shadow = env.write(".opencode/commands/quoin-plan.md", "---\nagent: quoin-x\n---\nbody\n", base=env.world.home)
    env.refused("command-file-overridden", "policy-denial")
    shadow.unlink()
    env.write("opencode/opencode.json", json.dumps({"command": {"quoin-plan": {"model": "x/y"}}}), base=env.world.tmp / "xdg")
    env.refused("command-key-overridden", "policy-denial")


def test_project_model_override_refuses(env):
    env.write("opencode.json", json.dumps({"model": "other/model"}))
    env.refused("protected-key-overridden", "policy-denial")


# ------------------------------------------------------------ resume mode


def _interrupted_run(env):
    drv = env.driver()
    prepared = drv.prepare(env.request())
    directory = runstore.store_dir(env.root)
    record = runstore.load_record(directory, prepared.run_id)
    attempt = runstore.new_attempt(
        1, pid=1, pgid=1, child_start="x", driver_pid=os.getpid(), driver_start=None, resume_mode="fresh",
    )
    attempt.update(state="interrupted", reason="signal")
    record["attempts"].append(attempt)
    record["state"] = "interrupted"
    record["resume_blocked"] = "effect-uncertain"
    runstore.write_record(directory, record)
    return prepared, directory


def test_resume_mode_refusal_leaves_the_run_state_alone(env):
    prepared, directory = _interrupted_run(env)
    before = runstore.load_record(directory, prepared.run_id)
    env.refused("opencode-binary-absent", "missing-binary", drv=env.driver(which=lambda n: None), resume_run_id=prepared.run_id)
    after = runstore.load_record(directory, prepared.run_id)
    assert after["state"] == "interrupted" and after["resume_blocked"] == "effect-uncertain"
    assert after["refusal"]["code"] == "opencode-binary-absent"
    assert after["input_hashes"] == before["input_hashes"] and after["repo_revisions"] == before["repo_revisions"]
    assert len(after["history"]) == len(before["history"])


def test_resume_mode_success_stages_an_attempt_without_a_state_change(env):
    prepared, directory = _interrupted_run(env)
    again = env.driver().prepare(env.request(), resume_run_id=prepared.run_id)
    assert again.run_id == prepared.run_id
    record = runstore.load_record(directory, prepared.run_id)
    assert record["state"] == "interrupted" and record["refusal"] is None
    staged = record["attempts"][-1]
    assert staged["state"] == "staged" and staged["attempt"] == 2
    assert staged["input_hashes_before"] == dict(again.input_hashes)
    env.driver().prepare(env.request(), resume_run_id=prepared.run_id)
    assert [a["state"] for a in runstore.load_record(directory, prepared.run_id)["attempts"]].count("staged") == 1


def test_resume_mode_rejects_a_mismatched_request(env):
    prepared, directory = _interrupted_run(env)
    env.refused("resume-request-mismatch", "workflow-validation", request=env.request(task="other"), resume_run_id=prepared.run_id)
    env.refused("resume-run-missing", "workflow-validation", resume_run_id="oc-20200101T000000Z-deadbeef")
