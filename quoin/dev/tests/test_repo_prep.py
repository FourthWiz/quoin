"""Offline tests for per-task repository preparation and patch capture.

Git runs through the file transport against a stand-in upstream built in
tmp_path; the socket patch below only guards in-process code.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from quoin.benchmarks.harness import repo_prep
from quoin.benchmarks.harness.repo_prep import (
    RepoPrepError, capture_patch, check_pin_refs, cleanup_task_repo,
    hermetic_git_env, prepare_mirrors, prepare_task_repo,
)
from verified_standin import build_upstream, configure_env, git


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def world(tmp_path, monkeypatch):
    standin = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, standin)
    work = tmp_path / "work"
    work.mkdir()
    rows = prepare_mirrors([standin["task"]], log=lambda *_: None)
    assert rows[0]["pin_ref_ok"]
    return {**standin, "cache": cache, "work": work, "tmp": tmp_path}


def _prep(w):
    return prepare_task_repo(w["task"], w["cache"], w["work"])


def _g(path, *args):
    return git(list(args), path)


def test_prepared_clone_has_no_later_history(world, tmp_path, monkeypatch):
    template = tmp_path / "tmpl"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    gitconfig = tmp_path / "operator.gitconfig"
    gitconfig.write_text(f"[init]\n\ttemplateDir = {template}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "bogus"))
    p = _prep(world)
    assert p.base_commit == world["base"]
    assert _g(p.path, "rev-parse", "HEAD") == world["base"]
    assert _g(p.path, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert _g(p.path, "log", "--all", "--format=%H") == world["base"]
    assert _g(p.path, "rev-list", "--all", f"^{world['base']}") == ""
    assert _g(p.path, "tag") == ""
    assert _g(p.path, "remote") == ""
    assert _g(p.path, "status", "--porcelain") == ""
    hooks = p.path / ".git" / "hooks"
    if hooks.is_dir():
        assert not [f for f in hooks.rglob("*") if f.is_file() and f.stat().st_mode & stat.S_IXUSR]
    assert not (p.path / ".git" / "FETCH_HEAD").exists()
    assert world["later"] not in _g(p.path, "log", "--all", "--format=%H")


def test_two_preparations_share_tree_not_path(world):
    a, b = _prep(world), _prep(world)
    assert a.tree_sha == b.tree_sha
    assert a.path != b.path


def test_capture_survives_commit_and_branch_switch(world):
    p = _prep(world)
    (p.path / "a.txt").write_text("base\nfixed\n")
    _g(p.path, "commit", "-q", "-am", "agent")
    _g(p.path, "checkout", "-q", "main")
    _g(p.path, "checkout", "-q", "-b", "other")
    cap = capture_patch(p, world["base"])
    assert b"+fixed" in cap.patch_bytes
    fresh = _prep(world)
    (fresh.path / "p.patch").write_bytes(cap.patch_bytes)
    _g(fresh.path, "apply", "--check", "p.patch")


def test_capture_keeps_crlf_and_latin1_bytes(world):
    p = _prep(world)
    (p.path / "crlf.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    cap = capture_patch(p, world["base"])
    assert b" one\r\n" in cap.patch_bytes
    assert b"-two\r\n" in cap.patch_bytes and b"+TWO\r\n" in cap.patch_bytes
    assert not cap.decode_lossy
    fresh = _prep(world)
    (fresh.path / "p.patch").write_bytes(cap.patch_bytes)
    _g(fresh.path, "apply", "--check", "p.patch")

    q = _prep(world)
    (q.path / "latin.txt").write_bytes(b"caf\xe9\n")
    cap2 = capture_patch(q, world["base"])
    assert b"caf\xe9\n" in cap2.patch_bytes
    assert cap2.decode_lossy
    assert cap2.sha256 and cap2.byte_count == len(cap2.patch_bytes)


def test_capture_ignores_agent_written_fsmonitor_and_filter(world, tmp_path):
    p = _prep(world)
    marker = tmp_path / "marker"
    script = tmp_path / "mon.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    clean = tmp_path / "clean.sh"
    clean.write_text("#!/bin/sh\nsed s/secret/FILTERED/\n")
    clean.chmod(0o755)
    _g(p.path, "config", "core.fsmonitor", str(script))
    _g(p.path, "config", "filter.x.clean", str(clean))
    (p.path / ".gitattributes").write_text("*.dat filter=x\n")
    (p.path / "f.dat").write_text("secret\n")
    cap = capture_patch(p, world["base"])
    assert not marker.exists()
    assert b"+secret" in cap.patch_bytes and b"FILTERED" not in cap.patch_bytes
    assert cap.repo_config_modified


def _filter_script(tmp_path, marker):
    clean = tmp_path / "clean.sh"
    clean.write_text(f"#!/bin/sh\ntouch {marker}\nsed s/secret/FILTERED/\n")
    clean.chmod(0o755)
    return clean


def test_capture_ignores_commondir_redirect_with_clean_filter(world, tmp_path):
    p = _prep(world)
    marker = tmp_path / "filter-ran"
    clean = _filter_script(tmp_path, marker)
    evil = tmp_path / "evil-gitdir"
    shutil.copytree(p.path / ".git", evil)
    with open(evil / "config", "a", encoding="utf-8") as fh:
        fh.write(f'[filter "x"]\n\tclean = {clean}\n')
    (p.path / ".git" / "commondir").write_text(str(evil) + "\n")
    (p.path / ".gitattributes").write_text("*.dat filter=x\n")
    (p.path / "f.dat").write_text("secret\n")
    cap = capture_patch(p, world["base"])
    assert not marker.exists()
    assert b"+secret" in cap.patch_bytes and b"FILTERED" not in cap.patch_bytes


def _arm_nested_filter(repo_dir, clean):
    with open(repo_dir / "config", "a", encoding="utf-8") as fh:
        fh.write(f'[filter "x"]\n\tclean = {clean}\n')


def test_capture_ignores_embedded_repo_clean_filter(world, tmp_path):
    p = _prep(world)
    marker = tmp_path / "filter-ran"
    clean = _filter_script(tmp_path, marker)
    sub = p.path / "sub"
    sub.mkdir()
    _g(sub, "init", "-q")
    (sub / ".gitattributes").write_text("* filter=x\n")
    (sub / "f.txt").write_text("base\n")
    _g(sub, "add", "-A")
    _g(sub, "commit", "-q", "-m", "nested")
    _g(p.path, "add", "sub")
    _g(p.path, "commit", "-q", "-m", "gitlink")
    _arm_nested_filter(sub / ".git", clean)
    (sub / "f.txt").write_text("secret\n")
    cap = capture_patch(p, world["base"])
    assert not marker.exists()
    assert b"sub/f.txt" in cap.patch_bytes and b"+secret" in cap.patch_bytes
    assert b"160000" not in cap.patch_bytes and b".git" not in cap.patch_bytes.replace(b".gitattributes", b"")


def test_capture_ignores_submodule_clean_filter(world, tmp_path):
    src = tmp_path / "smsrc"
    src.mkdir()
    _g(src, "init", "-q")
    (src / ".gitattributes").write_text("* filter=x\n")
    (src / "f.txt").write_text("base\n")
    _g(src, "add", "-A")
    _g(src, "commit", "-q", "-m", "sm")
    p = _prep(world)
    git(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sm"], p.path)
    _g(p.path, "commit", "-q", "-m", "add submodule")
    modules = p.path / ".git" / "modules" / "sm"
    assert modules.is_dir() and (p.path / "sm" / ".git").is_file()
    marker = tmp_path / "filter-ran"
    _arm_nested_filter(modules, _filter_script(tmp_path, marker))
    (p.path / "sm" / "f.txt").write_text("secret\n")
    cap = capture_patch(p, world["base"])
    assert not marker.exists()
    assert b"sm/f.txt" in cap.patch_bytes and b"+secret" in cap.patch_bytes
    assert b"FILTERED" not in cap.patch_bytes


def test_capture_sees_edits_hidden_by_agent_index_flags(world):
    p = _prep(world)
    _g(p.path, "update-index", "--skip-worktree", "a.txt")
    _g(p.path, "update-index", "--assume-unchanged", "crlf.txt")
    (p.path / "a.txt").write_text("base\nhidden edit\n")
    (p.path / "crlf.txt").write_bytes(b"one\r\nhidden\r\n")
    cap = capture_patch(p, world["base"])
    assert b"+hidden edit" in cap.patch_bytes
    assert b"+hidden\r\n" in cap.patch_bytes


def test_capture_ignores_agent_replace_refs(world):
    p = _prep(world)
    (p.path / "a.txt").write_text("base\nreal change\n")
    _g(p.path, "commit", "-q", "-am", "change")
    _g(p.path, "replace", world["base"], _g(p.path, "rev-parse", "HEAD"))
    cap = capture_patch(p, world["base"])
    assert b"+real change" in cap.patch_bytes


def test_capture_neutralizes_worktree_gitattributes(world, tmp_path):
    p = _prep(world)
    (p.path / ".gitattributes").write_text("* text=auto eol=lf\n*.u16 working-tree-encoding=UTF-16\n")
    (p.path / "new.txt").write_bytes(b"x\r\ny\r\n")
    (p.path / "w.u16").write_bytes(b"plain\n")
    cap = capture_patch(p, world["base"])
    assert b"+x\r\n" in cap.patch_bytes
    assert b"+plain\n" in cap.patch_bytes
    fresh = _prep(world)
    (fresh.path / "p.patch").write_bytes(cap.patch_bytes)
    _g(fresh.path, "apply", "--check", "p.patch")


_BASE_ATTR_FILES = {
    ".gitattributes": b"*.crlf text eol=crlf\n*.id ident\n",
    "w.crlf": b"one\ntwo\nthree\n",
    "v.id": b"$Id$\nbody\n",
}


@pytest.fixture
def attr_world(tmp_path, monkeypatch):
    standin = build_upstream(tmp_path, extra_base_files=_BASE_ATTR_FILES)
    cache = configure_env(monkeypatch, tmp_path, standin)
    work = tmp_path / "work"
    work.mkdir()
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    return {**standin, "cache": cache, "work": work, "tmp": tmp_path}


def test_capture_is_empty_for_untouched_files_converted_on_checkout(attr_world):
    p = _prep(attr_world)
    # Checkout applied the base tree's attributes, so the worktree bytes
    # differ from the blobs; capture must undo exactly that conversion.
    assert (p.path / "w.crlf").read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert (p.path / "v.id").read_bytes().startswith(b"$Id: ")
    cap = capture_patch(p, attr_world["base"])
    assert cap.patch_bytes == b""


def test_capture_edit_under_eol_crlf_is_not_a_full_rewrite(attr_world):
    p = _prep(attr_world)
    (p.path / "w.crlf").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    cap = capture_patch(p, attr_world["base"])
    assert b"-two\n" in cap.patch_bytes and b"+TWO\n" in cap.patch_bytes
    assert b"-one" not in cap.patch_bytes and b"-three" not in cap.patch_bytes
    assert b"v.id" not in cap.patch_bytes
    fresh = _prep(attr_world)
    (fresh.path / "p.patch").write_bytes(cap.patch_bytes)
    _g(fresh.path, "apply", "--check", "p.patch")


def test_capture_ignores_agent_edits_to_conversion_attributes(attr_world):
    p = _prep(attr_world)
    (p.path / ".gitattributes").write_text("* -text\n")
    cap = capture_patch(p, attr_world["base"])
    assert b"w.crlf" not in cap.patch_bytes and b"v.id" not in cap.patch_bytes
    assert b"+* -text" in cap.patch_bytes


def test_capture_records_symlinks_without_following(world, tmp_path):
    p = _prep(world)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET-FILE\n")
    (outside / "inner.txt").write_text("TOP-SECRET-DIR\n")
    (p.path / "link.txt").symlink_to(outside / "secret.txt")
    (p.path / "linkdir").symlink_to(outside)
    cap = capture_patch(p, world["base"])
    assert b"TOP-SECRET" not in cap.patch_bytes
    assert b"new file mode 120000" in cap.patch_bytes
    assert str(outside / "secret.txt").encode() in cap.patch_bytes
    assert b"linkdir/inner.txt" not in cap.patch_bytes
    assert cap.symlinks_recorded == 2


def test_capture_never_writes_through_agent_git_symlinks(world, tmp_path):
    p = _prep(world)
    gd = p.path / ".git"
    precious = tmp_path / "precious.cfg"
    precious.write_text("precious\n")
    (gd / "config").unlink()
    (gd / "config").symlink_to(precious)
    outside_info = tmp_path / "outside-info"
    outside_info.mkdir()
    (outside_info / "exclude").write_text("keep\n")
    shutil.rmtree(gd / "info", ignore_errors=True)
    (gd / "info").symlink_to(outside_info)
    outside_objinfo = tmp_path / "outside-objinfo"
    outside_objinfo.mkdir()
    (outside_objinfo / "alternates").write_text("keep\n")
    shutil.rmtree(gd / "objects" / "info", ignore_errors=True)
    (gd / "objects" / "info").symlink_to(outside_objinfo)
    (p.path / "a.txt").write_text("base\nedit\n")
    cap = capture_patch(p, world["base"])
    assert b"+edit" in cap.patch_bytes
    assert precious.read_text() == "precious\n"
    assert (outside_info / "exclude").read_text() == "keep\n"
    assert (outside_objinfo / "alternates").read_text() == "keep\n"
    assert cap.repo_config_modified and cap.repo_info_modified


def test_capture_leaves_agent_git_dir_untouched(world, tmp_path):
    p = _prep(world)
    gd = p.path / ".git"
    alt = gd / "objects" / "info" / "alternates"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text(str(tmp_path / "elsewhere") + "\n")
    (gd / "config.worktree").write_text("[core]\n\tfsmonitor = true\n")
    (p.path / "a.txt").write_text("x\n")
    cap = capture_patch(p, world["base"])
    assert b"+x" in cap.patch_bytes
    assert alt.exists() and (gd / "config.worktree").exists()


def test_capture_works_with_symlinked_or_missing_git_dir(world, tmp_path):
    p = _prep(world)
    moved = tmp_path / "moved-git"
    shutil.move(str(p.path / ".git"), str(moved))
    (p.path / ".git").symlink_to(moved)
    (p.path / "a.txt").write_text("base\nviasymlink\n")
    cap = capture_patch(p, world["base"])
    assert b"+viasymlink" in cap.patch_bytes and cap.head_commit == ""

    q = _prep(world)
    shutil.rmtree(q.path / ".git")
    (q.path / "a.txt").write_text("base\nnogit\n")
    cap2 = capture_patch(q, world["base"])
    assert b"+nogit" in cap2.patch_bytes


def test_capture_rejects_oversized_patch(world, monkeypatch):
    p = _prep(world)
    monkeypatch.setattr(repo_prep, "MAX_PATCH_BYTES", 64)
    (p.path / "big.txt").write_text("y" * 500 + "\n")
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p, world["base"])
    assert exc.value.reason == "patch-too-large"


def test_capture_snapshot_cap_applies_while_reading(world, monkeypatch):
    p = _prep(world)
    monkeypatch.setattr(repo_prep, "MAX_SNAPSHOT_BYTES", 1024)
    monkeypatch.setattr(repo_prep, "_READ_CHUNK", 256)
    (p.path / "huge.bin").write_bytes(b"z" * 4096)
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p, world["base"])
    assert exc.value.reason == "snapshot-too-large"
    assert not [d for d in os.listdir(p.path.parent) if d.startswith("qbench_capture_")]


def test_capture_skips_fifo_and_requires_prepared_repo(world):
    p = _prep(world)
    os.mkfifo(p.path / "pipe")
    (p.path / "a.txt").write_text("base\nfifo-ok\n")
    cap = capture_patch(p, world["base"])
    assert b"+fifo-ok" in cap.patch_bytes and cap.special_files_skipped == 1
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p.path, world["base"])
    assert exc.value.reason == "patch-capture-failed"


def test_capture_honours_info_exclude_never(world):
    p = _prep(world)
    info = p.path / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("hidden.txt\n")
    (info / "attributes").write_text("* -diff\n")
    (p.path / "hidden.txt").write_text("surprise\n")
    cap = capture_patch(p, world["base"])
    assert b"+surprise" in cap.patch_bytes
    assert cap.repo_info_modified


def test_capture_modes_and_exclusions(world):
    p = _prep(world)
    (p.path / "a.txt").write_text("base\ncommitted\n")
    _g(p.path, "commit", "-q", "-am", "c")
    (p.path / "crlf.txt").write_bytes(b"one\r\ntwo\r\nSTAGED\r\n")
    _g(p.path, "add", "crlf.txt")
    (p.path / "a.txt").write_text("base\ncommitted\nunstaged\n")
    (p.path / "new.py").write_text("print(1)\n")
    (p.path / "blob.bin").write_bytes(bytes(range(256)))
    for rel in (".workflow_artifacts/x", ".claude/settings.json", "CLAUDE.md", "AGENTS.md"):
        f = p.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("noise\n")
    cap = capture_patch(p, world["base"])
    t = cap.patch_bytes
    for needle in (b"+committed", b"+unstaged", b"STAGED", b"new.py", b"blob.bin", b"GIT binary patch"):
        assert needle in t
    for banned in (b".workflow_artifacts", b".claude", b"CLAUDE.md", b"AGENTS.md"):
        assert banned not in t


def test_missing_pin_ref_raises_and_leaves_no_clone(world, tmp_path):
    spec = dict(world["task"], source_id="org__repo-2")
    before = set(os.listdir(world["work"]))
    with pytest.raises(RepoPrepError) as exc:
        prepare_task_repo(spec, world["cache"], world["work"])
    assert exc.value.reason == "missing-pin-ref"
    assert set(os.listdir(world["work"])) == before
    probs = check_pin_refs([spec], world["cache"])
    assert probs and "verified_easy_000" in probs[0]
    mirror = repo_prep.mirror_path(world["cache"], "org/repo")
    git(["update-ref", "refs/quoin-bench/org__repo-1", world["later"]], mirror)
    wrong = check_pin_refs([world["task"]], world["cache"])
    assert wrong and "expected" in wrong[0]


@pytest.mark.parametrize("repo", ["../x", "a/b/c", "owner/name;rm"])
def test_bad_repo_names_never_reach_the_url_template(world, repo):
    class Exploding(str):
        def format(self, **kw):
            raise AssertionError("template formatted")

    spec = dict(world["task"], repo=repo)
    with pytest.raises(RepoPrepError) as exc:
        prepare_mirrors([spec], world["cache"], Exploding("x/{repo}"), log=lambda *_: None)
    assert exc.value.reason == "bad-repo-name"
    with pytest.raises(RepoPrepError) as exc2:
        prepare_task_repo(spec, world["cache"], world["work"])
    assert exc2.value.reason == "bad-repo-name"


def test_hermetic_env_allowlist():
    base = {
        "PATH": "/bin", "HOME": "/home/x", "HTTPS_PROXY": "p", "NO_PROXY": "n",
        "SSL_CERT_FILE": "c", "GIT_SSL_CAINFO": "i", "GIT_DIR": "/bogus",
        "GIT_CONFIG_GLOBAL": "/etc/x", "GIT_TEMPLATE_DIR": "/t",
    }
    off = hermetic_git_env(base)
    for k in ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "GIT_SSL_CAINFO", "GIT_DIR", "GIT_TEMPLATE_DIR"):
        assert k not in off
    assert off["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert off["HOME"] != "/home/x"
    on = hermetic_git_env(base, network=True)
    for k in ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "GIT_SSL_CAINFO"):
        assert on[k] == base[k]
    assert "GIT_DIR" not in on


def test_cleanup_handles_readonly_and_refuses_foreign_paths(world, tmp_path, monkeypatch):
    p = _prep(world)
    ro = p.path / "ro"
    ro.mkdir()
    (ro / "f").write_text("x")
    ro.chmod(0o500)
    assert cleanup_task_repo(p.path) is None
    assert not p.path.exists()

    foreign = tmp_path / "precious"
    foreign.mkdir()
    (foreign / "keep").write_text("k")
    assert cleanup_task_repo(foreign) == "refused-path"
    assert (foreign / "keep").exists()

    q = _prep(world)

    def boom(*a, **k):
        raise OSError("stuck")

    monkeypatch.setattr(repo_prep.shutil, "rmtree", boom)
    reason = cleanup_task_repo(q.path)
    assert isinstance(reason, str) and "stuck" in reason


def test_capture_skips_names_git_rejects(world):
    p = _prep(world)
    (p.path / ".git ").mkdir()
    (p.path / ".git " / "x.txt").write_text("hidden\n")
    (p.path / "GIT~1").write_text("alias\n")
    (p.path / "a.txt").write_text("base\nkept\n")
    cap = capture_patch(p, world["base"])
    assert cap.rejected_names == 2
    assert b"+kept" in cap.patch_bytes
    assert b"hidden" not in cap.patch_bytes and b"GIT~1" not in cap.patch_bytes


def test_capture_counts_readlink_race_as_special(world, monkeypatch):
    p = _prep(world)
    (p.path / "link.txt").symlink_to("a.txt")

    def racing(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(repo_prep.os, "readlink", racing)
    cap = capture_patch(p, world["base"])
    assert cap.special_files_skipped == 1 and cap.symlinks_recorded == 0
    assert b"link.txt" not in cap.patch_bytes


def test_capture_leaves_out_oversize_files(world):
    p = _prep(world)
    big = p.path / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(101 * 1024 * 1024)
    (p.path / "a.txt").write_text("base\nsmall-ok\n")
    cap = capture_patch(p, world["base"])
    assert cap.oversize_files == ["big.bin"]
    assert b"big.bin" not in cap.patch_bytes and b"+small-ok" in cap.patch_bytes


def test_capture_drops_a_file_that_grows_past_the_cap_while_copied(world, monkeypatch):
    p = _prep(world)
    monkeypatch.setattr(repo_prep, "MAX_SNAPSHOT_FILE_BYTES", 1024)
    monkeypatch.setattr(repo_prep, "_READ_CHUNK", 256)
    real_fstat = os.fstat

    class _Stat:
        def __init__(self, st):
            self._st = st
            self.st_size = 0

        def __getattr__(self, name):
            return getattr(self._st, name)

    monkeypatch.setattr(repo_prep.os, "fstat", lambda fd: _Stat(real_fstat(fd)))
    (p.path / "grow.bin").write_bytes(b"z" * 4096)
    cap = capture_patch(p, world["base"])
    assert cap.oversize_files == ["grow.bin"]
    assert b"grow.bin" not in cap.patch_bytes


def test_heavy_directories_do_not_change_the_patch(world):
    p = _prep(world)
    (p.path / "a.txt").write_text("base\nedit\n")
    plain = capture_patch(p, world["base"])
    (p.path / ".venv").mkdir()
    (p.path / ".venv" / "lib.py").write_text("x\n")
    (p.path / "pkg" / "node_modules").mkdir(parents=True)
    (p.path / "pkg" / "node_modules" / "m.js").write_text("y\n")
    with_heavy = capture_patch(p, world["base"])
    assert with_heavy.skipped_heavy_dirs == 2
    assert b".venv" not in with_heavy.patch_bytes and b"node_modules" not in with_heavy.patch_bytes
    assert with_heavy.patch_bytes == plain.patch_bytes


def test_heavy_dir_tracked_in_base_is_reported(tmp_path, monkeypatch, world):
    assert repo_prep.check_heavy_dirs([world["task"]], world["cache"]) == []
    other = tmp_path / "tracked"
    other.mkdir()
    standin = build_upstream(other, extra_base_files={"node_modules/x.js": b"tracked\n"})
    cache = configure_env(monkeypatch, other, standin)
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    problems = repo_prep.check_heavy_dirs([standin["task"]], cache)
    assert len(problems) == 1 and "heavy-dir-tracked node_modules/x.js" in problems[0]


def test_capture_error_carries_the_inner_reason(world, tmp_path):
    p = _prep(world)
    empty = tmp_path / "empty.git"
    git(["init", "--bare", "-q", str(empty)], tmp_path)
    p.mirror = empty
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p, world["base"])
    assert exc.value.reason == "patch-capture-failed"
    assert exc.value.inner_reason == "base-missing"
    p.mirror = None
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p, world["base"])
    assert exc.value.inner_reason == "mirror-unavailable"


def test_attr_source_support_is_parsed_from_the_version_line():
    assert repo_prep._parse_attr_source_support("git version 2.41.0") is False
    assert repo_prep._parse_attr_source_support("git version 2.54.0") is True
    assert repo_prep._parse_attr_source_support("git version 3.0.1 (Apple Git-1)") is True
    assert repo_prep._parse_attr_source_support("nonsense") is False


def test_attr_source_probe_is_cached_and_recorded(world, monkeypatch):
    calls = []

    class _Proc:
        returncode = 0
        stdout = "git version 2.41.0"

    monkeypatch.setattr(repo_prep.subprocess, "run", lambda *a, **k: calls.append(a) or _Proc())
    repo_prep.git_supports_attr_source.cache_clear()
    try:
        assert repo_prep.git_supports_attr_source() is False
        assert repo_prep.git_supports_attr_source() is False
        assert len(calls) == 1
    finally:
        repo_prep.git_supports_attr_source.cache_clear()
