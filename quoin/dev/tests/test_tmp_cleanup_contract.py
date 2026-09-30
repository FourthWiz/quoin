"""Static-assertion contract tests for temp-file handling in artifact writers.

The six Class B writers (plan, architect, review, revise, revise-fast,
security_review) write an artifact through `<path>.body.tmp` and `<path>.tmp`
and publish it with one `fsops.py finalize` call. The three Class A writers
(critic, gate, implement) publish with the same call. These tests fail if an
edit drops the pre-write sweep, removes the English-fallback cleanup, changes
the cleanup targets so they no longer match the destination, or brings back a
shell `rm`, `rmdir` or `mv` command.

The predicates below are shared by the real tests and by the synthetic
negative cases, so a predicate that stops rejecting bad text is caught.

Scope note: only command-start forms are treated as shell commands here.
Keyword-led forms (`if ...; then rm x; fi`, `xargs rm`) and prose are covered
by the instruction lint in test_no_shell_rm_mv_in_instructions.py.
"""

import pathlib
import re

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / "quoin" / "skills"
ADAPTER_SKILLS_DIR = PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills"
MIGRATED_SKILLS_DIR_OVERRIDES = {
    "architect": ADAPTER_SKILLS_DIR,
    "review": ADAPTER_SKILLS_DIR,
    "plan": ADAPTER_SKILLS_DIR,
    "revise": ADAPTER_SKILLS_DIR,
    "revise-fast": ADAPTER_SKILLS_DIR,
    "security_review": ADAPTER_SKILLS_DIR,
}

CLASS_B_WRITERS = ["plan", "architect", "review", "revise", "revise-fast", "security_review"]

SKILL_PATHS = {
    name: MIGRATED_SKILLS_DIR_OVERRIDES.get(name, SKILLS_DIR) / name / "SKILL.md"
    for name in CLASS_B_WRITERS
}


def _skill_text(name: str) -> str:
    return (ADAPTER_SKILLS_DIR / name / "SKILL.md").read_text()


FINALIZE_RE = re.compile(
    r'fsops\.py finalize "(?P<src>[^"\n]+)" "(?P<dst>[^"\n]+)" --cleanup (?P<cl>(?:"[^"\n]+"\s*)+)'
)
SWEEP_RE = re.compile(r'fsops\.py rm "(?P<p>[^"\n]+)\.body\.tmp" "(?P=p)\.tmp"')
FALLBACK_RE = re.compile(r'fsops\.py rm "[^"\n]+\.body\.tmp"')
_SEPARATORS = re.compile(r"&&|\|\||;|\||\(|`")


def _finalize_ok(text: str) -> bool:
    """True if a finalize call exists whose cleanup set is exactly {dst.body.tmp, dst.tmp}."""
    for line in text.splitlines():
        # A rename followed by a separate delete can skip the delete; reject it.
        if re.search(r"fsops\.py mv\b.*fsops\.py rm\b", line):
            return False
    for m in FINALIZE_RE.finditer(text):
        src, dst = m.group("src"), m.group("dst")
        cleanups = re.findall(r'"([^"\n]+)"', m.group("cl"))
        if src == dst + ".tmp" and sorted(cleanups) == sorted([dst + ".body.tmp", dst + ".tmp"]):
            return True
    return False


def _sweep_before_step2(text: str) -> bool:
    lines = text.splitlines()
    step2 = next(
        (i for i, l in enumerate(lines) if re.match(r"\*\*Step 2[: ]", l) or re.match(r"Step 2:", l)),
        len(lines),
    )
    return bool(SWEEP_RE.search("\n".join(lines[:step2])))


def _fallback_cleans_body(text: str) -> bool:
    m = re.search(r"English-fallback.*?(?=\*\*Step 6|fsops\.py finalize|\Z)", text, re.DOTALL)
    assert m, "no English-fallback span found"
    return bool(FALLBACK_RE.search(m.group(0)))


def _legacy_shell_form(text: str) -> bool:
    """True if any command segment starts with a shell rm, rmdir or mv that has an argument."""
    for line in text.splitlines():
        for seg in _SEPARATORS.split(line):
            toks = re.sub(r"^\s*(?:Run:|run:|\$)\s+", "", seg).split()
            if len(toks) >= 2 and toks[0] in {"rm", "rmdir", "mv"}:
                return True
    return False


def test_finalize_present_and_matches_destination():
    for name in CLASS_B_WRITERS:
        assert _finalize_ok(_skill_text(name)), f"{name}: finalize call missing or cleanup targets wrong"
    for name in ["critic", "gate", "implement"]:
        assert _finalize_ok(_skill_text(name)), f"{name}: finalize call missing or cleanup targets wrong"


def test_step1_pre_write_sweep_present():
    for name in CLASS_B_WRITERS:
        assert _sweep_before_step2(_skill_text(name)), f"{name}: pre-write sweep missing before Step 2"


def test_english_fallback_cleans_body_tmp():
    for name in CLASS_B_WRITERS:
        assert _fallback_cleans_body(_skill_text(name)), f"{name}: English-fallback does not clean .body.tmp"


def test_no_legacy_shell_form_in_writers():
    for name in CLASS_B_WRITERS + ["critic", "gate", "implement"]:
        assert not _legacy_shell_form(_skill_text(name)), f"{name}: shell rm/mv form present"


SWEEP_LINE = '**Step 1 pre-write sweep:** `python3 __QUOIN_HOME__/scripts/fsops.py rm "<plan-path>.body.tmp" "<plan-path>.tmp"`.'
FALLBACK_LINE = 'Clean up body.tmp: `python3 __QUOIN_HOME__/scripts/fsops.py rm "<plan-path>.body.tmp"`.'
FINALIZE_B = '`python3 __QUOIN_HOME__/scripts/fsops.py finalize "<path>.tmp" "<path>" --cleanup "<path>.body.tmp" "<path>.tmp"`'
FINALIZE_A = '`python3 __QUOIN_HOME__/scripts/fsops.py finalize "{path}.tmp" "{path}" --cleanup "{path}.body.tmp" "{path}.tmp"`'


def test_negative_case_caught():
    """The predicates must reject each synthetic bad text (guards against no-op tests)."""
    fz = 'python3 __QUOIN_HOME__/scripts/fsops.py'
    # Correct forms are accepted and never flagged as legacy.
    assert _finalize_ok(FINALIZE_B) and _finalize_ok(FINALIZE_A)
    for ok in (SWEEP_LINE, FALLBACK_LINE, FINALIZE_B, FINALIZE_A):
        assert not _legacy_shell_form(ok), ok
    assert _sweep_before_step2(SWEEP_LINE + "\n**Step 2: next**\n")
    assert _fallback_cleans_body("English-fallback: " + FALLBACK_LINE)

    bad_finalize = [
        f'{fz} finalize "<p>.tmp" "<p>" --cleanup "<p>.tmp"',
        f'{fz} finalize "<p>.tmp" "<p>" --cleanup "<p>.body.tmp"',
        f'{fz} finalize "<p>.tmp" "<p>" --cleanup "<q>.body.tmp" "<q>.tmp"',
        f'{fz} finalize <p>.tmp <p> --cleanup <p>.body.tmp <p>.tmp',
        f'{fz} mv "<p>.tmp" "<p>" && {fz} rm "<p>.body.tmp" "<p>.tmp"',
        'mv <p>.tmp <p>; (rm -f <p>.body.tmp <p>.tmp 2>/dev/null || true)',
        'mv <p>.tmp <p> && rm -f <p>.body.tmp',
    ]
    for text in bad_finalize:
        assert not _finalize_ok(text), text
    # The fsops mv + rm chain is caught for the right reason: not a legacy form.
    assert not _legacy_shell_form(bad_finalize[4])
    for text in ('mv <p>.tmp <p>; (rm -f <p>.body.tmp <p>.tmp)', 'mv a b && rm x', 'Run: rm -f x',
                 '`a; rm -f x`'):
        assert _legacy_shell_form(text), text

    assert not _sweep_before_step2("Step 1 text\n**Step 2: next**\n" + SWEEP_LINE)
    assert not _sweep_before_step2("**Step 1:** write `<p>.body.tmp`\n**Step 2: x**\n")
    assert not _fallback_cleans_body("English-fallback: skip cleanup **Step 6")
