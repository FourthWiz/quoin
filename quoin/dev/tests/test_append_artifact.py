"""Behavior and exit-code tests for the artifact append helper.

Skills append to scratchpads, lesson logs and ledgers through this helper
instead of the editor's Write/Edit tools, so the contract is: no prior read,
header only for a new or empty file, newline repair, no lost rows under
concurrent writers, one `append_artifact: ` stderr line per failure.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "append_artifact.py"
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "append_artifact.py"


def _run(args, stdin=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(WRAPPER_PATH), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_creates_file_and_parents_with_header_from_stdin(tmp_path):
    dst = tmp_path / "with space" / "daily" / "insights.md"
    r = _run([str(dst), "--header", "# Title\n\n---"], stdin="\n## 10:00 — a\nbody\n")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert dst.read_text() == "# Title\n\n---\n\n## 10:00 — a\nbody\n"

    r = _run([str(dst), "--header", "# Title\n\n---", "--text", "## 11:00 — b"])
    assert r.returncode == 0
    assert dst.read_text().count("# Title") == 1
    assert dst.read_text().endswith("body\n## 11:00 — b\n")


def test_repairs_missing_trailing_newline(tmp_path):
    dst = tmp_path / "ledger.md"
    dst.write_text("row1")
    assert _run([str(dst), "--text", "row2"]).returncode == 0
    assert dst.read_text() == "row1\nrow2\n"


def test_blank_line_separates_entries_once(tmp_path):
    dst = tmp_path / "lessons-learned.md"
    dst.write_text("## a\n**Applies to:** x")
    assert _run([str(dst), "--blank-line"], stdin="\n## b\nbody\n").returncode == 0
    assert _run([str(dst), "--blank-line", "--text", "## c"]).returncode == 0
    assert dst.read_text() == "## a\n**Applies to:** x\n\n## b\nbody\n\n## c\n"


def test_concurrent_appends_keep_every_row(tmp_path):
    # Rows this small rarely interleave even without a lock; the single-header
    # assertion is what exercises the lock (every writer races to create it).
    dst = tmp_path / "cost-ledger.md"
    rows = ["row-{:03d} | {}".format(i, "x" * 200) for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda row: _run([str(dst), "--header", "# Ledger", "--text", row]), rows))
    assert all(r.returncode == 0 for r in results)
    lines = dst.read_text().splitlines()
    assert lines[0] == "# Ledger" and lines.count("# Ledger") == 1
    assert sorted(lines[1:]) == sorted(rows)


def test_undecodable_bytes_are_kept_verbatim(tmp_path):
    dst = tmp_path / "f.md"
    r = subprocess.run(
        [sys.executable, str(WRAPPER_PATH), str(dst)],
        input=b"bad \xff byte",
        capture_output=True,
    )
    assert r.returncode == 0 and r.stderr == b""
    assert dst.read_bytes() == b"bad \xff byte\n"


def test_parent_is_a_file_is_one_line_exit_1(tmp_path):
    blocker = tmp_path / "l.md"
    blocker.write_text("x")
    r = _run([str(blocker / "child.md"), "--text", "row"])
    assert r.returncode == 1 and r.stdout == ""
    lines = r.stderr.strip().splitlines()
    assert len(lines) == 1 and "parent is not a directory" in lines[0]


def test_usage_errors_are_one_line_exit_2(tmp_path):
    for args, stdin in (([str(tmp_path / "f.md"), "--text", ""], None), ([str(tmp_path)], "x")):
        r = _run(args, stdin=stdin)
        assert r.returncode == 2 and r.stdout == ""
        lines = r.stderr.strip().splitlines()
        assert len(lines) == 1 and lines[0].startswith("append_artifact: ")


def test_sources_parse_as_python_38():
    for path in (CORE_PATH, WRAPPER_PATH):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))
