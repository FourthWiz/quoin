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
CEILING_BYTES = 135919
# S-5 ratchet (T-12): measured 128662 B, candidate 128662*1.10=141529 B > prior
# ceiling 135919 B -> HOLD; number unchanged. This is the ceiling surface the
# round-1 tally initially dropped; restored here as its own recorded decision,
# not folded into test_footprint_ceilings.py's skill: keys (this file guards
# an orchestrator excluded from that dict's scope).


def test_run_skill_md_byte_ceiling():
    size = RUN_SKILL.stat().st_size
    assert size <= CEILING_BYTES, (
        f"run/SKILL.md is {size} bytes, over its {CEILING_BYTES}-byte ceiling -- "
        "either trim the file or deliberately raise this ceiling alongside the "
        "change that grew it."
    )
