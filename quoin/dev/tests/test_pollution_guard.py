"""Tests for quoin/scripts/pollution_guard.py (the on-demand pollution score)."""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parents[1] / "scripts"
GUARD = SCRIPTS / "pollution_guard.py"
OLD_LIB_REF = "906e072f"
UUID = "11111111-2222-3333-4444-555555555555"


def _run(args, tmp_path, env_extra=None, python=None, cwd=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([python or sys.executable, str(GUARD), *args],
                          capture_output=True, text=True, env=env, timeout=60,
                          cwd=str(cwd or tmp_path))


def _score(args, tmp_path, env_extra=None):
    proc = _run(args, tmp_path, env_extra)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout
    status, score, _path = lines[0].split("|", 2)
    return status, int(score)


def _tool_line(name, extra=None):
    content = [{"type": "tool_use", "id": "t", "name": name, "input": {}}]
    if extra:
        content = extra
    return json.dumps({"type": "assistant", "message": {"content": content}}) + "\n"


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestScore:
    def test_empty_file_scores_zero(self, tmp_path):
        f = _write(tmp_path / "e.jsonl", "")
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", 0)

    def test_5kb_without_tools_scores_5(self, tmp_path):
        line = json.dumps({"type": "text", "content": "x" * 80}) + "\n"
        content = (line * (5000 // len(line) + 1))[:5000]
        f = tmp_path / "5kb.jsonl"
        f.write_bytes(content.encode())
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", 5)

    def test_tool_weighted_score(self, tmp_path):
        filler = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "x" * 870}]}}) + "\n"
        lines = [filler] * 1000
        lines += [_tool_line("Agent")] * 10 + [_tool_line("Read")] * 20 + [_tool_line("Bash")] * 5
        content = "".join(lines)
        pad = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "p" * 870}]}}) + "\n"
        while len(content.encode()) < 1_000_000:
            content += pad
        f = tmp_path / "heavy.jsonl"
        f.write_bytes(content.encode()[:1_000_000])
        # the byte cut can drop the last tool lines only if they sit past 1 MB; they do not
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", 1000 + 50 + 20 + 5)

    def test_real_shape_fixture(self, tmp_path):
        text = "".join([_tool_line("Bash")] * 30 + [_tool_line("Read")] * 15 + [_tool_line("Agent")] * 5)
        f = _write(tmp_path / "real.jsonl", text)
        kb = f.stat().st_size // 1000
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", kb + 70)

    def test_one_line_with_several_tool_items_counts_each(self, tmp_path):
        items = [{"type": "tool_use", "name": "Read"}, {"type": "text", "text": "hi"},
                 {"type": "tool_use", "name": "Agent"}, {"type": "tool_use", "name": "Other"}]
        f = _write(tmp_path / "multi.jsonl", _tool_line("x", extra=items))
        kb = f.stat().st_size // 1000
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", kb + 6)

    def test_only_assistant_lines_count(self, tmp_path):
        user = json.dumps({"type": "user", "message": {"content": [{"type": "tool_use", "name": "Agent"}]}}) + "\n"
        f = _write(tmp_path / "u.jsonl", user)
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", f.stat().st_size // 1000)

    def test_malformed_line_does_not_stop_counting(self, tmp_path):
        text = _tool_line("Agent") + "this is {not json \"tool_use\"\n" + _tool_line("Agent")
        f = _write(tmp_path / "bad.jsonl", text)
        kb = f.stat().st_size // 1000
        assert _score(["--transcript", str(f)], tmp_path) == ("OK", kb + 10)

    def test_appended_file_equals_fresh_score(self, tmp_path):
        f = _write(tmp_path / "a.jsonl", _tool_line("Agent") + _tool_line("Read"))
        first = _score(["--transcript", str(f)], tmp_path)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(_tool_line("Bash"))
        again = _score(["--transcript", str(f)], tmp_path)
        copy = tmp_path / "copy.jsonl"
        shutil.copyfile(str(f), str(copy))
        assert again == _score(["--transcript", str(copy)], tmp_path)
        assert (first[1], again[1]) == (6, 7)

    def test_partial_trailing_line_not_counted_twice(self, tmp_path):
        f = _write(tmp_path / "p.jsonl", _tool_line("Agent"))
        with f.open("a", encoding="utf-8") as fh:
            fh.write(_tool_line("Agent").rstrip("\n"))
        with f.open("a", encoding="utf-8") as fh:
            fh.write("\n")
        done = _score(["--transcript", str(f)], tmp_path)[1]
        assert done == f.stat().st_size // 1000 + 10


class TestVerdict:
    def test_boundary(self, tmp_path):
        below = tmp_path / "below.jsonl"
        below.write_bytes(b"x" * 4_999_000)
        at = tmp_path / "at.jsonl"
        at.write_bytes(b"x" * 5_000_000)
        assert _score(["--transcript", str(below)], tmp_path) == ("OK", 4999)
        assert _score(["--transcript", str(at)], tmp_path) == ("OVER", 5000)

    def test_threshold_flag_and_env(self, tmp_path):
        f = tmp_path / "f.jsonl"
        f.write_bytes(b"x" * 2_000_000)
        assert _score(["--transcript", str(f)], tmp_path)[0] == "OK"
        assert _score(["--transcript", str(f), "--threshold", "2000"], tmp_path)[0] == "OVER"
        assert _score(["--transcript", str(f)], tmp_path, {"QUOIN_POLLUTION_THRESHOLD": "2000"})[0] == "OVER"
        assert _score(["--transcript", str(f), "--threshold", "2001"], tmp_path,
                      {"QUOIN_POLLUTION_THRESHOLD": "1"})[0] == "OK"

    def test_non_integer_env_falls_back(self, tmp_path):
        f = tmp_path / "f.jsonl"
        f.write_bytes(b"x" * 5_000_000)
        proc = _run(["--transcript", str(f)], tmp_path, {"QUOIN_POLLUTION_THRESHOLD": "lots"})
        assert proc.stdout.startswith("OVER|5000|")
        assert "non-integer" in proc.stderr


class TestFailOpen:
    @pytest.mark.parametrize("args", [
        ["--transcript", "/nonexistent/x.jsonl"],
        ["--transcript", ""],
        ["--bogus-flag"],
        ["--threshold", "abc"],
    ])
    def test_bad_input_prints_ok_zero_and_exits_zero(self, tmp_path, args):
        proc = _run(args, tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == "OK|0|\n"

    def test_unreadable_file(self, tmp_path):
        f = tmp_path / "locked.jsonl"
        f.write_text("x")
        f.chmod(0)
        try:
            if os.access(str(f), os.R_OK):
                pytest.skip("can read chmod 000 files")
            proc = _run(["--transcript", str(f)], tmp_path)
            assert proc.returncode == 0
            assert proc.stdout == "OK|0|\n"
        finally:
            f.chmod(0o600)


class TestResolution:
    def _projects(self, tmp_path, project_root):
        from_hash = "".join(c if c.isalnum() or c == "-" else "-" for c in str(project_root).rstrip("/"))
        d = tmp_path / "home" / ".claude" / "projects" / from_hash
        d.mkdir(parents=True)
        return d

    def test_order_transcript_uuid_env_newest(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pdir = self._projects(tmp_path, project.resolve())
        newest = pdir / "99999999-0000-0000-0000-000000000000.jsonl"
        newest.write_bytes(b"x" * 9000)
        env_one = pdir / (UUID + ".jsonl")
        env_one.write_bytes(b"x" * 7000)
        uuid_one = pdir / "22222222-3333-4444-5555-666666666666.jsonl"
        uuid_one.write_bytes(b"x" * 5000)
        explicit = tmp_path / "explicit.jsonl"
        explicit.write_bytes(b"x" * 3000)
        os.utime(str(newest), (2_000_000_000, 2_000_000_000))
        base = ["--project-root", str(project)]
        assert _score(base, tmp_path)[1] == 9
        assert _score(base, tmp_path, {"CLAUDE_CODE_SESSION_ID": UUID})[1] == 7
        both = base + ["--current-uuid", "22222222-3333-4444-5555-666666666666"]
        assert _score(both, tmp_path, {"CLAUDE_CODE_SESSION_ID": UUID})[1] == 5
        allthree = both + ["--transcript", str(explicit)]
        assert _score(allthree, tmp_path, {"CLAUDE_CODE_SESSION_ID": UUID})[1] == 3

    def test_malformed_env_uuid_ignored(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pdir = self._projects(tmp_path, project.resolve())
        (pdir / "a.jsonl").write_bytes(b"x" * 4000)
        proc = _run(["--project-root", str(project)], tmp_path, {"CLAUDE_CODE_SESSION_ID": "not-a-uuid"})
        assert proc.stdout.startswith("OK|4|")

    def test_missing_session_file_is_fail_open(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        self._projects(tmp_path, project.resolve())
        proc = _run(["--project-root", str(project)], tmp_path, {"CLAUDE_CODE_SESSION_ID": UUID})
        assert proc.stdout == "OK|0|\n"


def test_source_parses_with_python_38_grammar():
    ast.parse(GUARD.read_text(encoding="utf-8"), feature_version=(3, 8))


def _python38():
    exe = shutil.which("python3.8")
    if not exe:
        return None
    probe = subprocess.run([exe, "--version"], capture_output=True, text=True)
    return exe if probe.returncode == 0 and "3.8" in (probe.stdout + probe.stderr) else None


@pytest.mark.skipif(_python38() is None, reason="python3.8 not available")
def test_runs_under_python_38(tmp_path):
    f = _write(tmp_path / "t.jsonl", _tool_line("Agent"))
    proc = _run(["--transcript", str(f)], tmp_path, python=_python38())
    assert proc.returncode == 0 and proc.stdout.startswith("OK|5|")


# ---- parity with the shell scorer this replaces -----------------------------

def _old_lib(tmp_path):
    if shutil.which("git") is None or shutil.which("jq") is None:
        pytest.skip("git and jq are needed to materialise the old scorer")
    proc = subprocess.run(["git", "-C", str(TESTS_DIR), "show", f"{OLD_LIB_REF}:quoin/hooks/_lib.sh"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("reference commit not available in this checkout")
    lib = tmp_path / "old_lib.sh"
    lib.write_text(proc.stdout, encoding="utf-8")
    return lib


def _old_score(lib, transcript, tmp_path):
    env = dict(os.environ, TMPDIR=str(tmp_path / "oldtmp"))
    (tmp_path / "oldtmp").mkdir(exist_ok=True)
    out = subprocess.run(["sh", "-c", f'. "{lib}" && compute_pollution_score "{transcript}"'],
                         capture_output=True, text=True, env=env)
    return int(out.stdout.strip())


def _parity_files(tmp_path):
    files = []
    a = _write(tmp_path / "p1.jsonl", "")
    files.append(a)
    files.append(_write(tmp_path / "p2.jsonl", "".join([_tool_line("Bash")] * 30 + [_tool_line("Agent")] * 5)))
    filler = json.dumps({"type": "user", "message": {"content": "q" * 400}}) + "\n"
    files.append(_write(tmp_path / "p3.jsonl", filler * 300 + _tool_line("Read") * 7))
    multi = [{"type": "tool_use", "name": "Read"}, {"type": "tool_use", "name": "Agent"}]
    files.append(_write(tmp_path / "p4.jsonl", _tool_line("x", extra=multi) * 4 + _tool_line("Bash")))
    return files


def test_parity_with_old_shell_scorer(tmp_path):
    lib = _old_lib(tmp_path)
    for f in _parity_files(tmp_path):
        new = _score(["--transcript", str(f)], tmp_path)[1]
        assert new == _old_score(lib, f, tmp_path), f.name


def test_malformed_line_is_the_one_deliberate_difference(tmp_path):
    text = _tool_line("Agent") + "{broken \"tool_use\"\n" + _tool_line("Agent")
    f = _write(tmp_path / "m.jsonl", text)
    assert _score(["--transcript", str(f)], tmp_path)[1] == f.stat().st_size // 1000 + 10
