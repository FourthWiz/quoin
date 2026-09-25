"""Tests for IVG-164 stage 1 T-04/T-05: build_claude_slim.py generator.

Two independent-operands discipline (MAJ-5 r1, lesson 2026-08-02 vacuous
substring drift tests): the byte-presence tests below use an
ASSERTION-LOCAL fence-aware section parser (`_local_sections`, a deliberate
second implementation of the same ~15-line algorithm), never a call into
build_claude_slim.py's own parser, so a bug shared by both implementations
cannot make these tests vacuously pass.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_SCRIPTS = _SOURCE_ROOT / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import build_claude_slim as bcs  # noqa: E402

SOURCE = _SOURCE_ROOT / "CLAUDE.md"
SLIM = _SOURCE_ROOT / "CLAUDE.slim.md"
CATALOG = _SOURCE_ROOT / "memory" / "workflow-catalog.md"
INSTALLER = _REPO_ROOT / "src" / "quoin" / "installer.py"

# Live baseline, re-derived at implement time (D-13: this table is the
# CLASSIFICATION authority; the generator's own --census is the enumeration
# authority — both are re-verified live here, not hand-copied from the plan).
EXPECTED_KEEP_HEADINGS = [
    h for h, (cls, _t) in bcs.CLASSIFICATION.items() if cls == "keep"
]
EXPECTED_DROP_HEADINGS = [
    h for h, (cls, _t) in bcs.CLASSIFICATION.items() if cls == "drop"
]


def _local_sections(text: str) -> list[tuple[str, str]]:
    """Assertion-local fence-aware section parser (independent of build_claude_slim.py).

    Deliberately re-implements the same ~15-line algorithm as
    build_claude_slim._fence_aware_headings / parse_sections so the
    byte-presence assertions below have two independent operands.
    """
    lines = text.split("\n")
    in_fence = False
    starts: list[tuple[int, str]] = []
    pos = 0
    for ln in lines[:-1]:
        s = ln.lstrip()
        if s.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#{1,3} ", ln):
            starts.append((pos, ln))
        pos += len(ln.encode("utf-8")) + 1
    raw = text.encode("utf-8")
    starts.append((len(raw), "<EOF>"))
    out = []
    for i in range(len(starts) - 1):
        s, heading = starts[i]
        e, _ = starts[i + 1]
        out.append((heading, raw[s:e].decode("utf-8")))
    return out


def test_slim_regen_byte_identical():
    """Regenerating from the live source reproduces the committed CLAUDE.slim.md exactly."""
    source_text = SOURCE.read_text(encoding="utf-8")
    slim_text, _catalog_text = bcs.build_outputs(source_text)
    assert slim_text == SLIM.read_text(encoding="utf-8")


def test_catalog_regen_byte_identical():
    """Regenerating from the live source reproduces the committed workflow-catalog.md exactly."""
    source_text = SOURCE.read_text(encoding="utf-8")
    _slim_text, catalog_text = bcs.build_outputs(source_text)
    assert catalog_text == CATALOG.read_text(encoding="utf-8")


def test_every_drop_section_bytes_present_in_catalog():
    """Every drop-row's FULL section bytes (independent parse) appear in the catalog.

    LEFT operand: assertion-local parse of the live quoin/CLAUDE.md. RIGHT
    operand: quoin/memory/workflow-catalog.md read from disk. Neither side
    calls into the generator — this is the test the plan Acceptance section
    designates as AC-2's content-level closure.
    """
    source_text = SOURCE.read_text(encoding="utf-8")
    catalog_text = CATALOG.read_text(encoding="utf-8")
    sections = dict(_local_sections(source_text))
    for heading in EXPECTED_DROP_HEADINGS:
        assert heading in sections, f"drop heading not found in live source: {heading!r}"
        assert sections[heading] in catalog_text, (
            f"full section bytes for {heading!r} not found verbatim in workflow-catalog.md"
        )


def test_every_keep_section_bytes_present_in_slim():
    """Every keep-row's FULL section bytes (independent parse) appear in CLAUDE.slim.md."""
    source_text = SOURCE.read_text(encoding="utf-8")
    slim_text = SLIM.read_text(encoding="utf-8")
    sections = dict(_local_sections(source_text))
    for heading in EXPECTED_KEEP_HEADINGS:
        assert heading in sections, f"keep heading not found in live source: {heading!r}"
        assert sections[heading] in slim_text, (
            f"full section bytes for {heading!r} not found verbatim in CLAUDE.slim.md"
        )


def test_classification_table_matches_plan_table():
    """CLASSIFICATION has exactly 12 keep rows / 24 drop rows with the expected byte sums.

    Hard-coded mirror of a plan decision (T-04's 36-row table) — a wrong
    membership reds here even though every content assertion above would
    still pass (e.g. additionally keeping ### Task profiles, or additionally
    dropping ## Task subfolder convention). Byte sums computed here via the
    assertion-local parser, independent of the generator's own --census.
    """
    assert len(EXPECTED_KEEP_HEADINGS) == 12, EXPECTED_KEEP_HEADINGS
    assert len(EXPECTED_DROP_HEADINGS) == 24, EXPECTED_DROP_HEADINGS

    source_text = SOURCE.read_text(encoding="utf-8")
    sections = dict(_local_sections(source_text))
    keep_bytes = sum(len(sections[h].encode("utf-8")) for h in EXPECTED_KEEP_HEADINGS)
    drop_bytes = sum(len(sections[h].encode("utf-8")) for h in EXPECTED_DROP_HEADINGS)
    assert keep_bytes == 7371, keep_bytes
    assert drop_bytes == 31926, drop_bytes  # IVG-263 tier-table edit (sleep/cleanup to Sonnet)


def test_classification_table_is_bijective_with_source_headings():
    """CLASSIFICATION's heading set equals the live source's heading set, both directions."""
    source_text = SOURCE.read_text(encoding="utf-8")
    source_headings = {h for _pos, h in bcs._fence_aware_headings(source_text)}
    table_headings = set(bcs.CLASSIFICATION.keys())
    assert source_headings == table_headings, (
        source_headings.symmetric_difference(table_headings)
    )


def test_unclassified_heading_aborts(tmp_path):
    """A source heading with no CLASSIFICATION entry aborts non-zero (fail-closed)."""
    source_text = SOURCE.read_text(encoding="utf-8")
    mutated = source_text + "\n### A Wholly New Unclassified Heading\n\nBody.\n"
    with pytest.raises(bcs.ClassificationError):
        bcs.build_outputs(mutated)


def test_orphan_table_entry_aborts(tmp_path, monkeypatch):
    """A CLASSIFICATION entry with no matching source heading aborts non-zero."""
    source_text = SOURCE.read_text(encoding="utf-8")
    patched = dict(bcs.CLASSIFICATION)
    patched["### Nonexistent Heading Not In Source"] = ("drop", "workflow-catalog.md")
    monkeypatch.setattr(bcs, "CLASSIFICATION", patched)
    with pytest.raises(bcs.ClassificationError):
        bcs.build_outputs(source_text)


def test_fenced_pseudo_heading_not_treated_as_heading():
    """A '## Cost'-shaped line inside a fenced code block is not parsed as a heading."""
    text = (
        "# Development Workflow — Shared Rules\n\n"
        "## Working Rules\n\n"
        "```\n## Cost\nnot a real heading\n```\n\n"
        "### Git & PR Safety\n\nbody\n"
    )
    headings = [h for _pos, h in bcs._fence_aware_headings(text)]
    assert "## Cost" not in headings
    assert headings == [
        "# Development Workflow — Shared Rules",
        "## Working Rules",
        "### Git & PR Safety",
    ]


def test_container_h2_keep_stops_at_first_child_heading():
    """A container H2's section bytes run only to its own child H3, not past it."""
    text = (
        "## Working Rules\n\nintro prose\n\n"
        "### Git & PR Safety\n\nchild body\n\n"
        "## Project structure\n\nnext\n"
    )
    # Use build_claude_slim's own byte-span algorithm to pin the model, then
    # assert the "## Working Rules" span stops before "### Git & PR Safety".
    headings = bcs._fence_aware_headings(text)
    raw = text.encode("utf-8")
    headings_with_eof = headings + [(len(raw), "<EOF>")]
    idx = [h for _p, h in headings].index("## Working Rules")
    start = headings_with_eof[idx][0]
    end = headings_with_eof[idx + 1][0]
    span = raw[start:end].decode("utf-8")
    assert "### Git & PR Safety" not in span
    assert "intro prose" in span


def test_pointer_index_row_grammar_and_count():
    """Every non-header index line matches the pinned row grammar; count == drop-row count."""
    slim_text = SLIM.read_text(encoding="utf-8")
    idx = slim_text.index("Dropped sections live in full under")
    index_block = slim_text[idx:]
    lines = [ln for ln in index_block.split("\n")[1:] if ln.strip()]
    pattern = re.compile(r"^- (?P<heading>#{1,3} .+) -> (?P<target>[A-Za-z0-9._-]+\.(md|yaml))$")
    for ln in lines:
        assert pattern.match(ln), f"index row does not match grammar: {ln!r}"
    assert len(lines) == len(EXPECTED_DROP_HEADINGS) == 24


def test_index_targets_are_deployed_memory_files():
    """Every drop row's target is 'workflow-catalog.md' or a deployed Tier-1 memory file.

    Reads TIER1_MEMORY_FILES via the same AST pattern
    test_workflow_catalog_registered.py uses (not imported), so a target
    that is not a real deployed Tier-1 memory file is caught mechanically.
    """
    import ast

    installer_src = INSTALLER.read_text(encoding="utf-8")
    tree = ast.parse(installer_src)
    tier1: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TIER1_MEMORY_FILES":
                    if isinstance(node.value, ast.Tuple):
                        tier1 = [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
    memory_dir = _SOURCE_ROOT / "memory"
    catalog_targets = 0
    guide_targets = set()
    for heading in EXPECTED_DROP_HEADINGS:
        _cls, tgt = bcs.CLASSIFICATION[heading]
        assert tgt is not None
        if tgt == "workflow-catalog.md":
            catalog_targets += 1
            continue
        guide_targets.add(tgt)
        assert (memory_dir / tgt).exists(), f"index target not a real file: {tgt}"
        assert tgt in tier1, f"index target not in TIER1_MEMORY_FILES: {tgt}"
    # 13 catalog targets (not the plan's original 12 — round-4 critic MIN-3
    # additionally re-targets row 35 "### /sleep importance signals" from
    # sleep-signals.yaml to workflow-catalog.md, since the YAML carries the
    # row's data, not its prose). 9 distinct guides across the 11 remaining
    # guide-bearing rows.
    assert catalog_targets == 13, catalog_targets
    assert len(guide_targets) == 9, guide_targets


def test_index_targets_mutation_proof():
    """Pointing one row at a nonexistent guide must break the reachability property.

    Mutation proof for test_index_targets_are_deployed_memory_files (review-1.md
    MINOR 1): re-runs that test's own reachability loop — not just an on-disk
    existence check — against a locally mutated `patched` CLASSIFICATION dict,
    and asserts the loop actually goes red (AssertionError) on the mutated row.
    A vacuous version of this test would build `patched` and never feed it back
    into any assertion logic; this version does.
    """
    patched = dict(bcs.CLASSIFICATION)
    patched["### Serena (conditional)"] = ("drop", "nonexistent-guide.md")
    memory_dir = _SOURCE_ROOT / "memory"

    def _run_reachability_loop(classification: dict[str, tuple[str, str | None]]) -> None:
        """Same assertion shape as test_index_targets_are_deployed_memory_files'
        guide-existence check, run against whichever CLASSIFICATION is passed in."""
        for heading in EXPECTED_DROP_HEADINGS:
            _cls, tgt = classification[heading]
            assert tgt is not None
            if tgt == "workflow-catalog.md":
                continue
            assert (memory_dir / tgt).exists(), f"index target not a real file: {tgt}"

    # Control: the real (unpatched) table passes.
    _run_reachability_loop(bcs.CLASSIFICATION)

    # Mutation proof: the patched table must go red on the mutated row.
    with pytest.raises(AssertionError, match="nonexistent-guide.md"):
        _run_reachability_loop(patched)


def test_slim_headings_subset_of_source_headings():
    """The fence-aware heading set of CLAUDE.slim.md is a subset of CLAUDE.md's.

    Both sides parsed with the SAME assertion-local helper (two-independent-
    operands discipline is about the byte content, not the parser itself —
    here the parser is intentionally shared because the property under test
    IS about heading-set membership, which only build_claude_slim's own
    fence-aware algorithm can define consistently).
    """
    source_headings = {h for _p, h in bcs._fence_aware_headings(SOURCE.read_text(encoding="utf-8"))}
    slim_headings = {h for _p, h in bcs._fence_aware_headings(SLIM.read_text(encoding="utf-8"))}
    assert slim_headings <= source_headings, slim_headings - source_headings
    assert slim_headings == set(EXPECTED_KEEP_HEADINGS)


def test_slim_placeholder_count_derived_not_magic():
    """slim's __QUOIN_HOME__ count == 1 (index header) + occurrences inside keep sections."""
    source_text = SOURCE.read_text(encoding="utf-8")
    sections = dict(_local_sections(source_text))
    keep_placeholder_occurrences = sum(
        sections[h].count("__QUOIN_HOME__") for h in EXPECTED_KEEP_HEADINGS
    )
    expected = 1 + keep_placeholder_occurrences
    slim_text = SLIM.read_text(encoding="utf-8")
    assert slim_text.count("__QUOIN_HOME__") == expected == 2


def test_check_mode_exits_clean_on_committed_outputs():
    """`--check` exits 0 when the committed outputs match a fresh regen."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "build_claude_slim.py"), "--check"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _copy_repo_for_check_mutation(tmp_path: Path) -> Path:
    """Copy the pieces build_claude_slim.py --check needs into a scratch repo
    root, so the exit-7 mutation test below never writes to (or has a crash
    window that could leave dirty) the committed CLAUDE.slim.md in the real
    tree (review-1.md MINOR 11)."""
    import shutil

    scratch_repo = tmp_path / "repo_copy"
    scratch_source_root = scratch_repo / "quoin"
    (scratch_source_root / "scripts").mkdir(parents=True)
    (scratch_source_root / "memory").mkdir()
    shutil.copy(_SCRIPTS / "build_claude_slim.py", scratch_source_root / "scripts" / "build_claude_slim.py")
    shutil.copy(SOURCE, scratch_source_root / "CLAUDE.md")
    shutil.copy(SLIM, scratch_source_root / "CLAUDE.slim.md")
    shutil.copy(CATALOG, scratch_source_root / "memory" / "workflow-catalog.md")
    return scratch_repo


def test_check_mode_exits_7_on_one_character_edit(tmp_path):
    """`--check` exits 7 after a one-character edit to either generated output.

    Runs against a scratch copy of the repo (not the committed tree) so a
    subprocess crash between write and restore can never leave the real
    CLAUDE.slim.md dirty.
    """
    scratch_repo = _copy_repo_for_check_mutation(tmp_path)
    scratch_slim = scratch_repo / "quoin" / "CLAUDE.slim.md"
    scratch_slim.write_text(scratch_slim.read_text(encoding="utf-8") + "x", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(scratch_repo / "quoin" / "scripts" / "build_claude_slim.py"), "--check"],
        cwd=str(scratch_repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 7, result.stdout + result.stderr


def test_deployed_copy_refuses_to_write_even_when_source_exists(tmp_path):
    """Round-2 minor 2 regression: a DEPLOYED copy (fake ~/.claude/scripts/)
    whose inferred repo root happens to contain quoin/CLAUDE.md must refuse the
    default write mode instead of silently overwriting the checkout's generated
    files. The old guard tested only source ABSENCE, so this scenario wrote."""
    fake_home = tmp_path / "home"
    deployed_scripts = fake_home / ".claude" / "scripts"
    deployed_scripts.mkdir(parents=True)
    deployed_script = deployed_scripts / "build_claude_slim.py"
    deployed_script.write_text(
        (_SCRIPTS / "build_claude_slim.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Fake checkout under the inferred repo root ($HOME): quoin/CLAUDE.md exists.
    fake_source_root = fake_home / "quoin"
    (fake_source_root / "memory").mkdir(parents=True)
    (fake_source_root / "CLAUDE.md").write_text(
        SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    sentinel = "PRE-EXISTING CONTENT MUST SURVIVE\n"
    (fake_source_root / "CLAUDE.slim.md").write_text(sentinel, encoding="utf-8")
    (fake_source_root / "memory" / "workflow-catalog.md").write_text(
        sentinel, encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(deployed_script)],
        cwd=str(fake_home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing to write" in result.stderr, result.stderr
    assert (fake_source_root / "CLAUDE.slim.md").read_text(encoding="utf-8") == sentinel
    assert (
        fake_source_root / "memory" / "workflow-catalog.md"
    ).read_text(encoding="utf-8") == sentinel


def test_placeholder_constant_survives_install_substitution():
    """Round-2 minor 2 (second half): the script's own placeholder constant must
    not literally contain the __QUOIN_HOME__ token as one contiguous source
    literal in its assignment, or the installer's deploy-time substitution
    rewrites the deployed copy's constant into an absolute home path (which then
    leaks into any output the deployed copy produces)."""
    text = (_SCRIPTS / "build_claude_slim.py").read_text(encoding="utf-8")
    assign_lines = [
        l for l in text.splitlines() if l.startswith("_QUOIN_HOME_PLACEHOLDER =")
    ]
    assert len(assign_lines) == 1, assign_lines
    assert "__QUOIN_HOME__" not in assign_lines[0], (
        "the placeholder-constant assignment carries the contiguous token; "
        "deploy substitution would rewrite it — keep it split across adjacent "
        "string literals"
    )
