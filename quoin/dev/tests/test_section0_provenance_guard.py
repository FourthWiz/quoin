"""§0-family provenance partition guard, plus per-file share ceilings.

Every §0/§0'/§0″/§0‴/§0c block in the adapter corpus is either generator-owned
(reproducible byte-for-byte from `inject_pollution_dispatch.py`) or one of six
named hand-written blocks. This module pins that partition and the five
roster-vs-corpus equalities that back it, so a roster edit that is not
reflected in the corpus (or vice versa) reds here rather than surfacing later
as silent drift.

The independent oracle: every family is discovered from CORPUS TEXT
ALONE — a marker or an exact heading-line match — never from a roster and
never from a renderer call keyed by roster membership. That is what makes a
roster-only edit and a corpus-only edit both observable: each moves only one
side of the corresponding equality.

Per-family, not union: the five rosters encode 55 (skill, roster)
membership facts, but only 7 of them (capture_insight, cost_snapshot,
next_steps, start_of_day, status, triage, weekly_review — all in SECTION0
and no other roster) are unique to any one roster. A union-based assertion
can therefore observe at most 7 of 55 facts; asserting each family against
its own roster observes all 55.

Load-bearing assertions (a future maintainer should not "simplify" these):
  - the five per-family carrier equalities (oracle vs SECTION0/POLLUTION/
    MINTIER/MINTIER_SONNET/ZC)
  - the §0c two-separator agreement (exact-heading set == ZC_BLOCKS-value
    content set) — the only assertion that catches a COORDINATED edit
    dropping a member from both `ZC_SKILLS` and `ZC_BLOCKS` at once
  - the declared six-block hand-written set, located by heading, each with
    its own line-count assertion
  - the declared-variant-set assertion (exactly 7 glyph tokens)
  - the blankness assertion on the 19 non-`start_of_day` overhangs

Everything else (partition disjointness/coverage/sum, the informational
snapshot) is a cheap extra check, not the mechanism that discriminates.

Snapshot (informational; a maintainer updating the generator template updates
this alongside, without touching the partition assertions above):
  corpus_lines=15475, census_lines=4652, generated=4323, handwritten=249,
  overhang=61 (start_of_day) + 19 (blank, one per other §0 carrier) = 80,
  carriers=30, variants=7, declared hand-written blocks=6.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Mirrors test_inject_pollution_dispatch.py:27-34 exactly: _TESTS_DIR is
# defined at :29, _SCRIPTS_DIR at :30, the `import ... as generator` at :34.
_TESTS_DIR = Path(__file__).parent
_SCRIPTS_DIR = _TESTS_DIR.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import inject_pollution_dispatch as generator  # noqa: E402

ADAPTER_DIR = _TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"
ADAPTER_FILES = sorted(ADAPTER_DIR.glob("*/SKILL.md"))

# ─── Rosters (imported, never hardcoded — an eighth copy false-greens on drift) ──
SECTION0_TARGET_SKILLS = set(generator.SECTION0_TARGET_SKILLS)
POLLUTION_TARGET_SKILLS = set(generator.POLLUTION_TARGET_SKILLS)
MINTIER_TARGET_SKILLS = set(generator.MINTIER_TARGET_SKILLS)
MINTIER_SONNET_TARGET_SKILLS = set(generator.MINTIER_SONNET_TARGET_SKILLS)
ZC_SKILLS = set(generator.ZC_SKILLS)

ROSTERS = {
    "§0": SECTION0_TARGET_SKILLS,
    "§0″": MINTIER_TARGET_SKILLS,
    "§0‴": MINTIER_SONNET_TARGET_SKILLS,
    "§0'": POLLUTION_TARGET_SKILLS,
    "§0c": ZC_SKILLS,
}

# ─── Heading constants and markers (imported, never retyped) ────────────────
SECTION0_HEADING = generator.SECTION0_HEADING
POLLUTION_HEADING = generator.POLLUTION_HEADING
MINTIER_HEADING = generator.MINTIER_HEADING
MINTIER_SONNET_HEADING = generator.MINTIER_SONNET_HEADING
ZC_HEADING = generator.ZC_HEADING
SECTION0_END_MARKER = generator.SECTION0_END_MARKER
MINTIER_BEGIN_MARKER = "<!-- §0doubleprime-begin -->"
MINTIER_SONNET_BEGIN_MARKER = "<!-- §0tripleprime-begin -->"

DECLARED_VARIANT_SET = {"§0", "§0'", "§0a", "§0b", "§0c", "§0″", "§0‴"}

# The six hand-written blocks, discovered by heading (§0a/§0b have no
# renderer at all; §0c is hand-written iff it fails the ZC_BLOCKS content
# match). Declared here with their line counts so a deletion or a silent
# prose expansion reds by name. A legitimate prose tightening inside one of
# these six blocks may re-seed its line count in the same commit as the
# edit — the same allowance the share ceilings below get.
DECLARED_HANDWRITTEN_BLOCKS = {
    ("implement", "§0a"): 73,
    ("implement", "§0b"): 53,
    ("end_of_task", "§0b"): 52,
    ("checkpoint", "§0c"): 23,
    ("cleanup", "§0c"): 24,
    ("sleep", "§0c"): 24,
}
DECLARED_HANDWRITTEN_TOTAL_LINES = 249

START_OF_DAY_TAIL_LINES = 61
START_OF_DAY_FIRST_NONBLANK = "### Step 1a: Resume from cookie"
START_OF_DAY_FIRST_NONBLANK_LINE_1BASED = 126

# Informational snapshot (see module docstring) — not the totality contract.
SNAPSHOT_CORPUS_LINES = 15475
SNAPSHOT_CENSUS_LINES = 4652
SNAPSHOT_GENERATED_LINES = 4323
SNAPSHOT_OVERHANG_LINES = 80  # 61 (start_of_day tail) + 19 (one blank line each)
SNAPSHOT_CARRIERS = 30


def _variant_of(line: str) -> str | None:
    """Glyph token of a `^## §0...` heading line.

    Deliberately NOT filtered against the seven known tokens: the token is
    whatever whitespace-delimited word follows `## ` and
    starts with `§0`, discovered generically, so an eighth variant this
    module has never seen is captured as its own distinct token rather than
    silently returning None and vanishing from DISCOVERED_VARIANTS. A fixed
    allow-list here would make test_declared_variant_set_is_exactly_seven
    blind to exactly the case it exists to catch.
    """
    if not line.startswith("## §0"):
        return None
    body = line[3:].strip()
    if not body:
        return None
    tok = body.split(None, 1)[0]
    if not tok.startswith("§0"):
        return None
    return "§0'" if tok == "§0’" else tok


def _next_h2(lines: list[str], i: int) -> int:
    """Index of the next `^## ` line after i, or len(lines) if none."""
    for j in range(i + 1, len(lines)):
        if lines[j].startswith("## "):
            return j
    return len(lines)


def _scan_file(path: Path):
    """Return (skill, [(variant, start, end_exclusive), ...], lines)."""
    skill = path.parent.name
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks = []
    for i, ln in enumerate(lines):
        v = _variant_of(ln)
        if v:
            blocks.append((v, i, _next_h2(lines, i)))
    return skill, blocks, lines


# ─── Corpus-wide scan, computed once at collection time ─────────────────────
_CENSUS_BLOCKS: list[tuple[str, str, int, int]] = []  # (skill, variant, start, end)
for _p in ADAPTER_FILES:
    _skill, _blocks, _lines = _scan_file(_p)
    for _v, _s, _e in _blocks:
        _CENSUS_BLOCKS.append((_skill, _v, _s, _e))

CENSUS_LINES = sum(e - s for _, _, s, e in _CENSUS_BLOCKS)
CENSUS_CARRIERS = {n for n, _, _, _ in _CENSUS_BLOCKS}
CORPUS_LINES = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in ADAPTER_FILES)
DISCOVERED_VARIANTS = {v for _, v, _, _ in _CENSUS_BLOCKS}
VARIANT_COUNTS: dict[str, int] = {}
for _, v, _, _ in _CENSUS_BLOCKS:
    VARIANT_COUNTS[v] = VARIANT_COUNTS.get(v, 0) + 1

# ─── Independent oracle: per-family carrier sets from corpus text alone ─────
# §0, §0″, §0‴ — marker-anchored (pure text signal, no roster, no renderer).
# §0', §0c — exact whole-line heading match; §0c additionally requires the
# ZC_BLOCKS-VALUE content match (not keyed by skill name) to agree, since a
# hand-written §0c block ("... FIRST STEP after §0 dispatch") also carries a
# `## §0c` heading but is not a generated carrier.
POLLUTION_HEAD_EXACT = POLLUTION_HEADING.strip()
ZC_HEAD_EXACT = ZC_HEADING.strip()
ZC_BLOCK_VALUES = [v.strip() for v in generator.ZC_BLOCKS.values()]

ORACLE_FAMILY: dict[str, set[str]] = {k: set() for k in ROSTERS}
ZC_BY_HEADING: set[str] = set()
ZC_BY_CONTENT: set[str] = set()
GEN_CARRIERS: set[str] = set()

for _p in ADAPTER_FILES:
    _skill = _p.parent.name
    _txt = _p.read_text(encoding="utf-8")
    _lines = _txt.splitlines()
    for _sig, _fam in (
        (SECTION0_END_MARKER, "§0"),
        (MINTIER_BEGIN_MARKER, "§0″"),
        (MINTIER_SONNET_BEGIN_MARKER, "§0‴"),
    ):
        if _sig in _txt:
            ORACLE_FAMILY[_fam].add(_skill)
            GEN_CARRIERS.add(_skill)
    if any(_l.strip() == POLLUTION_HEAD_EXACT for _l in _lines):
        ORACLE_FAMILY["§0'"].add(_skill)
        GEN_CARRIERS.add(_skill)
    _has_heading = any(_l.strip() == ZC_HEAD_EXACT for _l in _lines)
    _has_content = any(_v in _txt for _v in ZC_BLOCK_VALUES)
    if _has_heading:
        ZC_BY_HEADING.add(_skill)
    if _has_content:
        ZC_BY_CONTENT.add(_skill)
    if _has_heading and _has_content:
        ORACLE_FAMILY["§0c"].add(_skill)
        GEN_CARRIERS.add(_skill)

ROSTER_UNION = set().union(*ROSTERS.values())


# ═══════════════════════════ Declared-variant-set ═══════════════════════════

def test_declared_variant_set_is_exactly_seven():
    assert DISCOVERED_VARIANTS == DECLARED_VARIANT_SET, (
        f"discovered §0-family glyph variants {sorted(DISCOVERED_VARIANTS)} "
        f"!= declared set {sorted(DECLARED_VARIANT_SET)} "
        f"(sym-diff {sorted(DISCOVERED_VARIANTS ^ DECLARED_VARIANT_SET)}). "
        "An eighth variant (or the disappearance of one of the seven) means "
        "the generator introduced a new §0-family shape that this guard has "
        "not been taught about yet."
    )


# ═══════════════════ Per-family carrier assertions (load-bearing) ═══════════
# Each asserted SEPARATELY with its own message — a single union
# assertion does not satisfy this. Every one of the 55 (skill, roster)
# membership facts moves exactly one side of exactly one of these five.

def test_oracle_section0_matches_roster():
    assert ORACLE_FAMILY["§0"] == SECTION0_TARGET_SKILLS, (
        "§0 corpus-text carriers != SECTION0_TARGET_SKILLS "
        f"(sym-diff {sorted(ORACLE_FAMILY['§0'] ^ SECTION0_TARGET_SKILLS)})"
    )


def test_oracle_mintier_matches_roster():
    assert ORACLE_FAMILY["§0″"] == MINTIER_TARGET_SKILLS, (
        "§0″ corpus-text carriers != MINTIER_TARGET_SKILLS "
        f"(sym-diff {sorted(ORACLE_FAMILY['§0″'] ^ MINTIER_TARGET_SKILLS)})"
    )


def test_oracle_mintier_sonnet_matches_roster():
    assert ORACLE_FAMILY["§0‴"] == MINTIER_SONNET_TARGET_SKILLS, (
        "§0‴ corpus-text carriers != MINTIER_SONNET_TARGET_SKILLS "
        f"(sym-diff {sorted(ORACLE_FAMILY['§0‴'] ^ MINTIER_SONNET_TARGET_SKILLS)})"
    )


def test_oracle_pollution_matches_roster():
    oracle_pollution = ORACLE_FAMILY["§0'"]
    assert oracle_pollution == POLLUTION_TARGET_SKILLS, (
        "§0' corpus-text carriers != POLLUTION_TARGET_SKILLS "
        f"(sym-diff {sorted(oracle_pollution ^ POLLUTION_TARGET_SKILLS)})"
    )


def test_oracle_zc_matches_roster():
    assert ORACLE_FAMILY["§0c"] == ZC_SKILLS, (
        "§0c corpus-text carriers != ZC_SKILLS "
        f"(sym-diff {sorted(ORACLE_FAMILY['§0c'] ^ ZC_SKILLS)})"
    )


def test_zc_two_separators_agree():
    """The ONLY assertion that catches a coordinated edit dropping a member
    from ZC_SKILLS *and* ZC_BLOCKS at once without regenerating. Under such
    an edit both sides of the per-family §0c equality above fall together and
    that assertion alone passes; the two separators below disagree instead.
    """
    assert ZC_BY_HEADING == ZC_BY_CONTENT, (
        f"§0c exact-heading carrier set {sorted(ZC_BY_HEADING)} != "
        f"ZC_BLOCKS-value content carrier set {sorted(ZC_BY_CONTENT)} "
        f"(sym-diff {sorted(ZC_BY_HEADING ^ ZC_BY_CONTENT)}) — a coordinated "
        "roster+content edit is the only edit shape this assertion detects."
    )


def test_union_equality_cheap_extra_check():
    """NOT the load-bearing assertion — kept only as a cheap extra check.

    A union-based comparison observes at most 7 of the 55 (skill, roster)
    membership facts (all 7 unique to SECTION0; POLLUTION/MINTIER/
    MINTIER_SONNET/ZC contribute zero unique members each), so it cannot by
    itself detect most single-member roster drops. The five per-family
    assertions above are what carries the claim.
    """
    assert GEN_CARRIERS == ROSTER_UNION, (
        f"oracle carrier union {sorted(GEN_CARRIERS)} != roster union "
        f"{sorted(ROSTER_UNION)} (sym-diff {sorted(GEN_CARRIERS ^ ROSTER_UNION)})"
    )


# ═══════════════════════ Declared hand-written block set ════════════════════

def test_declared_handwritten_blocks_present_with_line_counts():
    discovered: dict[tuple[str, str], int] = {}
    for skill, variant, start, end in _CENSUS_BLOCKS:
        if variant in ("§0a", "§0b"):
            discovered[(skill, variant)] = end - start
        elif variant == "§0c" and skill not in ORACLE_FAMILY["§0c"]:
            discovered[(skill, variant)] = end - start

    assert set(discovered) == set(DECLARED_HANDWRITTEN_BLOCKS), (
        f"discovered hand-written (skill, variant) set {sorted(discovered)} != "
        f"declared set {sorted(DECLARED_HANDWRITTEN_BLOCKS)} "
        f"(sym-diff {sorted(set(discovered) ^ set(DECLARED_HANDWRITTEN_BLOCKS))}). "
        "Deleting one of the six declared blocks reds this by name."
    )
    for pair, expected_lines in DECLARED_HANDWRITTEN_BLOCKS.items():
        assert discovered[pair] == expected_lines, (
            f"{pair[0]} {pair[1]} block is {discovered[pair]} lines, expected "
            f"{expected_lines}. A same-hunk prose tightening of exactly this "
            "block may re-seed this constant to the new count — an "
            "unrelated growth elsewhere does not."
        )
    assert sum(DECLARED_HANDWRITTEN_BLOCKS.values()) == DECLARED_HANDWRITTEN_TOTAL_LINES, (
        f"sum of DECLARED_HANDWRITTEN_BLOCKS is {sum(DECLARED_HANDWRITTEN_BLOCKS.values())}, "
        f"expected DECLARED_HANDWRITTEN_TOTAL_LINES={DECLARED_HANDWRITTEN_TOTAL_LINES}. "
        "Re-sum DECLARED_HANDWRITTEN_BLOCKS.values() and update this constant in the "
        "same hunk as any individual block's re-seed above."
    )


# ═══════════════════════════ Span / overhang assertions ═════════════════════
# Asserted by CONTENT, not only by count: a carrier
# whose overhang silently turns into prose must not pass just because the
# line count stayed at 1.

def _section0_overhangs() -> dict[str, list[str]]:
    overhangs: dict[str, list[str]] = {}
    for p in ADAPTER_FILES:
        skill = p.parent.name
        lines = p.read_text(encoding="utf-8").splitlines()
        marker_i = next((i for i, l in enumerate(lines) if SECTION0_END_MARKER in l), None)
        if marker_i is None:
            continue
        head_i = next((i for i, l in enumerate(lines) if _variant_of(l) == "§0"), None)
        if head_i is None:
            continue
        end = _next_h2(lines, head_i)
        overhangs[skill] = lines[marker_i + 1:end]
    return overhangs


_OVERHANGS = _section0_overhangs()


def test_section0_overhang_exception_set_is_start_of_day_only():
    non_one_line = {k: v for k, v in _OVERHANGS.items() if len(v) != 1}
    assert set(non_one_line) == {"start_of_day"}, (
        f"§0 carriers whose overhang is not exactly 1 line: {sorted(non_one_line)} "
        "— expected exactly {'start_of_day'}"
    )


def test_section0_overhang_is_blank_for_every_non_start_of_day_carrier():
    non_blank = {
        k: v for k, v in _OVERHANGS.items()
        if k != "start_of_day" and any(line.strip() for line in v)
    }
    assert not non_blank, (
        f"non-blank §0 overhang content found outside start_of_day: {non_blank} "
        "— an overhang turning into prose must red here even though the "
        "line count would still read as 1."
    )


def test_start_of_day_tail_anchored_on_its_own_first_nonblank_line():
    tail = _OVERHANGS["start_of_day"]
    assert len(tail) == START_OF_DAY_TAIL_LINES, (
        f"start_of_day §0 tail is {len(tail)} lines, expected {START_OF_DAY_TAIL_LINES}"
    )
    first_nonblank = next((l for l in tail if l.strip()), None)
    assert first_nonblank == START_OF_DAY_FIRST_NONBLANK, (
        f"start_of_day tail's first non-blank line is {first_nonblank!r}, "
        f"expected {START_OF_DAY_FIRST_NONBLANK!r} "
        f"(SKILL.md:{START_OF_DAY_FIRST_NONBLANK_LINE_1BASED})"
    )


def test_overhang_total_matches_snapshot():
    assert sum(len(v) for v in _OVERHANGS.values()) == SNAPSHOT_OVERHANG_LINES


# ══════════════════════════════ Partition ════════════════════════════════
# Disjointness, coverage and the cardinality sum. Honestly scoped: these
# catch an unclaimed heading or a double-claimed line, but on the
# 80-line overhang term the buckets are positionally implied by the census
# slicers, so this alone cannot discriminate there — the assertions above
# (per-family, declared set, blankness) are what actually discriminates.
# The hand-written bucket is NEVER defined as census-minus-generated.

def _compute_partition():
    """Bucket every census line into generated / hand-written / overhang,
    purely from the LIVE scan (no fixed constants), and assert disjointness
    along the way. Returns (generated_lines, handwritten_lines, overhang_lines).

    Deliberately dynamic: a hand-written block that
    is cleanly removed moves generated/handwritten totals
    and CENSUS_LINES together, so this structural check stays green on that
    removal — only the declared six-block SET assertion (which names the
    missing pair) is supposed to red there. Comparing against FIXED snapshot
    numbers is the separate, explicitly-informational job of
    test_census_snapshot_reproduces_state.
    """
    generated_lines = 0
    handwritten_lines = 0
    overhang_lines = sum(len(v) for v in _OVERHANGS.values())
    claimed: set[tuple[str, int]] = set()
    lines_by_skill = {
        p.parent.name: p.read_text(encoding="utf-8").splitlines() for p in ADAPTER_FILES
    }

    for skill, variant, start, end in _CENSUS_BLOCKS:
        if variant == "§0":
            skill_lines = lines_by_skill[skill]
            marker_i = next(
                (i for i in range(start, end) if SECTION0_END_MARKER in skill_lines[i]),
                None,
            )
            gen_end = (marker_i + 1) if marker_i is not None else end
            span = range(start, gen_end)
        else:
            span = range(start, end)  # §0'/§0″/§0‴ in full; §0a/§0b/§0c likewise

        is_handwritten = (skill, variant) in DECLARED_HANDWRITTEN_BLOCKS
        for i in span:
            key = (skill, i)
            assert key not in claimed, f"line {i} of {skill} claimed twice ({variant})"
            claimed.add(key)
        if is_handwritten:
            handwritten_lines += len(span)
        else:
            generated_lines += len(span)

    return generated_lines, handwritten_lines, overhang_lines


def test_partition_disjoint_and_covers_census():
    generated_lines, handwritten_lines, overhang_lines = _compute_partition()
    total = generated_lines + handwritten_lines + overhang_lines
    assert total == CENSUS_LINES, (
        f"partition sum {generated_lines} + {handwritten_lines} + {overhang_lines} "
        f"= {total} != census {CENSUS_LINES}"
    )


def test_census_snapshot_reproduces_state():
    """Informational snapshot — a maintainer legitimately tightening the
    generator template updates these numbers alongside, without touching the
    load-bearing assertions above (this test is NOT one of them; see the
    module docstring's load-bearing list).

    CORPUS_LINES (whole-file line totals) is reported in the module docstring
    but deliberately NOT asserted here: it moves under any hand-written prose
    edit anywhere in the corpus, generated span or not, so asserting it would
    red on a single hand-written-prose deletion in a carrier that is only
    supposed to move that carrier's own SHARE_CEILINGS entry.

    Re-seed procedure: when this test reds after a deliberate, reviewed
    corpus edit, re-run the corpus scan (import this module and read
    CENSUS_LINES / CENSUS_CARRIERS / VARIANT_COUNTS / _compute_partition()
    fresh) and update the matching SNAPSHOT_* constant or literal dict below
    in the same commit as the edit — this is informational, not one of the
    module's load-bearing assertions (see module docstring).
    """
    generated_lines, handwritten_lines, _overhang = _compute_partition()
    assert generated_lines == SNAPSHOT_GENERATED_LINES, (
        f"generated_lines={generated_lines} != SNAPSHOT_GENERATED_LINES="
        f"{SNAPSHOT_GENERATED_LINES} — re-seed SNAPSHOT_GENERATED_LINES per the "
        "re-seed procedure above."
    )
    assert handwritten_lines == DECLARED_HANDWRITTEN_TOTAL_LINES, (
        f"handwritten_lines={handwritten_lines} != DECLARED_HANDWRITTEN_TOTAL_LINES="
        f"{DECLARED_HANDWRITTEN_TOTAL_LINES} — re-derive from DECLARED_HANDWRITTEN_BLOCKS "
        "and update that constant per the re-seed procedure above."
    )
    assert CENSUS_LINES == SNAPSHOT_CENSUS_LINES, (
        f"CENSUS_LINES={CENSUS_LINES} != SNAPSHOT_CENSUS_LINES={SNAPSHOT_CENSUS_LINES} "
        "— re-seed SNAPSHOT_CENSUS_LINES per the re-seed procedure above."
    )
    assert len(CENSUS_CARRIERS) == SNAPSHOT_CARRIERS, (
        f"{len(CENSUS_CARRIERS)} carrier skills found != SNAPSHOT_CARRIERS="
        f"{SNAPSHOT_CARRIERS} — re-seed SNAPSHOT_CARRIERS per the re-seed procedure above."
    )
    assert VARIANT_COUNTS == {
        "§0": 20, "§0'": 10, "§0a": 1, "§0b": 2, "§0c": 5, "§0″": 10, "§0‴": 13,
    }, (
        f"VARIANT_COUNTS={VARIANT_COUNTS} no longer matches the recorded per-variant "
        "block counts — re-seed the literal dict above per the re-seed procedure above."
    )


# ═══════════════════ Renderer-call safety ════════════════════════════════
# An earlier version of this oracle caught bare Exception and returned None
# on any renderer failure, which made "renderer raised" indistinguishable
# from "skill is not a carrier". This module makes no renderer calls for
# carrier discovery at all — the §0c content check above compares against
# generator.ZC_BLOCKS values pasted directly, not a renderer call — so there
# is nothing here that swallows an exception. This test instead pins that
# the two roster-gated renderers actually raise on a non-member, rather than
# silently returning an empty/falsy value that a broad except could hide.

@pytest.mark.parametrize(
    "render_fn_name", ["render_pollution_block", "render_zc_block"]
)
def test_roster_gated_renderers_raise_on_non_member(render_fn_name):
    fn = getattr(generator, render_fn_name)
    with pytest.raises(Exception):
        fn("nosuchskill_xyz")


# ═══════════════════════════ Per-file share ceilings ═════════════════════════
# ceiling = measured_share + SHARE_HEADROOM_PP, where SHARE_HEADROOM_PP is an
# ABSOLUTE PERCENTAGE-POINT headroom (not a multiplier — shares near 0% and
# near 70% behave very differently under the same multiplier).
#
# Shrink side: 5.00 pp is the smallest whole-point headroom that survives a
# 10-line hand-written-prose reduction in every carrier; `status` binds at
# 0.2009 pp residual slack. The 10-line figure is a CHOSEN design parameter,
# not a measurement.
#
# Growth side (the same headroom is a blind spot upward): the
# same 5.00 pp lets a carrier's GENERATED span grow before this guard fires
# — 25 lines at `cleanup` (binding), 74 at `checkpoint` (loosest). Generated-
# span growth is caught by test_generator_fidelity.py's byte-compare and by
# the byte ceilings in test_footprint_ceilings.py, NOT by this share guard.
# That asymmetry is a recorded decision, not an oversight.
#
# Zero carve-out: `run` and `thorough_plan` measure exactly 0.0000% (zero
# §0-family lines at all) and get an exact 0.0 ceiling with NO headroom
# added — their zero is asserted, not merely unobserved. Note: the roster
# union is 30 of 32 adapter skills, so this zero carve-out and the "carriers
# equal the oracle set, size 30" assertion above are the SAME FACT from two
# directions, not two independent confirmations.
#
# Waiver / re-ratchet procedure: the numerator (generated span) is
# generator-owned and the denominator (file length) is skill-owned, so
# legitimately tightening hand-written prose in a carrier RAISES its share.
# When that happens, the fix is to re-seed that key from the new measurement
# IN THE SAME HUNK as the prose change, with a one-line note — never to
# relax SHARE_HEADROOM_PP itself.

SHARE_HEADROOM_PP = 5.00

# measured_share = 100 * generated_span_lines / file_lines, generated_span
# defined identically to the partition assertion above (§0 up to and
# including SECTION0_END_MARKER; §0'/§0″/§0‴ in full; §0c only when it is a
# generated carrier, i.e. in ORACLE_FAMILY["§0c"]).
SHARE_CEILINGS: dict[str, float] = {
    "architect": 25.0608,
    "capture_insight": 63.0311,
    "checkpoint": 18.8218,
    "cleanup": 50.8213,  # re-seeded (IVG-263): tier move to Sonnet adds the
                         # §0‴ Minimum-tier guard block, raising the
                         # generator-owned share

    "continue_work": 56.2903,
    "cost_snapshot": 43.0952,
    "critic": 38.5277,
    "discover": 24.8953,
    "end_of_day": 30.5778,
    "end_of_task": 29.6722,
    "enrich": 61.7961,
    "expand": 56.8750,
    "gate": 34.4849,
    "implement": 31.8135,
    "init_workflow": 22.1946,
    "next_steps": 58.3333,
    "plan": 42.5405,
    "pr": 56.1002,
    "review": 29.8658,
    "revise": 40.0610,
    "revise-fast": 48.0052,
    "rollback": 54.2857,
    "run": 0.0,
    "security_review": 46.0714,
    "sleep": 36.9231,  # re-seeded (IVG-263): tier move to Sonnet adds the
                       # §0‴ Minimum-tier guard block, raising the
                       # generator-owned share

    "specify": 44.8625,
    "start_of_day": 32.2506,  # span-based (27.2506%) + 5.00pp — NEVER the
                              # heading-based 42.09% figure an earlier
                              # census over-captured
    "status": 73.6275,
    "thorough_plan": 0.0,
    "triage": 34.1667,
    "weekly_review": 40.5556,
    "workspace": 75.3704,
}


def _measured_share(skill: str) -> float:
    path = ADAPTER_DIR / skill / "SKILL.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    gen = 0
    for sk, variant, start, end in _CENSUS_BLOCKS:
        if sk != skill:
            continue
        if variant == "§0":
            marker_i = next((i for i in range(start, end) if SECTION0_END_MARKER in lines[i]), None)
            gen += (marker_i - start + 1) if marker_i is not None else 0
        elif variant in ("§0'", "§0″", "§0‴"):
            gen += end - start
        elif variant == "§0c" and skill in ORACLE_FAMILY["§0c"]:
            gen += end - start
    return 100.0 * gen / len(lines) if lines else 0.0


def test_share_ceilings_key_set_matches_adapter_skills():
    """Structural companion to test_section0_skills_set_matches_ceiling_keys:
    the ceiling-dict key set must equal the discovered adapter skill
    directories, so a new skill reds this suite instead of escaping the
    guard silently."""
    discovered = {p.parent.name for p in ADAPTER_FILES}
    assert set(SHARE_CEILINGS) == discovered, (
        f"SHARE_CEILINGS keys {sorted(SHARE_CEILINGS)} != discovered adapter "
        f"skills {sorted(discovered)} "
        f"(sym-diff {sorted(set(SHARE_CEILINGS) ^ discovered)})"
    )
    assert len(SHARE_CEILINGS) == 32


def test_zero_share_carve_out_is_exact():
    for skill in ("run", "thorough_plan"):
        assert _measured_share(skill) == 0.0, (
            f"{skill} is expected to carry zero §0-family lines; measured "
            f"{_measured_share(skill)}%"
        )
        assert SHARE_CEILINGS[skill] == 0.0, (
            f"{skill} ceiling must be an exact 0.0 with no headroom added"
        )


@pytest.mark.parametrize("skill", sorted(SHARE_CEILINGS))
def test_file_within_share_ceiling(skill):
    measured = _measured_share(skill)
    ceiling = SHARE_CEILINGS[skill]
    assert measured <= ceiling, (
        f"{skill}'s §0-family share is {measured:.4f}%, exceeds ceiling "
        f"{ceiling:.4f}% (measured_share + {SHARE_HEADROOM_PP:.2f}pp). If "
        "this is a legitimate hand-written-prose tightening in this "
        "carrier, re-seed SHARE_CEILINGS[skill] from the new measurement in "
        "the SAME HUNK as the prose change, with a one-line note — never "
        "relax SHARE_HEADROOM_PP itself."
    )


def test_start_of_day_ceiling_is_span_based_not_heading_based():
    """Pins the over-capture regression this guard exists to prevent: an
    earlier (withdrawn) heading-based measurement put start_of_day's share
    at 42.09%; the correct span-based measurement is 27.2506%."""
    measured = _measured_share("start_of_day")
    assert abs(measured - 27.2506) < 0.001, (
        f"start_of_day measured share {measured} drifted from the expected "
        "span-based 27.2506% — if this is legitimate, confirm it was NOT "
        "caused by reverting to heading-based measurement before updating "
        "this test."
    )
    assert abs(SHARE_CEILINGS["start_of_day"] - 32.2506) < 0.001


# ═══════════ In-domain caps-density metric and per-file ceilings ═══════════
# A pressure-density metric: per-file ceilings over the DOMAIN COMPLEMENT of
# the generated spans above (so a generated block's own dispatch-machinery
# prose — which legitimately uses MUST/NEVER/CRITICAL — never counts against
# a skill's hand-written density), gated by a materiality floor so a
# near-zero-count file does not get a ceiling that one legitimate new
# constraint blows through.
#
# Corpus-level result: "domination" means the top two files hold >=50% of
# in-domain caps tokens. Measured on all four report surfaces (manifest-33
# corpus slice, the 57-file primary domain, the 39-file secondary domain,
# and their union) — 28.34%, 29.62%, 16.31%, 13.03% — every one well under
# 50%. Not dominated; the per-file half ships.

CAPS_PATTERNS = [
    ("MUST", r"MUST(?! NOT)"), ("ALWAYS", r"ALWAYS"), ("NEVER", r"NEVER"),
    ("CRITICAL", r"CRITICAL"), ("MUST NOT", r"MUST NOT"), ("REQUIRED", r"REQUIRED"),
    ("DO NOT", r"DO NOT"), ("IMPORTANT", r"IMPORTANT"),
]

DENSITY_MATERIALITY_FLOOR = 5  # caps tokens; below this, ratcheting is brittle

CLAUDE_MD = ADAPTER_DIR.parent.parent.parent / "CLAUDE.md"

# 15 files at or above the materiality floor (measured, manifest-33 domain).
# Ratchet-down-only from this first-measured baseline (mirrors the byte
# ceilings' own "provisional = current size" starting convention).
DENSITY_CEILINGS: dict[str, int] = {
    "gate": 34,
    "review": 19,
    "run": 18,
    "critic": 14,
    "thorough_plan": 10,
    "checkpoint": 9,
    "architect": 8,
    "security_review": 8,
    "end_of_task": 7,
    "revise": 7,
    "revise-fast": 7,
    "end_of_day": 6,
    "plan": 6,
    "implement": 5,
    "claude_md": 5,
}

# The 18 excluded files, named explicitly (never silently absent from the
# module) — every one measures below DENSITY_MATERIALITY_FLOOR.
DENSITY_EXCLUDED_BELOW_FLOOR = {
    "cleanup", "specify", "start_of_day", "discover", "enrich", "expand",
    "init_workflow", "rollback", "workspace", "cost_snapshot", "sleep",
    "capture_insight", "continue_work", "next_steps", "pr", "status",
    "triage", "weekly_review",
}


def _in_domain_text(skill: str | None) -> str:
    """Text of a manifest-33 member with its OWN generator-backed §0-family
    spans excised, mirroring derive/density_domains.py's gen_spans() —
    which drops a span only when the skill carries it on a generator
    roster. A block declared in DECLARED_HANDWRITTEN_BLOCKS shares a
    heading token with a generated family (§0a/§0b have no generator at
    all; a hand-written §0c fails the ZC_BLOCKS content match) but is not
    itself generator output, so it must stay in the domain and get
    measured like any other hand-written prose."""
    path = CLAUDE_MD if skill is None else ADAPTER_DIR / skill / "SKILL.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if skill is None:
        return "".join(lines)  # CLAUDE.md carries no §0-family spans
    drop: set[int] = set()
    plain_lines = [l.rstrip("\n") for l in lines]
    for sk, variant, start, end in _CENSUS_BLOCKS:
        if sk != skill:
            continue
        if (sk, variant) in DECLARED_HANDWRITTEN_BLOCKS:
            continue  # hand-written, not generator-backed — keep in domain
        if variant == "§0":
            marker_i = next((i for i in range(start, end) if SECTION0_END_MARKER in plain_lines[i]), None)
            span_end = (marker_i + 1) if marker_i is not None else end
        else:
            span_end = end
        drop.update(range(start, span_end))
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _in_domain_caps_count(skill: str | None) -> int:
    text = _in_domain_text(skill)
    return sum(len(re.findall(pattern, text)) for _, pattern in CAPS_PATTERNS)


MANIFEST_33 = sorted(p.parent.name for p in ADAPTER_FILES) + ["claude_md"]


def test_density_ceiling_and_excluded_sets_cover_manifest_33_exactly():
    covered = set(DENSITY_CEILINGS) | DENSITY_EXCLUDED_BELOW_FLOOR
    assert covered == set(MANIFEST_33), (
        f"DENSITY_CEILINGS + DENSITY_EXCLUDED_BELOW_FLOOR != the 33-file manifest "
        f"(sym-diff {sorted(covered ^ set(MANIFEST_33))}) — every manifest-33 file "
        "must be named in exactly one of the two sets, never silently absent."
    )
    assert len(DENSITY_CEILINGS) == 15, (
        f"DENSITY_CEILINGS has {len(DENSITY_CEILINGS)} entries, expected 15. If a "
        "skill's in-domain caps count crossed DENSITY_MATERIALITY_FLOOR, move it "
        "between DENSITY_CEILINGS and DENSITY_EXCLUDED_BELOW_FLOOR and update both "
        "this count and the one below to match."
    )
    assert len(DENSITY_EXCLUDED_BELOW_FLOOR) == 18, (
        f"DENSITY_EXCLUDED_BELOW_FLOOR has {len(DENSITY_EXCLUDED_BELOW_FLOOR)} "
        "entries, expected 18. If a skill's in-domain caps count crossed "
        "DENSITY_MATERIALITY_FLOOR, move it between the two sets and update both "
        "this count and the one above to match."
    )
    assert sum(DENSITY_CEILINGS.values()) == 163, (
        f"sum(DENSITY_CEILINGS.values())={sum(DENSITY_CEILINGS.values())}, expected "
        "163. A per-file ceiling was re-seeded without updating this checksum — "
        "re-sum DENSITY_CEILINGS.values() and update this constant in the same "
        "hunk as the ceiling change."
    )


def test_excluded_files_are_genuinely_below_materiality_floor():
    for skill in DENSITY_EXCLUDED_BELOW_FLOOR:
        count = _in_domain_caps_count(None if skill == "claude_md" else skill)
        assert count < DENSITY_MATERIALITY_FLOOR, (
            f"{skill} measures {count} in-domain caps tokens, which is >= the "
            f"materiality floor ({DENSITY_MATERIALITY_FLOOR}) — it should have a "
            "ceiling in DENSITY_CEILINGS instead of being excluded"
        )


@pytest.mark.parametrize("skill", sorted(DENSITY_CEILINGS))
def test_file_within_density_ceiling(skill):
    count = _in_domain_caps_count(None if skill == "claude_md" else skill)
    ceiling = DENSITY_CEILINGS[skill]
    assert count <= ceiling, (
        f"{skill}'s in-domain caps-token count is {count}, exceeds ratchet-down-only "
        f"ceiling {ceiling}. If a new MUST/NEVER/CRITICAL/etc. is a deliberate, "
        "reviewed addition, re-seed DENSITY_CEILINGS[skill] to the new count in the "
        "same hunk as the prose change — never relax the ceiling ahead of a change."
    )
