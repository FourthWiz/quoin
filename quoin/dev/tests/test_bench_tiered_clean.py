"""Offline tests of `quoin bench tiered --clean` against a seeded tree under tmp_path.

HOME, the temp directory, the system temp directory and the mirror cache all point
inside tmp_path, and starting a process raises, so nothing outside the test tree
can be listed, changed or deleted.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import quoin
from quoin import cli
from quoin.benchmarks.harness import paired_spend
from quoin.benchmarks.scripts import tiered_clean, tiered_entry


class Tty(io.StringIO):
    def isatty(self):
        return True


class NoRead(Tty):
    def readline(self, *a):
        raise AssertionError("the prompt must not be shown")

    read = readline


class CT:
    pass


def _boom(*a, **k):
    raise AssertionError("clean must not start a process")


def _restore_modes(root: Path) -> None:
    for dirpath, dirnames, _files in os.walk(root, onerror=lambda e: None):
        for name in dirnames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass


def _make_unreadable_tree(run_a: Path) -> None:
    (run_a / "qbench_sandbox_plain_b" / "home").mkdir(parents=True)
    (run_a / "qbench_sandbox_plain_b" / "home" / "file").write_text("x")
    (run_a / "locked").mkdir()
    os.chmod(run_a / "locked", 0o000)


def _bare(path: Path) -> Path:
    """A directory shaped like a bare repository (a HEAD file and objects/)."""
    (path / "objects").mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _decoys(where: Path) -> list:
    """Entries a mirror cleanup must never delete, seeded into `where`."""
    git_dir = where / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "HEAD").write_text("x")
    wt = where / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    return [git_dir, wt / ".git", _bare(where / "dotfiles.git"), _bare(where / "plain.git")]


@pytest.fixture
def ct(tmp_path, monkeypatch):
    c = CT()
    c.tmp_path = tmp_path
    c.home = tmp_path / "home"
    (c.home / ".claude").mkdir(parents=True)
    (c.home / ".claude" / "settings.json").write_text("{}")
    (c.home / ".config" / "quoin").mkdir(parents=True)
    (c.home / ".config" / "quoin" / "models.json").write_text("{}")
    monkeypatch.setenv("HOME", str(c.home))
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    c.checkout = tmp_path / "checkout"
    c.checkout.mkdir()

    c.runs = tmp_path / "runs"
    run = c.runs / "R"
    (run / "simple-claude" / "t1").mkdir(parents=True)
    (run / "simple-claude" / "t1" / "metrics.json").write_text("{}")
    for name in ("run-manifest.yaml", "summary.md", "summary.json"):
        (run / name).write_text("x")
    (c.runs / "M").mkdir()
    (c.runs / "M" / "run-manifest.yaml").write_text("x")
    (c.runs / ".superseded" / "R" / "quoin-claude" / "t1").mkdir(parents=True)
    c.ledger = c.runs / "R.spend-ledger.jsonl"
    attempt = paired_spend.reserve(c.ledger, "R", "t1", "simple-claude", 5.5)
    paired_spend.settle(c.ledger, attempt, "R", "t1", "simple-claude", 5.5, 2.5)
    (c.runs / "OTHER").mkdir()
    (c.runs / "OTHER" / "run-manifest.yaml").write_text("x")
    (c.runs / ".superseded" / "OTHER").mkdir(parents=True)
    (c.runs / "OTHER.spend-ledger.jsonl").write_text("")

    c.tmp = tmp_path / "tmp"
    c.tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(c.tmp))
    for name in ("qbc_a", "qbench_verified_a", "qbench_capture_a", "qbench_home_a", "qbench_t1_x",
                 "unrelated", "qbenchx"):
        (c.tmp / name).mkdir()
    (c.tmp / "quoin-benchmarks" / "wt").mkdir(parents=True)
    (c.tmp / "qbench_note.txt").write_text("x")
    (c.tmp / "qbench_link").symlink_to(c.home / ".claude")
    c.run_a = c.tmp / "qbench_run_a"
    c.run_a.mkdir()
    _make_unreadable_tree(c.run_a)

    c.systmp = tmp_path / "systmp"
    (c.systmp / "qbc_b").mkdir(parents=True)
    (c.systmp / "keep").mkdir()
    monkeypatch.setattr(tiered_clean, "SYSTEM_TMP", str(c.systmp))

    c.mirrors = tmp_path / "mirrors"
    c.mirrors.mkdir()
    (c.mirrors / "repo").write_text("x")
    _bare(c.mirrors / "owner__repo.git")
    (c.mirrors / "owner__other.git.partial-123").mkdir()
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(c.mirrors))

    # Traverse-only parent last, so the seeding above could still write into it.
    os.chmod(c.run_a, 0o100)
    yield c
    _restore_modes(tmp_path)


@pytest.fixture
def cli_path(monkeypatch):
    monkeypatch.setattr(quoin, "__path__", list(quoin.__path__))


def _clean(c, *extra, **over):
    args = {"--run-id": "R", "--run-dir": str(c.runs), "--repo-root": str(c.checkout)}
    args.update(over)
    argv = []
    for key, value in args.items():
        argv += [key, value]
    return tiered_entry.main(argv + ["--clean", *extra])


def _tree(c):
    out = []
    for dirpath, dirnames, files in os.walk(c.tmp_path, onerror=lambda e: None):
        for name in sorted(dirnames + files):
            p = Path(dirpath) / name
            out.append((str(p.relative_to(c.tmp_path)), os.lstat(p).st_mode))
    return sorted(out)


def _mode(p: Path):
    try:
        return os.lstat(p).st_mode
    except OSError:
        return None


def _known(c):
    return [_mode(c.run_a / "qbench_sandbox_plain_b" / "home" / "file"), _mode(c.run_a / "locked")]


def _line(out: str, path) -> str:
    hits = [l for l in out.splitlines() if l.rstrip().endswith(str(path)) or f"{path} (" in l]
    assert len(hits) == 1, (path, out)
    return hits[0]


def _snapshot(c):
    return _tree(c), _known(c)


def _gone(p: Path) -> bool:
    return not os.path.lexists(p)


# --- the listing, the one question and the deletion ---------------------------

def test_yes_deletes_this_runs_leftovers_and_keeps_the_rest(ct, monkeypatch, capsys):
    c = ct
    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert _clean(c) == 0
    out = capsys.readouterr().out
    assert out.count("Delete ") == 1
    for p in (c.runs / "R", c.runs / ".superseded" / "R", c.ledger, c.run_a, c.tmp / "qbc_a",
              c.tmp / "qbench_verified_a", c.tmp / "qbench_capture_a", c.tmp / "qbench_home_a",
              c.tmp / "qbench_t1_x", c.tmp / "quoin-benchmarks", c.tmp / "qbench_link",
              c.systmp / "qbc_b"):
        assert _gone(p), p
    for p in (c.runs / "OTHER", c.runs / "M", c.runs / ".superseded" / "OTHER",
              c.runs / "OTHER.spend-ledger.jsonl", c.tmp / "unrelated", c.tmp / "qbenchx",
              c.tmp / "qbench_note.txt", c.systmp / "keep", c.home / ".claude" / "settings.json",
              c.home / ".config" / "quoin" / "models.json", c.mirrors / "repo"):
        assert os.path.lexists(p), p
    assert "git worktree prune" in out
    assert "keep    mirror cache:" in _line(out, c.mirrors)


def test_no_changes_neither_files_nor_modes(ct, monkeypatch, capsys):
    c = ct
    before = _snapshot(c)
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert _clean(c) == 1
    assert "declined" in capsys.readouterr().out
    assert _snapshot(c) == before
    assert stat.S_IMODE(os.lstat(c.run_a).st_mode) == 0o100
    assert stat.S_IMODE(_known(c)[1]) == 0o000


def test_a_run_holding_only_its_manifest_is_deleted(ct, capsys):
    c = ct
    assert _clean(c, "--yes", **{"--run-id": "M"}) == 0
    assert _line(capsys.readouterr().out, c.runs / "M").lstrip().startswith("delete")
    assert _gone(c.runs / "M") and os.path.lexists(c.runs / "R")


def test_mirrors_are_deleted_only_with_their_flag(ct, capsys):
    c = ct
    assert _clean(c, "--yes") == 0
    assert (c.mirrors / "owner__repo.git").is_dir()
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    out = capsys.readouterr().out
    assert _line(out, c.mirrors / "owner__repo.git").lstrip().startswith("delete")
    assert _gone(c.mirrors / "owner__repo.git") and _gone(c.mirrors / "owner__other.git.partial-123")


def test_clean_mirrors_leaves_other_files_and_the_cache_holding_them(ct, capsys):
    c = ct
    (c.mirrors / "notes").mkdir()
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    assert f"clean: kept {c.mirrors} (not empty)" in capsys.readouterr().out
    assert (c.mirrors / "repo").read_text() == "x" and (c.mirrors / "notes").is_dir()
    assert _gone(c.mirrors / "owner__repo.git")


def test_clean_mirrors_removes_the_cache_once_it_is_empty(ct):
    c = ct
    (c.mirrors / "repo").unlink()
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    assert _gone(c.mirrors)


def test_an_ordinary_directory_named_as_the_mirror_cache_is_untouched(ct, monkeypatch):
    c = ct
    precious = c.tmp_path / "precious"
    precious.mkdir()
    (precious / "thesis.tex").write_text("x")
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(precious))
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    assert (precious / "thesis.tex").read_text() == "x"


@pytest.mark.parametrize("where", ["home", "checkout", "cwd"])
def test_nothing_inside_a_protected_or_working_directory_cache_is_deleted(
        ct, monkeypatch, capsys, where):
    c = ct
    if where == "cwd":
        base = c.tmp_path / "project"
        base.mkdir()
        monkeypatch.chdir(base)
        cache = "."
    else:
        base = getattr(c, where)
        cache = str(base)
    seeded = [*_decoys(base), _bare(base / "owner__x.git"), base / "o__r.git.partial-7"]
    seeded[-1].mkdir()
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", cache)
    rc = _clean(c, "--yes", "--clean-mirrors")
    out = capsys.readouterr().out
    assert all(os.path.lexists(p) for p in seeded[:4]), out
    if where == "cwd":
        # An ordinary directory: its real mirrors go, nothing else does.
        assert rc == 0 and _gone(seeded[4]) and _gone(seeded[5])
        assert "delete  mirror:" in out
    else:
        assert all(os.path.lexists(p) for p in seeded), out
        assert "delete  mirror:" not in out
        assert "inside a skipped mirror cache (protected path)" in out


def test_only_mirror_shaped_directories_are_deleted_from_an_ordinary_cache(ct, capsys):
    c = ct
    decoys = _decoys(c.mirrors)
    (c.mirrors / "owner__file.git").write_text("x")
    (c.mirrors / "owner__empty.git").mkdir()
    (c.mirrors / "owner__link.git").symlink_to(c.mirrors / "owner__repo.git")
    (c.mirrors / "o__r.git.partial-x").mkdir()
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    out = capsys.readouterr().out
    assert _gone(c.mirrors / "owner__repo.git") and _gone(c.mirrors / "owner__other.git.partial-123")
    for p in decoys + [c.mirrors / n for n in ("owner__file.git", "owner__empty.git",
                                               "owner__link.git", "o__r.git.partial-x")]:
        assert os.path.lexists(p), (p, out)


def test_a_symlinked_mirror_cache_is_listed_as_kept(ct, monkeypatch, capsys):
    c = ct
    link = c.tmp_path / "mirrors-link"
    link.symlink_to(c.mirrors)
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(link))
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    line = _line(capsys.readouterr().out, link)
    assert line.lstrip().startswith("keep") and "symlink; its target is not cleaned" in line
    assert link.is_symlink() and (c.mirrors / "owner__repo.git").is_dir()


def test_a_tree_reaching_another_file_system_is_not_deleted(ct, monkeypatch, capsys):
    c = ct
    os.chmod(c.run_a, 0o700)
    mount = c.run_a / "mnt"
    (mount / "data").mkdir(parents=True)
    os.chmod(c.run_a, 0o100)
    real_lstat = os.lstat

    def fake_lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if os.fspath(path) == "mnt":
            fields = list(st)
            fields[stat.ST_DEV] = st.st_dev + 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(tiered_clean.os, "lstat", fake_lstat)
    assert _clean(c, "--yes") == 1
    assert "mnt is on another file system" in capsys.readouterr().out
    monkeypatch.setattr(tiered_clean.os, "lstat", real_lstat)
    os.chmod(c.run_a, 0o700)
    assert (mount / "data").is_dir()


def test_without_a_safe_chmod_a_locked_tree_is_left_as_is(ct, monkeypatch, capsys):
    c = ct
    monkeypatch.setattr(tiered_clean, "_CHMOD_NOFOLLOW", False)
    monkeypatch.delattr(tiered_clean.os, "O_PATH", raising=False)
    assert _clean(c, "--yes") == 1
    assert f"could not delete {c.run_a}" in capsys.readouterr().out
    assert stat.S_IMODE(os.lstat(c.run_a).st_mode) == 0o100


def test_no_terminal_without_yes_is_refused(ct, capsys):
    c = ct
    before = _snapshot(c)
    assert _clean(c) == 2
    assert "REFUSED: confirm:" in capsys.readouterr().err
    assert _snapshot(c) == before


# --- symlinks and protected paths ---------------------------------------------

def test_a_symlinked_temp_root_is_unlinked_not_followed(ct):
    c = ct
    assert _clean(c, "--yes") == 0
    assert _gone(c.tmp / "qbench_link")
    assert (c.home / ".claude" / "settings.json").is_file()


def test_a_symlinked_run_directory_is_unlinked_not_followed(ct):
    c = ct
    import shutil
    shutil.rmtree(c.runs / "R")
    (c.runs / "R").symlink_to(c.home / ".config")
    assert _clean(c, "--yes") == 0
    assert _gone(c.runs / "R")
    assert (c.home / ".config" / "quoin" / "models.json").is_file()


def _protected_case(c, capsys, path, reason, *extra, **over):
    assert _clean(c, "--yes", *extra, **over) == 0
    line = _line(capsys.readouterr().out, path)
    assert line.lstrip().startswith("skip") and f"({reason}" in line, line


def test_home_as_the_run_directory_is_protected(ct, capsys):
    c = ct
    _protected_case(c, capsys, c.home, "protected path",
                    **{"--run-dir": str(c.tmp_path), "--run-id": "home"})
    assert (c.home / ".claude" / "settings.json").is_file()


def test_the_repo_root_as_the_run_directory_is_protected(ct, capsys):
    c = ct
    _protected_case(c, capsys, c.checkout, "protected path",
                    **{"--run-dir": str(c.tmp_path), "--run-id": "checkout"})
    assert c.checkout.is_dir()


def test_a_run_inside_claude_config_is_protected(ct, capsys):
    c = ct
    target = c.home / ".claude" / "R"
    target.mkdir()
    (target / "run-manifest.yaml").write_text("x")
    _protected_case(c, capsys, target, "inside ~/.claude or ~/.config",
                    **{"--run-dir": str(c.home / ".claude")})
    assert (target / "run-manifest.yaml").is_file()


def test_an_ancestor_of_protected_paths_is_skipped(ct, capsys):
    c = ct
    (c.tmp_path / "run-manifest.yaml").write_text("x")
    _protected_case(c, capsys, c.tmp_path, "contains a protected path",
                    **{"--run-dir": str(c.tmp_path.parent), "--run-id": c.tmp_path.name})
    assert c.home.is_dir() and c.checkout.is_dir()


@pytest.mark.parametrize("which", ["home", "checkout", "root"])
def test_a_directory_is_never_an_explicit_ledger(ct, capsys, which):
    c = ct
    path = {"home": c.home, "checkout": c.checkout, "root": Path("/")}[which]
    _protected_case(c, capsys, path, "not a ledger file", "--clean-explicit-ledger",
                    **{"--spend-ledger": str(path)})
    assert path.is_dir() and (c.home / ".claude" / "settings.json").is_file()


def _case_insensitive(path: Path) -> bool:
    return os.path.exists(str(path.parent / path.name.swapcase()))


def test_a_case_variant_of_home_as_the_mirror_cache_is_protected(ct, monkeypatch, capsys):
    c = ct
    if not _case_insensitive(c.home):
        pytest.skip("the temp file system is case-sensitive")
    flipped = c.tmp_path / "HOME"
    seeded = [_bare(c.home / "x.git"), _bare(c.home / "owner__x.git"), *_decoys(c.home)]
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(flipped))
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    line = _line(capsys.readouterr().out, flipped)
    assert line.lstrip().startswith("skip") and "(protected path" in line
    assert (c.home / ".claude" / "settings.json").is_file()
    assert all(os.path.lexists(p) for p in seeded)


def test_a_case_variant_of_the_checkout_as_the_run_directory_is_protected(ct, capsys):
    c = ct
    if not _case_insensitive(c.checkout):
        pytest.skip("the temp file system is case-sensitive")
    (c.checkout / "run-manifest.yaml").write_text("x")
    _protected_case(c, capsys, c.tmp_path / "CHECKOUT", "protected path",
                    **{"--run-dir": str(c.tmp_path), "--run-id": "CHECKOUT"})
    assert (c.checkout / "run-manifest.yaml").is_file()


@pytest.mark.parametrize("which", ["home", "checkout"])
def test_another_spelling_of_a_protected_path_is_recognised(ct, which):
    # A `..` detour keeps the string different on any file system; only the
    # file identity says it is the same directory.
    c = ct
    target = {"home": c.home, "checkout": c.checkout}[which]
    other = "checkout" if which == "home" else "home"
    spelled = c.tmp_path / other / ".." / target.name
    assert str(spelled) != str(target)
    guard = tiered_clean.guard_for(tiered_clean.protected_paths(c.checkout), [],
                                   tiered_clean._config_trees())
    cand = tiered_clean.Candidate(spelled, "temp root", c.tmp_path, False)
    assert tiered_clean.check(cand, guard) == "protected path"
    holder = tiered_clean.Candidate(c.tmp_path / other / ".." / ".." / c.tmp_path.name,
                                    "temp root", c.tmp_path.parent, False)
    assert tiered_clean.check(holder, guard) == "contains a protected path"


def test_another_spelling_inside_claude_config_is_recognised(ct):
    c = ct
    (c.home / ".claude" / "qbench_x").mkdir()
    spelled = c.home / ".config" / ".." / ".claude" / "qbench_x"
    guard = tiered_clean.guard_for(tiered_clean.protected_paths(c.checkout), [],
                                   tiered_clean._config_trees())
    cand = tiered_clean.Candidate(spelled, "temp root", c.home / ".claude", False)
    assert tiered_clean.check(cand, guard) == "inside ~/.claude or ~/.config"


def test_a_temp_directory_inside_home_is_not_scanned(ct, monkeypatch, capsys):
    c = ct
    (c.home / "qbc_mine").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(c.home))
    assert _clean(c, "--yes") == 0
    assert (c.home / "qbc_mine").is_dir()
    assert str(c.home / "qbc_mine") not in capsys.readouterr().out


def test_a_candidate_that_became_protected_after_the_listing_is_skipped(ct, monkeypatch, capsys):
    c = ct
    calls = []
    real_check = tiered_clean.check

    def check_then_protect(cand, guard):
        calls.append(cand.path)
        if cand.path == c.ledger and calls.count(c.ledger) == 2:
            guard.protected.add(tiered_clean._ident(c.ledger, follow=False))
        return real_check(cand, guard)

    monkeypatch.setattr(tiered_clean, "check", check_then_protect)
    assert _clean(c, "--yes") == 0
    assert f"clean: skipped {c.ledger}: protected path" in capsys.readouterr().out
    assert c.ledger.is_file()


def test_protected_paths_hold_the_homes_root_and_checkouts(ct):
    import pwd
    c = ct
    got = tiered_clean.protected_paths(c.checkout)
    real = lambda p: Path(os.path.realpath(p))  # noqa: E731
    for p in (pwd.getpwuid(os.getuid()).pw_dir, Path.home(), "/", c.checkout,
              tiered_entry.CHECKOUT_ROOT):
        assert real(p) in got, p


# --- ledgers ------------------------------------------------------------------------

def test_an_unrelated_directory_given_as_ledger_is_untouched(ct, capsys):
    c = ct
    work = c.tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.txt").write_text("x")
    before = sorted((str(p), os.lstat(p).st_mode) for p in work.rglob("*"))
    assert _clean(c, "--yes", "--clean-explicit-ledger", **{"--spend-ledger": str(work)}) == 0
    out = capsys.readouterr().out
    line = _line(out, work)
    assert line.lstrip().startswith("skip") and "(not a ledger file" in line
    assert "could not delete" not in out
    assert sorted((str(p), os.lstat(p).st_mode) for p in work.rglob("*")) == before


def _linked_ledger(c):
    real = c.tmp_path / "elsewhere" / "real.jsonl"
    real.parent.mkdir()
    attempt = paired_spend.reserve(real, "R", "t1", "simple-claude", 5.5)
    paired_spend.settle(real, attempt, "R", "t1", "simple-claude", 5.5, 1.0)
    link = c.tmp_path / "link.jsonl"
    link.symlink_to(real)
    return link, real


def test_a_symlinked_explicit_ledger_is_unlinked_with_the_flag(ct):
    c = ct
    link, real = _linked_ledger(c)
    assert _clean(c, "--yes", "--clean-explicit-ledger", **{"--spend-ledger": str(link)}) == 0
    assert _gone(link) and real.is_file()


def test_a_symlinked_explicit_ledger_is_kept_without_the_flag(ct, capsys):
    c = ct
    link, real = _linked_ledger(c)
    assert _clean(c, "--yes", **{"--spend-ledger": str(link)}) == 0
    line = _line(capsys.readouterr().out, link)
    assert line.lstrip().startswith("keep") and str(real) not in line
    assert link.is_symlink() and real.is_file()


@pytest.mark.parametrize("where", ["run", "superseded"])
def test_a_kept_ledger_protects_the_directory_holding_it(ct, capsys, where):
    c = ct
    holder = c.runs / "R" if where == "run" else c.runs / ".superseded" / "R"
    ledger = holder / "ledger.jsonl"
    ledger.write_text("")
    assert _clean(c, "--yes", **{"--spend-ledger": str(ledger)}) == 0
    line = _line(capsys.readouterr().out, holder)
    assert line.lstrip().startswith("skip") and "(contains a kept path" in line
    assert ledger.is_file()
    assert _clean(c, "--yes", "--clean-explicit-ledger", **{"--spend-ledger": str(ledger)}) == 0
    assert _gone(holder)


def test_a_kept_mirror_cache_protects_the_temp_root_holding_it(ct, monkeypatch, capsys):
    c = ct
    holder = c.tmp / "qbench_run_c"
    (holder / "mirrors").mkdir(parents=True)
    (holder / "mirrors" / "repo").write_text("x")
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(holder / "mirrors"))
    assert _clean(c, "--yes") == 0
    line = _line(capsys.readouterr().out, holder)
    assert line.lstrip().startswith("skip") and "(contains a kept path" in line
    assert (holder / "mirrors" / "repo").is_file()
    assert _clean(c, "--yes", "--clean-mirrors") == 0
    assert _gone(holder)


@pytest.mark.parametrize("marker", ["summary.md", "summary.json", "simple-claude"])
def test_a_single_summary_or_an_arm_named_file_is_not_a_run(ct, capsys, marker):
    c = ct
    notes = c.tmp_path / "notes"
    notes.mkdir()
    (notes / marker).write_text("x")
    assert _clean(c, "--yes", **{"--run-dir": str(c.tmp_path), "--run-id": "notes"}) == 0
    line = _line(capsys.readouterr().out, notes)
    assert line.lstrip().startswith("skip") and "(does not look like a benchmark run" in line
    assert (notes / marker).is_file()


def test_a_symlinked_candidate_is_marked_as_a_link(ct, monkeypatch, capsys):
    c = ct
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert _clean(c) == 1
    assert "(link)" in _line(capsys.readouterr().out, c.tmp / "qbench_link")


def test_a_directory_that_is_not_a_run_is_skipped(ct, capsys):
    c = ct
    stuff = c.tmp_path / "stuff"
    stuff.mkdir()
    (stuff / "a.txt").write_text("x")
    assert _clean(c, "--yes", **{"--run-dir": str(c.tmp_path), "--run-id": "stuff"}) == 0
    line = _line(capsys.readouterr().out, stuff)
    assert line.lstrip().startswith("skip") and "(does not look like a benchmark run" in line
    assert (stuff / "a.txt").is_file()


# --- the recorded total and explicit ledgers -----------------------------------

def test_an_explicit_ledger_is_kept_unless_its_flag_is_given(ct, capsys):
    c = ct
    shared = c.tmp_path / "shared.jsonl"
    attempt = paired_spend.reserve(shared, "S", "t1", "simple-claude", 5.5)
    paired_spend.settle(shared, attempt, "S", "t1", "simple-claude", 5.5, 1.0)
    assert _clean(c, "--yes", **{"--spend-ledger": str(shared)}) == 0
    out = capsys.readouterr().out
    assert _line(out, shared).lstrip().startswith("keep") and shared.is_file()
    assert _clean(c, "--yes", "--clean-explicit-ledger", **{"--spend-ledger": str(shared)}) == 0
    assert _gone(shared)


def test_the_default_ledger_total_is_shown_before_the_question(ct, monkeypatch, capsys):
    c = ct
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert _clean(c) == 1
    out = capsys.readouterr().out
    line = _line(out, c.ledger)
    assert "(recorded $2.50)" in line
    assert out.index("recorded $2.50") < out.index("Delete ")


def test_an_unreadable_default_ledger_is_still_deletable(ct, capsys):
    c = ct
    c.ledger.write_text("not json\n")
    assert _clean(c, "--yes") == 0
    assert "recorded total unreadable" in capsys.readouterr().out
    assert _gone(c.ledger)


# --- links, owners and nothing to do -------------------------------------------------

def test_permission_repair_does_not_follow_links(ct):
    c = ct
    outside = c.tmp_path / "outside"
    outside.mkdir()
    os.chmod(c.run_a, 0o700)
    (c.run_a / "out").symlink_to(outside)
    os.chmod(outside, 0o500)
    os.chmod(c.run_a, 0o100)
    assert _clean(c, "--yes") == 0
    assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o500
    assert _gone(c.run_a)


def test_temp_roots_of_another_owner_are_not_listed(ct, monkeypatch, capsys):
    c = ct
    monkeypatch.setattr(tiered_clean, "_uid", lambda: os.getuid() + 1)
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert _clean(c) == 1
    out = capsys.readouterr().out
    assert "temp root" not in out


def test_nothing_to_delete_does_not_ask(ct, monkeypatch, capsys, tmp_path):
    empty = tmp_path / "empty"
    (empty / "tmp").mkdir(parents=True)
    (empty / "sys").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(empty / "tmp"))
    monkeypatch.setattr(tiered_clean, "SYSTEM_TMP", str(empty / "sys"))
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(empty / "mirrors"))
    monkeypatch.setattr(sys, "stdin", NoRead())
    rc = tiered_entry.main(["--clean", "--run-dir", str(empty / "runs"),
                            "--repo-root", str(ct.checkout)])
    assert rc == 0
    assert "clean: nothing to delete" in capsys.readouterr().out


# --- through the CLI -----------------------------------------------------------------

def test_clean_through_the_cli(ct, cli_path):
    c = ct
    rc = cli.main(["bench", "tiered", "--clean", "--yes", "--repo-root", str(tiered_entry.CHECKOUT_ROOT),
                   "--run-dir", str(c.runs), "--run-id", "R"])
    assert rc == 0
    assert _gone(c.runs / "R")
