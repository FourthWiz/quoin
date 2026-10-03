"""The fake OpenCode executable's write verbs and boundary scenarios."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fake_opencode import Harness, fake  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX-only")


def _scenario(*steps):
    return {"attempts": [{"steps": list(steps) + [{"do": "exit", "code": 0}]}]}


def _effects(h):
    return (h.state / "effects.log").read_text().splitlines()


@pytest.fixture()
def harness(tmp_path):
    made = []

    def _make(scenario):
        h = Harness(tmp_path, scenario)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.cleanup()


@pytest.mark.parametrize("verb", ["try_write", "shell_write", "spawn_write"])
def test_each_write_verb_writes_relative_to_cwd(harness, verb):
    h = harness(_scenario({"do": verb, "path": "deep/dir/out.txt", "content": "hello"}))
    res = h.run("run", "--", "x")
    assert res.returncode == 0
    assert (h.cwd / "deep" / "dir" / "out.txt").read_text() == "hello"
    assert _effects(h) == ["deep/dir/out.txt"]
    assert not (Path.cwd() / "deep").exists()


@pytest.mark.parametrize("verb", ["try_write", "shell_write", "spawn_write"])
def test_read_only_target_is_logged_denied_and_the_run_continues(harness, verb):
    h = harness(_scenario(
        {"do": verb, "path": "ro/out.txt", "content": "no"},
        {"do": "write_file", "path": "after.txt", "content": "ok"}))
    ro = h.cwd / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        res = h.run("run", "--", "x")
    finally:
        ro.chmod(stat.S_IRWXU)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    assert res.returncode == 0
    lines = _effects(h)
    assert lines[0].startswith("denied ro/out.txt ")
    assert lines[1] == "after.txt"
    assert not (ro / "out.txt").exists()


def test_try_write_names_the_errno(harness):
    h = harness(_scenario({"do": "try_write", "path": "ro/out.txt", "content": "no"}))
    ro = h.cwd / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        h.run("run", "--", "x")
    finally:
        ro.chmod(stat.S_IRWXU)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    assert _effects(h) == ["denied ro/out.txt EACCES"]


def test_shell_and_spawned_writes_land_in_the_fake_cwd(harness, tmp_path):
    h = harness(_scenario(
        {"do": "shell_write", "path": "s.txt", "content": "a"},
        {"do": "spawn_write", "path": "p.txt", "content": "b"}))
    start = tmp_path / "elsewhere"
    start.mkdir()
    import subprocess
    res = subprocess.run([str(h.shim), "run", "--", "x"], cwd=str(h.cwd), env=h.env(),
                         capture_output=True, timeout=20)
    assert res.returncode == 0
    assert (h.cwd / "s.txt").read_text() == "a"
    assert (h.cwd / "p.txt").read_text() == "b"
    assert not (start / "s.txt").exists()


def test_run_cmd_passes_the_environment_through(harness):
    h = harness(_scenario({
        "do": "run_cmd",
        "argv": [sys.executable, "-c", "import os; print(os.environ['FAKE_TEST_SECRET'])"]}))
    res = h.run("run", "--", "x")
    assert res.returncode == 0
    assert _effects(h) == ["ran 0 s3cr3t-value-123"]


def test_run_cmd_logs_a_failing_exit_and_cwd(harness):
    h = harness(_scenario({
        "do": "run_cmd",
        "argv": [sys.executable, "-c", "import os, sys; print(os.getcwd()); sys.exit(3)"]}))
    h.run("run", "--", "x")
    line = _effects(h)[0]
    assert line.startswith("ran 3 ")
    assert os.path.realpath(line[len("ran 3 "):]) == os.path.realpath(str(h.cwd))


@pytest.mark.parametrize("name,kwargs", [
    ("boundary_escape", {}),
    ("boundary_escape", {"absolute": True, "real_root": "/somewhere"}),
    ("edits_path", {}),
    ("writes_paths", {}),
    ("writes_paths", {"paths": [("a.txt", "x"), ("b/c.txt", "y")]}),
    ("opencode_managed_writes", {}),
    ("opencode_managed_writes", {"extra": [("x.txt", "y")]}),
])
def test_new_scenarios_build_and_serialize(name, kwargs):
    scenario = fake.SCENARIOS[name](**kwargs)
    assert scenario["attempts"][0]["steps"]
    json.dumps(scenario)


def test_boundary_escape_writes_everything_relative_and_the_outbox_files(harness):
    h = harness(fake.SCENARIOS["boundary_escape"]())
    res = h.run("run", "--", "x")
    assert res.returncode == 0
    for rel in ("src/app.py", "src/shell.txt", "src/spawned.txt"):
        assert (h.cwd / rel).exists(), rel
    stage = h.cwd / ".workflow_artifacts" / "demo" / "stage-1"
    assert (stage / "review-1.md").read_text() == fake.REVIEW_APPROVED_TEXT
    assert (stage / "review-1.md.tmp").exists()
    assert not any("escaped.txt" in line for line in _effects(h))


def test_boundary_escape_absolute_writes_under_the_real_root(harness, tmp_path):
    real = tmp_path / "real"
    h = harness(fake.SCENARIOS["boundary_escape"](real_root=str(real), absolute=True))
    assert h.run("run", "--", "x").returncode == 0
    assert (real / "src" / "escaped.txt").exists()


def test_opencode_managed_writes_lists_the_managed_names(harness):
    h = harness(fake.SCENARIOS["opencode_managed_writes"](extra=[("keep.txt", "k")]))
    assert h.run("run", "--", "x").returncode == 0
    assert (h.cwd / ".opencode" / ".gitignore").exists()
    assert (h.cwd / ".opencode" / "bun.lock").exists()
    assert (h.cwd / ".opencode/node_modules/@opencode-ai/plugin/package.json").exists()
    assert (h.cwd / "keep.txt").read_text() == "k"


def test_text_constants_are_non_empty():
    assert "<verdict>PASS</verdict>" in fake.CRITIC_PASS_TEXT
    assert "APPROVED" in fake.REVIEW_APPROVED_TEXT
