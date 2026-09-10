"""Unit tests for comment_cleanup.py.

Loaded via a sys.path insert + plain import (not spec_from_file_location) —
the module under test does a sibling-core import (`from
authored_content_lint import ...`) that only resolves when its own directory
is already on sys.path, mirroring test_authored_content_lint.py.

Two sections: pure-layer tests operate on strings and Block objects with no
git or filesystem; the pinned-corpus and git-fixture tests read the five
real, live source files this cleanup pass was measured against
(dashboard_model.py, quoin_claude.py, simple_claude.py, context_bundle.py,
run_benchmark.py) — never an embedded copy, so an edit to a measured file
fails this test loudly rather than letting the yield claim silently drift.
"""

import ast
import itertools
import json
import os
import random
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CORE = Path(__file__).parent.parent.parent / "core" / "scripts"
sys.path.insert(0, str(_CORE))
import comment_cleanup as cc  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Git repo fixture helpers
# ---------------------------------------------------------------------------

def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "switch", "-c", "feature")
    return repo


def _commit_all(repo, message="commit"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _block(joined, is_trailing=False, code_prefix="", indent="", width=None):
    return cc.Block(
        start=1, end=1, indent=indent, raw_lines=[joined], joined=joined,
        is_trailing=is_trailing, code_prefix=code_prefix,
        width=width if width is not None else len(joined) + len(indent) + 2,
    )


# ---------------------------------------------------------------------------
# Docstring exclusion + block grouping
# ---------------------------------------------------------------------------

def test_docstring_exclusion_set_covers_module_docstring():
    text = (
        '"""Title\n'
        "\n"
        "# Heading\n"
        "# ---\n"
        '"""\n'
        "\n"
        "x = 1\n"
    )
    exclusion = cc.docstring_exclusion_lines(text)
    assert exclusion == {1, 2, 3, 4, 5}


def test_docstring_lines_never_enter_a_block():
    text = (
        '"""Title\n'
        "# Heading\n"
        '"""\n'
        "x = 1\n"
    )
    exclusion = cc.docstring_exclusion_lines(text)
    blocks = cc.group_blocks("m.py", text, exclusion)
    assert blocks == []


def test_consecutive_same_indent_lines_group_into_one_block():
    text = "def f():\n    # line one\n    # line two\n    return 1\n"
    blocks = cc.group_blocks("m.py", text, set())
    assert len(blocks) == 1
    assert blocks[0].start == 2 and blocks[0].end == 3


def test_indent_change_splits_the_group():
    text = "# top level\n    # indented\nx = 1\n"
    blocks = cc.group_blocks("m.py", text, set())
    assert len(blocks) == 2


def test_trailing_comment_is_a_singleton_block_with_code_prefix():
    text = "x = 1  # a trailing comment\n"
    blocks = cc.group_blocks("m.py", text, set())
    assert len(blocks) == 1
    assert blocks[0].is_trailing
    assert blocks[0].code_prefix == "x = 1  "
    assert blocks[0].indent == ""


def test_trailing_comment_survives_hash_inside_string_literal():
    # code_prefix must come from rfind on the region's own token text, never
    # the first '#' — which here sits inside a string literal.
    text = 'x = "a#b"  # an earlier fix, foo\n'
    blocks = cc.group_blocks("m.py", text, set())
    assert len(blocks) == 1
    assert blocks[0].code_prefix == 'x = "a#b"  '


# ---------------------------------------------------------------------------
# Archaeology and pointer discriminators
# ---------------------------------------------------------------------------

def test_hyphenated_archaeology_requires_terminal_punctuation():
    assert cc.match_archaeology("keeps the pre-fix line-buffered read") is None
    assert cc.match_archaeology("pre-fix, this returned") is not None


def test_multiword_archaeology_case_insensitive():
    assert cc.match_archaeology("An Earlier Fix that only flushed") is not None


def test_measured_adjectival_blocks_are_rejected():
    joined_texts = [
        "See simple_claude.invoke for the full rationale — a real OS pipe gets the "
        "chunk-based, timer-driven read; a test double without a usable fd keeps the "
        "pre-fix line-buffered read.",
        "A real OS pipe has a usable fd — that's the case this fix targets. A test "
        "double or anything else without one keeps the pre-fix line-buffered read.",
        "Summary byte clamp: the pre-fix first-line emission had an implicit bound "
        "that full-block emission removed.",
    ]
    for joined in joined_texts:
        assert cc.match_archaeology(joined) is None


def test_bare_pointer_stock_filler():
    m = cc._POINTER_RE.search("see simple_claude.invoke for the full rationale.")
    assert m is not None
    assert cc.is_bare_pointer("see simple_claude.invoke for the full rationale.", m)


def test_pointer_with_substantive_tail_is_not_bare():
    text = "see simple_claude.invoke for the full rationale; both cells share the same streaming shape."
    m = cc._POINTER_RE.search(text)
    assert not cc.is_bare_pointer(text, m)


def test_see_above_is_not_a_pointer():
    assert cc._POINTER_RE.search("see above") is None


def test_judge_marker_prefilter():
    assert cc.is_judge_candidate("This is not a bug, contrary to appearances.")
    assert not cc.is_judge_candidate("A plain comment with no marker form.")


# ---------------------------------------------------------------------------
# Clause span and reflow
# ---------------------------------------------------------------------------

def test_clause_span_none_when_no_preceding_separator():
    joined = "An earlier fix did the thing."
    m = cc.match_archaeology(joined)
    assert cc.separable_clause_span(joined, m.start(), m.end()) is None


def test_dashboard_model_parenthetical_seam_character_exact():
    # The pinned dashboard_model.py:345-347 shape: a trailing parenthetical
    # immediately preceded by a space, tail is a single period. Must reflow
    # with no seam defect (no double period, no missing period).
    joined = (
        'Merge provider output over counts default. "partial" propagates the '
        "never-silent-$0 signal into the emitted dashboard JSON "
        "(MAJOR-1 fix — previously dropped here)."
    )
    m = cc.match_archaeology(joined)
    span = cc.separable_clause_span(joined, m.start(), m.end())
    assert span is not None
    block = _block(joined)
    result = cc.reflow(block, span)
    assert result == [
        '# Merge provider output over counts default. "partial" propagates the '
        "never-silent-$0 signal into the emitted dashboard JSON."
    ]


def test_substantive_trailing_text_clamped_character_exact():
    # The clamp preserves a real trailing sentence, and the seam-repair
    # clause terminates the head with exactly one period, no more.
    joined = "Buffered read is required here, an earlier fix did X. Always call foo() before bar()."
    m = cc.match_archaeology(joined)
    assert (m.start(), m.end()) == (32, 46)
    span = cc.separable_clause_span(joined, m.start(), m.end())
    assert span == (30, 53)
    block = _block(joined)
    result = cc.reflow(block, span)
    assert result == ["# Buffered read is required here. Always call foo() before bar()."]


def test_block_entirely_one_clause_reflows_to_none():
    joined = "An earlier fix did X."
    block = _block(joined)
    # No separator precedes the match -> no span -> nothing to test via
    # reflow; the block-is-fully-archaeological case routes to a whole-block
    # removal, not an excise, so this exercises decide_category_1 instead.
    assert cc.all_sentences_archaeological(joined)


def test_synthetic_separator_and_conjunction_reflow():
    # The pinned corpus never exercises the leading-separator +
    # coordinating-conjunction strip inside a fully-excised tail; only a
    # synthetic fixture does.
    joined = "Keep this part, and an earlier fix broke it entirely for good."
    m = cc.match_archaeology(joined)
    span = cc.separable_clause_span(joined, m.start(), m.end())
    block = _block(joined)
    result = cc.reflow(block, span)
    assert result == ["# Keep this part."]


def test_reflow_output_never_exceeds_original_block_width():
    long_comment = "# " + ("word " * 40) + ", an earlier fix did this too."
    text = long_comment + "\n"
    blocks = cc.group_blocks("m.py", text, set())
    block = blocks[0]
    m = cc.match_archaeology(block.joined)
    span = cc.separable_clause_span(block.joined, m.start(), m.end())
    result = cc.reflow(block, span)
    assert result is not None
    for line in result:
        assert len(line) <= block.width


def test_trailing_comment_multiline_wrap_abandons():
    long_comment = "Keep this short lead-in, an earlier fix did the thing, " + ("word " * 30) + "trailing."
    text = f"x = 1  # {long_comment}\n"
    blocks = cc.group_blocks("m.py", text, set())
    block = blocks[0]
    m = cc.match_archaeology(block.joined)
    assert m is not None
    span = cc.separable_clause_span(block.joined, m.start(), m.end())
    result = cc.reflow(block, span)
    # Either abandoned (None) or a single re-wrapped line — never multiple.
    assert result is None or len(result) == 1


def test_docstring_heading_line_with_archaeology_phrase_survives_every_removal_path():
    # A "#"-leading docstring line that itself carries an archaeology phrase
    # must never form a block — the docstring exclusion set is applied
    # before grouping, so this content is inert to every removal path
    # (decide_file included), not merely to group_blocks in isolation.
    text = (
        '"""Title\n'
        "\n"
        "# an earlier fix note kept for historical context, entirely done.\n"
        "# ---\n"
        '"""\n'
        "\n"
        "x = 1\n"
    )
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert not decision.changed


def test_archaeology_match_spans_original_line_break():
    # The phrase is split across two physical comment lines pre-join;
    # match_archaeology must find it in the block's single joined string.
    text = (
        "def f():\n"
        "    # an earlier\n"
        "    # fix broke this decisively for good.\n"
        "    return 1\n"
    )
    blocks = cc.group_blocks("m.py", text, set())
    assert len(blocks) == 1
    block = blocks[0]
    assert block.joined == "an earlier fix broke this decisively for good."
    assert cc.match_archaeology(block.joined) is not None


def test_clause_span_last_separator_not_first():
    joined = "First, second, an earlier fix did the thing."
    m = cc.match_archaeology(joined)
    span = cc.separable_clause_span(joined, m.start(), m.end())
    assert span is not None
    assert span[0] == joined.rindex(",", 0, m.start())


def test_clause_span_mid_block_parenthetical_falls_through_to_separator():
    joined = (
        "This part must stay exactly as documented for the API contract "
        "clearly. An earlier fix broke this here (previously dropped "
        "silently). Still matters for the caller."
    )
    m = cc.match_archaeology(joined)
    assert m is not None
    span = cc.separable_clause_span(joined, m.start(), m.end())
    assert span is not None
    open_idx = joined.index("(")
    close_idx = joined.index(")")
    # The parenthetical does not reach end-of-block -- real text follows it
    # -- so step 1 must not select it; the span comes from the last
    # separator before the match instead.
    assert span != (open_idx, close_idx + 1)
    block = _block(joined)
    result = cc.reflow(block, span)
    assert result is not None
    joined_result = " ".join(result)
    assert "Still matters for the caller." in joined_result
    assert "previously dropped" not in joined_result


def test_reflow_appends_terminal_period_when_residue_lacks_punctuation():
    joined = "Keep this part, an earlier fix did X entirely for good"
    m = cc.match_archaeology(joined)
    span = cc.separable_clause_span(joined, m.start(), m.end())
    block = _block(joined)
    result = cc.reflow(block, span)
    assert result == ["# Keep this part."]


def test_reflow_abandons_when_residue_is_empty():
    joined = "An earlier fix did the thing entirely for good."
    block = _block(joined)
    result = cc.reflow(block, (0, len(joined)))
    assert result is None


def test_reflow_preserves_block_indent():
    text = (
        "def f():\n"
        "        # Keep this, an earlier fix did X here entirely for good.\n"
        "        return 1\n"
    )
    blocks = cc.group_blocks("m.py", text, set())
    block = blocks[0]
    m = cc.match_archaeology(block.joined)
    span = cc.separable_clause_span(block.joined, m.start(), m.end())
    result = cc.reflow(block, span)
    assert result is not None
    for line in result:
        assert line.startswith(block.indent + "# ")


def test_all_sentences_archaeological_requires_every_sentence_to_match():
    both = "An earlier fix did X. Previously dropped for good reason."
    assert cc.all_sentences_archaeological(both)
    mixed = "An earlier fix did X. This sentence has no archaeology phrase at all."
    assert not cc.all_sentences_archaeological(mixed)


# ---------------------------------------------------------------------------
# Pinned measured-corpus fixture — reads the five live source files
# ---------------------------------------------------------------------------

_DASHBOARD_MODEL = "quoin/core/scripts/dashboard_model.py"
_QUOIN_CLAUDE = "quoin/benchmarks/harness/cells/quoin_claude.py"
_SIMPLE_CLAUDE = "quoin/benchmarks/harness/cells/simple_claude.py"
_CONTEXT_BUNDLE = "quoin/scripts/context_bundle.py"
_RUN_BENCHMARK = "quoin/benchmarks/scripts/run_benchmark.py"


def _read_live(relpath):
    path = REPO_ROOT / relpath
    if not path.exists():
        pytest.skip(f"pinned corpus file not found at {path}; repo layout changed")
    return path.read_text(encoding="utf-8")


def _block_level_verdict_counts(relpath, text):
    """Re-derive category 1 and category 2's per-block verdicts directly
    (mirroring decide_file's own block loop) so the excision count can be
    asserted exactly, instead of inferring it from residue substrings or a
    line-count inequality that a whole-block removal would also satisfy."""
    exclusion = cc.docstring_exclusion_lines(text)
    blocks = cc.group_blocks(relpath, text, exclusion)
    blocks = [b for b in blocks if not cc._has_pragma(b) and not cc._has_directive(b)]
    cat2 = cc._category2_verdicts(blocks, None, retain=1)
    counts = {"excise": 0, "remove_block": 0}
    for block in blocks:
        cat1 = cc.decide_category_1(block, None, relpath, include_tests=False)
        for verdict in (cat1, cat2.get(id(block))):
            if verdict is None:
                continue
            kind, _span = verdict
            if kind in counts:
                counts[kind] += 1
    return counts


def test_pinned_corpus_yields_exactly_six_excisions():
    dashboard_text = _read_live(_DASHBOARD_MODEL)
    quoin_claude_text = _read_live(_QUOIN_CLAUDE)
    simple_claude_text = _read_live(_SIMPLE_CLAUDE)

    d1 = cc.decide_file(_DASHBOARD_MODEL, dashboard_text, None, retain=1, include_tests=False)
    d2 = cc.decide_file(_QUOIN_CLAUDE, quoin_claude_text, None, retain=1, include_tests=False)
    d3 = cc.decide_file(_SIMPLE_CLAUDE, simple_claude_text, None, retain=1, include_tests=False)

    assert d1.changed and d2.changed and d3.changed

    assert '# ... into the emitted dashboard JSON.'.strip("# .") in d1.new_text or (
        "dashboard JSON.\n" in d1.new_text
    )
    assert "dashboard JSON.." not in d1.new_text
    assert "AS IT ARRIVES.\n" in d2.new_text
    assert "not event-driven.\n" in d2.new_text
    assert "how the block above exits.\n" in d2.new_text
    assert "worth recovering.\n" in d2.new_text
    assert "worth recovering.\n" in d3.new_text

    # Count block-level verdicts directly: 1 category-1 excision each in
    # dashboard_model.py, quoin_claude.py and simple_claude.py, plus 3
    # category-2 excisions in quoin_claude.py (428-429, 525, 597-598) — 6
    # total, and zero whole-block removals anywhere in the pinned corpus.
    totals = {"excise": 0, "remove_block": 0}
    for relpath, text in (
        (_DASHBOARD_MODEL, dashboard_text),
        (_QUOIN_CLAUDE, quoin_claude_text),
        (_SIMPLE_CLAUDE, simple_claude_text),
    ):
        counts = _block_level_verdict_counts(relpath, text)
        totals["excise"] += counts["excise"]
        totals["remove_block"] += counts["remove_block"]
    assert totals == {"excise": 6, "remove_block": 0}


def test_pinned_corpus_untouched_files_stay_byte_identical():
    context_bundle_text = _read_live(_CONTEXT_BUNDLE)
    run_benchmark_text = _read_live(_RUN_BENCHMARK)
    d4 = cc.decide_file(_CONTEXT_BUNDLE, context_bundle_text, None, retain=1, include_tests=False)
    d5 = cc.decide_file(_RUN_BENCHMARK, run_benchmark_text, None, retain=1, include_tests=False)
    assert not d4.changed
    assert not d5.changed


def test_pinned_corpus_pointer_block_census():
    quoin_claude_text = _read_live(_QUOIN_CLAUDE)
    exclusion = cc.docstring_exclusion_lines(quoin_claude_text)
    blocks = cc.group_blocks(_QUOIN_CLAUDE, quoin_claude_text, exclusion)
    pointer_linenos = sorted(
        b.start for b in blocks if cc._POINTER_RE.search(b.joined)
    )
    assert pointer_linenos == [402, 416, 428, 437, 443, 455, 525, 540, 556, 575, 597, 663]
    assert 464 not in [b.start for b in blocks]
    assert 464 in exclusion


def test_pinned_corpus_docstring_hits_byte_identical():
    simple_claude_text = _read_live(_SIMPLE_CLAUDE)
    run_benchmark_text = _read_live(_RUN_BENCHMARK)
    exclusion_sc = cc.docstring_exclusion_lines(simple_claude_text)
    exclusion_rb = cc.docstring_exclusion_lines(run_benchmark_text)
    assert 115 in exclusion_sc
    assert 371 in exclusion_rb


# ---------------------------------------------------------------------------
# Git fixtures
# ---------------------------------------------------------------------------

def test_pragma_survives_category_1(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # an earlier fix did the thing here entirely.  quoin-lint: allow\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert not decision.changed


def test_pragma_survives_category_2(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # see helpers.util for details.\n"
        "    return 1\n"
        "\n"
        "\n"
        "def g():\n"
        "    # see helpers.util for details.  quoin-lint: allow\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    # Without the pragma, this would be the second occurrence of the same
    # referent and bare-pointer eligible for excision; the pragma removes
    # the block from consideration before category 2 ever orders it.
    assert not decision.changed


def test_pragma_survives_category_3(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # This is not a bug, contrary to appearances.  quoin-lint: allow\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    out = cc.emit_candidates(repo, "main", judge_max=40)
    assert out["count"] == 0
    assert out["candidates"] == []


@pytest.mark.parametrize(
    "directive_line",
    [
        "    # pyright: ignore[reportAny]",
        "    # pytype: disable=attribute-error",
        "    # nosec",
        "    # yapf: disable",
        "    # noinspection PyUnresolvedReferences",
        "    # skipcq: PTC-W0033",
        "    # sourcery skip: no-conditionals",
        "    # type : ignore",
    ],
)
def test_directive_re_covers_extended_markers(directive_line):
    assert cc._DIRECTIVE_RE.search(directive_line) is not None


def test_reflow_suppresses_terminal_period_on_directive_residue():
    prefix = "An earlier fix noted this and"
    tail = " pyright: ignore[reportAny]"
    block = _block(prefix + tail, width=200)
    result = cc.reflow(block, (0, len(prefix)))
    assert result is not None
    assert result[0].rstrip().endswith("]")


def test_reflow_still_appends_terminal_period_without_directive():
    prefix = "An earlier fix noted this and"
    tail = " the value stays here"
    block = _block(prefix + tail, width=200)
    result = cc.reflow(block, (0, len(prefix)))
    assert result is not None
    assert result[0].rstrip().endswith(".")


def test_directive_comment_survives_reflow(tmp_path):
    # A block containing a tool-directive line (here, `# type: ignore`) must
    # be vetoed entirely, not reflowed into free prose alongside the
    # archaeological line it happens to sit next to.
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # Do the thing, an earlier fix returned None here.\n"
        "    # type: ignore[arg-type]\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert not decision.changed


def test_atomic_write_preserves_destination_file_mode(tmp_path):
    target = tmp_path / "m.py"
    target.write_text("x = 1\n", encoding="utf-8")
    target.chmod(0o755)
    cc.atomic_write(target, "x = 2\n")
    assert target.read_text(encoding="utf-8") == "x = 2\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_non_py_file_reported_not_written(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.sh"
    original = "echo hi  # do the setup, an earlier fix did the thing here.\n"
    src.write_text(original, encoding="utf-8")
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.sh", text, None, retain=1, include_tests=False)
    # decide_file computes the decision regardless of suffix; the CLI's
    # apply step is what gates writes to .py only. Confirm the decision
    # layer still proposes a change...
    assert decision.changed
    # ...but the file on disk is untouched unless the CLI writes it.
    assert src.read_text(encoding="utf-8") == original


def test_test_path_archaeology_reported_not_removed(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tests").mkdir()
    src = repo / "tests" / "test_m.py"
    src.write_text(
        "def test_f():\n"
        "    # an earlier fix did this exact thing.\n"
        "    assert True\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("tests/test_m.py", text, None, retain=1, include_tests=False)
    assert not decision.changed


def test_include_tests_env_widens(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_COMMENT_CLEANUP_INCLUDE_TESTS", "1")
    text = "def test_f():\n    # an earlier fix did this exact thing.\n    assert True\n"
    decision = cc.decide_file("tests/test_m.py", text, None, retain=1, include_tests=True)
    assert decision.changed


def test_multiline_whole_block_removal_leaves_no_orphaned_fragment(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # An earlier fix did the thing. Before the fix this was broken.\n"
        "    # Previously dropped entirely, used to be missing.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert decision.changed
    assert decision.new_text == "def f():\n    return 1\n"


def test_reapplying_decide_file_is_idempotent(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # Guaranteed cleanup here, an earlier fix did X for real now.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    first = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert first.changed
    second = cc.decide_file("m.py", first.new_text, None, retain=1, include_tests=False)
    assert not second.changed


def test_two_archaeology_hits_converge_in_one_pass(tmp_path):
    # A block with two archaeological sentences (and one live one) must
    # excise both hits in the same decide_file pass; a second pass on the
    # result is then a genuine no-op, not a further shrink.
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # Alpha holds here. Foo bar, pre-fix, this returned Y. Baz used to be Z.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    first = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert first.changed
    assert "Alpha holds here" in first.new_text
    assert "pre-fix" not in first.new_text
    assert "used to be" not in first.new_text
    second = cc.decide_file("m.py", first.new_text, None, retain=1, include_tests=False)
    assert not second.changed


# ---------------------------------------------------------------------------
# CRITICAL regression — whole-block removal must never take a trailing
# comment's code line with it. All three reproduced shapes must leave the
# code intact and the file parseable.
# ---------------------------------------------------------------------------

def test_trailing_fully_archaeological_comment_never_deletes_the_statement(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f(a):\n"
        "    return a  # an earlier fix, this used to be wrong.\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    new_text = decision.new_text if decision.changed else text
    assert "return a" in new_text
    ast.parse(new_text)


def test_trailing_two_sentence_archaeological_comment_never_deletes_the_assignment(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    x = compute()  # An earlier fix. Used to be None.\n"
        "    return x\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    new_text = decision.new_text if decision.changed else text
    assert "x = compute()" in new_text
    ast.parse(new_text)


def test_trailing_archaeological_comment_never_deletes_a_call_argument(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    return g(\n"
        "        a,  # an earlier fix, previously dropped.\n"
        "    )\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    new_text = decision.new_text if decision.changed else text
    assert "a," in new_text or "a)" in new_text
    ast.parse(new_text)


def test_self_exclusion_of_own_two_paths():
    assert "quoin/core/scripts/comment_cleanup.py" in cc._EXCLUDE_PATHS
    assert "quoin/scripts/comment_cleanup.py" in cc._EXCLUDE_PATHS


def test_disable_env_exits_0_before_argparse(monkeypatch, capsys):
    monkeypatch.setenv("QUOIN_DISABLE_COMMENT_CLEANUP", "1")
    rc = cc.main(["--nonsense-flag"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"disabled": true' in out


def test_allow_dirty_without_emit_candidates_exits_2():
    with pytest.raises(SystemExit) as exc_info:
        cc.main(["--allow-dirty"])
    assert exc_info.value.code == 2


def test_cli_help_exits_0():
    with pytest.raises(SystemExit) as exc_info:
        cc.main(["--help"])
    assert exc_info.value.code == 0


def test_unresolvable_base_exits_3(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.delenv("QUOIN_BASE_BRANCH", raising=False)
    rc = cc.main(["--project-root", str(repo), "--base", "does-not-exist"])
    assert rc == 3


def test_dirty_tree_without_allow_dirty_exits_3(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
    rc = cc.main(["--project-root", str(repo), "--base", "main"])
    assert rc == 3


def test_commit_subjects_never_rewrites_git_log(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, message="feat: add a new capability")
    before = _git(repo, "log", "--format=%H %s")
    rc = cc.main(["--project-root", str(repo), "--base", "main", "--commit-subjects"])
    after = _git(repo, "log", "--format=%H %s")
    assert before == after
    assert rc == 0


def test_commit_subject_lint_assertion_via_match_taxonomy():
    msg = "chore: remove superseded and duplicated code comments"
    assert cc.match_taxonomy(msg, cc.resolve_tracker_prefixes("feat/ivg-255-extra-comments-in-pr")) is None


def test_judge_max_overflow_reports_count_emits_nothing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    lines = []
    for i in range(45):
        lines.append(f"def f_{i}():")
        lines.append(f"    # this is not a bug, it is behavior number {i}.")
        lines.append("    return 1")
        lines.append("")
    (repo / "m.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _commit_all(repo)
    monkeypatch.setenv("QUOIN_COMMENT_JUDGE_MAX", "40")
    out = cc.emit_candidates(repo, "main", judge_max=40)
    assert out["count"] > 40
    assert out["candidates"] == []


def test_emit_candidates_drops_oversized_candidate_text(tmp_path):
    repo = _init_repo(tmp_path)
    long_comment = "# this is not a bug, it just looks odd. " + ("filler " * 200)
    (repo / "m.py").write_text(
        f"def f():\n    {long_comment}\n    return 1\n", encoding="utf-8"
    )
    _commit_all(repo)
    capped = cc.emit_candidates(repo, "main", judge_max=40, text_max=50)
    assert capped["count"] == 0
    assert capped["candidates"] == []
    uncapped = cc.emit_candidates(repo, "main", judge_max=40, text_max=100_000)
    assert uncapped["count"] == 1


def test_mid_apply_failure_restores_written_files_and_reports_them(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def f():\n    return 1\n", encoding="utf-8")
    b.write_text("def g():\n    return 2\n", encoding="utf-8")
    _commit_all(repo, message="base files")
    a.write_text(
        "def f():\n"
        "    # An earlier fix did the thing here for real, entirely done.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    b.write_text(
        "def g():\n"
        "    # An earlier fix did another thing here for real, entirely done.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo, message="add comments")

    real_atomic_write = cc.atomic_write
    calls = []

    def _flaky_write(path, text):
        calls.append(Path(path).name)
        if len(calls) == 2:
            raise OSError("simulated disk failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(cc, "atomic_write", _flaky_write)

    rc = cc.main(["--project-root", str(repo), "--apply", "--base", "main"])
    captured = capsys.readouterr()

    assert rc == 3
    assert "a.py" in captured.err
    committed_a = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:a.py"], capture_output=True, text=True, check=True
    ).stdout
    assert a.read_text(encoding="utf-8") == committed_a


def test_restore_written_without_cause_keeps_implicit_context(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    src.write_text("x = 2\n", encoding="utf-8")
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            cc.restore_written(repo, ["m.py"])
        pytest.fail("expected Undeterminable")
    except cc.Undeterminable as exc:
        assert exc.__cause__ is None
        assert exc.__suppress_context__ is False
        assert isinstance(exc.__context__, RuntimeError)


def test_restore_written_with_cause_chains_it(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    src.write_text("x = 2\n", encoding="utf-8")
    original = ValueError("disk full")
    try:
        cc.restore_written(repo, ["m.py"], cause=original)
        pytest.fail("expected Undeterminable")
    except cc.Undeterminable as exc:
        assert exc.__cause__ is original


def test_untouched_file_survives_diff_scoping(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "untouched.py").write_text(
        "def g():\n"
        "    # An earlier fix did this exact thing, entirely for good.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    (repo / "touched.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "switch", "-c", "feature")
    (repo / "touched.py").write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
    _commit_all(repo, message="touch only touched.py")

    before = (repo / "untouched.py").read_text(encoding="utf-8")
    cc.main(["--project-root", str(repo), "--apply", "--base", "main"])
    assert (repo / "untouched.py").read_text(encoding="utf-8") == before


def test_emit_candidates_reflects_post_edit_worktree_not_pre_edit_frame(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    # An earlier fix did this thing here.\n"
        "    # Before the fix it broke everything badly for real this time.\n"
        "    # Previously dropped entirely, used to be missing before.\n"
        "    return 1\n"
        "\n"
        "\n"
        "def g():\n"
        "    # This is not a bug, contrary to appearances in this codebase.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo, message="base")
    pre_edit_marker_lineno = 9

    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert decision.changed
    src.write_text(decision.new_text, encoding="utf-8")

    post_edit_lines = src.read_text(encoding="utf-8").splitlines()
    post_edit_marker_lineno = next(
        i + 1 for i, line in enumerate(post_edit_lines) if "This is not a bug" in line
    )
    assert post_edit_marker_lineno != pre_edit_marker_lineno

    out = cc.emit_candidates(repo, "main", judge_max=40)
    matches = [c for c in out["candidates"] if "This is not a bug" in c["text"]]
    assert len(matches) == 1
    assert matches[0]["start"] == post_edit_marker_lineno


def test_overlapping_keep_vetoes_the_excision_it_intersects():
    text = (
        "def f():\n"
        "    # This part must stay exactly as documented for the API "
        "contract clearly. An earlier fix broke this, see module.func for "
        "the full rationale.\n"
        "    return 1\n"
    )
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert not decision.changed


def test_category3_emits_marker_block_in_untouched_region_of_touched_file(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.py"
    src.write_text(
        "def f():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def g():\n"
        "    # This is not a bug, contrary to appearances in this project.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo, message="base")
    # Touch f() only -- g()'s marker-bearing block stays on the exact lines
    # it already occupied at HEAD, an untouched *region* of a touched *file*.
    src.write_text(
        "def f():\n"
        "    return 42\n"
        "\n"
        "\n"
        "def g():\n"
        "    # This is not a bug, contrary to appearances in this project.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo, message="touch f only")

    out = cc.emit_candidates(repo, "main", judge_max=40)
    matches = [c for c in out["candidates"] if "This is not a bug" in c["text"]]
    assert len(matches) == 1
    assert matches[0]["start"] == 6


def test_pointer_retention_anchored_by_first_occurrence_even_if_unchanged():
    text = (
        "def f():\n"
        "    # see helpers.util for the full rationale.\n"
        "    return 1\n"
        "\n"
        "\n"
        "def g():\n"
        "    # Also, see helpers.util for the full rationale.\n"
        "    return 2\n"
    )
    # Only line 7 (the second occurrence) is this branch's diff -- line 2
    # (the first occurrence) predates the branch entirely and is not a
    # candidate line, yet it must still anchor the retained-count: retention
    # is ordered over the whole post-image file, not just the diff.
    decision = cc.decide_file("m.py", text, {7}, retain=1, include_tests=False)
    assert decision.changed
    new_lines = decision.new_text.splitlines()
    assert new_lines[1] == "    # see helpers.util for the full rationale."
    assert "helpers.util" not in new_lines[6]


def test_base_ref_accepts_a_non_default_parent_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "m.py").write_text(
        "def f():\n"
        "    # An earlier fix did the parent-branch thing here, entirely done.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main base")
    _git(repo, "switch", "-c", "parent-feature")
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "parent adds other.py")
    _git(repo, "switch", "-c", "stacked-feature")
    (repo / "stacked.py").write_text(
        "def g():\n"
        "    # An earlier fix did the stacked-branch thing, entirely done here.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "stacked adds stacked.py")

    before = (repo / "m.py").read_text(encoding="utf-8")
    rc = cc.main(["--project-root", str(repo), "--apply", "--base", "parent-feature"])
    # The parent branch's own pre-existing comment sits outside the
    # parent-feature..HEAD diff and must stay untouched -- the cleanup
    # diffs against the ref, not the branch name gh pr create would use.
    assert (repo / "m.py").read_text(encoding="utf-8") == before
    assert rc == 1


# ---------------------------------------------------------------------------
# CRITICAL regression — a file with non-UTF-8 bytes anywhere must never be
# silently transcoded (errors="replace" round-tripped through a whole-file
# UTF-8 rewrite turns every undecodable byte into U+FFFD, changing live
# string literals outside the edited region). The fix reads strictly and
# marks an undecodable file undeterminable instead.
# ---------------------------------------------------------------------------

def test_resolve_worktree_text_raises_on_non_utf8_bytes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "x.py"
    f.write_bytes(b"NAME = \"caf\xe9\"\n")
    with pytest.raises(UnicodeDecodeError):
        cc.resolve_worktree_text(repo, "x.py")


def test_non_utf8_untouched_bytes_never_transcoded(tmp_path, capsys):
    # The non-UTF-8 byte is committed on the base, unchanged on the branch —
    # matching the reported repro exactly, including why it survives to
    # decide_file at all: `git diff -U0 <base> HEAD` only emits *changed*
    # lines, so a file-scoped fix that only guarded the diff step would
    # still pass here. The comment removal candidate is on a separate,
    # pure-ASCII line that IS part of the diff.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    src = repo / "m.py"
    src.write_bytes(b'NAME = "caf\xe9"\n' b"z = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base file with a non-utf8 byte")
    _git(repo, "switch", "-c", "feature")
    src.write_bytes(
        b'NAME = "caf\xe9"\n'
        b"z = 0  # keep this; an earlier fix did the thing here.\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "touch only the pure-ascii line")

    before = src.read_bytes()
    rc = cc.main(
        ["--project-root", str(repo), "--apply", "--base", "main", "--format", "json"]
    )
    after = src.read_bytes()
    assert b"\xef\xbf\xbd" not in after, "must never transcode undecodable bytes to U+FFFD"
    # A file that fails to decode is skipped whole, not partially rewritten
    # -- decide_file never runs on text the tool couldn't read strictly.
    assert after == before
    assert rc == 0
    out = capsys.readouterr().out
    assert '"undeterminable_files"' in out
    assert "m.py" in out


def test_mid_apply_failure_reports_written_files_in_stdout_json(tmp_path, monkeypatch, capsys):
    """MAJOR 2 companion: main's exception path must print the JSON result
    (with the files written so far) to stdout before returning exit 3 -- an
    exit-3 caller must be able to identify affected files from stdout, not
    only from the stderr message."""
    repo = _init_repo(tmp_path)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def f():\n    return 1\n", encoding="utf-8")
    b.write_text("def g():\n    return 2\n", encoding="utf-8")
    _commit_all(repo, message="base files")
    a.write_text(
        "def f():\n"
        "    # An earlier fix did the thing here for real, entirely done.\n"
        "    return 1\n",
        encoding="utf-8",
    )
    b.write_text(
        "def g():\n"
        "    # An earlier fix did another thing here for real, entirely done.\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(repo, message="add comments")

    real_atomic_write = cc.atomic_write
    calls = []

    def _flaky_write(path, text):
        calls.append(Path(path).name)
        if len(calls) == 2:
            raise OSError("simulated disk failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(cc, "atomic_write", _flaky_write)

    rc = cc.main(
        ["--project-root", str(repo), "--apply", "--base", "main", "--format", "json"]
    )
    captured = capsys.readouterr()

    assert rc == 3
    assert captured.out.strip(), "exit 3 must not leave stdout empty"
    payload = json.loads(captured.out)
    assert "a.py" in payload.get("written", []), (
        "the JSON result must name the file written before the failure"
    )
    assert "error" in payload


# ---------------------------------------------------------------------------
# MAJOR regression — atomic_write must refuse a non-regular destination
# (symlink) rather than replacing it with a regular file, and must refuse
# to write when the destination's current bytes are not valid UTF-8 (a
# byte-level backstop for the same corruption resolve_worktree_text's
# strict read exists to prevent).
# ---------------------------------------------------------------------------

def test_atomic_write_refuses_symlink_destination(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(outside)
    with pytest.raises(cc.Undeterminable):
        cc.atomic_write(link, "x = 2\n")
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "x = 1\n"


def test_atomic_write_refuses_non_utf8_destination(tmp_path):
    target = tmp_path / "m.py"
    target.write_bytes(b'NAME = "caf\xe9"\n')
    with pytest.raises(cc.Undeterminable):
        cc.atomic_write(target, "NAME = 'new'\n")
    assert target.read_bytes() == b'NAME = "caf\xe9"\n'


def test_emit_candidates_reports_symlink_as_undeterminable(tmp_path):
    """The apply path already surfaces skipped files under
    `undeterminable_files`; `emit_candidates` skipped a symlinked candidate
    with a bare `continue` and recorded nothing, so an operator could not
    tell "nothing to clean" from "one file could not be read" at the /pr
    layer."""
    repo = _init_repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    link = repo / "link.py"
    os.symlink(outside, link)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a tracked symlink")

    out = cc.emit_candidates(repo, "main", judge_max=40)

    assert "undeterminable_files" in out
    entry = next(f for f in out["undeterminable_files"] if f["file"] == "link.py")
    assert entry["reason"] == "symlink"


def test_emit_candidates_reports_non_utf8_as_undeterminable(tmp_path):
    # Mirrors test_non_utf8_untouched_bytes_never_transcoded: the non-UTF-8
    # byte is committed on the base, unchanged on the branch, so `git diff
    # -U0` (which resolve_candidates uses) never has to decode it — only the
    # pure-ASCII line change makes the file a candidate at all.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    src = repo / "bad.py"
    src.write_bytes(b'NAME = "caf\xe9"\n' b"z = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base file with a non-utf8 byte")
    _git(repo, "switch", "-c", "feature")
    src.write_bytes(
        b'NAME = "caf\xe9"\n'
        b"z = 0  # not a bug, this is expected behavior.\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "touch only the pure-ascii line")

    out = cc.emit_candidates(repo, "main", judge_max=40)

    assert "undeterminable_files" in out
    entry = next(f for f in out["undeterminable_files"] if f["file"] == "bad.py")
    assert "codec" in entry["reason"] or "decode" in entry["reason"]


def test_apply_skips_symlinked_candidate_via_paths(tmp_path):
    """The --paths escape hatch reaches a symlinked candidate directly
    (bypassing resolve_candidates' diff scoping); the tool must still skip
    it rather than replacing the link with a regular file holding the
    target's (edited) content."""
    repo = _init_repo(tmp_path)
    outside_content = (
        "def f():\n"
        "    # An earlier fix did the thing here for real, entirely done.\n"
        "    return 1\n"
    )
    outside = tmp_path / "outside.py"
    outside.write_text(outside_content, encoding="utf-8")
    link = repo / "link.py"
    os.symlink(outside, link)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a tracked symlink")

    rc = cc.main(["--project-root", str(repo), "--apply", "--paths", "link.py"])

    assert link.is_symlink()
    assert os.readlink(link) == str(outside)
    assert outside.read_text(encoding="utf-8") == outside_content
    assert rc == 0


# ---------------------------------------------------------------------------
# MAJOR regression — _SEPARATOR_RE must not be quadratic in whitespace-run
# length, and the negative-lookbehind fix must be behaviorally equivalent
# to the old pattern over a broad corpus, not just the reported shapes.
# ---------------------------------------------------------------------------

_OLD_SEPARATOR_RE = re.compile(r"\s*(?:—|–|;|:|,)|(?<=[.!?])\s+")


def _spans(pattern, s):
    return [(m.start(), m.end()) for m in pattern.finditer(s)]


def test_separator_regex_equivalence_exhaustive_short_strings():
    alphabet = " .!?,;a\t"
    for length in range(0, 6):
        for combo in itertools.product(alphabet, repeat=length):
            s = "".join(combo)
            assert _spans(cc._SEPARATOR_RE, s) == _spans(_OLD_SEPARATOR_RE, s), repr(s)


def test_separator_regex_equivalence_random_corpus():
    alphabet = "ab .!?,;:—–\t()"
    rng = random.Random(20260909)
    for _ in range(20000):
        s = "".join(rng.choice(alphabet) for _ in range(30))
        assert _spans(cc._SEPARATOR_RE, s) == _spans(_OLD_SEPARATOR_RE, s), repr(s)


def test_separator_regex_performance_on_long_whitespace_run():
    text = "x" + " " * 16000 + "y"
    start = time.perf_counter()
    list(cc._SEPARATOR_RE.finditer(text))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"expected near-linear scaling, took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Review round 5 — reflow terminator must never suffix every payload line
# with a possibly-empty terminator (MAJOR 2), and must derive that
# terminator from the actual line ending, not membership in "\n" (MINOR 3).
# ---------------------------------------------------------------------------

def test_reflow_at_eof_without_trailing_newline_emits_clean_lines():
    # A wrapping (multi-line) reflow block that is also the very last thing
    # in the file, with no trailing newline. Pre-fix, the empty terminator
    # derived from the last line got applied to every payload line, so the
    # join collapsed three lines into one run-on line with "#" markers
    # embedded mid-sentence.
    text = (
        "def f():\n"
        "    return 1\n"
        "    # The parser walks each node and produces a result which is passed\n"
        "    # further down. An earlier fix returned early here for safety reasons.\n"
        "    # The caller then merges the result into the final output by its name."
    )
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert decision.changed
    body_lines = decision.new_text.splitlines()
    comment_lines = [l for l in body_lines if l.strip().startswith("#")]
    # Clean line-by-line output: every comment line is its own line, and no
    # line carries a "#" anywhere but at its own start (the run-on bug
    # embedded a second "#" mid-line).
    assert len(comment_lines) >= 2
    for line in comment_lines:
        assert line.count("#") == 1
    assert not decision.new_text.endswith("\n")


def test_reflow_crlf_file_preserves_carriage_returns():
    text = "x = 1\r\ny = 2  # keep this; an earlier fix did X\r\nz = 3\r\n"
    decision = cc.decide_file("m.py", text, None, retain=1, include_tests=False)
    assert decision.changed
    for line in decision.new_text.splitlines(keepends=True):
        if line.strip("\r\n"):
            assert line.endswith("\r\n")
    assert "y = 2  # keep this.\r\n" in decision.new_text
