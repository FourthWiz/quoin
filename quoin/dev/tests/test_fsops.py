"""Behavior, exit-code and Python 3.8 floor tests for the fsops helper.

The helper replaces shell rm/rmdir/mv in skill instructions, so the contract
is: nothing on stdout when it succeeds, exactly one `fsops <sub>: ` line on
stderr per failure, and exit codes 0 (ok), 1 (OS error), 2 (usage), 3 (refused).
"""
from __future__ import annotations

import ast
import datetime
import errno
import glob
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "fsops.py"
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "fsops.py"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"

_SAFE_ENV_EXTRA = {"PYTHONDONTWRITEBYTECODE": "1"}


def _load_core():
    spec = importlib.util.spec_from_file_location("fsops_under_test", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fsops():
    return _load_core()


@pytest.fixture()
def root(tmp_path):
    """A scratch dir whose own name contains a space."""
    d = tmp_path / "My Drive"
    d.mkdir()
    return d


def _run(args, stdin_text=None, cwd=None):
    env = dict(os.environ)
    env.update(_SAFE_ENV_EXTRA)
    return subprocess.run(
        [sys.executable, str(WRAPPER_PATH)] + [str(a) for a in args],
        input=stdin_text if stdin_text is not None else "",
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _memory_tree(root: Path) -> Path:
    mem = root / ".workflow_artifacts" / "memory"
    _write(mem / "sub" / "keep.txt", "keep")
    return mem


def _case_insensitive_fs(root: Path) -> bool:
    _memory_tree(root)
    return os.path.isdir(str(root / ".workflow_artifacts" / "MEMORY"))


# ---------------------------------------------------------------- mv


def test_mv_file_replaces_existing_destination(fsops, root):
    src = _write(root / "a.txt", "new")
    dst = _write(root / "b.txt", "old")
    assert fsops.main(["mv", str(src), str(dst)]) == 0
    assert not src.exists()
    assert dst.read_text() == "new"


def test_mv_directory_replaces_empty_destination_directory(fsops, root):
    src = root / "srcdir"
    _write(src / "f.txt", "data")
    dst = root / "dstdir"
    dst.mkdir()
    assert fsops.main(["mv", str(src), str(dst)]) == 0
    assert not src.exists()
    assert (dst / "f.txt").read_text() == "data"
    assert not (dst / "srcdir").exists()


def test_mv_directory_onto_non_empty_directory_is_refused(fsops, root, capsys):
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    dst = root / "dstdir"
    _write(dst / "g.txt", "d")
    assert fsops.main(["mv", str(src), str(dst)]) == 3
    assert (src / "f.txt").read_text() == "s"
    assert (dst / "g.txt").read_text() == "d"
    assert capsys.readouterr().err.startswith("fsops mv: ")


def test_mv_overwrite_replaces_non_empty_directory_inside_artifacts(fsops, root):
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    dst = root / ".workflow_artifacts" / "x"
    _write(dst / "g.txt", "d")
    assert fsops.main(["mv", "--overwrite", str(src), str(dst)]) == 0
    assert (dst / "f.txt").read_text() == "s"
    assert not (dst / "g.txt").exists()
    assert not src.exists()
    assert [p.name for p in dst.parent.iterdir()] == ["x"]


def test_mv_overwrite_refuses_case_variant_of_memory(fsops, root):
    if not _case_insensitive_fs(root):
        pytest.skip("case-sensitive filesystem")
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    target = root / ".workflow_artifacts" / "Memory"
    assert fsops.main(["mv", "--overwrite", str(src), str(target)]) == 3
    assert (root / ".workflow_artifacts" / "memory" / "sub" / "keep.txt").read_text() == "keep"
    assert (src / "f.txt").exists()


def test_mv_overwrite_outside_artifacts_is_refused(fsops, root):
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    dst = root / "elsewhere"
    _write(dst / "g.txt", "d")
    assert fsops.main(["mv", "--overwrite", str(src), str(dst)]) == 3
    assert (dst / "g.txt").read_text() == "d"


def test_mv_overwrite_refuses_replacing_directory_with_file(fsops, root):
    src = _write(root / "a.txt", "s")
    dst = root / ".workflow_artifacts" / "x"
    _write(dst / "g.txt", "d")
    assert fsops.main(["mv", "--overwrite", str(src), str(dst)]) == 3
    assert (dst / "g.txt").read_text() == "d"
    assert src.exists()


def test_mv_overwrite_restores_destination_when_the_move_fails(fsops, root, monkeypatch, capsys):
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    dst = root / ".workflow_artifacts" / "x"
    _write(dst / "g.txt", "d")
    real_replace = os.replace

    def failing_replace(a, b, *args, **kwargs):
        if str(a) == str(src):
            raise OSError(errno.EACCES, "Permission denied")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(fsops.os, "replace", failing_replace)
    assert fsops.main(["mv", "--overwrite", str(src), str(dst)]) == 1
    monkeypatch.undo()
    assert (dst / "g.txt").read_text() == "d"
    assert (src / "f.txt").read_text() == "s"
    assert [p.name for p in dst.parent.iterdir()] == ["x"]
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_mv_overwrite_first_rename_failure_changes_nothing(fsops, root, monkeypatch):
    src = root / "srcdir"
    _write(src / "f.txt", "s")
    dst = root / ".workflow_artifacts" / "x"
    _write(dst / "g.txt", "d")

    def always_fail(a, b, *args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fsops.os, "replace", always_fail)
    assert fsops.main(["mv", "--overwrite", str(src), str(dst)]) == 1
    monkeypatch.undo()
    assert (dst / "g.txt").read_text() == "d"
    assert (src / "f.txt").read_text() == "s"


def test_mv_parents_creates_missing_parent(fsops, root):
    src = _write(root / "a.txt", "a")
    dst = root / "deep" / "er" / "b.txt"
    assert fsops.main(["mv", "--parents", str(src), str(dst)]) == 0
    assert dst.read_text() == "a"


def test_mv_missing_source_exits_1_with_one_line(fsops, root, capsys):
    assert fsops.main(["mv", str(root / "nope"), str(root / "dst")]) == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and err.startswith("fsops mv: ")


def test_mv_moves_a_symlink_as_a_link(fsops, root):
    target = _write(root / "target.txt", "t")
    link = root / "link"
    os.symlink(str(target), str(link))
    dst = root / "moved"
    assert fsops.main(["mv", str(link), str(dst)]) == 0
    assert os.path.islink(str(dst))
    assert target.read_text() == "t"
    assert not os.path.lexists(str(link))


def test_mv_cross_device_fallback(fsops, root, monkeypatch):
    src = _write(root / "a.txt", "a")
    dst = root / "b.txt"
    real_replace = os.replace
    calls = {"n": 0}

    def once_exdev(a, b, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(fsops.os, "replace", once_exdev)
    assert fsops.main(["mv", str(src), str(dst)]) == 0
    monkeypatch.undo()
    assert dst.read_text() == "a" and not src.exists()


# ---------------------------------------------------------------- rm


def test_rm_missing_path_is_silent_success():
    r = _run(["rm", "/nonexistent-fsops-path/x"])
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_rm_several_paths_and_regular_file(fsops, root):
    a = _write(root / "a")
    b = _write(root / "b")
    assert fsops.main(["rm", str(a), str(b)]) == 0
    assert not a.exists() and not b.exists()


def test_rm_directory_without_recursive_is_refused(fsops, root):
    d = root / ".workflow_artifacts" / "task"
    _write(d / "f")
    assert fsops.main(["rm", str(d)]) == 3
    assert (d / "f").exists()


@pytest.mark.parametrize(
    "rel",
    [
        "plain-dir",
        ".workflow_artifacts",
        ".workflow_artifacts/memory",
        ".workflow_artifacts/memory/sub",
        ".workflow_artifacts/x/.workflow_artifacts/memory",
        ".workflow_artifacts/x/../memory",
    ],
)
def test_rm_recursive_refused_for_unconfined_paths(fsops, root, rel):
    _memory_tree(root)
    _write(root / "plain-dir" / "f")
    _write(root / ".workflow_artifacts" / "x" / ".workflow_artifacts" / "memory" / "f")
    assert fsops.main(["rm", "-r", str(root / rel)]) == 3
    assert (root / ".workflow_artifacts" / "memory" / "sub" / "keep.txt").read_text() == "keep"
    assert (root / "plain-dir" / "f").exists()


@pytest.mark.parametrize(
    "rel",
    [".workflow_artifacts/MEMORY", ".workflow_artifacts/Memory/sub", ".WORKFLOW_ARTIFACTS/memory"],
)
def test_rm_recursive_refused_for_case_variants(fsops, root, rel):
    if not _case_insensitive_fs(root):
        pytest.skip("case-sensitive filesystem")
    assert fsops.main(["rm", "-r", str(root / rel)]) == 3
    assert (root / ".workflow_artifacts" / "memory" / "sub" / "keep.txt").read_text() == "keep"


def test_is_confined_casefold_rules_on_synthetic_paths(fsops):
    assert not fsops.is_confined("/p/.workflow_artifacts/MEMORY")
    assert not fsops.is_confined("/p/.workflow_artifacts/Memory/sub")
    assert not fsops.is_confined("/p/.Workflow_Artifacts/memory")
    assert not fsops.is_confined("/p/.WORKFLOW_ARTIFACTS")
    assert not fsops.is_confined("/p/plain")
    assert fsops.is_confined("/p/.workflow_artifacts/task/scratch")


def test_rm_recursive_accepted_inside_artifacts(fsops, root):
    d = root / ".workflow_artifacts" / "task" / "scratch"
    _write(d / "f")
    assert fsops.main(["rm", "-r", str(d)]) == 0
    assert not d.exists()


def test_rm_symlink_to_directory_removes_only_the_link(fsops, root):
    target = root / "tdir"
    _write(target / "f")
    link = root / "link"
    os.symlink(str(target), str(link))
    assert fsops.main(["rm", str(link)]) == 0
    assert not os.path.lexists(str(link)) and (target / "f").exists()
    os.symlink(str(target), str(link))
    assert fsops.main(["rm", "-r", str(link)]) == 0
    assert not os.path.lexists(str(link)) and (target / "f").exists()


def test_rm_recursive_with_trailing_slash_on_link_keeps_target(fsops, root):
    target = root / "tdir"
    _write(target / "f")
    link = root / "link"
    os.symlink(str(target), str(link))
    assert fsops.main(["rm", "-r", str(link) + "/"]) == 0
    assert not os.path.lexists(str(link)) and (target / "f").exists()


def test_rm_recursive_link_inside_artifacts_pointing_outside(fsops, root):
    outside = root / "outside"
    _write(outside / "f")
    link = root / ".workflow_artifacts" / "task" / "out"
    link.parent.mkdir(parents=True)
    os.symlink(str(outside), str(link))
    assert fsops.main(["rm", "-r", str(link)]) == 0
    assert not os.path.lexists(str(link)) and (outside / "f").exists()


def test_rm_mixed_call_handles_what_it_can_and_reports_worst_code(fsops, root):
    f = _write(root / "f")
    guarded = root / "plain-dir"
    _write(guarded / "g")
    assert fsops.main(["rm", "-r", str(root / "missing"), str(f), str(guarded)]) == 3
    assert not f.exists() and (guarded / "g").exists()


# ---------------------------------------------------------------- write-atomic


def test_write_atomic_writes_stdin_and_replaces_existing(root):
    dst = _write(root / "out.txt", "old")
    r = _run(["write-atomic", dst], stdin_text="fresh\n")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert dst.read_text() == "fresh\n"
    assert not (root / "out.txt.tmp").exists()


def test_write_atomic_parents_creates_parent(root):
    dst = root / "a" / "b" / "out.txt"
    r = _run(["write-atomic", "--parents", dst], stdin_text="v")
    assert r.returncode == 0 and dst.read_text() == "v"


def test_write_atomic_onto_directory_fails_and_leaves_no_tmp(root):
    dst = root / "adir"
    dst.mkdir()
    r = _run(["write-atomic", dst], stdin_text="v")
    assert r.returncode == 1
    assert len(r.stderr.strip().splitlines()) == 1 and r.stderr.startswith("fsops write-atomic: ")
    assert not (root / "adir.tmp").exists()


def test_write_atomic_refuses_a_terminal_stdin(fsops, root, monkeypatch, capsys):
    dst = root / "out.txt"
    monkeypatch.setattr(fsops.sys.stdin, "isatty", lambda: True, raising=False)
    assert fsops.main(["write-atomic", str(dst)]) == 2
    assert not dst.exists() and not (root / "out.txt.tmp").exists()
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


# ---------------------------------------------------------------- trash


def _today_dirs(base: Path):
    now = datetime.datetime.now(datetime.timezone.utc)
    days = [now, now - datetime.timedelta(days=1)]
    return [base / "trash" / d.strftime("%Y-%m-%d") for d in days]


def _trashed(base: Path, name: str):
    return [d / name for d in _today_dirs(base) if os.path.lexists(str(d / name))]


def test_trash_moves_file_and_numbers_collisions(fsops, root):
    base = root / "base"
    for i in range(3):
        f = _write(root / "name", "v{}".format(i))
        assert fsops.main(["trash", "--base", str(base), str(f)]) == 0
    day_dir = _trashed(base, "name")[0].parent
    assert sorted(p.name for p in day_dir.iterdir()) == ["name", "name-1", "name-2"]


def test_trash_accepts_directory_and_symlink_and_continues_past_missing(fsops, root, capsys):
    base = root / "base"
    d = root / "adir"
    _write(d / "f")
    target = _write(root / "t")
    link = root / "alink"
    os.symlink(str(target), str(link))
    code = fsops.main(["trash", "--base", str(base), str(root / "missing"), str(d), str(link)])
    assert code == 1
    assert _trashed(base, "adir") and os.path.islink(str(_trashed(base, "alink")[0]))
    assert target.exists()
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_trash_without_base_is_a_usage_error(root):
    r = _run(["trash", root / "x"])
    assert r.returncode == 2 and len(r.stderr.strip().splitlines()) == 1


# ---------------------------------------------------------------- finalize


def test_finalize_success_replaces_destination_and_removes_cleanup_paths(fsops, root):
    dst = _write(root / "plan.md", "old")
    tmp = _write(root / "plan.md.tmp", "new")
    body = _write(root / "plan.md.body.tmp", "b")
    assert fsops.main(["finalize", str(tmp), str(dst), "--cleanup", str(body), str(tmp)]) == 0
    assert dst.read_text() == "new"
    assert not tmp.exists() and not body.exists()


def test_finalize_missing_source_still_cleans_up_with_one_error_line(fsops, root, capsys):
    dst = _write(root / "plan.md", "old")
    tmp = root / "plan.md.tmp"
    body = _write(root / "plan.md.body.tmp", "b")
    assert fsops.main(["finalize", str(tmp), str(dst), "--cleanup", str(body), str(tmp)]) == 1
    assert dst.read_text() == "old" and not body.exists()
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and err.startswith("fsops finalize: ")


# ---------------------------------------------------------------- contract


@pytest.mark.parametrize("sub", ["mv", "rm", "write-atomic", "trash", "finalize"])
def test_success_prints_nothing_on_stdout(root, sub):
    f = _write(root / "f.txt", "x")
    if sub == "mv":
        args = ["mv", f, root / "g.txt"]
    elif sub == "rm":
        args = ["rm", f]
    elif sub == "write-atomic":
        args = ["write-atomic", root / "w.txt"]
    elif sub == "trash":
        args = ["trash", "--base", root / "base", f]
    else:
        args = ["finalize", f, root / "g.txt", "--cleanup", root / "nothing"]
    r = _run(args, stdin_text="v")
    assert (r.returncode, r.stdout) == (0, "")


@pytest.mark.parametrize(
    "args",
    [["frobnicate"], [], ["mv", "only-one"], ["rm"], ["finalize", "a", "b"], ["trash", "--base"]],
)
def test_usage_errors_exit_2_with_a_single_line(args):
    r = _run(args)
    assert r.returncode == 2
    assert r.stdout == ""
    lines = r.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("fsops")


def test_help_exits_zero():
    r = _run(["--help"])
    assert r.returncode == 0 and "write-atomic" in r.stdout


# ---------------------------------------------------------------- Python 3.8 floor

_DENYLIST = (
    ".removeprefix(",
    ".removesuffix(",
    ".is_relative_to(",
    ".readlink(",
    "BooleanOptionalAction",
    "functools.cache(",
    "import zoneinfo",
    "utcnow(",
)


@pytest.mark.parametrize("path", [CORE_PATH, WRAPPER_PATH])
def test_sources_parse_as_python_38(path):
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))


def test_core_has_future_annotations_and_no_newer_apis():
    text = CORE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    for needle in _DENYLIST:
        assert needle not in text, needle
    assert not any(isinstance(n, getattr(ast, "Match", ())) for n in ast.walk(tree))


def test_core_imports_only_the_standard_library():
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        pytest.skip("sys.stdlib_module_names needs Python 3.10+")
    for path in (CORE_PATH, WRAPPER_PATH):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert name in stdlib, "{} imports non-stdlib {}".format(path.name, name)


def _find_python38():
    candidates = [
        shutil.which("python3.8"),
        shutil.which("python3"),
        "/usr/bin/python3",
        os.path.expanduser("~/.pyenv/shims/python3.8"),
    ] + sorted(glob.glob(os.path.expanduser("~/.pyenv/versions/3.8*/bin/python3.8")))
    probe = "import sys; print(sys.version_info[0], sys.version_info[1]); print(sys.executable)"
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            out = subprocess.run([cand, "-c", probe], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = out.stdout.split("\n")
        if out.returncode == 0 and lines[0].strip() == "3 8" and len(lines) > 1 and lines[1].strip():
            return lines[1].strip()
    return None


def test_every_subcommand_runs_under_a_real_python_38(tmp_path):
    exe = _find_python38()
    if exe is None:
        pytest.skip("no Python 3.8 interpreter found")
    work = tmp_path / "My Drive"
    work.mkdir()
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}

    def run38(args, stdin_text=""):
        return subprocess.run(
            [exe, str(WRAPPER_PATH)] + [str(a) for a in args],
            input=stdin_text, capture_output=True, text=True, env=env, timeout=60,
        )

    assert run38(["--help"]).returncode == 0
    a = _write(work / "a.txt", "a")
    assert run38(["mv", "--parents", a, work / "sub" / "b.txt"]).returncode == 0
    assert (work / "sub" / "b.txt").read_text() == "a"
    assert run38(["write-atomic", work / "w.txt"], "hello").returncode == 0
    assert (work / "w.txt").read_text() == "hello"
    t = _write(work / "t.txt", "t")
    assert run38(["trash", "--base", work / "base", t]).returncode == 0
    assert not t.exists()
    tmpf = _write(work / "p.md.tmp", "new")
    assert run38(["finalize", tmpf, work / "p.md", "--cleanup", work / "p.md.body.tmp", tmpf]).returncode == 0
    assert (work / "p.md").read_text() == "new"
    d = work / ".workflow_artifacts" / "task"
    _write(d / "f")
    assert run38(["rm", "-r", d]).returncode == 0
    assert not d.exists()
    assert run38(["rm", "-r", work / ".workflow_artifacts"]).returncode == 3


# ---------------------------------------------------------------- registry


def test_installer_registers_fsops_in_both_script_tuples():
    spec = importlib.util.spec_from_file_location("installer_under_test", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "fsops.py" in mod.DEPLOYED_SCRIPTS
    assert "fsops.py" in mod.CORE_SCRIPTS
