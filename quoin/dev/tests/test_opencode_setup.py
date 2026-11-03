"""`quoin opencode setup` and `quoin code`: idempotency, the missing-binary
path with a fake installer, the refusal paths and the work-profile limits.
Everything runs offline against a temporary home, project and fake binary."""
from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

from _opencode_merge_helpers import NOW, PROFILE_WORK, fixture, write_probe_records
from quoin import cli
from quoin.opencode_adapter import compiler, config, manifest, merge, paths, setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake executables are POSIX shell scripts")

SOURCE_DIR = cli._resolve_source_dir(None)
PINNED = manifest.read_pinned_version(SOURCE_DIR)


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = Path(tmp_path)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env = {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.environ = dict(self.env, HOME=str(self.home), OPENROUTER_API_KEY="sk-test-0000")
        for key, value in self.environ.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.home))
        self.version = PINNED  # what the opencode on PATH reports
        self.answers = []
        self.asked = []
        self.installs = []
        self.probes = []
        self.install_creates = True
        self.interactive = True
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.put_binary(self.bin / "opencode")

    def put_binary(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho %s\n" % PINNED, encoding="utf-8")
        path.chmod(0o755)

    def which(self, name):
        target = self.bin / name
        return str(target) if target.is_file() else None

    def version_runner(self, path):
        if Path(path).parent == self.bin:
            return "opencode %s\n" % self.version
        return Path(path).read_text(encoding="utf-8").split()[-1] + "\n"

    def installer(self, version, environ):
        self.installs.append(version)
        if self.install_creates:
            self.put_binary(self.home / ".opencode" / "bin" / "opencode")
        return 0

    def probe(self, **kw):
        self.probes.append(kw["model"])
        loaded = config.load_all(project_root=self.root, profile=kw["profile"], env=self.env, home=self.home)
        write_probe_records(self.env, self.home, merge.merge(loaded))
        return 0

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else True

    def host(self, **overrides):
        self.out, self.err = io.StringIO(), io.StringIO()
        values = dict(
            environ=dict(self.environ), config_env=dict(self.env), home=self.home, now=NOW,
            platform="linux", source_dir=SOURCE_DIR, which=self.which, version_runner=self.version_runner,
            installer=self.installer, probe=self.probe, ask=self.ask if self.interactive else None,
            out=self.out, err=self.err,
        )
        values.update(overrides)
        return setup.Host(**values)

    def setup(self, profile="personal", **kw):
        return setup.run_setup(self.host(), self.root, profile, **kw)

    def profile_path(self, name="personal"):
        return paths.profile_path(name, self.env, self.home)

    def snapshot(self):
        tree = {}
        for base in (self.root, self.tmp / "xdg", self.tmp / "state"):
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    tree[str(path)] = (path.stat().st_mtime_ns, path.read_bytes())
        return tree


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_clean_setup_does_every_step_and_finishes_with_the_doctor(world):
    assert world.setup() == 0, world.out.getvalue() + world.err.getvalue()
    out = world.out.getvalue()
    assert "done: wrote the project scaffold" in out
    assert "done: created profile personal" in out
    assert "done: compiled profile personal" in out
    assert out.rstrip().endswith("next: quoin code")
    assert world.profile_path().is_file()
    assert world.installs == []
    assert world.probes  # every model was qualified
    state = setup.load_state(world.host(), world.root)
    assert state["last_profile"] == "personal"
    assert not (world.root / ".quoin" / setup.STATE_FILE).exists()  # per-user, never in the project
    assert state["confirmed_model_ids"]["personal"]
    assert "sk-test-0000" not in json.dumps(state)


def test_rerun_with_nothing_changed_is_a_no_op(world):
    assert world.setup() == 0
    before = world.snapshot()
    world.probes.clear()
    world.asked.clear()
    assert world.setup() == 0
    assert "nothing to do" in world.out.getvalue()
    assert world.snapshot() == before
    assert world.probes == [] and world.asked == [] and world.installs == []


def test_missing_binary_is_installed_with_the_pinned_version(world):
    (world.bin / "opencode").unlink()
    assert world.setup() == 1
    assert world.installs == [PINNED]
    assert "installed OpenCode %s" % PINNED in world.out.getvalue()
    # Installed under the user's home, not yet on PATH: setup says so.
    assert "add %s to PATH" % (world.home / ".opencode" / "bin") in world.err.getvalue()


def test_wrong_version_offers_the_pinned_release(world):
    world.version = "0.0.1"
    world.answers = [False]
    assert world.setup() == 1
    assert "reports 0.0.1" in world.asked[0]
    assert world.installs == []
    assert "install declined" in world.err.getvalue()


def test_install_while_a_wrong_version_is_on_path_says_to_fix_path(world):
    world.version = "0.0.1"
    assert world.setup() == 1
    assert world.installs == [PINNED]
    assert "installed OpenCode %s" % PINNED in world.out.getvalue()
    assert "put %s ahead of %s on PATH" % (world.home / ".opencode" / "bin", world.bin) in world.err.getvalue()


def test_installer_that_does_not_produce_the_binary_is_reported(world):
    (world.bin / "opencode").unlink()
    world.install_creates = False
    assert world.setup() == 1
    assert world.installs == [PINNED]
    assert "the installer did not produce it" in world.err.getvalue()


def test_non_interactive_without_yes_refuses_to_install_or_confirm(world):
    (world.bin / "opencode").unlink()
    world.interactive = False
    assert world.setup() == 1
    err = world.err.getvalue()
    assert world.installs == []
    assert "run interactively or pass --yes" in err
    assert not world.profile_path().exists()


def test_yes_never_confirms_a_new_model_id(world):
    assert world.setup(yes=True) == 1
    assert "--yes only reuses confirmations already given" in world.err.getvalue()
    assert not world.profile_path().exists()
    assert world.asked == []


def test_yes_reuses_confirmations_already_given(world):
    assert world.setup() == 0
    world.profile_path().unlink()
    world.asked.clear()
    assert world.setup(yes=True) == 0
    assert world.profile_path().is_file()
    assert world.asked == []


def test_declined_model_id_writes_no_profile(world):
    world.answers = [False]
    assert world.setup() == 1
    assert "declined" in world.err.getvalue()
    assert not world.profile_path().exists()
    assert world.probes == []


def test_missing_credential_is_named_and_nothing_is_probed(world):
    del world.environ["OPENROUTER_API_KEY"]
    assert world.setup() == 1
    err = world.err.getvalue()
    assert "set the OPENROUTER_API_KEY environment variable" in err
    assert world.probes == []


def install_work_profile(world):
    target = world.profile_path("work")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture(PROFILE_WORK)), encoding="utf-8")


@pytest.mark.parametrize("problem", ["missing", "wrong-version", "newer-than-qualified"])
def test_work_profile_never_installs_a_binary(world, problem):
    install_work_profile(world)
    if problem == "missing":
        (world.bin / "opencode").unlink()
    else:
        world.version = "0.0.1" if problem == "wrong-version" else "99.0.0"
    assert world.setup(profile="work", yes=True) == 1
    assert world.installs == [] and world.asked == []
    assert "work profiles accept only qualified releases" in world.err.getvalue()


def test_work_project_also_takes_the_strict_path(world):
    (world.root / ".quoin").mkdir()
    (world.root / ".quoin" / "runtime.json").write_text(json.dumps({"classification": "work"}), encoding="utf-8")
    (world.bin / "opencode").unlink()
    assert world.setup(yes=True) == 1
    assert world.installs == []
    assert "work profiles accept only qualified releases" in world.err.getvalue()


def test_unreadable_existing_profile_takes_the_strict_path(world):
    target = world.profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    (world.bin / "opencode").unlink()
    assert world.setup(yes=True) == 1
    assert world.installs == []


def test_declined_project_classification_writes_nothing(world):
    world.answers = [True, True, True, False]  # three model ids, then the classification
    assert world.setup() == 1
    assert "classify this project in .quoin/runtime.json (declined)" in world.err.getvalue()
    assert not (world.root / ".quoin" / "runtime.json").exists()
    assert world.probes == []


def test_keychain_credential_is_a_note_not_a_blocker(world):
    assert world.setup() == 0
    doc = json.loads(world.profile_path().read_text(encoding="utf-8"))
    for provider in doc["providers"].values():
        provider["credential_ref"] = "keychain:quoin-openrouter/me"
    world.profile_path().write_text(json.dumps(doc), encoding="utf-8")
    assert world.setup() == 0, world.err.getvalue()
    assert "note: make sure the keychain entry quoin-openrouter/me exists" in world.out.getvalue()


def test_invalid_profile_label_is_a_usage_error(world):
    assert world.setup(profile="Bad Name") == 2


# ------------------------------------------------------------------ quoin code


@pytest.fixture
def launched(world, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_opencode_setup_host", lambda args: world.host())
    monkeypatch.setattr(cli, "_cmd_opencode_start", lambda args: calls.append(args) or 0)
    return calls


def code(world, *extra):
    return cli.main(["code", "--project-root", str(world.root), *extra])


def test_code_uses_the_remembered_profile(world, launched):
    assert world.setup() == 0
    assert code(world) == 0
    assert [(c.profile, c.dry_run) for c in launched] == [("personal", False)]


def test_code_without_a_profile_or_record_is_refused(world, launched):
    assert code(world) == 2
    assert launched == []


def test_code_recompiles_when_the_compiled_pair_is_stale(world, launched):
    assert world.setup() == 0
    ev = compiler.evaluate(project_root=world.root, profile="personal", env=world.env, home=world.home, now=NOW)
    directory = setup.compiled_directory(world.host(), ev)
    (directory / compiler.NATIVE_FILE).unlink()
    assert code(world) == 0
    assert "compiled profile personal" in world.out.getvalue()
    assert compiler.check(ev, directory).ok
    assert len(launched) == 1


def test_code_refuses_when_setup_is_incomplete(world, launched):
    assert world.setup() == 0
    world.version = "0.0.1"
    assert code(world) == 1
    assert "run quoin opencode setup" in world.err.getvalue()
    assert launched == []


def test_a_newer_release_is_accepted_for_a_personal_profile(world, launched):
    world.version = "99.0.0"
    assert world.setup() == 0
    assert world.installs == []
    assert code(world) == 0
    assert len(launched) == 1


def test_code_rejects_an_invalid_profile_label(world, launched):
    assert code(world, "--profile", "../x") == 2
    assert launched == []


def test_code_remembers_an_explicit_profile(world, launched):
    assert world.setup() == 0
    assert world.setup(profile="second") == 0
    assert code(world, "--profile", "personal") == 0
    assert setup.load_state(world.host(), world.root)["last_profile"] == "personal"
