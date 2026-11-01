"""
Tests for the missing-EOD banner described in start_of_day/SKILL.md Step 1.

The banner fires if EITHER signal is positive:
  Signal A: insights file with Promote?: yes/maybe entries (existing check)
  Signal B: session-state files with end_of_day_due: yes written in last 36h

These tests simulate both signals using tmp fixtures.
"""
import pathlib
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Helpers that simulate the /start_of_day banner-check logic
# ---------------------------------------------------------------------------

def _make_insights_file(directory: pathlib.Path, date: str, n_yes: int, n_no: int) -> pathlib.Path:
    """Create a fixture insights file with promotable and non-promotable entries."""
    path = directory / f"insights-{date}.md"
    lines = []
    for i in range(n_yes):
        lines.append(f"## Insight {i}\nSome observation.\nPromote?: yes\n")
    for i in range(n_no):
        lines.append(f"## Non-insight {i}\nSkip this.\nPromote?: no\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _make_session_file(
    directory: pathlib.Path, name: str, due: bool, date_prefix: str = ""
) -> pathlib.Path:
    """Create a fixture session-state file with end_of_day_due set.

    If date_prefix (YYYY-MM-DD) is given, the filename is prefixed
    "<date_prefix>-<name>.md" per the real "<date>-<task>.md" session
    filename convention (T-04 same-day/cross-day branching keys on this).
    """
    fname = f"{date_prefix}-{name}.md" if date_prefix else f"{name}.md"
    path = directory / fname
    value = "yes" if due else "no"
    path.write_text(
        f"---\n## Status\nin_progress\n## Cost\n- end_of_day_due: {value}\n",
        encoding="utf-8",
    )
    return path


def _session_date_prefix(path: pathlib.Path) -> str:
    """Extract the YYYY-MM-DD date prefix from a session filename, or '' if absent."""
    stem = path.stem
    prefix = stem[:10]
    if len(prefix) == 10 and prefix[4] == "-" and prefix[7] == "-":
        return prefix
    return ""


def _check_banner(
    daily_dir: pathlib.Path,
    sessions_dir: pathlib.Path,
    yesterday: str,
    now: datetime,
    window_hours: int = 36,
    today: str = "",
) -> tuple:
    """Simulate /start_of_day banner check. Returns (fires, n, m, all_today).

    all_today is None when m == 0 (no date to branch on, T-04 / Round 2 MIN-2);
    otherwise True iff every flagged session's filename date prefix equals `today`
    (an ISO string compare, matching the SKILL.md Step 1a computation), False if
    any flagged session predates today.
    """
    if not today:
        today = now.strftime("%Y-%m-%d")

    # Signal A: insights file
    insights_path = daily_dir / f"insights-{yesterday}.md"
    n = 0
    if insights_path.exists():
        for line in insights_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("Promote?:") and (
                "yes" in line or "maybe" in line
            ):
                n += 1

    # Signal B: session files with end_of_day_due: yes written in last window_hours
    m = 0
    all_today = None
    cutoff = now - timedelta(hours=window_hours)
    for p in sessions_dir.glob("*.md"):
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if "end_of_day_due: yes" in line:
                m += 1
                prefix = _session_date_prefix(p)
                is_today = prefix == today
                all_today = is_today if all_today is None else (all_today and is_today)
                break

    fires = n > 0 or m > 0
    return fires, n, m, all_today


# ---------------------------------------------------------------------------
# Test parametrize over the four signal-presence cases
# ---------------------------------------------------------------------------

@pytest.fixture
def dirs(tmp_path):
    daily = tmp_path / "daily"
    sessions = tmp_path / "sessions"
    daily.mkdir()
    sessions.mkdir()
    return daily, sessions


YESTERDAY = "2026-04-29"
NOW = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)


TODAY = "2026-04-30"


def test_signal_a_only(dirs):
    """Signal A only: insights file with yes entries, no session end_of_day_due: yes.

    T-04 / Round 2 MIN-2: with no flagged session, there is no date to branch on —
    all_today must be None (the SKILL.md banner keeps its unconditional /end_of_day
    wording in this case, not a same-day/cross-day split).
    """
    daily, sessions = dirs
    _make_insights_file(daily, YESTERDAY, n_yes=2, n_no=1)
    # No session files with due: yes
    _make_session_file(sessions, "session-no", due=False)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW)
    assert fires is True
    assert n == 2
    assert m == 0
    assert all_today is None


def test_signal_b_only(dirs):
    """Signal B only: session files with end_of_day_due: yes, no insights."""
    daily, sessions = dirs
    # No insights file
    _make_session_file(sessions, "session-1", due=True)
    _make_session_file(sessions, "session-2", due=True)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW)
    assert fires is True
    assert n == 0
    assert m == 2


def test_both_signals(dirs):
    """Both signals: banner fires and counts both."""
    daily, sessions = dirs
    _make_insights_file(daily, YESTERDAY, n_yes=1, n_no=0)
    _make_session_file(sessions, "session-1", due=True)
    _make_session_file(sessions, "session-2", due=True)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW)
    assert fires is True
    assert n == 1
    assert m == 2


def test_neither_signal(dirs):
    """No signal: banner must NOT fire."""
    daily, sessions = dirs
    # All sessions have end_of_day_due: no
    _make_session_file(sessions, "session-done-1", due=False)
    _make_session_file(sessions, "session-done-2", due=False)
    # No insights file
    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW)
    assert fires is False
    assert n == 0
    assert m == 0


# ---------------------------------------------------------------------------
# T-04: same-day/cross-day branch tests
# ---------------------------------------------------------------------------

def test_signal_b_same_day_only(dirs):
    """Every flagged session dated today -> all_today True (wrap-up reminder only)."""
    daily, sessions = dirs
    _make_session_file(sessions, "task-a", due=True, date_prefix=TODAY)
    _make_session_file(sessions, "task-b", due=True, date_prefix=TODAY)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW, today=TODAY)
    assert fires is True
    assert m == 2
    assert all_today is True


def test_signal_b_cross_day_present(dirs):
    """A flagged session predates today -> all_today False (recommend /end_of_day)."""
    daily, sessions = dirs
    _make_session_file(sessions, "task-a", due=True, date_prefix=YESTERDAY)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW, today=TODAY)
    assert fires is True
    assert m == 1
    assert all_today is False


def test_signal_b_mixed_dates(dirs):
    """One session today, one from a prior day -> all_today False (cross-day wins)."""
    daily, sessions = dirs
    _make_session_file(sessions, "task-a", due=True, date_prefix=TODAY)
    _make_session_file(sessions, "task-b", due=True, date_prefix=YESTERDAY)

    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW, today=TODAY)
    assert fires is True
    assert m == 2
    assert all_today is False


def test_signal_a_only_no_date_branch(dirs):
    """T-04 / Round 2 MIN-2: signal A alone never attempts a same-day/cross-day split."""
    daily, sessions = dirs
    _make_insights_file(daily, YESTERDAY, n_yes=1, n_no=0)
    # No flagged sessions at all
    fires, n, m, all_today = _check_banner(daily, sessions, YESTERDAY, NOW, today=TODAY)
    assert fires is True
    assert n == 1
    assert m == 0
    assert all_today is None
