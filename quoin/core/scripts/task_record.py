#!/usr/bin/env python3
"""Task record for plain Claude Code sessions.

A session that only reads leaves nothing. The first edit opens a record:

- on a task branch, a task at ``.workflow_artifacts/<branch-slug>/`` with
  ``brief.md`` (the request verbatim, sessions, branch, repositories) and
  ``journal.md`` (one line per completed turn), keyed by the branch name;
- anywhere else, a provisional record keyed by the session: a journal only,
  never listed as a task, deleted after seven days, and promoted to a task
  when a later turn finds a task branch checked out.

Two callers drive it: the capture mod in ``hooks/capture.ts`` and, where mods
cannot load, the settings hooks through ``hook <event>``, which read the hook
input on stdin. A session the mod has marked is left to the mod, so a turn is
journaled once. Standard library only; every hook path fails open.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from path_resolve import resolve_artifact_root  # noqa: E402

PROVISIONAL_TTL_DAYS = 7
MOD_MARKER_TTL_DAYS = 2
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
TRUNK_BRANCHES = frozenset({"main", "master", "develop", "development", "trunk", "dev"})
JOURNAL_FILES_SHOWN = 8
ANSWER_CHARS = 160
STALE_DAYS = 7
LIFECYCLE_INTERVAL = 30 * 60
ACTIVE_STATUSES = ("open", "review")
_PR_URL = re.compile(r"https://github\.com/[^/\s]+/([^/\s]+)/pull/\d+")
_COMMIT_LINE = re.compile(r"^\[[^\]]+ [0-9a-f]{7,}\]", re.M)
_CD_PREFIX = re.compile(r"""^\s*cd\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*&&""")


# --- locations -------------------------------------------------------------


def artifacts_dir(cwd: str | os.PathLike) -> Path:
    return resolve_artifact_root(cwd) / ".workflow_artifacts"


def capture_dir(art: Path) -> Path:
    return art / ".capture"


def _ensure_capture_dir(art: Path) -> None:
    path = capture_dir(art)
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        (path / ".gitignore").write_text("*\n", encoding="utf-8")


def _session_file(art: Path, session: str) -> Path:
    return capture_dir(art) / "sessions" / f"{_safe(session)}.json"


def _provisional_dir(art: Path, session: str) -> Path:
    return capture_dir(art) / "provisional" / _safe(session)


def _mod_marker(session: str) -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "quoin" / "capture-mod" / _safe(session)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:120] or "_"


def branch_slug(branch: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.replace("/", "-")).strip("-.")
    return slug[:100] or "task"


# --- git -------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def repo_of(path: str | os.PathLike) -> Path | None:
    start = Path(path)
    while not start.is_dir():
        if start == start.parent:
            return None
        start = start.parent
    top = _git(start, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def task_branch(repo: Path | None) -> str | None:
    """Return the checked-out branch when it names a task, else None."""
    if repo is None:
        return None
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch or branch in TRUNK_BRANCHES:
        return None
    default = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if default and default.split("/", 1)[-1] == branch:
        return None
    return branch


# --- state -----------------------------------------------------------------


def load_state(art: Path, session: str) -> dict | None:
    path = _session_file(art, session)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(art: Path, state: dict) -> None:
    _ensure_capture_dir(art)
    path = _session_file(art, state["session"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


# --- brief -----------------------------------------------------------------


def _brief_path(art: Path, state: dict) -> Path:
    return art / state["dir"] / "brief.md"


def _journal_path(art: Path, state: dict) -> Path:
    if state["kind"] == "provisional":
        return _provisional_dir(art, state["session"]) / "journal.md"
    return art / state["dir"] / "journal.md"


def _set_field(text: str, field: str, value: str) -> str:
    pattern = re.compile(rf"^- {re.escape(field)}: .*$", re.M)
    line = f"- {field}: {value}"
    if pattern.search(text):
        return pattern.sub(lambda _m: line, text, count=1)
    return text


def _get_field(text: str, field: str) -> str | None:
    match = re.search(rf"^- {re.escape(field)}: (.*)$", text, re.M)
    return match.group(1).strip() if match else None


def _add_to_list_field(text: str, field: str, item: str) -> str:
    current = _get_field(text, field)
    if current is None:
        return text
    items = [part.strip() for part in current.split(",") if part.strip()]
    if item in items:
        return text
    return _set_field(text, field, ", ".join([*items, item]))


def _write_brief(art: Path, state: dict) -> None:
    path = _brief_path(art, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        text = _add_to_list_field(text, "Sessions", state["session"])
        for repo in state.get("repos", []):
            text = _add_to_list_field(text, "Repositories", repo)
        path.write_text(text, encoding="utf-8")
        return
    first = (state.get("first_request") or "").strip()
    at_edit = (state.get("edit_request") or "").strip()
    parts = [
        f"# {state['name']}",
        "",
        f"- Task: {state['key']}",
        "- Status: open",
        f"- Opened: {_now()}",
        f"- Branch: {state.get('branch') or 'none'}",
        f"- Sessions: {state['session']}",
        f"- Repositories: {', '.join(state.get('repos', []))}",
        "- PRs: none",
        "",
        "## Request",
        "",
        first or "(not recorded)",
        "",
    ]
    if at_edit and at_edit != first:
        parts += ["## Prompt at first edit", "", at_edit, ""]
    path.write_text("\n".join(parts), encoding="utf-8")


def _default_name(state: dict) -> str:
    if state.get("branch"):
        return state["branch"].rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    words = (state.get("edit_request") or state.get("first_request") or "").split()
    return " ".join(words[:6]) or f"session {state['session'][:8]}"


# --- operations ------------------------------------------------------------


def _note_repo(state: dict, repo: Path | None) -> None:
    if repo is None:
        return
    state["repo_path"] = str(repo)
    if repo.name not in state.setdefault("repos", []):
        state["repos"].append(repo.name)


def _make_task(art: Path, state: dict, key: str, branch: str | None) -> None:
    state.update(kind="task", key=key, branch=branch, dir=branch_slug(key))
    brief = _brief_path(art, state)
    if brief.is_file() and not state.get("named"):
        heading = brief.read_text(encoding="utf-8").split("\n", 1)[0]
        state["name"] = heading.lstrip("# ").strip() or _default_name(state)
    elif not state.get("named"):
        state["name"] = _default_name(state)
    _write_brief(art, state)


def _promote(art: Path, state: dict, key: str, branch: str | None) -> None:
    old = _provisional_dir(art, state["session"])
    lines = ""
    if (old / "journal.md").is_file():
        lines = (old / "journal.md").read_text(encoding="utf-8")
    _make_task(art, state, key, branch)
    if lines:
        with _journal_path(art, state).open("a", encoding="utf-8") as fh:
            fh.write(lines)
    shutil.rmtree(old, ignore_errors=True)


def _maybe_promote(art: Path, state: dict) -> None:
    if state["kind"] != "provisional" or not state.get("repo_path"):
        return
    branch = task_branch(Path(state["repo_path"]))
    if branch:
        _promote(art, state, branch, branch)


def open_record(
    cwd: str,
    session: str,
    file: str | None,
    *,
    first_request: str = "",
    edit_request: str = "",
) -> dict | None:
    """Open (or extend) the session's record for an edit of ``file``."""
    art = artifacts_dir(cwd)
    expire_provisional(art)
    state = load_state(art, session)
    if state and state["kind"] == "skip":
        return state
    repo = repo_of(file or cwd)
    if state is None:
        state = {
            "session": session,
            "kind": "provisional",
            "key": None,
            "dir": None,
            "named": False,
            "pending_files": [],
            "first_request": first_request,
            "edit_request": edit_request,
        }
        _note_repo(state, repo)
        branch = task_branch(repo)
        if branch:
            _make_task(art, state, branch, branch)
        else:
            state["name"] = _default_name(state)
            _ensure_capture_dir(art)
            _provisional_dir(art, session).mkdir(parents=True, exist_ok=True)
    else:
        _note_repo(state, repo)
        _maybe_promote(art, state)
        if state["kind"] == "task":
            _write_brief(art, state)
    if file and file not in state["pending_files"]:
        state["pending_files"].append(file)
    save_state(art, state)
    return state


def _display_files(files: list[str], repo_path: str | None) -> str:
    shown = []
    for name in files:
        path = Path(name)
        rel = name
        for base in filter(None, [repo_path]):
            try:
                rel = str(path.relative_to(Path(base).parent))
            except ValueError:
                pass
        shown.append(rel)
    if not shown:
        return "no edits"
    extra = len(shown) - JOURNAL_FILES_SHOWN
    text = ", ".join(shown[:JOURNAL_FILES_SHOWN])
    return f"{text} (+{extra} more)" if extra > 0 else text


def _first_line(answer: str) -> str:
    for line in (answer or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= ANSWER_CHARS else line[: ANSWER_CHARS - 1] + "…"
    return "(no answer text)"


def journal(cwd: str, session: str, answer: str, files: list[str] | None = None) -> dict | None:
    """Append one line for a completed turn, when the session has a record."""
    art = artifacts_dir(cwd)
    state = load_state(art, session)
    if not state or state["kind"] == "skip":
        return None
    for name in files or []:
        _note_repo(state, repo_of(name))
        if name not in state["pending_files"]:
            state["pending_files"].append(name)
    _maybe_promote(art, state)
    if state["kind"] == "task":
        _write_brief(art, state)
    line = (
        f"- {_now()} [{session[:8]}] "
        f"{_display_files(state['pending_files'], state.get('repo_path'))} · {_first_line(answer)}\n"
    )
    path = _journal_path(art, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    state["pending_files"] = []
    save_state(art, state)
    return state


def rename(cwd: str, session: str, name: str, *, by_user: bool = False) -> dict | None:
    art = artifacts_dir(cwd)
    state = load_state(art, session)
    name = " ".join(name.split())[:80]
    if not state or state["kind"] == "skip" or not name:
        return state
    if state.get("named") and not by_user:
        return state
    state["name"] = name
    state["named"] = True
    if state["kind"] == "task":
        path = _brief_path(art, state)
        if path.is_file():
            body = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(f"# {name}\n" + (body[1] if len(body) > 1 else ""), encoding="utf-8")
    save_state(art, state)
    return state


def make_task(cwd: str, session: str, name: str, *, first_request: str = "") -> dict:
    """Turn the session into a named task now, edits or not."""
    art = artifacts_dir(cwd)
    state = load_state(art, session) or {
        "session": session,
        "kind": "provisional",
        "pending_files": [],
        "first_request": first_request,
        "edit_request": "",
    }
    if state["kind"] == "skip":
        state["kind"] = "provisional"
    name = " ".join(name.split())[:80]
    if name:
        state.update(name=name, named=True)
    if state["kind"] != "task":
        repo = Path(state["repo_path"]) if state.get("repo_path") else repo_of(cwd)
        _note_repo(state, repo)
        branch = task_branch(repo)
        key = branch or branch_slug(state.get("name") or session[:8]).lower()
        _promote(art, state, key, branch)
    save_state(art, state)
    if name:
        state = rename(cwd, session, name, by_user=True) or state
    return state


def skip(cwd: str, session: str) -> dict:
    art = artifacts_dir(cwd)
    state = load_state(art, session) or {"session": session, "pending_files": []}
    state["kind"] = "skip"
    save_state(art, state)
    return state


def expire_provisional(art: Path, *, now: float | None = None) -> list[str]:
    root = art / ".capture" / "provisional"
    if not root.is_dir():
        return []
    cutoff = (now or time.time()) - PROVISIONAL_TTL_DAYS * 86400
    gone = []
    for folder in root.iterdir():
        newest = max((p.stat().st_mtime for p in folder.rglob("*")), default=folder.stat().st_mtime)
        if newest < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            (art / ".capture" / "sessions" / f"{folder.name}.json").unlink(missing_ok=True)
            gone.append(folder.name)
    return gone


def open_tasks(cwd: str) -> list[dict]:
    art = artifacts_dir(cwd)
    expire_provisional(art)
    tasks = []
    if not art.is_dir():
        return tasks
    now = time.time()
    for brief in sorted(art.glob("*/brief.md")):
        text = brief.read_text(encoding="utf-8", errors="replace")
        status = _get_field(text, "Status")
        if status not in ACTIVE_STATUSES or _get_field(text, "Task") is None:
            continue
        journal_file = brief.parent / "journal.md"
        last = None
        touched = brief.stat().st_mtime
        if journal_file.is_file():
            lines = journal_file.read_text(encoding="utf-8", errors="replace").splitlines()
            last = lines[-1] if lines else None
            touched = max(touched, journal_file.stat().st_mtime)
        prs = _prs(text)
        tasks.append(
            {
                "name": text.split("\n", 1)[0].lstrip("# ").strip(),
                "key": _get_field(text, "Task"),
                "branch": _get_field(text, "Branch"),
                "status": status,
                "prs": prs,
                "stale": status == "open" and not prs and now - touched > STALE_DAYS * 86400,
                "brief": str(brief),
                "last": last,
            }
        )
    return tasks


def task_for_branch(cwd: str, branch: str) -> dict | None:
    for task in open_tasks(cwd):
        if task["key"] == branch:
            return task
    return None


# --- lifecycle -------------------------------------------------------------


def _prs(text: str) -> dict[str, str]:
    """``{repository: pr url}`` from a brief's PRs field."""
    raw = _get_field(text, "PRs") or "none"
    out = {}
    for part in raw.split(","):
        repo, _, url = part.strip().partition("=")
        if url:
            out[repo] = url
    return out


def _with_prs(text: str, prs: dict[str, str]) -> str:
    value = ", ".join(f"{repo}={url}" for repo, url in prs.items()) or "none"
    if _get_field(text, "PRs") is None:
        return re.sub(r"^(- Repositories: .*)$", lambda m: f"{m.group(1)}\n- PRs: {value}", text, count=1, flags=re.M)
    return _set_field(text, "PRs", value)


def _list_field(text: str, field: str) -> list[str]:
    return [p.strip() for p in (_get_field(text, field) or "").split(",") if p.strip()]


def _brief_for(cwd: str, session: str, repo: Path | None) -> Path | None:
    art = artifacts_dir(cwd)
    state = load_state(art, session)
    if state and state.get("kind") == "provisional":
        _maybe_promote(art, state)
        save_state(art, state)
    if state and state.get("kind") == "task":
        return _brief_path(art, state)
    branch = task_branch(repo)
    task = task_for_branch(cwd, branch) if branch else None
    return Path(task["brief"]) if task else None


def record_pr(cwd: str, session: str, url: str, repo_name: str, repo: Path | None = None) -> dict | None:
    """Record a PR; the task moves to review once every repository it touched has one."""
    brief = _brief_for(cwd, session, repo)
    if brief is None or not brief.is_file():
        return None
    text = brief.read_text(encoding="utf-8")
    prs = _prs(text)
    prs[repo_name] = url
    text = _with_prs(text, prs)
    repos = _list_field(text, "Repositories") or [repo_name]
    if _get_field(text, "Status") == "open" and all(r in prs for r in repos):
        text = _set_field(text, "Status", "review")
    brief.write_text(text, encoding="utf-8")
    return {"brief": str(brief), "status": _get_field(text, "Status"), "prs": prs}


def _gh_state(url: str) -> str | None:
    try:
        out = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state", "-q", ".state"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _release_claims(sessions: list[str]) -> None:
    try:
        import run_isolation
    except Exception:  # noqa: BLE001 - claims also expire on their own
        return
    for sid in sessions:
        try:
            run_isolation.release_session(sid)
        except Exception:  # noqa: BLE001
            pass


def lifecycle(cwd: str, *, force: bool = False, gh=_gh_state) -> list[dict]:
    """Archive tasks in review whose PRs have all merged, and release their claims."""
    art = artifacts_dir(cwd)
    if not art.is_dir():
        return []
    in_review = [
        brief for brief in sorted(art.glob("*/brief.md"))
        if _get_field(brief.read_text(encoding="utf-8", errors="replace"), "Status") == "review"
    ]
    if not in_review:
        return []
    stamp = capture_dir(art) / "lifecycle.json"
    try:
        last = json.loads(stamp.read_text(encoding="utf-8")).get("at", 0)
    except (OSError, ValueError, AttributeError):
        last = 0
    if not force and time.time() - last < LIFECYCLE_INTERVAL:
        return []
    changes = []
    for brief in in_review:
        text = brief.read_text(encoding="utf-8", errors="replace")
        prs = _prs(text)
        if _get_field(text, "Status") != "review" or not prs:
            continue
        states = [gh(url) for url in prs.values()]
        if all(state == "MERGED" for state in states):
            brief.write_text(_set_field(text, "Status", "archived"), encoding="utf-8")
            _release_claims(_list_field(text, "Sessions"))
            changes.append({"brief": str(brief), "status": "archived"})
    _ensure_capture_dir(art)
    stamp.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    return changes


def close_task(cwd: str, session: str) -> dict | None:
    """Close the session's task by hand, for work that ends without a PR."""
    brief = _brief_for(cwd, session, repo_of(cwd))
    if brief is None or not brief.is_file():
        return None
    text = brief.read_text(encoding="utf-8")
    brief.write_text(_set_field(text, "Status", "closed"), encoding="utf-8")
    _release_claims(_list_field(text, "Sessions"))
    return {"brief": str(brief), "status": "closed", "name": text.split("\n", 1)[0].lstrip("# ").strip()}


def _command_dir(command: str, cwd: str) -> Path:
    match = _CD_PREFIX.match(command)
    if match:
        target = Path(os.path.expanduser(next(g for g in match.groups() if g)))
        return target if target.is_absolute() else Path(cwd) / target
    return Path(cwd)


def _discovery_check(cwd: str, session: str, repo: Path) -> None:
    art = artifacts_dir(cwd)
    state = load_state(art, session) or {}
    task = state.get("key") or task_branch(repo) or f"session-{session[:8]}"
    script = Path(__file__).resolve().parent / "discovery_dirty.py"
    _spawn([sys.executable, str(script), "--repo", str(repo), "--task", task, "--project-root", str(art.parent)])


def _spawn(argv: list[str]) -> None:
    """Start a background job that outlives the hook and never holds the session."""
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def mark_mod(session: str) -> None:
    marker = _mod_marker(session)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    cutoff = time.time() - MOD_MARKER_TTL_DAYS * 86400
    for old in marker.parent.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def mod_active(session: str) -> bool:
    return _mod_marker(session).exists()


# --- transcripts -----------------------------------------------------------


def user_prompts(transcript: str | None) -> list[str]:
    """Return the person's prompts, oldest first, from a session transcript."""
    prompts: list[str] = []
    if not transcript:
        return prompts
    try:
        handle = open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return prompts
    with handle:
        for raw in handle:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
                continue
            content = (row.get("message") or {}).get("content")
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                content = "\n".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
            if not isinstance(content, str):
                continue
            text = content.strip()
            if text and not text.startswith(("<local-command", "<system-reminder", "<task-notification")):
                prompts.append(text)
    return prompts


# --- settings hooks --------------------------------------------------------


def _hook_output(event: str, *, title: str | None = None, context: str | None = None) -> str:
    out: dict = {"hookEventName": event}
    if title:
        out["sessionTitle"] = title
    if context:
        out["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": out})


def _pointer(task: dict, cwd: str) -> str:
    brief = Path(task["brief"])
    try:
        shown = brief.relative_to(Path(cwd))
    except ValueError:
        shown = brief
    return f'Quoin task "{task["name"]}" is open on this branch; its brief is {shown}, with journal.md beside it.'


def hook(event: str, data: dict) -> str:
    session = data.get("session_id") or ""
    cwd = data.get("cwd") or os.getcwd()
    if not session:
        return ""
    if event == "post-tool-use":
        if data.get("tool_name") not in EDIT_TOOLS or mod_active(session):
            return ""
        tool_input = data.get("tool_input") or {}
        file = tool_input.get("file_path") or tool_input.get("notebook_path")
        art = artifacts_dir(cwd)
        if load_state(art, session) is None:
            prompts = user_prompts(data.get("transcript_path"))
            open_record(
                cwd,
                session,
                file,
                first_request=prompts[0] if prompts else "",
                edit_request=prompts[-1] if prompts else "",
            )
        else:
            open_record(cwd, session, file)
        return ""
    if event == "stop":
        if data.get("stop_hook_active") or mod_active(session):
            return ""
        journal(cwd, session, data.get("last_assistant_message") or "")
        return ""
    if event == "user-prompt-submit":
        state = load_state(artifacts_dir(cwd), session)
        if state and state.get("kind") == "task" and state.get("name") != data.get("session_title"):
            return _hook_output("UserPromptSubmit", title=state["name"])
        return ""
    if event == "post-bash":
        command = (data.get("tool_input") or {}).get("command") or ""
        response = data.get("tool_response")
        stdout = response.get("stdout", "") if isinstance(response, dict) else str(response or "")
        where = _command_dir(command, cwd)
        repo = repo_of(where)
        if "gh pr create" in command:
            match = _PR_URL.search(stdout)
            if match:
                record_pr(cwd, session, match.group(0), repo.name if repo else match.group(1), repo)
        if "git commit" in command and repo is not None and _COMMIT_LINE.search(stdout):
            _discovery_check(cwd, session, repo)
        return ""
    if event == "session-start":
        source = data.get("source")
        if source not in ("startup", "resume"):
            return ""
        _spawn([sys.executable, str(Path(__file__).resolve()), "lifecycle", "--cwd", cwd])
        art = artifacts_dir(cwd)
        state = load_state(art, session)
        task = None
        if state and state.get("kind") == "task":
            task = task_for_branch(cwd, state["key"])
        if task is None:
            branch = task_branch(repo_of(cwd))
            task = task_for_branch(cwd, branch) if branch else None
        if task is None:
            return ""
        return _hook_output("SessionStart", title=task["name"], context=_pointer(task, cwd))
    return ""


# --- command line ----------------------------------------------------------


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_session(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--cwd", default=os.getcwd())
        p.add_argument("--session", required=True)
        return p

    p_open = with_session(sub.add_parser("open", help="open the record on an edit; JSON body on stdin"))
    p_open.add_argument("--file")
    p_journal = with_session(sub.add_parser("journal", help="append a turn; JSON {answer, files} on stdin"))
    p_rename = with_session(sub.add_parser("rename", help="name the task"))
    p_rename.add_argument("--name", required=True)
    p_task = with_session(sub.add_parser("task", help="turn the session into a task"))
    p_task.add_argument("--name", default="")
    with_session(sub.add_parser("skip", help="turn capture off for the session"))
    p_status = sub.add_parser("status", help="list open tasks")
    p_status.add_argument("--cwd", default=os.getcwd())
    p_status.add_argument("--json", action="store_true")
    p_mark = sub.add_parser("mark-mod", help="record that the capture mod drives this session")
    p_mark.add_argument("--session", required=True)
    p_pr = with_session(sub.add_parser("pr", help="record a PR opened for the session's task"))
    p_pr.add_argument("--url", required=True)
    p_pr.add_argument("--repo", default=None, help="the repository folder the PR is for (default: --cwd)")
    with_session(sub.add_parser("done", help="close the session's task without a PR"))
    p_life = sub.add_parser("lifecycle", help="archive tasks whose PRs have all merged")
    p_life.add_argument("--cwd", default=os.getcwd())
    p_life.add_argument("--force", action="store_true", help="ignore the 30-minute interval")
    p_hook = sub.add_parser("hook", help="settings-hook entry; hook input on stdin")
    p_hook.add_argument("event", choices=["post-tool-use", "post-bash", "stop", "user-prompt-submit", "session-start"])
    args = parser.parse_args(argv)

    if args.cmd == "hook":
        try:
            out = hook(args.event, _read_stdin_json())
        except Exception as exc:  # a hook must never break the session
            print(f"task_record: {exc}", file=sys.stderr)
            return 0
        if out:
            print(out)
        return 0
    if args.cmd == "status":
        tasks = open_tasks(args.cwd)
        if args.json:
            print(json.dumps(tasks, indent=1))
        elif not tasks:
            print("No open tasks.")
        else:
            for task in tasks:
                flag = " [in review]" if task["status"] == "review" else (" [stale]" if task["stale"] else "")
                print(f"{task['name']}  ({task['key']}){flag}")
                if task["last"]:
                    print(f"    {task['last'].lstrip('- ')}")
        return 0
    if args.cmd == "mark-mod":
        mark_mod(args.session)
        return 0
    if args.cmd == "lifecycle":
        print(json.dumps(lifecycle(args.cwd, force=args.force)))
        return 0
    if args.cmd == "pr":
        repo = repo_of(args.repo or args.cwd)
        match = _PR_URL.search(args.url)
        name = repo.name if repo else (match.group(1) if match else "repo")
        print(json.dumps(record_pr(args.cwd, args.session, args.url, name, repo) or {}))
        return 0
    if args.cmd == "done":
        print(json.dumps(close_task(args.cwd, args.session) or {}))
        return 0

    body = _read_stdin_json() if args.cmd in ("open", "journal", "task") else {}
    if args.cmd == "open":
        state = open_record(
            args.cwd,
            args.session,
            args.file,
            first_request=body.get("first_request", ""),
            edit_request=body.get("edit_request", ""),
        )
    elif args.cmd == "journal":
        state = journal(args.cwd, args.session, body.get("answer", ""), body.get("files") or [])
    elif args.cmd == "rename":
        state = rename(args.cwd, args.session, args.name)
    elif args.cmd == "task":
        state = make_task(args.cwd, args.session, args.name, first_request=body.get("first_request", ""))
    else:
        state = skip(args.cwd, args.session)
    print(json.dumps(state or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
