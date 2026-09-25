"""
Structural invariant tests for quoin/skills/cleanup/SKILL.md.

Tests verifying that the /cleanup SKILL.md satisfies the structural
contracts: Sonnet tier declaration, §0/§0‴/§0c preamble presence and
ordering, pidfile_acquire/release call sites, write-restriction prose,
sentinel families, current-session preservation with UUID-before-age
ordering, UUID-unavailable fail-safe, and recovery instruction safety.

Per Stage 1 plan D-03 and lesson 2026-04-23 LLM-replay non-determinism:
this file contains NO live LLM calls — only deterministic pathlib + string
parsing.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent
ADAPTER_SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"
CLEANUP_SKILL = ADAPTER_SKILLS_DIR / "cleanup" / "SKILL.md"

DEPLOYED_CLEANUP_SKILL = Path.home() / ".claude" / "skills" / "cleanup" / "SKILL.md"


def _lines() -> list[str]:
    return CLEANUP_SKILL.read_text(encoding="utf-8").splitlines()


def _text() -> str:
    return CLEANUP_SKILL.read_text(encoding="utf-8")


def _line_index(lines: list[str], fragment: str) -> int | None:
    """Return 0-based index of the first line containing `fragment`, or None."""
    for i, ln in enumerate(lines):
        if fragment in ln:
            return i
    return None


# ── 1. Frontmatter declares model: sonnet ────────────────────────────────────

def test_model_declared_sonnet():
    text = _text()
    lines = text.splitlines()
    assert lines[0].strip() == "---", "SKILL.md does not start with YAML frontmatter '---'"
    end_idx = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    assert end_idx is not None, "SKILL.md frontmatter closing '---' not found"
    frontmatter = "\n".join(lines[1:end_idx])
    assert "model: sonnet" in frontmatter, (
        f"cleanup/SKILL.md frontmatter does not declare 'model: sonnet'. "
        f"Frontmatter content:\n{frontmatter}"
    )


# ── 2. §0 Model dispatch heading is present ───────────────────────────────────

def test_sec0_present():
    text = _text()
    assert "## §0 Model dispatch" in text, (
        "cleanup/SKILL.md is missing the '## §0 Model dispatch' heading. "
        "Cheap-tier skills must carry the §0 cost-guardrail block as their first body H2."
    )


# ── 3. §0 is the first body H2 after H1 ──────────────────────────────────────

def test_sec0_is_first_body_h2():
    lines = _lines()
    h1_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ") and not ln.startswith("## ")), None)
    assert h1_idx is not None, "cleanup/SKILL.md is missing an H1 heading"
    # Find first ## heading after H1
    first_h2_idx = next(
        (i for i, ln in enumerate(lines) if i > h1_idx and ln.startswith("## ")),
        None,
    )
    assert first_h2_idx is not None, "cleanup/SKILL.md has no H2 after H1"
    assert "§0 Model dispatch" in lines[first_h2_idx], (
        f"First body H2 after H1 (line {first_h2_idx+1}) is not '## §0 Model dispatch'. "
        f"Found: '{lines[first_h2_idx]}'. "
        "The §0 cost-guardrail block must be the first body H2."
    )


# ── 4. §0c Pidfile lifecycle heading is present ───────────────────────────────

def test_sec0c_present():
    text = _text()
    assert "## §0c Pidfile lifecycle" in text, (
        "cleanup/SKILL.md is missing the '## §0c Pidfile lifecycle' heading."
    )


# ── 5. §0c heading appears after §0 heading ──────────────────────────────────

def test_sec0c_after_sec0():
    lines = _lines()
    sec0_idx = _line_index(lines, "## §0 Model dispatch")
    sec0c_idx = _line_index(lines, "## §0c Pidfile lifecycle")
    assert sec0_idx is not None, "cleanup/SKILL.md missing '## §0 Model dispatch'"
    assert sec0c_idx is not None, "cleanup/SKILL.md missing '## §0c Pidfile lifecycle'"
    assert sec0c_idx > sec0_idx, (
        f"cleanup/SKILL.md: §0c (line {sec0c_idx+1}) must appear AFTER §0 (line {sec0_idx+1})."
    )


# ── 5b. §0‴ Minimum-tier guard sits strictly between §0 and §0c ──────────────

def test_sec0tripleprime_between_sec0_and_sec0c():
    lines = _lines()
    sec0_idx = _line_index(lines, "## §0 Model dispatch")
    sec0tp_idx = _line_index(lines, "## §0‴ Minimum-tier guard")
    sec0c_idx = _line_index(lines, "## §0c Pidfile lifecycle")
    assert sec0_idx is not None, "cleanup/SKILL.md missing '## §0 Model dispatch'"
    assert sec0tp_idx is not None, "cleanup/SKILL.md missing '## §0‴ Minimum-tier guard'"
    assert sec0c_idx is not None, "cleanup/SKILL.md missing '## §0c Pidfile lifecycle'"
    assert sec0_idx < sec0tp_idx < sec0c_idx, (
        f"cleanup/SKILL.md: expected order §0 (line {sec0_idx+1}) < §0‴ "
        f"(line {sec0tp_idx+1}) < §0c (line {sec0c_idx+1})."
    )


# ── 6. pidfile_acquire cleanup call site present ──────────────────────────────

def test_pidfile_acquire_cleanup():
    text = _text()
    assert "pidfile_acquire cleanup" in text, (
        "cleanup/SKILL.md does not contain 'pidfile_acquire cleanup'. "
        "The §0c pidfile lifecycle block must call pidfile_acquire with the skill name."
    )


# ── 7. pidfile_release cleanup call site present ──────────────────────────────

def test_pidfile_release_cleanup():
    text = _text()
    assert "pidfile_release cleanup" in text, (
        "cleanup/SKILL.md does not contain 'pidfile_release cleanup'. "
        "The §0c pidfile lifecycle block must call pidfile_release with the skill name."
    )


# ── 8. Write-target / delete-target restriction uses "ONLY" ──────────────────

def test_write_target_restriction_only():
    text = _text()
    assert "ONLY" in text, (
        "cleanup/SKILL.md does not contain 'ONLY'. "
        "The write-target restriction section must explicitly state 'ONLY trash-moves files under ...'."
    )
    assert "never touches" in text.lower() or "never touch" in text.lower(), (
        "cleanup/SKILL.md should state that it never touches lessons-learned.md or forgotten/."
    )


# ── 9. trash-move prose present ──────────────────────────────────────────────

def test_trash_move_present():
    text = _text()
    assert "trash_move" in text or "trash-move" in text, (
        "cleanup/SKILL.md does not contain 'trash_move' or 'trash-move'. "
        "The skill must document use of the trash_move helper."
    )


# ── 10. Current-session preservation with UUID-before-age ordering ────────────

def test_uuid_before_age_check():
    """UUID check must precede any age check for sentinel sweep (T-07 STRENGTHEN MIN-1)."""
    text = _text()
    # Must mention current/freshest session preservation
    assert any(phrase in text for phrase in [
        "current/freshest session",
        "freshest/current session",
        "current session",
    ]), (
        "cleanup/SKILL.md does not contain current-session preservation language."
    )
    # Must state UUID check happens before age check
    assert any(phrase in text for phrase in [
        "UUID check FIRST",
        "UUID check happens BEFORE",
        "before any age check",
        "BEFORE any age check",
    ]), (
        "cleanup/SKILL.md must explicitly state UUID check is performed BEFORE any age check "
        "in the sentinel sweep (current-session-preservation ordering invariant)."
    )


# ── 11. UUID-unavailable → skip sentinel sweep (fail-safe wording) ────────────

def test_uuid_unavailable_skip_sentinel_sweep():
    text = _text()
    # Must say skip the sentinel sweep (not fall back to age-only floor)
    assert any(phrase in text for phrase in [
        "skip sentinel sweep",
        "skip step 4",
        "skip the sentinel sweep",
        "skipping sentinel sweep",
    ]), (
        "cleanup/SKILL.md must explicitly state that when UUID is unavailable, "
        "the sentinel sweep is SKIPPED entirely (fail-safe). "
        "Must not fall back to age-only sweep for sentinels."
    )


# ── 12. Recovery instruction uses manual mv, NOT /sleep --restore ─────────────

def test_recovery_is_manual_mv_not_sleep_restore():
    text = _text()
    # Must mention trash/<date>/ and mv
    assert "trash/" in text, (
        "cleanup/SKILL.md does not mention 'trash/' directory for recovery."
    )
    assert " mv " in text or "`mv " in text or "mv .workflow" in text, (
        "cleanup/SKILL.md does not mention 'mv' for recovery from trash/."
    )
    # Must NOT say "recoverable via /sleep --restore" for trash/ files
    # (This is a negative assertion — /sleep --restore only reads forgotten/)
    assert "/sleep --restore" not in text or (
        "NOT /sleep --restore" in text or
        "not /sleep --restore" in text.lower() or
        "NOT `/sleep --restore`" in text
    ), (
        "cleanup/SKILL.md should not claim recovery via '/sleep --restore'. "
        "/sleep --restore only reads forgotten/ text entries, not trash/ files."
    )


# ── 13. No literal ~/.claude/ paths ──────────────────────────────────────────

def test_no_literal_tilde_claude():
    text = _text()
    # grep for literal ~/.claude/ (tilde followed by .claude/)
    matches = re.findall(r"~/\.claude/", text)
    assert len(matches) == 0, (
        f"cleanup/SKILL.md contains {len(matches)} literal '~/.claude/' reference(s). "
        "All load-bearing paths must use '__QUOIN_HOME__' token instead."
    )


# ── 14. Deployed copy sync (requires install.sh to have run) ──────────────────

def test_deployed_copy_sync():
    print("  SKIP  test_deployed_copy_sync — requires install.sh to have run; verify via: diff quoin/skills/cleanup/SKILL.md ~/.claude/skills/cleanup/SKILL.md")


# ── 15. IVG-137 T-06: session temp-file sweep (sessions/*.body.tmp, *.tmp) ────

def test_session_temp_file_sweep_step_present():
    text = _text()
    assert "## Step 5b" in text or "Step 5b." in text, (
        "cleanup/SKILL.md is missing the Step 5b session temp-file sweep block (IVG-137 T-06)."
    )


def test_session_temp_file_globs_present():
    text = _text()
    assert "*.body.tmp" in text, (
        "cleanup/SKILL.md must reference the '*.body.tmp' glob for the session temp-file sweep."
    )
    assert "sessions" in text and "*.tmp" in text, (
        "cleanup/SKILL.md must reference the 'sessions/*.tmp' glob for the session temp-file sweep."
    )


def test_session_temp_file_sweep_never_touches_md():
    text = _text()
    assert "real `*.md`" in text or "no .md session file" in text.lower() or (
        "never touches" in text.lower() and "session" in text.lower()
    ), (
        "cleanup/SKILL.md must state the session temp-file sweep never touches real .md session files."
    )


def test_session_temp_file_sweep_uses_trash_move():
    """The Step 5b block itself must call trash_move (not just mention it elsewhere)."""
    lines = _lines()
    step5b_idx = _line_index(lines, "Step 5b")
    assert step5b_idx is not None, "cleanup/SKILL.md missing Step 5b heading"
    # trash_move should appear within a reasonable window after Step 5b's heading
    window = "\n".join(lines[step5b_idx:step5b_idx + 15])
    assert "trash_move" in window, (
        "cleanup/SKILL.md Step 5b must call trash_move for session temp-file candidates."
    )


def test_dry_run_mentions_session_temp_files():
    text = _text()
    assert "SESSION TEMP FILES" in text, (
        "cleanup/SKILL.md --dry-run report must include a SESSION TEMP FILES section (IVG-137 T-06)."
    )
