"""Contract tests for the residual userpromptsubmit.sh hook.

The hook only clears or stamps run-continuation sentinels. Its decisions are
checked against a frozen copy of the block it replaced, so any drift in the
disarm or consent rules shows up as a parity failure.
"""
import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
HOOKS = TESTS.parents[1] / "hooks"
HOOK = HOOKS / "userpromptsubmit.sh"
REFERENCE = TESTS / "fixtures" / "ups_disarm_reference.sh"
TRANSCRIPT_97 = TESTS / "fixtures" / "hooks" / "transcript_97pct.jsonl"
SID = "sess-1"

needs_sed_grep = pytest.mark.skipif(
    shutil.which("sed") is None or shutil.which("grep") is None,
    reason="reference block needs sed and grep")


def _env(home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def _run(script, payload, home, cwd):
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    return subprocess.run(["sh", str(script)], input=payload, capture_output=True,
                          text=True, env=_env(home), cwd=str(cwd), timeout=60)


class Proj:
    def __init__(self, tmp_path, name="p", memory=True, record=False, arm=True):
        self.home = tmp_path / ("home-" + name)
        self.home.mkdir(parents=True, exist_ok=True)
        self.root = self.home / "work" / "My Drive" / name
        self.root.mkdir(parents=True)
        self.mem = self.root / ".workflow_artifacts" / "memory"
        if memory:
            self.mem.mkdir(parents=True)
            if arm:
                (self.mem / ("run-continue-arm-%s.txt" % SID)).write_text("x")
            if record:
                (self.mem / "run-state-demo.json").write_text("{}")

    def state(self):
        return (
            (self.mem / ("run-continue-arm-%s.txt" % SID)).exists(),
            (self.mem / ("run-continue-consent-%s.txt" % SID)).exists(),
        )

    def payload(self, prompt="hello", **over):
        data = {"session_id": SID, "transcript_path": "/nonexistent/t.jsonl",
                "cwd": str(self.root), "prompt": prompt}
        data.update(over)
        return data


def test_shebang_and_static_text():
    text = HOOK.read_text(encoding="utf-8")
    assert re.match(r"#!/bin/sh( |\n)", text)
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for token in (r"\bjq\b", r"\bpython3\b", r"\btail\b", r"\bwc\b", r"_lib\.sh",
                  r"\bsed\b", r"\bgrep\b", r"\bawk\b", r"\bdirname\b"):
        assert not re.search(token, code), token


def test_exemption_is_a_single_case_arm():
    arms = re.findall(r"'<([a-zA-Z0-9_-]+)>'\*\)", HOOK.read_text(encoding="utf-8"))
    assert arms == ["task-notification"]


def test_plain_prompt_removes_arm(tmp_path):
    p = Proj(tmp_path)
    r = _run(HOOK, p.payload(), p.home, p.root)
    assert r.returncode == 0 and r.stdout == ""
    assert p.state() == (False, False)


def test_never_opens_transcript(tmp_path):
    p = Proj(tmp_path)
    r = _run(HOOK, p.payload(transcript_path=str(tmp_path / "missing.jsonl")), p.home, p.root)
    assert r.returncode == 0 and p.state() == (False, False)
    locked = tmp_path / "locked.jsonl"
    locked.write_text("{}\n")
    locked.chmod(0)
    try:
        if os.access(str(locked), os.R_OK):
            pytest.skip("running as a user that can read chmod 000 files")
        p2 = Proj(tmp_path, "q")
        _run(HOOK, p2.payload(transcript_path=str(locked)), p2.home, p2.root)
        assert p2.state() == (False, False)
    finally:
        locked.chmod(0o600)


def test_silent_on_high_fill_transcript(tmp_path):
    assert TRANSCRIPT_97.is_file()
    p = Proj(tmp_path)
    r = _run(HOOK, p.payload(transcript_path=str(TRANSCRIPT_97)), p.home, p.root)
    assert r.stdout == "" and r.returncode == 0
    names = {x.name for x in p.mem.iterdir()}
    assert not any(n.startswith(("pending-prompt-", "idle-advisory-pending-")) for n in names)
    assert "pollution-score-latest.txt" not in names
    assert "recent-sessions.md" not in names


def test_bare_cwd_creates_nothing(tmp_path):
    home = tmp_path / "home"
    cwd = home / "scratch" / "deep"
    cwd.mkdir(parents=True)

    def snap():
        return sorted((str(Path(d) / n)) for d, dirs, files in os.walk(str(tmp_path)) for n in dirs + files)

    before = snap()
    r = _run(HOOK, {"session_id": SID, "cwd": str(cwd), "prompt": "x"}, home, cwd)
    assert r.returncode == 0 and r.stdout == ""
    assert snap() == before


def test_outermost_owner_is_acted_on(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    deep = proj / "a" / "b" / "c"
    deep.mkdir(parents=True)
    outer = proj / ".workflow_artifacts" / "memory"
    inner = proj / "a" / ".workflow_artifacts" / "memory"
    outer.mkdir(parents=True)
    inner.mkdir(parents=True)
    for m in (outer, inner):
        (m / ("run-continue-arm-%s.txt" % SID)).write_text("x")
    _run(HOOK, {"session_id": SID, "cwd": str(deep), "prompt": "x"}, home, deep)
    assert not (outer / ("run-continue-arm-%s.txt" % SID)).exists()
    assert (inner / ("run-continue-arm-%s.txt" % SID)).exists()


# ---------------------------------------------------------------------------
# decision parity against the frozen reference block
# ---------------------------------------------------------------------------

def _case(prompt="hello", record=False, memory=True, **over):
    return dict(prompt=prompt, record=record, memory=memory, over=over)


CORPUS = {
    "plain": _case(),
    "task-notification": _case("<task-notification> x"),
    "task-notification-blanks": _case("   <task-notification> x"),
    "run": _case("/run demo", record=True),
    "run-resume": _case("/run --resume demo", record=True),
    "run-exact": _case("/run", record=True),
    "run-newline": _case("/run\nmore", record=True),
    "run-leading-blanks": _case("  /run demo", record=True),
    "runner": _case("/runner x", record=True),
    "run-no-record": _case("/run demo"),
    "run-mid-sentence": _case("please /run later", record=True),
    "newline-first-run": _case("\n/run demo", record=True),
    "newline-first-notification": _case("\n<task-notification> x"),
    "tab-first-run": _case("\t/run demo", record=True),
    "no-memory": _case(memory=False),
    "evil-field-in-prompt": _case('say "session_id":"evil" now'),
}


_COUNTER = itertools.count()


def _both(tmp_path, spec, mutate=None):
    results = []
    for tag, script in (("res", HOOK), ("ref", REFERENCE)):
        p = Proj(tmp_path, "%s%d" % (tag, next(_COUNTER)), memory=spec["memory"], record=spec["record"])
        payload = p.payload(spec["prompt"], **spec["over"])
        if mutate:
            payload = mutate(p, payload)
        r = _run(script, payload, p.home, p.root)
        assert r.stdout == "", tag
        results.append(p.state() if p.mem.exists() else None)
    return results


@needs_sed_grep
@pytest.mark.parametrize("name", sorted(CORPUS))
def test_decision_parity(tmp_path, name):
    res, ref = _both(tmp_path, CORPUS[name])
    assert res == ref


@needs_sed_grep
def test_parity_expected_outcomes(tmp_path):
    expected = {
        "plain": (False, False), "task-notification": (True, False),
        "task-notification-blanks": (True, False), "run": (False, True),
        "run-resume": (False, True), "run-exact": (False, True),
        "run-newline": (False, True), "run-leading-blanks": (False, True),
        "runner": (False, False), "run-no-record": (False, False),
        "run-mid-sentence": (False, False), "newline-first-run": (False, False),
        "newline-first-notification": (False, False), "tab-first-run": (False, False),
        "evil-field-in-prompt": (False, False),
    }
    for name, want in expected.items():
        res, ref = _both(tmp_path, CORPUS[name])
        assert res == ref == want, name


@needs_sed_grep
def test_parity_payload_variants(tmp_path):
    def sub(p, d):
        d["transcript_path"] = "/x/sess/subagents/agent-1.jsonl"
        return d

    def nosid(p, d):
        d.pop("session_id")
        return d

    def badsid(p, d):
        d["session_id"] = "abc_def"
        return d

    def nullcwd(p, d):
        d["cwd"] = None
        return d

    def nulltp(p, d):
        d["transcript_path"] = None
        return d

    base = CORPUS["plain"]
    for mut in (sub, nosid, badsid):
        res, ref = _both(tmp_path, base, mut)
        assert res == ref
    # missing cwd falls back to the process cwd (the project)
    for mut in (nullcwd, nulltp):
        res, ref = _both(tmp_path, base, mut)
        assert res == ref == (False, False)

    def dropcwd(p, d):
        d.pop("cwd")
        return d

    res, ref = _both(tmp_path, base, dropcwd)
    assert res == ref == (False, False)


@needs_sed_grep
def test_parity_garbage_stdin(tmp_path):
    for junk in ("", "not json at all", "{"):
        for script in (HOOK, REFERENCE):
            p = Proj(tmp_path, "g" + script.name[:3] + str(abs(hash(junk)) % 1000))
            r = _run(script, junk, p.home, p.root)
            assert r.returncode == 0 and r.stdout == ""


@needs_sed_grep
def test_parity_nested_cwd_and_spaces(tmp_path):
    def nested(p, d):
        sub = p.root / "x" / "y"
        sub.mkdir(parents=True, exist_ok=True)
        d["cwd"] = str(sub)
        return d

    res, ref = _both(tmp_path, CORPUS["plain"], nested)
    assert res == ref == (False, False)


# ---------------------------------------------------------------------------
# resolver parity against resolve_project_root in _lib.sh
# ---------------------------------------------------------------------------

def _lib_root(cwd, home):
    out = subprocess.run(
        ["sh", "-c", '. "%s/_lib.sh"; resolve_project_root "$1"' % HOOKS, "sh", cwd],
        capture_output=True, text=True, env=_env(home))
    return out.stdout.strip("\n")


def _hook_root(tmp_path, cwd, home, candidates):
    """Which candidate directory's arm file the hook removed."""
    for c in candidates:
        m = Path(c) / ".workflow_artifacts" / "memory"
        m.mkdir(parents=True, exist_ok=True)
        (m / ("run-continue-arm-%s.txt" % SID)).write_text("x")
    _run(HOOK, {"session_id": SID, "cwd": cwd, "prompt": "x"}, home, home)
    removed = [c for c in candidates
               if not (Path(c) / ".workflow_artifacts" / "memory" / ("run-continue-arm-%s.txt" % SID)).exists()]
    return removed


def test_resolver_parity_matrix(tmp_path):
    cases = []

    # outermost owner below HOME
    h = tmp_path / "h1"
    (h / "a" / "b").mkdir(parents=True)
    cases.append((h, str(h / "a" / "b"), [h / "a", h / "a" / "b"], h / "a"))
    # owner exactly at HOME is ignored; the nearest .git decides
    h = tmp_path / "h2"
    (h / "g" / "sub").mkdir(parents=True)
    (h / "g" / ".git").mkdir()
    (h / ".workflow_artifacts" / "memory").mkdir(parents=True)
    cases.append((h, str(h / "g" / "sub"), [h / "g"], h / "g"))
    # .git file instead of dir, path with spaces
    h = tmp_path / "h3"
    (h / "My Drive" / "proj" / "src").mkdir(parents=True)
    (h / "My Drive" / "proj" / ".git").write_text("gitdir: x")
    cases.append((h, str(h / "My Drive" / "proj" / "src"), [h / "My Drive" / "proj"], h / "My Drive" / "proj"))
    # trailing slash on the start dir
    h = tmp_path / "h4"
    (h / "p" / "q").mkdir(parents=True)
    cases.append((h, str(h / "p" / "q") + "/", [h / "p"], h / "p"))

    for home, cwd, owners, expect in cases:
        removed = _hook_root(tmp_path, cwd, home, owners)
        lib = _lib_root(cwd, home)
        assert len(removed) == 1
        assert str(removed[0]).rstrip("/") == str(expect).rstrip("/")
        if owners and any(
                (Path(o) / ".workflow_artifacts").is_dir() for o in owners):
            assert lib.rstrip("/") == str(expect).rstrip("/")


def test_resolver_no_marker_falls_back_to_start(tmp_path):
    home = tmp_path / "h5"
    cwd = home / "plain" / "dir"
    cwd.mkdir(parents=True)
    assert _lib_root(str(cwd), home) == str(cwd)
    m = cwd / ".workflow_artifacts" / "memory"
    m.mkdir(parents=True)
    (m / ("run-continue-arm-%s.txt" % SID)).write_text("x")
    # no walk marker above start, but the start itself now owns artifacts
    _run(HOOK, {"session_id": SID, "cwd": str(cwd), "prompt": "x"}, home, home)
    assert not (m / ("run-continue-arm-%s.txt" % SID)).exists()


def test_relative_cwd_without_slash(tmp_path):
    home = tmp_path / "h6"
    root = home / "w"
    m = root / ".workflow_artifacts" / "memory"
    m.mkdir(parents=True)
    (m / ("run-continue-arm-%s.txt" % SID)).write_text("x")
    r = _run(HOOK, {"session_id": SID, "cwd": ".", "prompt": "x"}, home, root)
    assert r.returncode == 0
    assert not (m / ("run-continue-arm-%s.txt" % SID)).exists()
