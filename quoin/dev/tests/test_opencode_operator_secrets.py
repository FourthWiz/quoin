"""A synthetic credential must not reach any operator-facing surface: the
doctor, status, start, the run summary, and the files a run leaves behind.

Two seeds are used. One matches a well-known secret shape and is therefore
caught even by a fresh redactor (the only kind status and the doctor have);
the other has no recognisable shape and is only caught by the run's own
redactor, so it must also never reach a surface that has none."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin import cli
from quoin.opencode_adapter import errors, install, secrets as credential_refs

from _opencode_run_helpers import InstalledProject

SHAPED = "sk-FAKEECHOSECRET0123456789"
PLAIN = "plainseededcredential-value-77"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


def test_the_two_seeds_have_the_shapes_the_test_relies_on():
    assert errors.SECRET_SHAPE_RE.search(SHAPED)
    assert not errors.SECRET_SHAPE_RE.search(PLAIN)


class _Resolver:
    def __init__(self, value):
        self.value = value

    def resolve(self, ref):
        return credential_refs.SecretValue(self.value)


@pytest.fixture(params=[SHAPED, PLAIN], ids=["shaped", "plain"])
def seeded(request, tmp_path, monkeypatch, capsys):
    seed = request.param
    project = InstalledProject(tmp_path, monkeypatch, ("secret_echo", {"secret": seed}))
    project._resolver = _Resolver(seed)  # noqa: SLF001
    factory = project.driver_factory()

    def make(root):
        drv = factory(root)
        drv._env["OPENROUTER_API_KEY"] = seed  # noqa: SLF001
        drv._env["AMBIENT_TOKEN"] = seed  # noqa: SLF001
        return drv

    monkeypatch.setattr(cli, "_make_opencode_driver", make)
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
    for key, value in project.world.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", str(project.world.home))
    monkeypatch.setenv("OPENROUTER_API_KEY", seed)
    monkeypatch.setattr(Path, "home", lambda: project.world.home)
    project.seed = seed
    yield project
    project.cleanup()


def main(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree_text(root):
    chunks = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            chunks.append(path.read_bytes().decode("utf-8", "replace"))
    return chunks


def test_no_surface_shows_the_seed(seeded, capsys):
    seed = seeded.seed
    root = str(seeded.root)
    surfaces = []

    code, out, err = main(capsys, "run", "demo", "--runtime", "opencode", "--profile", "work",
                          "--phase", "plan", "--project-root", root, "--halt-on-abort")
    summary = json.loads(out.strip().splitlines()[-1])
    assert code != 0 and summary["run_id"]
    surfaces += [out, err]

    for extra in ((), ("--json",)):
        _code, out, err = main(capsys, "opencode", "status", "--task", "demo", "--project-root", root, *extra)
        surfaces += [out, err]
    _code, out, err = main(capsys, "opencode", "status", "--run-id", summary["run_id"], "--project-root", root)
    surfaces += [out, err]

    _code, out, err = main(capsys, "opencode", "start", "--profile", "work", "--project-root", root, "--dry-run")
    surfaces += [out, err]

    base = ["doctor", "--runtime", "opencode", "--project-root", root, "--source-dir", str(helpers.SOURCE_DIR)]
    for extra in ((), ("--json",), ("--profile", "work"), ("--profile", "work", "--json")):
        _code, out, err = main(capsys, *base, *extra)
        surfaces += [out, err]

    surfaces += tree_text(seeded.root)
    for text in surfaces:
        assert seed not in text


def test_a_refusal_carrying_a_shaped_seed_is_redacted_everywhere(tmp_path, monkeypatch, capsys):
    project = InstalledProject(tmp_path, monkeypatch, ("replay", {"fixture": "plain-complete.jsonl"}))
    try:
        monkeypatch.setattr(cli, "_make_opencode_driver", project.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)

        def broken(root):
            raise install.InstallError("record unusable near %s" % SHAPED)

        monkeypatch.setattr(install, "load_metadata", broken)
        root = str(project.root)
        code, out, err = main(capsys, "run", "demo", "--runtime", "opencode", "--profile", "work",
                              "--phase", "plan", "--project-root", root)
        summary = json.loads(out.strip().splitlines()[-1])
        assert code == 3 and summary["refusal"]["code"] == "install-record-invalid"
        assert SHAPED not in out and SHAPED not in err and "<redacted>" in out
        code, out, err = main(capsys, "opencode", "start", "--profile", "work", "--project-root", root)
        assert code == 3 and SHAPED not in err and "<redacted>" in err
        _code, out, err = main(capsys, "opencode", "status", "--task", "demo", "--project-root", root, "--json")
        assert SHAPED not in out
        for text in tree_text(project.root):
            assert SHAPED not in text
    finally:
        project.cleanup()
