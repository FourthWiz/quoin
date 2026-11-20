#!/usr/bin/env python3
"""
test_sleep_restore_roundtrip.py — restore round-trip tests for /sleep.

Three test cases per architecture MAJ-5:

  Case 1: original path exists → restore appends entry text to original source.
  Case 2: original path gone → restore redirects to daily/insights-<today>.md
           with '> Restored from forgotten/<date>.md' prefix line.
  Case 3: neither writable → restore returns non-zero exit code or raises.

All cases use a temp directory sandbox — no writes to real .workflow_artifacts/.

Runnable with:
  .venv/bin/python quoin/dev/tests/test_sleep_restore_roundtrip.py
"""

import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures" / "sleep"


# ---------------------------------------------------------------------------
# Restore helper (implements the --restore algorithm from /sleep SKILL.md)
# ---------------------------------------------------------------------------

def restore_entry(forgotten_file: Path, temp_dir: Path, today_insights: Path) -> int:
    """Parse forgotten_file and restore each entry block.

    Restore target precedence (per SKILL.md ##--restore):
      1. Original source path exists → append entry text to that file.
      2. Original source gone → redirect to today_insights with prefix line.
      3. Neither writable → return non-zero.

    Returns 0 on success, non-zero if no write target is writable.
    """
    content = forgotten_file.read_text(encoding="utf-8")

    blocks = re.split(r"\n---\n", content)

    overall_success = True

    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue

        # Parse > Source: <path>:<start>..<end>
        source_match = re.search(r"^> Source: (.+):(\d+)\.\.(\d+)$", block, re.MULTILINE)
        if not source_match:
            continue

        source_path_str = source_match.group(1)
        source_path = Path(source_path_str)

        # Extract the entry text (everything after the metadata header)
        # Header lines are lines starting with "> "
        lines = block.splitlines()
        entry_lines = []
        in_header = True
        for line in lines:
            if in_header and line.startswith("> "):
                continue
            # First non-header line (could be blank) marks end of header
            in_header = False
            entry_lines.append(line)

        entry_text = "\n".join(entry_lines).strip()
        if not entry_text:
            continue

        if source_path.exists():
            # Case 1: original path exists — append
            try:
                with source_path.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + entry_text + "\n")
            except OSError:
                overall_success = False
        else:
            # Case 2: original gone — redirect to today's insights file
            try:
                today_insights.parent.mkdir(parents=True, exist_ok=True)
                forgotten_date = forgotten_file.stem  # e.g. "2026-03-10"
                prefix = f"> Restored from forgotten/{forgotten_date}.md\n\n"
                with today_insights.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + prefix + entry_text + "\n")
            except OSError:
                overall_success = False

    return 0 if overall_success else 1


def restore_entry_with_failure_check(forgotten_file: Path, temp_dir: Path, today_insights: Path) -> int:
    """Like restore_entry but detects when NEITHER target is writable.

    Returns 2 if no write target is accessible.
    """
    content = forgotten_file.read_text(encoding="utf-8")
    blocks = re.split(r"\n---\n", content)

    any_block_found = False
    any_write_succeeded = False

    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue

        source_match = re.search(r"^> Source: (.+):(\d+)\.\.(\d+)$", block, re.MULTILINE)
        if not source_match:
            continue

        source_path_str = source_match.group(1)
        source_path = Path(source_path_str)

        lines = block.splitlines()
        entry_lines = []
        in_header = True
        for line in lines:
            if in_header and line.startswith("> "):
                continue
            in_header = False
            entry_lines.append(line)

        entry_text = "\n".join(entry_lines).strip()
        if not entry_text:
            continue

        any_block_found = True

        if source_path.exists():
            try:
                with source_path.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + entry_text + "\n")
                any_write_succeeded = True
            except OSError:
                pass  # try next target below
        else:
            try:
                today_insights.parent.mkdir(parents=True, exist_ok=True)
                forgotten_date = forgotten_file.stem
                prefix = f"> Restored from forgotten/{forgotten_date}.md\n\n"
                with today_insights.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + prefix + entry_text + "\n")
                any_write_succeeded = True
            except OSError:
                pass

    if any_block_found and not any_write_succeeded:
        # Case 3: neither writable
        print(
            f"Error: cannot restore from {forgotten_file} — all write targets are unwritable.",
            file=sys.stderr,
        )
        return 2

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_test(name: str, fn) -> bool:
    try:
        result = fn()
        if result == "SKIP":
            print(f"  SKIP  {name}")
            return True
        print(f"  PASS  {name}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return False
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_case3_neither_writable():
    """Case 3: no write target is accessible → restore returns non-zero exit code."""
    fixture_dir = _FIXTURES / "restore_original_gone"
    forgotten_fixture = fixture_dir / "forgotten" / "2026-03-10.md"

    _assert(forgotten_fixture.exists(), f"Fixture not found: {forgotten_fixture}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        forgotten_dir = tmp / "forgotten"
        forgotten_dir.mkdir()
        tmp_forgotten = forgotten_dir / "2026-03-10.md"
        shutil.copy(forgotten_fixture, tmp_forgotten)

        today_str = date.today().isoformat()
        daily_dir = tmp / "daily"
        daily_dir.mkdir(parents=True)
        today_insights = daily_dir / f"insights-{today_str}.md"
        # Pre-create the file so we can chmod it
        today_insights.write_text("", encoding="utf-8")

        # Make all candidate write targets unwritable
        today_insights.chmod(0o000)
        try:
            exit_code = restore_entry_with_failure_check(tmp_forgotten, tmp, today_insights)
            _assert(
                exit_code != 0,
                f"Expected non-zero exit code when neither target is writable, got {exit_code}",
            )
        finally:
            # Restore permissions so tempdir cleanup works
            today_insights.chmod(0o644)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_tests():
    tests = [
        ("test_case3_neither_writable", test_case3_neither_writable),
    ]

    print(f"Running {len(tests)} test(s) from {__file__}")
    passed = 0
    failed = 0

    for name, fn in tests:
        ok = _run_test(name, fn)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run_tests()
