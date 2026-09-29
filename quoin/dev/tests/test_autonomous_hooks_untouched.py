"""Hooks-untouched guard for autonomous mode — IVG-153, T-20(b).

Autonomous mode (`--autonomous` on `/run`) cooperates with the existing
hooks-driven session-lifecycle machinery (context-utilization advisories,
sentinel sweeps, worktree isolation) — it never edits it. This test asserts
that claim two ways:

  1. No hook mentions the mode: none of the scripts under `quoin/hooks/**`
     say "autonomous" anywhere — including `stop.sh`, the run-continuation
     hook added by ordinary development (IVG-280), which cooperates with an
     armed session purely through file sentinels and never names the mode
     it serves.
  2. The tunable `QUOIN_*_BPS` / threshold constants in `hooks/_lib.sh`
     (`read_constants()`) are byte-identical to their known pre-autonomous
     default values — a content check, not a hash, so a future intentional
     tuning of one constant fails loudly with a clear diff instead of a
     silent drift.

Also asserts no autonomous-related skill/script text instructs a WRITE
(as opposed to a read/source) against a path under `hooks/`.

FAILS-without-the-guard: if a future autonomous-mode change edited
`hooks/_lib.sh`'s thresholds (e.g. to silence advisories during unattended
runs instead of cooperating with them), this test would catch the changed
constant value or the new "autonomous" mention in the hooks dir.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
HOOKS_DIR = PKG_DIR / "hooks"
LIB_SH = HOOKS_DIR / "_lib.sh"
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
SCRIPTS_DIR = PKG_DIR / "scripts"
CORE_SKILLS_DIR = PKG_DIR / "core" / "skills"
MEMORY_DIR = PKG_DIR / "memory"

# Known pre-autonomous default values for hooks/_lib.sh:read_constants().
# Sourced from the read_constants() body — verified against source 2026-07-20,
# unchanged by this branch (git diff of quoin/hooks/ vs. the branch base is
# empty as of this writing).
EXPECTED_CONSTANTS = {
    "QUOIN_BYTES_PER_TOKEN": "8.0",
    "QUOIN_EFFECTIVE_CONTEXT_LIMIT": "150000",
    "QUOIN_STOP_BPS": "7000",
    "QUOIN_BLOCK_BPS": "9500",
    "QUOIN_COMPACT_FIRST_BPS": "9000",
    "QUOIN_PANIC_BPS": "10000",
}

# Pinned thirteen-name roster of every `${QUOIN_*:-...}` env-var override read by
# read_constants() — a superset of EXPECTED_CONSTANTS above (which only pins
# the six BPS/threshold-shaped defaults). Used by
# test_read_constants_env_var_roster_unchanged() to catch an added or
# removed constant via set-equality, not just a value change on an
# already-known name.
READ_CONSTANTS_ENV_VAR_ROSTER = frozenset(
    {
        "QUOIN_BYTES_PER_TOKEN",
        "QUOIN_EFFECTIVE_CONTEXT_LIMIT",
        "QUOIN_STOP_BPS",
        "QUOIN_BLOCK_BPS",
        "QUOIN_STALE_SENTINEL_DAYS",
        "QUOIN_SESSIONSTART_SWEEP_DAYS",
        "QUOIN_COMPACT_FIRST_BPS",
        "QUOIN_PANIC_BPS",
        "QUOIN_DISCOVERY_STALE_DAYS",
        "QUOIN_SERENA_STALE_DAYS",
        "QUOIN_RUN_STATE_STALE_DAYS",
        "QUOIN_PRECOMPACT_NORUN_CHECKPOINT",
        "QUOIN_TELEMETRY_MAX_BYTES",
    }
)


@pytest.fixture(scope="module")
def lib_sh_text() -> str:
    assert LIB_SH.exists(), f"hooks/_lib.sh not found at {LIB_SH}"
    return LIB_SH.read_text(encoding="utf-8")


def test_hooks_dir_has_expected_files() -> None:
    """Sanity: the hooks dir exists and has the known 8-script roster (`stop.sh`
    is the run-continuation hook added by ordinary development, IVG-280 — no
    hook script was added by autonomous work, no existing one was removed).
    Non-script housekeeping files (e.g. `.keep`) are ignored."""
    assert HOOKS_DIR.is_dir()
    names = sorted(p.name for p in HOOKS_DIR.glob("*.sh"))
    expected = sorted(
        [
            "_lib.sh",
            "postcompact.sh",
            "precompact.sh",
            "sessionend.sh",
            "sessionstart.sh",
            "stop.sh",
            "userpromptsubmit.sh",
            "worktreecreate.sh",
        ]
    )
    assert names == expected, f"hooks/ script roster changed: {names}"


def test_no_hook_file_mentions_autonomous() -> None:
    """No hook script was edited to add autonomous-specific logic — the
    autonomous plan's entire scope is SKILL.md/scripts/memory files."""
    offenders = []
    for path in sorted(HOOKS_DIR.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        if "autonomous" in text.lower():
            offenders.append(path.name)
    assert not offenders, (
        f"hooks file(s) mention 'autonomous' — hooks must never be edited by "
        f"autonomous-mode work: {offenders}"
    )


def test_bps_threshold_constants_unchanged(lib_sh_text: str) -> None:
    """Every QUOIN_*_BPS / threshold constant in read_constants() is
    byte-identical to its known pre-autonomous default."""
    text = lib_sh_text
    for name, expected_default in EXPECTED_CONSTANTS.items():
        # Constants are consumed as `<VAR>=${QUOIN_FOO_BAR:-<default>}`, e.g.
        # `BPT=${QUOIN_BYTES_PER_TOKEN:-8.0}` — the QUOIN_* name is the env-var
        # override token inside `${...:-...}`, not the local shell variable.
        pattern = re.compile(r"\$\{" + re.escape(name) + r":-([^}]+)\}")
        match = pattern.search(text)
        assert match, f"constant {name} not found (or its shape changed) in hooks/_lib.sh"
        actual_default = match.group(1)
        assert actual_default == expected_default, (
            f"hooks/_lib.sh constant {name} default changed: "
            f"expected {expected_default!r}, found {actual_default!r} — "
            "autonomous mode must never modify hook thresholds"
        )


def test_read_constants_env_var_roster_unchanged(lib_sh_text: str) -> None:
    """Set-equality guard: assert the exact set of `${QUOIN_*:-...}` env-var
    override names inside read_constants() equals the pinned thirteen-name roster
    below. This is strictly stronger than the `_BPS`/`BPS`/`threshold`
    substring tokens dropped from the git-diff corroboration test's
    forbidden-token regex (see the comment on that test explaining why those
    tokens were dropped): a substring test on the diff cannot catch an
    *added* threshold constant or override (e.g. a hypothetical
    `QUOIN_SUPPRESS_ADVISORY`) because appending a new variable to the
    export line still passes a substring check. Set-equality does catch it —
    any name added to or removed from read_constants() fails this test
    immediately, and it does not false-positive on comments (only the
    `${QUOIN_*:-...}` parameter-expansion shape is matched, not free text)."""
    # Isolate the read_constants() function body so a `${QUOIN_*:-...}`
    # reference elsewhere in the file (outside this function) is not folded
    # into the roster by accident.
    func_match = re.search(
        r"read_constants\(\)\s*\{(.*?)\n\}", lib_sh_text, re.DOTALL
    )
    assert func_match, "read_constants() function body not found in hooks/_lib.sh"
    func_body = func_match.group(1)
    found_names = set(re.findall(r"\$\{(QUOIN_[A-Z_]+):-", func_body))
    expected_names = set(READ_CONSTANTS_ENV_VAR_ROSTER)
    assert found_names == expected_names, (
        f"read_constants() env-var roster changed: found {sorted(found_names)}, "
        f"expected {sorted(expected_names)} — a threshold constant was added, "
        "removed, or renamed without updating this guard"
    )


def test_read_constants_function_present_and_unmodified_shape(lib_sh_text: str) -> None:
    """The read_constants() function itself still exists with its documented
    export line — a coarse structural check that the function wasn't
    restructured (e.g. to bypass env-var overrides) by autonomous work."""
    text = lib_sh_text
    assert "read_constants() {" in text
    assert (
        "export BPT LIMIT STOP_BPS BLOCK_BPS STALE_DAYS SESSIONSTART_SWEEP_DAYS "
        "COMPACT_FIRST_BPS PANIC_BPS DISCOVERY_STALE_DAYS SERENA_STALE_DAYS" in text
    )


# ─── No autonomous-tagged text co-occurs with a hooks/ reference ────────────

# Files in scope for the autonomous plan (the 15-skill transitive spawn set
# plus the generator scripts and memory doc T-20 is meant to guard).
AUTONOMOUS_TAGGED_ROOTS = [ADAPTER_SKILLS_DIR, SCRIPTS_DIR, CORE_SKILLS_DIR, MEMORY_DIR]


def test_no_autonomous_line_references_hooks_dir() -> None:
    """Line-level co-occurrence check: no line that mentions "autonomous"
    (the sentinel, the state flag, or prose describing autonomous behavior)
    also mentions `hooks/` anywhere in the autonomous-tagged surface. This is
    deliberately coarse (co-occurrence, not just writes) — the autonomous
    plan's entire scope is SKILL.md/scripts/memory text, and it should never
    even DISCUSS touching hooks/, let alone write to it. Pre-existing hooks/
    references in these files (e.g. `. __QUOIN_HOME__/hooks/_lib.sh` sourcing
    in checkpoint/cleanup/implement/end_of_task, or hooks-table.md doc links)
    are unrelated to autonomous mode and never share a line with
    "autonomous" — verified true as of this writing."""
    offenders: list[str] = []
    for root in AUTONOMOUS_TAGGED_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")) + sorted(root.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "autonomous" in line.lower() and "hooks/" in line:
                    offenders.append(f"{path.relative_to(PKG_DIR)}:{lineno}: {line.strip()!r}")
    assert not offenders, (
        f"found autonomous-tagged line(s) that also reference hooks/ — "
        f"autonomous mode must never touch the hooks dir: {offenders}"
    )


def test_hooks_dir_untouched_git_diff_if_available() -> None:
    """Best-effort structural corroboration: if this checkout is a git repo
    with a resolvable merge-base against origin/main or main, confirm the
    quoin/hooks/ diff vs. that base introduces no *autonomous-mode / unattended
    approval-gate* logic (the test's actual intent — see module docstring).

    Legitimate NON-autonomous hook edits on a feature branch (e.g. the IVG-158
    S-04 opt-in workspace heartbeat block in sessionstart.sh) are allowed. What
    is NOT allowed is any added hook line that reintroduces the very machinery
    this guard exists to keep out: autonomous/unattended-run gating, approval
    bypasses, or advisory silencing. We reject a BROAD forbidden-token set
    rather than only the literal word "autonomous", so equivalent gating logic
    phrased without that word (e.g. an `UNATTENDED` env gate) is still caught
    here. Context-utilization threshold constants are guarded separately by
    the two literal-pin tests (test_bps_threshold_constants_unchanged() above
    and test_lib_thresholds_unchanged.py), which assert the live values rather
    than merely the absence of the token. Skips (does not fail) when git or a
    base ref is unavailable — the content-check tests above are the primary,
    environment-independent guard."""
    import subprocess

    repo_root = PKG_DIR.parent  # quoin/ (git root)
    try:
        base = None
        for ref in ("origin/main", "main", "origin/master", "master"):
            probe = subprocess.run(
                ["git", "merge-base", "HEAD", ref],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                base = probe.stdout.strip()
                break
        if base is None:
            pytest.skip("no resolvable base ref for git diff corroboration")

        diff = subprocess.run(
            ["git", "diff", base, "HEAD", "--", "quoin/hooks/"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if diff.returncode != 0:
            pytest.skip("git diff failed; skipping best-effort corroboration")
        # Intent (module docstring): autonomous/unattended work must not sneak
        # gating, approval-bypass, advisory-silencing, or threshold-tuning logic
        # into the hooks dir. Rather than requiring a byte-for-byte untouched
        # hooks dir (which wrongly rejects unrelated legitimate hook edits like
        # the S-04 heartbeat), reject any ADDED line matching a broad forbidden-
        # token set — so equivalent logic phrased without the literal word
        # "autonomous" is still caught.
        # IVG-258 S-2: the `_BPS`/`BPS`/`threshold` alternatives are dropped. They are
        # genuinely subsumed by the four literal pins in
        # test_bps_threshold_constants_unchanged() above and by
        # test_lib_thresholds_unchanged.py, both of which assert the LIVE VALUES rather
        # than merely the absence of the token. Keeping them here would make every
        # legitimate hook edit that so much as NAMES a threshold in a comment fail this
        # guard. `approv` and `silence` are KEPT: nothing else in the suite guards their
        # intent.
        _FORBIDDEN = re.compile(
            r"autonomous|unattend|approv|silence",
            re.IGNORECASE,
        )
        added_forbidden = [
            ln for ln in diff.stdout.splitlines()
            if ln.startswith("+") and not ln.startswith("+++") and _FORBIDDEN.search(ln)
        ]
        assert not added_forbidden, (
            "autonomous/unattended-gating or threshold-tuning logic was "
            "introduced into quoin/hooks/ vs base:\n" + "\n".join(added_forbidden)
        )
    except FileNotFoundError:
        pytest.skip("git binary not available")
