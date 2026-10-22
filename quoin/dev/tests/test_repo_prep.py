"""Offline tests for per-task repository preparation and patch capture.

Git runs through the file transport against a stand-in upstream built in
tmp_path; the socket patch below only guards in-process code.
"""
from __future__ import annotations

import os
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


def test_capture_resets_info_exclude_and_attributes(world):
    p = _prep(world)
    info = p.path / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("hidden.txt\n")
    (info / "attributes").write_text("* -diff\n")
    (p.path / "hidden.txt").write_text("surprise\n")
    cap = capture_patch(p, world["base"])
    assert b"+surprise" in cap.patch_bytes
    assert not (info / "exclude").exists() and not (info / "attributes").exists()
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


def test_capture_failure_when_git_dir_deleted(world):
    import shutil

    p = _prep(world)
    shutil.rmtree(p.path / ".git")
    with pytest.raises(RepoPrepError) as exc:
        capture_patch(p, world["base"])
    assert exc.value.reason == "patch-capture-failed"
