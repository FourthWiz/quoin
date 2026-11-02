"""A seeded work credential must not appear on any surface the launcher, the
CLI or a child process can write to.

The work seed is delivered only through the launcher's resolver, never through
an ambient variable. It is generated without any recognisable secret shape, so
only the run's own value registration can mask it; a second parametrisation uses
a shaped seed that the shape rule also catches. The scan covers both profile
roots (minus the credential files the retention policy keeps), the project and
its run store, the fake executable's state, and everything the CLI printed.

Each spawned path first proves that the seed reached the child and the gateway,
so an empty scan cannot pass because nothing ever carried the value.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

import pytest

import _opencode_capture_helpers as ch
from _opencode_capture_helpers import egress_guard  # noqa: F401  (pytest fixture)
import _opencode_driver_helpers as dh
import _opencode_helpers as helpers
import _opencode_isolation_helpers as iso
from _opencode_run_helpers import InstalledProject
from test_opencode_isolation_launch import GW
from quoin import cli
from quoin.opencode_adapter import config, errors, launch_env, netpolicy, profile_dirs, retention, runstore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

SHAPELESS_PREFIX = "wseed"


def shapeless_seed() -> str:
    return SHAPELESS_PREFIX + secrets.token_hex(16)


def shaped_seed() -> str:
    return "sk-" + secrets.token_hex(12) + "A1"


SEED_KINDS = {"shapeless": shapeless_seed, "shaped": shaped_seed}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_seeds_have_the_shapes_the_scan_relies_on():
    plain = shapeless_seed()
    assert errors.SECRET_SHAPE_RE.search(plain) is None
    assert len(plain) >= launch_env.MIN_REDACTED_LENGTH
    assert errors.SECRET_SHAPE_RE.search(shaped_seed())


# ------------------------------------------------------------------ the world


class Scene:
    """One project, one work seed, and the surfaces a run leaves behind."""

    def __init__(self, tmp_path, monkeypatch, capsys, guard, scenario, seed, *, summary=False, network=False):
        self.tmp = tmp_path
        self.capsys = capsys
        self.guard = guard
        self.seed = seed
        self.personal_seed = ch.make_seed("pseed")
        self.texts = []
        doc = ch.with_allowed_targets(_with_delivery_hash(scenario), guard.allowed_targets())
        self.project = InstalledProject(tmp_path, monkeypatch, doc, secret=seed)
        ch.bind_work_providers(self.project.world, guard.work)
        if network:
            guard.watch_network_log(dh.state_of(tmp_path) / "network.jsonl", required=True)
        if summary:
            policy = self.project.world.profile.setdefault("policy", {})
            policy["logging"] = {"content": "summary"}
            self.project.world.write()
            self.project.world.write_records()
        monkeypatch.setattr(cli, "_make_opencode_driver", self.project.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        for key, value in self.project.world.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("HOME", str(self.project.world.home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.project.world.home))
        self.root = str(self.project.root)

    # -- driving the CLI ---------------------------------------------------
    def main(self, *argv):
        code = cli.main(list(argv))
        captured = self.capsys.readouterr()
        self.texts += [("stdout %s" % " ".join(argv[:3]), captured.out), ("stderr %s" % " ".join(argv[:3]), captured.err)]
        return code, captured.out, captured.err

    def run(self):
        code, out, _err = self.main("run", "demo", "--runtime", "opencode", "--profile", "work",
                                    "--phase", "plan", "--project-root", self.root, "--halt-on-abort")
        summary = json.loads(out.strip().splitlines()[-1])
        return code, summary

    def operator_commands(self, run_id):
        explain = ["opencode", "config", "explain", "--profile", "work", "--project-root", self.root]
        for extra in ((), ("--redact",), ("--json",), ("--run", run_id), ("--run", run_id, "--json"), ("--export",)):
            self.main(*explain, *extra)
        base = ["doctor", "--runtime", "opencode", "--project-root", self.root, "--source-dir", str(helpers.SOURCE_DIR)]
        for extra in (("--json",), ("--profile", "work", "--json")):
            self.main(*base, *extra)
        self.main("opencode", "status", "--run-id", run_id, "--project-root", self.root)
        self.main("opencode", "status", "--task", "demo", "--project-root", self.root, "--json")
        self.main("opencode", "start", "--profile", "work", "--project-root", self.root, "--dry-run")

    def purge(self):
        base = ["opencode", "purge", "--profile", "work", "--project-root", self.root]
        self.main(*base, "--dry-run", "--json")
        return self.main(*base, "--yes", "--json")

    # -- controls ----------------------------------------------------------
    def delivery_control(self):
        """The seed reached the child through the compiled credential variable."""
        name = config.provider_env_name(ch.WORK_PROVIDER_ID)
        lines = (dh.state_of(self.tmp) / "env-hashes.txt").read_text(encoding="utf-8").splitlines()
        assert "%s %s" % (name, _sha(self.seed)) in lines, "the work seed never reached the child"
        assert len(self.project.resolver_calls) >= 1

    # -- the scan ----------------------------------------------------------
    def roots(self):
        adapter = profile_dirs.adapter_root(self.project.world.env, self.project.world.home)
        return {name: profile_dirs.profile_root(adapter, name) for name in ("work", "personal")}

    def credential_files(self):
        out = {}
        for name, root in self.roots().items():
            dirs = profile_dirs.ProfileDirs.at(root)
            out[name] = [Path(getattr(dirs, cls)) / rel
                         for cls, rels in retention.CREDENTIAL_EXCLUDES.items() for rel in rels]
        return out

    def plant_personal(self):
        if getattr(self, "personal_auth", None) is not None:
            return
        self.personal_auth = ch.install_personal_profile(self.project.world, self.personal_seed)

    def corpus(self):
        return [self.roots()["work"], self.roots()["personal"], self.project.root,
                dh.state_of(self.tmp)] + list(self.texts)

    def corpus_files(self):
        exclude = [p for files in self.credential_files().values() for p in files]
        names = []
        for item in self.corpus():
            if isinstance(item, tuple):
                continue
            names += [Path(p) for p in ch._walk(Path(item), exclude)]  # noqa: SLF001
        return names

    def scan(self):
        """Plant the personal profile, check the credential-file controls and
        scan every surface for both seeds."""
        self.plant_personal()
        credentials = self.credential_files()
        personal_forms = ch.secret_forms(self.personal_seed)
        work_forms = ch.secret_forms(self.seed)
        assert self.personal_seed in self.personal_auth.read_text(encoding="utf-8")
        assert ch.scan_for_secrets(credentials["personal"] and [p for p in credentials["personal"] if p.exists()],
                                   work_forms) == []
        work_credentials = [p for p in credentials["work"] if p.exists()]
        if work_credentials:
            assert ch.scan_for_secrets(work_credentials, personal_forms) == []
        exclude = [p for files in credentials.values() for p in files]
        corpus = self.corpus()
        assert ch.scan_for_secrets(corpus, work_forms, exclude=exclude) == []
        assert ch.scan_for_secrets(corpus, personal_forms, exclude=exclude) == []


def _with_delivery_hash(scenario):
    doc = json.loads(json.dumps(dh.scenario_dict(scenario)))
    name = config.provider_env_name(ch.WORK_PROVIDER_ID)
    doc["attempts"][0]["steps"].insert(0, {"do": "hash_env", "name": name})
    return doc


@pytest.fixture
def cleanup(tmp_path):
    yield
    dh.cleanup_fakes(tmp_path)
    assert dh.stray_pids(tmp_path) == []


@pytest.fixture(params=sorted(SEED_KINDS))
def seed(request):
    return SEED_KINDS[request.param]()


# ------------------------------------------------------------------ spawned paths


def test_a_successful_headless_run_leaks_nothing(tmp_path, monkeypatch, capsys, egress_guard, cleanup, seed):
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"),
                  seed, network=True)
    code, summary = scene.run()
    assert code == 0 and summary["run_id"]
    scene.delivery_control()
    captures = egress_guard.work.captures()
    assert len(captures) == 1 and captures[0]["auth_sha256"] == _sha("Bearer " + seed)
    scene.operator_commands(summary["run_id"])
    scene.scan()


def test_a_crashing_child_leaks_nothing(tmp_path, monkeypatch, capsys, egress_guard, cleanup, seed):
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, "crash_after_start", seed)
    code, summary = scene.run()
    assert code != 0 and summary["run_id"]
    scene.delivery_control()
    scene.operator_commands(summary["run_id"])
    scene.scan()


@pytest.mark.parametrize("builder", ["secret_echo", "secret_echo_straddle"])
def test_a_child_that_echoes_the_key_is_not_stored_in_metadata_mode(
        tmp_path, monkeypatch, capsys, egress_guard, cleanup, seed, builder):
    """Absence check only: in metadata mode the echoed text is kept as a length
    and a digest, so no mask is exercised here."""
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS[builder](secret=seed), seed)
    code, summary = scene.run()
    assert summary["run_id"]
    scene.delivery_control()
    scene.operator_commands(summary["run_id"])
    scene.scan()


@pytest.mark.parametrize("builder", ["secret_echo", "secret_echo_straddle"])
def test_a_child_that_echoes_the_key_is_masked_in_summary_mode(
        tmp_path, monkeypatch, capsys, egress_guard, cleanup, seed, builder):
    """Summary mode stores the echoed text, so the run's own value registration
    is the only thing standing between the seed and the run store."""
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS[builder](secret=seed), seed,
                  summary=True)
    code, summary = scene.run()
    assert summary["run_id"]
    scene.delivery_control()
    if builder == "secret_echo":
        store = runstore.store_dir(scene.project.root)
        stored = [p for p in store.iterdir() if p.name.endswith(".jsonl") or p.name.endswith(".run.json")]
        assert len(stored) >= 2
        for path in stored:
            assert launch_env._REDACTED in path.read_text(encoding="utf-8"), path.name  # noqa: SLF001
    scene.operator_commands(summary["run_id"])
    scene.scan()


def test_purge_leaves_no_seed_and_keeps_credentials(tmp_path, monkeypatch, capsys, egress_guard, cleanup):
    seed = shapeless_seed()
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS["secret_echo"](secret=seed), seed,
                  summary=True)
    _code, summary = scene.run()
    scene.delivery_control()
    code, out, _err = scene.purge()
    assert code in (0, 1), out
    scene.scan()


# ------------------------------------------------------------------ refusals


def _refuse_destination(scene):
    scene.project.world.profile["providers"]["corp-gw"]["base_url"] = "https://personal.example.invalid/v1"
    scene.project.world.write()
    scene.project.world.write_records()


def _refuse_inventory(scene):
    (scene.project.root / "opencode.json").write_text(
        json.dumps({"provider": {"anthropic": {"options": {}}}}), encoding="utf-8")


def _refuse_isolation(scene):
    scene.project._seams["isolation_roots"] = iso.unverified_roots(scene.tmp / "empty")  # noqa: SLF001


def _refuse_identity(scene):
    scene.project.world.profile["providers"]["corp-gw"]["base_url"] = "https://%s/v1" % GW
    scene.project.world.write()
    scene.project.world.write_records()
    scene.project._seams["identity_checker"] = iso.FakeIdentity(  # noqa: SLF001
        {GW: netpolicy.IdentityResult("mismatch", "tls", "name-mismatch")})


@pytest.mark.parametrize("kind,breaker", [
    ("destination", _refuse_destination), ("inventory", _refuse_inventory),
    ("isolation", _refuse_isolation), ("identity", _refuse_identity),
], ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_a_refused_launch_resolves_nothing_and_prints_no_seed(
        tmp_path, monkeypatch, capsys, egress_guard, cleanup, seed, kind, breaker):
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"), seed)
    breaker(scene)
    code, out, _err = scene.main("run", "demo", "--runtime", "opencode", "--profile", "work",
                                 "--phase", "plan", "--project-root", scene.root)
    assert code != 0
    assert scene.project.resolver_calls == []
    assert not (dh.state_of(tmp_path) / "invocations.jsonl").exists()
    scene.main("opencode", "status", "--task", "demo", "--project-root", scene.root, "--json")
    scene.main("opencode", "start", "--profile", "work", "--project-root", scene.root, "--dry-run")
    scene.scan()


# ------------------------------------------------------------------ scan controls


def test_the_corpus_keeps_the_non_credential_data_files(tmp_path, monkeypatch, capsys, egress_guard, cleanup):
    """Excluding only the credential files, not the whole data directory: a seed
    planted in another file under `data/opencode/` is found, a seed in
    `auth.json` is not looked at."""
    seed = shapeless_seed()
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"), seed)
    _code, _summary = scene.run()
    scene.plant_personal()
    roots = scene.roots()
    for name in ("work", "personal"):
        data = profile_dirs.ProfileDirs.at(roots[name]).data / "opencode"
        data.mkdir(parents=True, exist_ok=True)
        (data / "session-marker.txt").write_text("marker", encoding="utf-8")
    files = {str(p) for p in scene.corpus_files()}
    for name in ("work", "personal"):
        data = profile_dirs.ProfileDirs.at(roots[name]).data / "opencode"
        assert str(data / "session-marker.txt") in files
        assert str(data / "auth.json") not in files and str(data / "mcp-auth.json") not in files
    planted = profile_dirs.ProfileDirs.at(roots["personal"]).data / "opencode" / "session-marker.txt"
    planted.write_text("leak " + seed, encoding="utf-8")
    with pytest.raises(AssertionError):
        scene.scan()
    planted.write_text("marker", encoding="utf-8")


def test_the_work_seed_in_a_personal_credential_file_is_found(tmp_path, monkeypatch, capsys, egress_guard, cleanup):
    seed = shapeless_seed()
    scene = Scene(tmp_path, monkeypatch, capsys, egress_guard, dh.fake.SCENARIOS["provider_call"]("implementer"), seed)
    scene.run()
    scene.plant_personal()
    scene.personal_auth.write_text(json.dumps({"openrouter": {"key": scene.personal_seed, "leak": seed}}), encoding="utf-8")
    with pytest.raises(AssertionError):
        scene.scan()


def test_the_scan_refuses_an_empty_corpus(tmp_path):
    with pytest.raises(AssertionError, match="empty corpus"):
        ch.scan_for_secrets([tmp_path], ch.secret_forms(shapeless_seed()))
