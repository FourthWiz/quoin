"""Byte-ceiling guard for `run/SKILL.md`.

`/run` is an orchestrator, excluded from the §0/§0' generator machinery that
`test_footprint_ceilings.py` guards (SECTION0_SKILLS is scoped to the 20
§0-carrying leaf/cheap-tier skills only). That left the file's own growth
unguarded: it is read into context on every `/run` invocation, so an
unbounded increase has a real per-invocation token cost with no test to
surface it.

This is a standalone, ratchet-only ceiling (current measured size * 1.10,
rounded up) — independent of the §0 generator/ratchet discipline in
test_footprint_ceilings.py, which does not apply to this file. A failure
here means the file genuinely grew and should either shrink back down or
have its ceiling deliberately raised alongside the change that grew it.
"""

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
RUN_SKILL = HERE.parent.parent / "adapters" / "claude" / "skills" / "run" / "SKILL.md"

# Ratchet: measured size at ceiling-authoring time * 1.10, rounded up.
# Lowered when the light-route steps moved to light-pipeline.md (read only on
# that route): measured 20,342 bytes after the split, times 1.10.
# Raised for the automatic light -> run_hard switch (Step 0 tags, Escalation):
# measured 23,736 bytes, plus a small margin.
# Lowered when SKILL.md became a short core for the Direct route and the rarely
# needed text moved to reference.md: measured 10,760 bytes, with a small margin
# only, since every turn of every /run session re-reads this file.
# Raised for the prompt-triage step that runs before Size on every route:
# measured 12,233 bytes, with a small margin.
# Raised when scope triage replaced Size and the review step moved here from
# light-pipeline.md, so an r1 run never reads that file (the two files
# together shrank): measured 14,040 bytes, with a small margin.
# Lowered when the prompt-triage step was removed: measured 12,958 bytes,
# with a small margin.
# Lowered when the brief, the r1 review and the r3 handling moved to routes.md,
# so an r0 session reads only this file: measured 10,682 bytes, small margin.
# Lowered when the commit bookkeeping became one scripted call and the
# important behaviours moved to reference.md: measured 8,123 bytes, small margin.
CEILING_BYTES = 8300
LIGHT_PIPELINE = RUN_SKILL.with_name("light-pipeline.md")
# Same ratchet for the reference file: 15,347 bytes at the split, times 1.10.
# Raised for the "Switch to run_hard" procedure: measured 17,794 bytes, plus margin.
# Lowered when it became the r2-only file and the review moved to SKILL.md:
# measured 15,004 bytes, plus margin.
# Raised for the stage list from the interface graph and the cross-repository
# review after the last stage: measured 15,882 bytes, plus margin.
# Raised for the option to run the stages unattended: measured 16,133 bytes.
LIGHT_CEILING_BYTES = 16300
REFERENCE = RUN_SKILL.with_name("reference.md")
# reference.md is read only for rare tags, non-Opus sessions, lessons and
# escalation: measured 14,666 bytes at the split, times 1.05.
# Raised when r0 stopped writing a brief up front and escalation, resume and
# [from-hard] gained the r0 cases: measured 15,513 bytes, plus margin.
# Raised when the important behaviours moved here from SKILL.md (an r0
# session no longer reads them): measured 15,963 bytes, plus margin.
REFERENCE_CEILING_BYTES = 16200
ROUTES = RUN_SKILL.with_name("routes.md")
# routes.md is read by every r1, r2 and r3 session: measured 3,226 bytes at the
# split, with a small margin.
ROUTES_CEILING_BYTES = 3400


def test_run_skill_md_byte_ceiling():
    size = RUN_SKILL.stat().st_size
    assert size <= CEILING_BYTES, (
        f"run/SKILL.md is {size} bytes, over its {CEILING_BYTES}-byte ceiling -- "
        "either trim the file or deliberately raise this ceiling alongside the "
        "change that grew it."
    )


def test_light_pipeline_byte_ceiling():
    size = LIGHT_PIPELINE.stat().st_size
    assert size <= LIGHT_CEILING_BYTES, (
        f"run/light-pipeline.md is {size} bytes, over its {LIGHT_CEILING_BYTES}-byte "
        "ceiling -- trim it or raise the ceiling alongside the change that grew it."
    )


def test_reference_byte_ceiling():
    size = REFERENCE.stat().st_size
    assert size <= REFERENCE_CEILING_BYTES, (
        f"run/reference.md is {size} bytes, over its {REFERENCE_CEILING_BYTES}-byte "
        "ceiling -- trim it or raise the ceiling alongside the change that grew it."
    )


def test_routes_byte_ceiling():
    size = ROUTES.stat().st_size
    assert size <= ROUTES_CEILING_BYTES, (
        f"run/routes.md is {size} bytes, over its {ROUTES_CEILING_BYTES}-byte "
        "ceiling -- trim it or raise the ceiling alongside the change that grew it."
    )
