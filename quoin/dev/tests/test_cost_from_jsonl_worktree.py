"""Transcript lookup for sessions whose cwd hash differs from the project dir
holding the transcript (git worktrees, subdirectories).

jsonl_path_for must prefer the cwd-hash dir, fall back to a UUID-keyed search
across all project dirs, and never pick an arbitrary newest file.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
SCRIPT = SCRIPTS_DIR / "cost_from_jsonl.py"

sys.path.insert(0, str(SCRIPTS_DIR))
from cost_from_jsonl import jsonl_path_for, project_hash  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
WORKTREE = "/Users/x/proj/.workspaces/feature/quoin"
ROOT = "/Users/x/proj"


def _row(output_tokens=50):
    return json.dumps({
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 100,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    }) + "\n"


def _write(home, proj_hash, name, mtime=None, content=None):
    d = home / ".claude" / "projects" / proj_hash
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content if content is not None else _row())
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_resolves_transcript_under_other_project_dir(tmp_path):
    target = _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl")
    (tmp_path / ".claude" / "projects" / project_hash(WORKTREE)).mkdir(parents=True)

    got = jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path)
    assert got == target
    assert got.exists()


def test_resolves_when_cwd_hash_dir_does_not_exist(tmp_path):
    target = _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl")
    assert jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path) == target


def test_exact_hash_hit_takes_precedence(tmp_path):
    exact = _write(tmp_path, project_hash(WORKTREE), f"{UUID}.jsonl", mtime=1_000)
    _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl", mtime=2_000)

    assert jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path) == exact


def test_newest_match_wins_across_several_project_dirs(tmp_path):
    _write(tmp_path, "-older", f"{UUID}.jsonl", mtime=1_000)
    newer = _write(tmp_path, "-newer", f"{UUID}.jsonl", mtime=2_000)

    assert jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path) == newer


def test_miss_returns_nonexistent_cwd_hash_path(tmp_path):
    _write(tmp_path, project_hash(ROOT), "99999999-0000-0000-0000-000000000000.jsonl")

    got = jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path)
    assert got == (tmp_path / ".claude" / "projects" / project_hash(WORKTREE)
                   / f"{UUID}.jsonl")
    assert not got.exists()


def test_no_uuid_less_newest_file_fallback(tmp_path):
    # Other sessions' transcripts in the cwd-hash dir and elsewhere must never
    # stand in for the requested UUID.
    _write(tmp_path, project_hash(WORKTREE), "aaaaaaaa-0000-0000-0000-000000000000.jsonl",
           mtime=9_000)
    _write(tmp_path, project_hash(ROOT), "bbbbbbbb-0000-0000-0000-000000000000.jsonl",
           mtime=9_500)

    assert not jsonl_path_for(UUID, project_hash(WORKTREE), home=tmp_path).exists()


@pytest.mark.parametrize("bad_id", ["unknown-implement-20261105T120000Z", "*", "a/b", ""])
def test_non_uuid_ids_skip_cross_project_search(tmp_path, bad_id):
    _write(tmp_path, project_hash(ROOT), f"{bad_id or 'x'}.jsonl".replace("/", "_"))
    _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl")

    got = jsonl_path_for(bad_id, project_hash(WORKTREE), home=tmp_path)
    assert got == (tmp_path / ".claude" / "projects" / project_hash(WORKTREE)
                   / f"{bad_id}.jsonl")
    assert not got.exists()


def test_cost_row_resolution_uses_cross_project_lookup(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "_acl_worktree_test", SCRIPTS_DIR / "analyze_cost_ledger.py")
    acl = importlib.util.module_from_spec(spec)
    sys.modules["_acl_worktree_test"] = acl
    spec.loader.exec_module(acl)

    _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl")
    cost, resolved = acl.lookup_session_cost(UUID, project_hash(WORKTREE), tmp_path)
    assert resolved is True
    assert cost > 0


def test_cli_resolves_transcript_from_worktree_cwd(tmp_path):
    _write(tmp_path, project_hash(ROOT), f"{UUID}.jsonl")
    env = dict(os.environ, HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "session", "-i", UUID, "--json",
         "--project-path", WORKTREE],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["totalCost"] > 0
