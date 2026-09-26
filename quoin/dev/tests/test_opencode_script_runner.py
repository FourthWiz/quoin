"""The `quoin opencode script` runner's tests.

Covers `unsafe_output_fds`, `find_project_root`, `discovery_map_output` and
`run` in `scripts.py`. Every in-process test that reaches a real script sets
the working directory with `monkeypatch.chdir` into a temporary project, and
the module-level fixture below asserts the repository's own discovery map is
never touched, so no test can write into this repository.

CLI wiring (`quoin opencode script ...` as a real subcommand) lands in a
later task; the pipe/redirect tests here invoke `scripts.run` directly in a
subprocess instead of going through the `quoin` executable, which still
exercises real OS-level fd behavior (the property under test) without a
forward dependency on that later task.
"""
from __future__ import annotations

import argparse
import io
import json
import stat as stat_module
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.opencode_adapter import generate, scripts

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"
SRC_DIR = REPO_ROOT / "src"
DISCOVERY_MAP_PATH = REPO_ROOT / generate.ARTIFACT_ROOT / "discovery-map.json"


@pytest.fixture(autouse=True)
def _protect_repo_discovery_map():
    existed_before = DISCOVERY_MAP_PATH.exists()
    before = DISCOVERY_MAP_PATH.read_bytes() if existed_before else None
    yield
    if existed_before:
        assert DISCOVERY_MAP_PATH.exists()
        assert DISCOVERY_MAP_PATH.read_bytes() == before
    else:
        assert not DISCOVERY_MAP_PATH.exists()


class _FakeStat:
    def __init__(self, mode):
        self.st_mode = mode


def _fstat_safe():
    return lambda fd: _FakeStat(stat_module.S_IFCHR)


def _fstat_regular_except(safe_fd):
    def _fstat(fd):
        if fd == safe_fd:
            return _FakeStat(stat_module.S_IFCHR)
        return _FakeStat(stat_module.S_IFREG)

    return _fstat


def _make_project(root: Path) -> Path:
    quoin_dir = root / ".quoin"
    quoin_dir.mkdir(parents=True, exist_ok=True)
    (quoin_dir / "opencode-install.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quoin_version": "0.1.0",
                "opencode_version": "1.18.32",
                "profile": None,
                "owned": {},
                "created_dirs": [],
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(name, argv, out=None, err=None, fstat=None, source_dir=SOURCE_DIR):
    out = out if out is not None else io.StringIO()
    err = err if err is not None else io.StringIO()
    code = scripts.run(name, argv, source_dir, out=out, err=err, fstat=fstat or _fstat_safe())
    return code, out.getvalue(), err.getvalue()


# --- unsafe_output_fds / allowlist / redirect-first ordering ---


def test_unsafe_output_fds_flags_regular_files_not_pipes_or_ttys():
    fstat = _fstat_regular_except(safe_fd=None)
    assert scripts.unsafe_output_fds(fstat) == [1, 2]
    assert scripts.unsafe_output_fds(_fstat_safe()) == []


def test_unsafe_output_fds_treats_a_closed_descriptor_as_safe():
    def _fstat(fd):
        raise OSError(9, "bad file descriptor")

    assert scripts.unsafe_output_fds(_fstat) == []


def test_unknown_script_name_returns_2_and_lists_all_six(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = _run("evil", [])
    assert code == 2
    for name in scripts.ALLOWED_SCRIPTS:
        assert name in err


def test_regular_file_on_fd1_refuses_without_running_anything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scripts.runpy, "run_path", lambda *a, **k: pytest.fail("script ran"))
    code, out, err = _run("path_resolve", ["--help"], fstat=_fstat_regular_except(safe_fd=2))
    assert code == 2
    assert "refusing to run" in err


def test_regular_file_on_fd2_refuses_without_running_anything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scripts.runpy, "run_path", lambda *a, **k: pytest.fail("script ran"))
    code, out, err = _run("path_resolve", ["--help"], fstat=_fstat_regular_except(safe_fd=1))
    assert code == 2
    assert "refusing to run" in out


def test_fd2_unsafe_with_unknown_name_writes_nothing_to_err(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = _run("evil", [], fstat=_fstat_regular_except(safe_fd=1))
    assert code == 2
    assert err == ""
    assert "refusing to run" in out


def test_both_fds_unsafe_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = _run("path_resolve", ["--help"], fstat=_fstat_regular_except(safe_fd=None))
    assert code == 2
    assert out == ""
    assert err == ""


# --- subprocess: real fd behavior ---

_SUBPROCESS_RUNNER = (
    "import sys; sys.path.insert(0, {src!r}); "
    "from quoin.opencode_adapter import scripts; "
    "sys.exit(scripts.run({name!r}, {argv!r}, {source_dir!r}))"
)


def _subprocess_code(name, argv):
    return _SUBPROCESS_RUNNER.format(src=str(SRC_DIR), name=name, argv=list(argv), source_dir=str(SOURCE_DIR))


def test_subprocess_with_pipes_is_accepted(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _subprocess_code("path_resolve", ["--help"])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0
    assert b"usage" in proc.stdout.lower()


def test_subprocess_stdout_redirected_to_a_file_refuses_and_leaves_it_emptied(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("KEEP")
    with open(target, "w") as fh:  # a shell `>` empties the target before the runner ever starts
        proc = subprocess.run(
            [sys.executable, "-c", _subprocess_code("path_resolve", [])],
            stdout=fh,
            stderr=subprocess.PIPE,
            cwd=str(tmp_path),
        )
    assert proc.returncode == 2
    assert b"refusing to run" in proc.stderr
    assert target.read_text() == ""


# --- every allowlisted script runs with --help ---


def test_every_allowlisted_script_runs_with_help(tmp_path, monkeypatch):
    for name in scripts.ALLOWED_SCRIPTS:
        if name in scripts.WRITE_CAPABLE_SCRIPTS:
            project = _make_project(tmp_path / ("proj-%s" % name))
            monkeypatch.chdir(project)
        else:
            monkeypatch.chdir(tmp_path)
        code, _, err = _run(name, ["--help"])
        assert code == 0, (name, err)


# --- exit-code passthrough ---


def test_exit_code_passthrough_for_a_failing_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, _ = _run("path_resolve", ["--task"])  # missing required value: argparse usage error
    assert code == 2


def test_non_int_system_exit_prints_and_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _raise(*a, **k):
        raise SystemExit("boom")

    monkeypatch.setattr(scripts.runpy, "run_path", _raise)
    code, _, err = _run("path_resolve", ["--help"])
    assert code == 1
    assert "boom" in err


def test_system_exit_none_returns_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _raise(*a, **k):
        raise SystemExit(None)

    monkeypatch.setattr(scripts.runpy, "run_path", _raise)
    code, _, _ = _run("path_resolve", [])
    assert code == 0


# --- sidecar injection ---


def test_validate_artifact_gets_the_sections_sidecar_prepended(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured = {}

    def _spy(path, run_name=None):
        captured["argv"] = list(sys.argv)
        raise SystemExit(0)

    monkeypatch.setattr(scripts.runpy, "run_path", _spy)
    code, _, _ = _run("validate_artifact", ["somefile.md"])
    assert code == 0
    argv = captured["argv"]
    assert argv[1] == "--sections-json"
    assert argv[2] == str(SOURCE_DIR / "memory" / "format-kit.sections.json")
    assert argv[-1] == "somefile.md"


def test_caller_supplied_sections_json_wins_over_the_sidecar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured = {}

    def _spy(path, run_name=None):
        captured["argv"] = list(sys.argv)
        raise SystemExit(0)

    monkeypatch.setattr(scripts.runpy, "run_path", _spy)
    _run("validate_artifact", ["somefile.md", "--sections-json", "mine.json"])
    assert captured["argv"][-2:] == ["--sections-json", "mine.json"]


# --- find_project_root ---


def test_find_project_root_walks_up_to_the_nearest_metadata_file(tmp_path):
    project = _make_project(tmp_path / "proj")
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    assert scripts.find_project_root(nested) == project


def test_find_project_root_returns_none_when_absent(tmp_path):
    assert scripts.find_project_root(tmp_path) is None


# --- output confinement (generate_discovery_map) ---


def test_output_confinement_default_output_lands_inside_the_artifact_root(tmp_path, monkeypatch):
    project = _make_project(tmp_path / "proj")
    monkeypatch.chdir(project)
    code, _, err = _run("generate_discovery_map", [])
    assert code == 0, err
    assert (project / generate.ARTIFACT_ROOT / "discovery-map.json").is_file()


def test_output_confinement_relative_output_inside_the_root_is_allowed(tmp_path, monkeypatch):
    project = _make_project(tmp_path / "proj")
    monkeypatch.chdir(project)
    code, _, err = _run("generate_discovery_map", ["--output", "%s/y.json" % generate.ARTIFACT_ROOT])
    assert code == 0, err
    assert (project / generate.ARTIFACT_ROOT / "y.json").is_file()


def test_output_confinement_stdout_flag_is_allowed_and_writes_no_file(tmp_path, monkeypatch):
    project = _make_project(tmp_path / "proj")
    monkeypatch.chdir(project)
    code, _, err = _run("generate_discovery_map", ["--stdout"])
    assert code == 0, err
    assert not (project / generate.ARTIFACT_ROOT).exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--output", "src/app.py"],
        ["-o", "../outside.json"],
        ["--out", "y.json"],
        ["--", "somewhere"],
    ],
    ids=["outside-relative", "outside-parent", "abbreviated-flag", "literal-dashdash"],
)
def test_output_confinement_refuses_paths_outside_the_root(tmp_path, monkeypatch, argv):
    project = _make_project(tmp_path / "proj")
    monkeypatch.chdir(project)
    code, _, _ = _run("generate_discovery_map", argv)
    assert code == 2
    assert not (project / generate.ARTIFACT_ROOT / "discovery-map.json").exists()


def test_output_confinement_refuses_a_symlinked_dest_tmp(tmp_path, monkeypatch):
    project = _make_project(tmp_path / "proj")
    artifact_dir = project / generate.ARTIFACT_ROOT
    artifact_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside.json"
    outside_target.write_text("do not touch")
    (artifact_dir / "discovery-map.json.tmp").symlink_to(outside_target)
    monkeypatch.chdir(project)

    code, _, _ = _run("generate_discovery_map", [])
    assert code == 2
    assert outside_target.read_text() == "do not touch"
    assert not (artifact_dir / "discovery-map.json").exists()


def test_output_confinement_refuses_when_no_project_is_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = _run("generate_discovery_map", [])
    assert code == 2
    assert "run from inside a project" in err


def test_output_confinement_through_the_cli_dashdash_still_refuses(tmp_path, monkeypatch):
    """Even if a caller passes `--` straight to the runner (a future CLI
    layer may or may not strip it first), the runner refuses rather than
    guessing."""
    project = _make_project(tmp_path / "proj")
    monkeypatch.chdir(project)
    code, _, _ = _run("generate_discovery_map", ["--", str(tmp_path)])
    assert code == 2
    assert not (project / generate.ARTIFACT_ROOT / "discovery-map.json").exists()


# --- mirror-drift ---


def test_mirror_parser_option_set_matches_the_real_script():
    import _opencode_helpers

    captured = {}
    original_parse_args = argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        captured["parser"] = self
        raise SystemExit(0)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(argparse.ArgumentParser, "parse_args", _capture)
        module = _opencode_helpers.load_module(
            SOURCE_DIR / "core" / "scripts" / "generate_discovery_map.py", "quoin_test_gdm_mirror_drift"
        )
        with pytest.raises(SystemExit):
            module.main([])

    real_parser = captured["parser"]

    def _signature(parser, exclude_help):
        return {
            (tuple(a.option_strings), a.dest, a.nargs, type(a).__name__)
            for a in parser._actions
            if not (exclude_help and a.dest == "help")
        }

    mirror_parser = scripts._build_discovery_map_mirror_parser()
    assert _signature(mirror_parser, exclude_help=True) == _signature(real_parser, exclude_help=True)
