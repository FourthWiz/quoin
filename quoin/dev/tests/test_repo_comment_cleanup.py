"""Tests for repo_comment_cleanup: list, apply and the removal-only verify proof.

Every fixture is a real git repository built in tmp_path, with files written as
bytes so line endings and encodings are exact. Sample comments that look like
process residue live in string literals only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE = REPO_ROOT / "quoin" / "core" / "scripts" / "repo_comment_cleanup.py"
WRAPPER = REPO_ROOT / "quoin" / "scripts" / "repo_comment_cleanup.py"


def _load_core():
    sys.path.insert(0, str(CORE.parent))
    spec = importlib.util.spec_from_file_location("_test_repo_comment_cleanup_core", CORE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def core():
    return _load_core()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def make_repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "tester")
    git(repo, "config", "user.email", "tester@example.com")
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def cli(*args: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(WRAPPER), *args], capture_output=True, text=True, cwd=cwd
    )


def list_records(repo: Path):
    res = cli("list", str(repo))
    assert res.returncode == 0, res.stderr
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()], res


def apply(repo: Path, deletions):
    f = repo.parent / "deletions.json"
    f.write_text(json.dumps(deletions))
    return cli("apply", str(repo), "--deletions", str(f))


def verify(repo: Path, *extra: str):
    return cli("verify", str(repo), "--base", "HEAD", *extra)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def test_list_fields_blocks_and_kinds(tmp_path):
    src = (
        b"# one\n# two\nx = 1  # trail\n    # indented\ny = 2\n"
        b"def f():\n    # deep\n    return 1\n"
    )
    repo = make_repo(tmp_path, {"m.py": src})
    recs, res = list_records(repo)
    assert res.returncode == 0
    for r in recs:
        assert {"file", "start", "end", "kind", "text", "protected", "looks_like_code",
                "next_code", "residue"} <= set(r)
    by_start = {r["start"]: r for r in recs}
    assert by_start[1]["end"] == 2 and by_start[1]["kind"] == "full"
    assert by_start[3]["kind"] == "trailing"
    assert by_start[4]["kind"] == "full" and by_start[4]["end"] == 4
    assert by_start[7]["start"] == 7 and by_start[7]["end"] == 7


def test_list_different_columns_split(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"if x:\n    # a\n# b\n    y = 1\n"})
    recs, _ = list_records(repo)
    assert [(r["start"], r["end"]) for r in recs] == [(2, 2), (3, 3)]


def test_list_excludes_untracked_and_default_excludes(tmp_path):
    files = {
        "a.py": b"# keep listed\n",
        "mutants/x.py": b"# no\n",
        ".venv/x.py": b"# no\n",
        "build/x.py": b"# no\n",
        "sub/.venv/y.py": b"# no\n",
    }
    repo = make_repo(tmp_path, files)
    (repo / "untracked.py").write_bytes(b"# untracked\n")
    recs, _ = list_records(repo)
    assert {r["file"] for r in recs} == {"a.py"}


def test_list_path_with_space(tmp_path):
    repo = make_repo(tmp_path, {"dir with space/m x.py": b"# c\n"})
    recs, _ = list_records(repo)
    assert [r["file"] for r in recs] == ["dir with space/m x.py"]


def test_list_unparseable_file_skipped(tmp_path):
    repo = make_repo(tmp_path, {"bad.py": b"def (:\n  # c\n", "ok.py": b"# fine\n"})
    recs, res = list_records(repo)
    assert "SKIP bad.py: " in res.stderr
    assert {r["file"] for r in recs} == {"ok.py"}


# --------------------------------------------------------------------------
# line splitting
# --------------------------------------------------------------------------

def test_split_lines_only_python_newlines(core):
    text = "a\r\nb\rc\nd\x0ce\u2028f\x85g"
    assert core.split_lines(text) == ["a\r\n", "b\r", "c\n", "d\x0ce\u2028f\x85g"]


def test_lone_cr_file(tmp_path):
    src = b"x = 1\r# drop me\ry = 2\r"
    repo = make_repo(tmp_path, {"m.py": src})
    recs, _ = list_records(repo)
    assert [(r["start"], r["kind"]) for r in recs] == [(2, "full")]
    assert apply(repo, [{"file": "m.py", "lines": [2]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == b"x = 1\ry = 2\r"
    res = verify(repo)
    assert res.returncode == 0, res.stdout


def test_form_feed_line_above_comment(tmp_path):
    src = b"x = 1\n\x0c\n# drop me\ny = 2\n"
    repo = make_repo(tmp_path, {"m.py": src})
    recs, _ = list_records(repo)
    assert len(recs) == 1
    row = recs[0]["start"]
    assert apply(repo, [{"file": "m.py", "lines": [row]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == b"x = 1\n\x0c\ny = 2\n"
    assert verify(repo).returncode == 0


def test_u2028_in_string_before_trailing_comment(tmp_path):
    src = 's = "a\u2028b"\nt = 2  # note\nu = 3\n'.encode("utf-8")
    repo = make_repo(tmp_path, {"m.py": src})
    recs, _ = list_records(repo)
    assert [r["start"] for r in recs] == [2]
    assert apply(repo, [{"file": "m.py", "lines": [2]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == 's = "a\u2028b"\nt = 2\nu = 3\n'.encode("utf-8")
    assert verify(repo).returncode == 0


# --------------------------------------------------------------------------
# protected categories
# --------------------------------------------------------------------------

PROTECTED_SAMPLES = [
    "# noqa: E501",
    "# type: ignore[attr]",
    "# pragma: no cover",
    "# NOSONAR",
    "# nosec B101",
    "# pylint: disable=foo",
    "# ruff: noqa",
    "# fmt: off",
    "# isort: skip",
    "# mypy: ignore-errors",
    "# pyright: basic",
    "# nolint",
    "# vim: set ts=4",
    "# type: int",
    "# flake8: noqa",
    "# pytype: disable=x",
    "# yapf: disable",
    "# skipcq: PYL-W0612",
    "# noinspection PyUnresolvedReferences",
    "# sourcery skip: foo",
    "#!/usr/bin/env python3",
    "# -*- coding: utf-8 -*-",
    "# Copyright 2020 Acme",
    "# Licensed under MIT",
    "# SPDX-License-Identifier: MIT",
    "# TODO fix KEY-123",
    "# TODO fix #123",
    "# TODO see https://x.example/z",
    "# see https://example.com/a",
]


@pytest.mark.parametrize("comment", PROTECTED_SAMPLES)
def test_protected_comment_refused_and_verify_fails(tmp_path, comment):
    src = f"x = 1\n{comment}\ny = 2\n".encode("utf-8")
    repo = make_repo(tmp_path, {"m.py": src})
    recs, _ = list_records(repo)
    assert recs[0]["protected"], comment
    res = apply(repo, [{"file": "m.py", "lines": [2]}])
    assert res.returncode != 0
    assert "protected" in res.stderr
    assert (repo / "m.py").read_bytes() == src
    (repo / "m.py").write_bytes(b"x = 1\ny = 2\n")
    res = verify(repo)
    assert res.returncode == 1
    assert "protected" in res.stdout


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def test_apply_full_line_delete_and_trailing_strip(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"# gone\nx = 1  # strip me\ny = 2\n"})
    res = apply(repo, [{"file": "m.py", "lines": [1, 2]}])
    assert res.returncode == 0
    assert "applied 2 comment deletions in 1 files; 0 files refused" in res.stdout
    assert (repo / "m.py").read_bytes() == b"x = 1\ny = 2\n"


def test_apply_blank_layout_keeps_longer_run(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"a = 1\n\n\n# c\n\nb = 2\n"})
    assert apply(repo, [{"file": "m.py", "lines": [4]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == b"a = 1\n\n\nb = 2\n"


def test_apply_block_at_file_start_drops_following_blanks(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"# c\n\n\nx = 1\n"})
    assert apply(repo, [{"file": "m.py", "lines": [1]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == b"x = 1\n"


def test_apply_line_without_comment_refused(tmp_path):
    src = b"x = 1\ny = 2\n"
    repo = make_repo(tmp_path, {"m.py": src})
    res = apply(repo, [{"file": "m.py", "lines": [1]}])
    assert res.returncode != 0
    assert "no comment on lines" in res.stderr
    assert (repo / "m.py").read_bytes() == src


def test_apply_crlf_preserved(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"# c\r\nx = 1  # t\r\ny = 2\r\n"})
    assert apply(repo, [{"file": "m.py", "lines": [1, 2]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == b"x = 1\r\ny = 2\r\n"
    assert verify(repo).returncode == 0


def test_apply_latin1_roundtrip(tmp_path):
    src = "# -*- coding: latin-1 -*-\ns = 'caf\xe9'  # drop\n".encode("latin-1")
    repo = make_repo(tmp_path, {"m.py": src})
    assert apply(repo, [{"file": "m.py", "lines": [2]}]).returncode == 0
    assert (repo / "m.py").read_bytes() == "# -*- coding: latin-1 -*-\ns = 'caf\xe9'\n".encode("latin-1")
    assert verify(repo).returncode == 0


def test_apply_refused_file_untouched_others_applied(tmp_path):
    bad = b"x = 1\n# noqa\n"
    repo = make_repo(tmp_path, {"bad.py": bad, "good.py": b"# drop\ny = 1\n"})
    res = apply(repo, [{"file": "bad.py", "lines": [2]}, {"file": "good.py", "lines": [1]}])
    assert res.returncode != 0
    assert "1 files refused" in res.stdout
    assert (repo / "bad.py").read_bytes() == bad
    assert (repo / "good.py").read_bytes() == b"y = 1\n"


# --------------------------------------------------------------------------
# verify passes
# --------------------------------------------------------------------------

def test_verify_passes_comment_only_removal(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"# a\nx = 1  # b\n\n# c\ny = 2\n"})
    (repo / "m.py").write_bytes(b"x = 1\n\ny = 2\n")
    res = verify(repo)
    assert res.returncode == 0, res.stdout
    assert "verify: 1 files checked, 3 comments removed, 0 failures" in res.stdout


def test_verify_passes_mixed_delete_then_strip(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"# a\nx = 1  # b\n"})
    (repo / "m.py").write_bytes(b"x = 1\n")
    assert verify(repo).returncode == 0


def test_verify_passes_non_ascii_before_comment(tmp_path):
    src = 's = "h\u00e9llo \u4e16\u754c"  # note\n'.encode("utf-8")
    repo = make_repo(tmp_path, {"m.py": src})
    (repo / "m.py").write_bytes('s = "h\u00e9llo \u4e16\u754c"\n'.encode("utf-8"))
    assert verify(repo).returncode == 0


def test_verify_ignores_mutation_work_paths(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1\n"})
    (repo / "mutants").mkdir()
    (repo / "mutants" / "a.py").write_bytes(b"x = 2\n")
    (repo / ".mutmut-cache").write_bytes(b"db")
    res = verify(repo)
    assert res.returncode == 0, res.stdout
    assert "IGNORED 2 mutation work paths" in res.stdout


def test_verify_large_file_one_strip_one_drop(tmp_path):
    lines = [f"x{i} = {i}\n" for i in range(5000)]
    old = lines[:]
    old[1000] = "x1000 = 1000  # trailing\n"
    old.insert(3000, "# whole line\n")
    repo = make_repo(tmp_path, {"big.py": "".join(old).encode()})
    (repo / "big.py").write_bytes("".join(lines).encode())
    res = verify(repo)
    assert res.returncode == 0, res.stdout


# --------------------------------------------------------------------------
# verify fails
# --------------------------------------------------------------------------

def _fail(repo: Path, expect: str, *extra: str):
    res = verify(repo, *extra)
    assert res.returncode == 1, res.stdout
    assert expect in res.stdout, res.stdout
    return res


def test_verify_fails_ast_change(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1  # c\n"})
    (repo / "m.py").write_bytes(b"x = 2\n")
    _fail(repo, "AST differs")


@pytest.mark.parametrize("new", [b"x = 1\n", b"s = 'a'\n"])
def test_verify_fails_token_change_with_same_ast(tmp_path, new):
    old = b"x = (1)  # c\n" if new == b"x = 1\n" else b's = "a"  # c\n'
    repo = make_repo(tmp_path, {"m.py": old})
    (repo / "m.py").write_bytes(new)
    _fail(repo, "code token stream differs")


def test_verify_fails_whitespace_reformat_as_alignment(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x=1  # c\n"})
    (repo / "m.py").write_bytes(b"x = 1\n")
    res = _fail(repo, "line alignment")
    assert "token" not in res.stdout


def test_verify_fails_comment_added(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1\n"})
    (repo / "m.py").write_bytes(b"x = 1\n# new\n")
    _fail(repo, "comments added or edited")


def test_verify_fails_comment_edited(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1  # old\n"})
    (repo / "m.py").write_bytes(b"x = 1  # new\n")
    _fail(repo, "comments added or edited")


def test_verify_fails_protected_removed(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1  # noqa\n"})
    (repo / "m.py").write_bytes(b"x = 1\n")
    _fail(repo, "protected comments removed")


def test_verify_fails_file_added_deleted_renamed(tmp_path):
    repo = make_repo(tmp_path, {"a.py": b"x = 1\n", "b.py": b"y = 1\n", "c.py": b"z = 1\n"})
    (repo / "new.py").write_bytes(b"w = 1\n")
    git(repo, "add", "new.py")
    (repo / "a.py").unlink()
    git(repo, "mv", "c.py", "d.py")
    res = verify(repo)
    assert res.returncode == 1
    assert "new.py: file A (only modifications allowed)" in res.stdout
    assert "a.py: file D (only modifications allowed)" in res.stdout
    assert "file R100 (only modifications allowed)" in res.stdout


def test_verify_fails_untracked_new_py(tmp_path):
    repo = make_repo(tmp_path, {"a.py": b"x = 1\n"})
    (repo / "extra.py").write_bytes(b"y = 1\n")
    _fail(repo, "extra.py: file A (only modifications allowed)")


def test_verify_non_py_needs_flag(tmp_path):
    repo = make_repo(tmp_path, {"a.py": b"x = 1\n", "notes.txt": b"hi\n"})
    (repo / "notes.txt").write_bytes(b"changed\n")
    _fail(repo, "non-Python file changed")
    assert verify(repo, "--allow-non-py").returncode == 0


def test_verify_fails_crlf_rewritten_to_lf(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1\r\ny = 2\r\n"})
    (repo / "m.py").write_bytes(b"x = 1\ny = 2\n")
    _fail(repo, "code token stream differs")


def test_verify_fails_blank_line_ending_rewritten(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1\r\n\r\ny = 2\r\n"})
    (repo / "m.py").write_bytes(b"x = 1\r\n\ny = 2\r\n")
    _fail(repo, "line alignment")


@pytest.mark.parametrize("old,new", [(b"x = 1\n", b"x = 1 \n"), (b"x = 1 \n", b"x = 1\n")])
def test_verify_fails_trailing_whitespace_change(tmp_path, old, new):
    repo = make_repo(tmp_path, {"m.py": old})
    (repo / "m.py").write_bytes(new)
    _fail(repo, "line alignment")


@pytest.mark.parametrize("add_bom", [True, False])
def test_verify_fails_bom_change(tmp_path, add_bom):
    plain, bom = b"x = 1\n", b"\xef\xbb\xbfx = 1\n"
    repo = make_repo(tmp_path, {"m.py": plain if add_bom else bom})
    (repo / "m.py").write_bytes(bom if add_bom else plain)
    _fail(repo, "encoding or BOM changed")


def test_verify_fails_mode_change(tmp_path):
    repo = make_repo(tmp_path, {"m.py": b"x = 1\n"})
    os.chmod(repo / "m.py", 0o755)
    _fail(repo, "mode change")


# --------------------------------------------------------------------------
# parses_as_code
# --------------------------------------------------------------------------

def test_parses_as_code_cases(core):
    assert core.parses_as_code("# if x:") is True
    assert core.parses_as_code("# x = 1") is True
    assert core.parses_as_code("# Note: see below:") is False
    assert core.parses_as_code("# just some prose here") is False


def test_parses_as_code_regression_bites(core):
    def rejects_every_colon_line(text: str) -> bool:
        body = text.lstrip("#").strip()
        if not body or body.endswith(":"):
            return False
        return True

    assert rejects_every_colon_line("# if x:") is False
    assert core.parses_as_code("# if x:") is True
