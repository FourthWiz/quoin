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
                  r"\bsed\b", r"\bgrep\b", r"\bdirname\b"):
        assert not re.search(token, code), token
    # awk is allowed only for the extraction of a payload over 8 KB
    assert len(re.findall(r"\bawk\b", code)) == 1
    assert code.index("-gt 8192") < code.index("awk")


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


# ---------------------------------------------------------------------------
# large prompts, locale, key-named values
# ---------------------------------------------------------------------------

def _utf8_env(home):
    env = _env(home)
    env["LANG"] = "en_US.UTF-8"
    env.pop("LC_ALL", None)
    return env


@pytest.mark.parametrize("filler", ["a", "caf\u00e9 \"q\"\n"])
def test_large_prompt_is_fast_and_still_decides(tmp_path, filler):
    import time
    big = "/run demo " + filler * (1_000_000 // len(filler))
    p = Proj(tmp_path, record=True)
    # real multibyte bytes on the wire, not \\u escapes
    payload = json.dumps(p.payload(big), ensure_ascii=False)
    assert len(payload) > 900_000
    if filler != "a":
        assert len(payload.encode("utf-8")) > len(payload)
    start = time.monotonic()
    r = subprocess.run(["sh", str(HOOK)], input=payload.encode("utf-8"), capture_output=True,
                       env=_utf8_env(p.home), cwd=str(p.root), timeout=30)
    elapsed = time.monotonic() - start
    assert r.returncode == 0 and r.stdout == b""
    assert p.state() == (False, True)
    assert elapsed < 1.5, elapsed


def _shells():
    out = ["sh"]
    d = shutil.which("dash")
    if d:
        out.append(d)
    return out


@pytest.mark.parametrize("shell", _shells())
@pytest.mark.parametrize("size", [40_000, 1_000_000])
@pytest.mark.parametrize("kind", ["run", "plain", "exempt"])
def test_prompt_first_payload_is_fast_in_any_key_order(tmp_path, shell, size, kind):
    """The prompt precedes session_id, transcript_path and cwd, so every other
    key sits far from the start of the payload."""
    import time
    head = {"run": "/run demo ", "plain": "hello ", "exempt": "<task-notification>"}[kind]
    p = Proj(tmp_path, record=True)
    payload = json.dumps({"prompt": head + "x" * size, "session_id": SID,
                          "transcript_path": "/nonexistent/t.jsonl", "cwd": str(p.root)})
    start = time.monotonic()
    r = subprocess.run([shell, str(HOOK)], input=payload, capture_output=True, text=True,
                       env=_env(p.home), cwd=str(p.root), timeout=30)
    elapsed = time.monotonic() - start
    assert r.returncode == 0 and r.stdout == ""
    expected = {"run": (False, True), "plain": (False, False), "exempt": (True, False)}[kind]
    assert p.state() == expected
    assert elapsed < 2.0, elapsed


def test_large_exempt_prompt_keeps_arm(tmp_path):
    p = Proj(tmp_path)
    payload = json.dumps(p.payload("<task-notification>" + "x" * 1_000_000))
    r = subprocess.run(["sh", str(HOOK)], input=payload, capture_output=True, text=True,
                       env=_utf8_env(p.home), cwd=str(p.root), timeout=30)
    assert r.returncode == 0 and p.state() == (True, False)


def test_prompt_after_large_leading_blanks(tmp_path):
    p = Proj(tmp_path, record=True)
    r = _run(HOOK, p.payload(" " * 150 + "/run demo"), p.home, p.root)
    assert r.returncode == 0 and p.state() == (False, True)
    q = Proj(tmp_path, "blanks", record=True)
    _run(HOOK, q.payload(" " * 200000 + "/run demo"), q.home, q.root)
    assert q.state()[0] is False  # still disarms, however the prompt is read


@needs_sed_grep
def test_parity_value_equal_to_key_name(tmp_path):
    def evil(p, d):
        return {"x": "session_id", "y": "cwd", "z": "prompt", "transcript_path": "t",
                **{k: v for k, v in d.items()}}

    res, ref = _both(tmp_path, CORPUS["plain"], evil)
    assert res == ref == (False, False)
    res, ref = _both(tmp_path, CORPUS["run"], evil)
    assert res == ref == (False, True)


def test_key_named_value_before_key_still_disarms(tmp_path):
    p = Proj(tmp_path)
    payload = '{"note":"session_id","session_id":"%s","cwd":"%s","prompt":"hi"}' % (SID, p.root)
    r = _run(HOOK, payload, p.home, p.root)
    assert r.returncode == 0 and p.state() == (False, False)


def test_non_ascii_session_id_is_ignored(tmp_path):
    p = Proj(tmp_path)
    (p.mem / "run-continue-arm-a\u00e9.txt").write_text("x")
    r = subprocess.run(["sh", str(HOOK)], input=json.dumps(p.payload(session_id="a\u00e9"), ensure_ascii=False),
                       capture_output=True, text=True, env=_utf8_env(p.home), cwd=str(p.root), timeout=30)
    assert r.returncode == 0
    assert (p.mem / "run-continue-arm-a\u00e9.txt").exists()


def _awk_program():
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("awk 'BEGIN")
    return text[start:text.index("}')", start)]


def test_awk_program_has_no_posix_bracket_classes():
    # mawk 1.3.3 (Debian 10, Ubuntu 18.04) does not support [[:space:]].
    assert "[[:" not in _awk_program()


def _awks():
    out = ["awk"]
    for name in ("gawk", "mawk", "busybox"):
        if shutil.which(name):
            out.append(name)
    return [a for a in out if shutil.which(a)]


@pytest.mark.parametrize("sep", ["\t", "\r\n", " \t\r\n "])
@pytest.mark.parametrize("prompt_first", [True, False])
def test_large_payload_with_tab_crlf_whitespace(tmp_path, sep, prompt_first):
    p = Proj(tmp_path, "ws%d%d" % (len(sep), prompt_first), record=True)
    items = [("prompt", "/run demo " + "x" * 20000), ("session_id", SID),
             ("transcript_path", "/nonexistent/t.jsonl"), ("cwd", str(p.root))]
    if not prompt_first:
        items.reverse()
    body = ("," + sep).join('"%s"%s:%s%s' % (k, sep, sep, json.dumps(v)) for k, v in items)
    payload = "{" + sep + body + sep + "}"
    r = _run(HOOK, payload, p.home, p.root)
    assert r.returncode == 0 and r.stdout == ""
    assert p.state() == (False, True)
