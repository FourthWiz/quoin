"""Instruction files must not tell an agent to run shell rm, rmdir or mv.

The project's shell deny rules block `rm`, `rmdir` and `mv`, so a skill or memory
document that instructs the agent to run them stalls on a permission prompt.
File deletes and moves go through `python3 <quoin-home>/scripts/fsops.py`.

`scan_text` looks for a shell `rm`/`rmdir`/`mv` command at the start of any
command segment in three modes: inside fenced code blocks, inside inline code
spans, and in plain prose after a `Run:`/`run:`/`$` lead-in (or as the whole
line). Segments are split on `&&`, `||`, `;`, `|` and `(`. Leading shell
keywords (`then`, `do`, `xargs`, ...) are skipped before the first-word test.
The text `Bash(rm:*)` (a permission rule, not a command) is never flagged.

Two allowlists cover text that names the commands without telling the agent to
run them: commands a human types, and descriptions of hook or human behavior.
"""
from __future__ import annotations

import re
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

Finding = namedtuple("Finding", "relpath line mode raw segment")

COMMANDS = {"rm", "rmdir", "mv"}
KEYWORDS = {"then", "do", "else", "elif", "!", "{", "command", "exec", "xargs", "sudo", "env", "time"}
_SEPARATORS = re.compile(r"&&|\|\||;|\||\(")
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_LEAD_IN = re.compile(r"^(?:Run:|run:|\$)\s+(.*)$")

# (relative path, literal that appears in the flagged line)
HUMAN_RUN_COMMAND_HINTS: tuple = (
    (
        "quoin/memory/lifecycle-guide.md",
        "`mv .workflow_artifacts/memory/trash/<date>/<file> .workflow_artifacts/memory/`",
    ),
    (
        "quoin/memory/lifecycle-guide.md",
        "`mv .workflow_artifacts/trash/<date>/<task> .workflow_artifacts/`",
    ),
)
DESCRIBES_HOOK_OR_HUMAN_BEHAVIOR: tuple = (
    ("quoin/memory/checkpoint-spec.md", "deleted (`rm -f`)"),
)


def _split(text: str):
    """Yield (segment, previous separator, text before that separator)."""
    prev_sep = ""
    before = ""
    start = 0
    for match in _SEPARATORS.finditer(text):
        yield text[start:match.start()], prev_sep, before
        prev_sep = match.group(0)
        before = text[:match.start()]
        start = match.end()
    yield text[start:], prev_sep, before


def _segment_is_shell_command(seg: str, prev_sep: str, before: str) -> bool:
    if prev_sep == "(" and before.rstrip().endswith("Bash"):
        return False
    toks = seg.split()
    skipped = False
    while toks and toks[0] in KEYWORDS:
        toks = toks[1:]
        skipped = True
    return bool(toks) and toks[0] in COMMANDS and (len(toks) >= 2 or skipped)


def scan_text(relpath: str, text: str) -> list:
    findings = []
    seen = set()
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = _FENCE.match(stripped)
        if marker and fence is None:
            fence = marker.group(1)
            continue
        if (
            marker
            and fence is not None
            and stripped == marker.group(1)
            and marker.group(1)[0] == fence[0]
            and len(marker.group(1)) >= len(fence)
        ):
            fence = None
            continue
        if fence is not None:
            candidates = [("fenced", stripped)]
        else:
            candidates = [("inline", span) for span in re.findall(r"`([^`\n]+)`", line)]
            plain = re.sub(r"`[^`\n]*`", " ", line).strip()
            candidates.append(("plain", plain))
            lead = _LEAD_IN.match(plain)
            if lead:
                candidates.append(("plain", lead.group(1)))
        for mode, candidate in candidates:
            for seg, prev_sep, before in _split(candidate):
                if not _segment_is_shell_command(seg, prev_sep, before):
                    continue
                key = (number, seg.strip())
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(relpath, number, mode, line, seg.strip()))
    return findings


def corpus_files() -> list:
    patterns = (
        "quoin/adapters/claude/skills/*/SKILL.md",
        "quoin/skills/*/SKILL.md",
        "quoin/core/skills/*.md",
        "quoin/memory/*.md",
    )
    files = set()
    for pattern in patterns:
        files.update(REPO_ROOT.glob(pattern))
    return sorted(files)


def scan_corpus(files=None) -> list:
    out = []
    for path in files if files is not None else corpus_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        out.extend(scan_text(rel, path.read_text(encoding="utf-8")))
    return out


# ---------------------------------------------------------------- scanner self-tests

_FLAGGED = [
    "```\nrm -f x\n```",
    "Delete it with `rm -f x` now.",
    "rm -f x",
    "Run: rm -f x",
    "a; rm -f x",
    "a && mv a b",
    "(rm -f x)",
    "`if a; then rm x; fi`",
    "`for f in *; do rm x; done`",
    "`{ rm x; }`",
    "`ls | xargs rm`",
    "````\n```\nrm -f y\n````\nlater `rm -f z`",
]

_NOT_FLAGGED = [
    "`rm`",
    "`mv`",
    '"Bash(rm:*)",',
    '"Bash(rm -rf:*)",',
    '"Bash(rmdir:*)",',
    "(recover: mv a b)",
    "python3 fsops.py rm x",
    "trash_move x y",
    "skip the move",
    '[ -L x ] && python3 fsops.py rm x',
    '`python3 __QUOIN_HOME__/scripts/fsops.py rm "P.body.tmp" "P.tmp"`',
    '`python3 __QUOIN_HOME__/scripts/fsops.py finalize "P.tmp" "P" --cleanup "P.body.tmp" "P.tmp"`',
    '[ "$_ERR" != "/dev/null" ] && python3 __QUOIN_HOME__/scripts/fsops.py rm "$_ERR"',
    '#   [ "$_ERR" != "/dev/null" ] && python3 __QUOIN_HOME__/scripts/fsops.py rm "$_ERR"',
]


def test_scanner_flags_each_shell_form():
    for text in _FLAGGED:
        found = scan_text("x.md", text)
        if text.startswith("````"):
            assert len(found) == 2, (text, found)
        else:
            assert found, text


def test_scanner_ignores_helper_calls_and_lookalikes():
    for text in _NOT_FLAGGED:
        assert scan_text("x.md", text) == [], text


def test_four_backtick_fence_is_not_closed_by_a_triple_fence():
    text = "````\n```\nrm -f y\n````\nlater `rm -f z`"
    assert [f.segment for f in scan_text("x.md", text)] == ["rm -f y", "rm -f z"]


def test_bare_keyword_led_command_is_flagged_without_arguments():
    assert scan_text("x.md", "`ls | xargs rm`")


def test_corpus_is_large_and_includes_known_files():
    files = {p.relative_to(REPO_ROOT).as_posix() for p in corpus_files()}
    assert len(files) >= 30
    assert "quoin/adapters/claude/skills/plan/SKILL.md" in files
    assert "quoin/memory/cost-ledger-format.md" in files


# ---------------------------------------------------------------- corpus checks


def _allowlisted(finding) -> bool:
    return any(
        finding.relpath == rel and literal in finding.raw
        for rel, literal in HUMAN_RUN_COMMAND_HINTS + DESCRIBES_HOOK_OR_HUMAN_BEHAVIOR
    )


def test_instruction_corpus_has_no_shell_rm_mv():
    left = [f for f in scan_corpus() if not _allowlisted(f)]
    assert not left, "shell rm/rmdir/mv in instruction files:\n" + "\n".join(
        f"{f.relpath}:{f.line}: {f.segment}" for f in left
    )


def test_allowlist_entries_are_live():
    findings = scan_corpus()
    for rel, literal in HUMAN_RUN_COMMAND_HINTS + DESCRIBES_HOOK_OR_HUMAN_BEHAVIOR:
        path = REPO_ROOT / rel
        assert path.exists(), f"allowlist file missing: {rel}"
        assert literal in path.read_text(encoding="utf-8"), f"allowlist literal gone: {rel}: {literal}"
        assert any(f.relpath == rel and literal in f.raw for f in findings), (
            f"stale allowlist entry (no finding uses it): {rel}: {literal}"
        )


def test_appending_a_shell_rm_to_any_scanned_file_is_caught():
    for path in corpus_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        assert len(scan_text(rel, text + "\nrm -f x\n")) == len(scan_text(rel, text)) + 1, rel
