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
CEILING_BYTES = 22380
LIGHT_PIPELINE = RUN_SKILL.with_name("light-pipeline.md")
# Same ratchet for the reference file: 15,347 bytes at the split, times 1.10.
LIGHT_CEILING_BYTES = 16890


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
