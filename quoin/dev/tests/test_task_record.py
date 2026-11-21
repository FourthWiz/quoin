"""Task record: read-only sessions leave nothing, edits open a provisional
record or a task, turns are journaled once, and the settings hooks stand aside
for sessions the capture mod drives."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import task_record as tr  # noqa: E402

SCRIPT = PKG / "core" / "scripts" / "task_record.py"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("QUOIN_ARTIFACT_ROOT", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".workflow_artifacts").mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _art(repo):
    return repo / ".workflow_artifacts"


def _files(repo):
    return sorted(
        str(p.relative_to(_art(repo))) for p in _art(repo).rglob("*") if p.is_file() and p.name != ".gitignore"
    )


def _hook(event, data):
    return tr.hook(event, data)


def _transcript(tmp_path, *prompts):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": p}} for p in prompts]
    rows.insert(1, {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}})
    rows.append({"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def test_read_only_session_leaves_nothing(repo):
    _git(repo, "checkout", "-q", "-b", "feat/x")
    base = {"session_id": "ro", "cwd": str(repo)}
    _hook("session-start", {**base, "source": "startup"})
    _hook("user-prompt-submit", {**base, "prompt": "what is here?"})
    _hook("post-tool-use", {**base, "tool_name": "Read", "tool_input": {"file_path": str(repo / "a")}})
    _hook("stop", {**base, "last_assistant_message": "It is empty."})
    assert list(_art(repo).iterdir()) == []


def test_edit_without_task_branch_is_provisional_and_unlisted(repo, tmp_path):
    t = _transcript(tmp_path, "tidy the readme")
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "README")}, "transcript_path": t})
    _hook("stop", {**base, "last_assistant_message": "Tidied.\nDetails"})
    assert _files(repo) == [".capture/provisional/s1/journal.md", ".capture/sessions/s1.json"]
    assert "Tidied." in (_art(repo) / ".capture/provisional/s1/journal.md").read_text()
    assert tr.open_tasks(str(repo)) == []
    assert _hook("user-prompt-submit", {**base, "prompt": "x"}) == ""


def test_edit_on_task_branch_opens_task_and_journals_each_turn(repo, tmp_path):
    _git(repo, "checkout", "-q", "-b", "feat/widget-fix")
    t = _transcript(tmp_path, "explain the widget", "now fix the widget")
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Write", "tool_input": {"file_path": str(repo / "a.py")}, "transcript_path": t})
    _hook("stop", {**base, "last_assistant_message": "Fixed it."})
    _hook("stop", {**base, "last_assistant_message": "Answered a question."})

    brief = (_art(repo) / "feat-widget-fix" / "brief.md").read_text()
    assert brief.startswith("# widget fix\n")
    assert "- Task: feat/widget-fix" in brief and "- Sessions: s1" in brief
    assert "## Request\n\nexplain the widget" in brief
    assert "## Prompt at first edit\n\nnow fix the widget" in brief
    lines = (_art(repo) / "feat-widget-fix" / "journal.md").read_text().splitlines()
    assert len(lines) == 2
    assert "proj/a.py · Fixed it." in lines[0]
    assert "no edits · Answered a question." in lines[1]


def test_provisional_record_is_promoted_when_a_branch_appears(repo):
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a.py")}})
    _hook("stop", {**base, "last_assistant_message": "First turn."})
    _git(repo, "checkout", "-q", "-b", "fix/late-branch")
    _hook("stop", {**base, "last_assistant_message": "Second turn."})

    journal = (_art(repo) / "fix-late-branch" / "journal.md").read_text().splitlines()
    assert [line.split(" · ")[1] for line in journal] == ["First turn.", "Second turn."]
    assert not (_art(repo) / ".capture/provisional/s1").exists()
    assert [t["key"] for t in tr.open_tasks(str(repo))] == ["fix/late-branch"]


def test_second_session_joins_the_branch_task(repo):
    _git(repo, "checkout", "-q", "-b", "feat/two")
    for sid in ("s1", "s2"):
        _hook("post-tool-use", {"session_id": sid, "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    brief = (_art(repo) / "feat-two" / "brief.md").read_text()
    assert "- Sessions: s1, s2" in brief


def test_titles_and_resume_pointer(repo):
    _git(repo, "checkout", "-q", "-b", "feat/titled")
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    tr.rename(str(repo), "s1", "Titled work")

    out = json.loads(_hook("user-prompt-submit", {**base, "prompt": "x"}))
    assert out["hookSpecificOutput"]["sessionTitle"] == "Titled work"
    assert _hook("user-prompt-submit", {**base, "prompt": "x", "session_title": "Titled work"}) == ""

    out = json.loads(_hook("session-start", {**base, "source": "resume"}))["hookSpecificOutput"]
    assert out["sessionTitle"] == "Titled work"
    assert "feat-titled/brief.md" in out["additionalContext"]
    assert _hook("session-start", {**base, "source": "compact"}) == ""


def test_rename_by_model_never_overrides_a_chosen_name(repo):
    _git(repo, "checkout", "-q", "-b", "feat/n")
    tr.make_task(str(repo), "s1", "My name")
    tr.rename(str(repo), "s1", "Model name")
    assert (_art(repo) / "feat-n" / "brief.md").read_text().startswith("# My name\n")


def test_make_task_without_branch_uses_the_name_as_key(repo):
    state = tr.make_task(str(repo), "s1", "Spike the cache")
    assert state["kind"] == "task" and state["key"] == "spike-the-cache"
    assert [t["name"] for t in tr.open_tasks(str(repo))] == ["Spike the cache"]


def test_skip_turns_capture_off(repo):
    _git(repo, "checkout", "-q", "-b", "feat/skip")
    tr.skip(str(repo), "s1")
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    _hook("stop", {**base, "last_assistant_message": "done"})
    assert _files(repo) == [".capture/sessions/s1.json"]


def test_hooks_stand_aside_for_mod_sessions(repo):
    _git(repo, "checkout", "-q", "-b", "feat/mod")
    tr.mark_mod("s1")
    base = {"session_id": "s1", "cwd": str(repo)}
    _hook("post-tool-use", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    _hook("stop", {**base, "last_assistant_message": "done"})
    assert _files(repo) == []


def test_provisional_records_expire_after_seven_days(repo):
    tr.open_record(str(repo), "old", str(repo / "a"))
    tr.journal(str(repo), "old", "x")
    later = time.time() + 8 * 86400
    assert tr.expire_provisional(_art(repo), now=later) == ["old"]
    assert _files(repo) == []


def test_user_prompts_skip_tool_results_and_meta_rows(tmp_path):
    assert tr.user_prompts(_transcript(tmp_path, "one", "two")) == ["one", "two"]
    assert tr.user_prompts(str(tmp_path / "missing.jsonl")) == []


def test_command_line_round_trip(repo):
    _git(repo, "checkout", "-q", "-b", "feat/cli")
    env = {**os.environ, "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]}
    body = json.dumps({"first_request": "do it"})
    run = lambda *a, stdin="": subprocess.run(  # noqa: E731
        [sys.executable, str(SCRIPT), *a], input=stdin, capture_output=True, text=True, env=env, check=True
    )
    opened = json.loads(run("open", "--cwd", str(repo), "--session", "c1", "--file", str(repo / "a"), stdin=body).stdout)
    assert opened["kind"] == "task"
    run("journal", "--cwd", str(repo), "--session", "c1", stdin=json.dumps({"answer": "Done.", "files": [str(repo / "b")]}))
    assert "proj/a, proj/b · Done." in (_art(repo) / "feat-cli" / "journal.md").read_text()
    assert "cli  (feat/cli)" in run("status", "--cwd", str(repo)).stdout
    bad = subprocess.run([sys.executable, str(SCRIPT), "hook", "stop"], input="not json", capture_output=True, text=True)
    assert bad.returncode == 0 and bad.stdout == ""


def test_plugin_registers_the_capture_hooks_and_module():
    hooks = json.loads((PKG / "hooks" / "hooks.json").read_text())
    assert hooks["modules"] == ["./capture.tsx"]
    assert (PKG / "hooks" / "capture.tsx").is_file()
    wanted = [
        ("PostToolUse", "post-tool-use"),
        ("PostToolUse", "post-bash"),
        ("Stop", "stop"),
        ("UserPromptSubmit", "user-prompt-submit"),
        ("SessionStart", "session-start"),
    ]
    for event, arg in wanted:
        commands = [h["command"] for entry in hooks["hooks"][event] for h in entry["hooks"]]
        assert any(c.endswith(f'/hooks/capture.sh" {arg}') for c in commands), event
    assert os.stat(PKG / "hooks" / "capture.sh").st_mode & stat.S_IXUSR


# ── lifecycle ──────────────────────────────────────────────────────────────


def _second_repo(tmp_path, name):
    other = tmp_path / name
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(other, "checkout", "-q", "-b", "feat/multi")
    return other


def _bash(repo, session, command, stdout):
    return _hook("post-bash", {"session_id": session, "cwd": str(repo), "tool_name": "Bash",
                               "tool_input": {"command": command}, "tool_response": {"stdout": stdout}})


def _field(repo, folder, name):
    return tr._get_field((_art(repo) / folder / "brief.md").read_text(), name)


def test_pr_moves_a_single_repo_task_to_review(repo):
    _git(repo, "checkout", "-q", "-b", "feat/one")
    _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    _bash(repo, "s1", "gh pr create --fill", "https://github.com/o/proj/pull/7\n")
    assert _field(repo, "feat-one", "Status") == "review"
    assert _field(repo, "feat-one", "PRs") == "proj=https://github.com/o/proj/pull/7"
    assert [t["status"] for t in tr.open_tasks(str(repo))] == ["review"]


def test_multi_repo_task_waits_for_a_pr_in_every_repository(repo, tmp_path):
    _git(repo, "checkout", "-q", "-b", "feat/multi")
    other = _second_repo(tmp_path, "api")
    for path in (repo / "a", other / "b"):
        _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(path)}})
    _bash(repo, "s1", "gh pr create --fill", "https://github.com/o/proj/pull/1\n")
    assert _field(repo, "feat-multi", "Status") == "open"
    _bash(repo, "s1", f'cd "{other}" && gh pr create --fill', "https://github.com/o/api/pull/2\n")
    assert _field(repo, "feat-multi", "Status") == "review"


def test_failed_pr_create_records_nothing(repo):
    _git(repo, "checkout", "-q", "-b", "feat/fail")
    _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    _bash(repo, "s1", "gh pr create --fill", "pull request create failed: GraphQL error\n")
    assert _field(repo, "feat-fail", "Status") == "open"


def test_merged_prs_archive_the_task_and_release_its_claims(repo, monkeypatch, tmp_path):
    import run_isolation

    monkeypatch.setenv("QUOIN_RUN_REGISTRY", str(tmp_path / "runs"))
    monkeypatch.setenv("QUOIN_RUN_OWNER_PID", str(os.getpid()))
    _git(repo, "checkout", "-q", "-b", "feat/merge")
    _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    run_isolation.claim_session(repo / "a", "s1")
    _bash(repo, "s1", "gh pr create", "https://github.com/o/proj/pull/9\n")

    assert tr.lifecycle(str(repo), force=True, gh=lambda url: "OPEN") == []
    assert _field(repo, "feat-merge", "Status") == "review"
    changes = tr.lifecycle(str(repo), force=True, gh=lambda url: "MERGED")
    assert [c["status"] for c in changes] == ["archived"]
    assert tr.open_tasks(str(repo)) == []
    assert run_isolation.load_entries(run_isolation.registry_dir(repo)) == {}
    assert tr.lifecycle(str(repo), gh=lambda url: "MERGED") == []  # within the interval


def test_done_closes_a_task_without_a_pr(repo):
    _git(repo, "checkout", "-q", "-b", "feat/done")
    _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    assert tr.close_task(str(repo), "s1")["status"] == "closed"
    assert tr.open_tasks(str(repo)) == []


def test_idle_task_without_a_pr_is_flagged_stale(repo):
    _git(repo, "checkout", "-q", "-b", "feat/idle")
    _hook("post-tool-use", {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "a")}})
    _hook("stop", {"session_id": "s1", "cwd": str(repo), "last_assistant_message": "done"})
    old = time.time() - 8 * 86400
    for name in ("brief.md", "journal.md"):
        os.utime(_art(repo) / "feat-idle" / name, (old, old))
    [task] = tr.open_tasks(str(repo))
    assert task["stale"] is True


def test_commit_runs_the_discovery_check(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "_spawn", calls.append)
    _git(repo, "checkout", "-q", "-b", "feat/commit")
    _bash(repo, "s1", "git commit -m x", "[feat/commit 1a2b3c4] x\n 1 file changed\n")
    _bash(repo, "s1", "git commit -m y", "nothing to commit, working tree clean\n")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[1].endswith("discovery_dirty.py") and argv[argv.index("--task") + 1] == "feat/commit"
