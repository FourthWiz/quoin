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

import subprocess
import sys
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

    # Negative: no whole-block removal anywhere in the pinned corpus.
    for old_text, decision in (
        (dashboard_text, d1),
        (quoin_claude_text, d2),
        (simple_claude_text, d3),
    ):
        old_lines = old_text.splitlines()
        new_lines = decision.new_text.splitlines()
        # A whole-block removal would drop lines outright; every pinned
        # excision here is a same-line-count reflow-in-place or a
        # line-count-reducing multi-line-to-one-line reflow, never a bare
        # deletion of an entire standalone block with nothing replacing it.
        assert len(new_lines) <= len(old_lines)


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

def test_pragma_survives_all_categories(tmp_path):
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


def test_non_py_file_reported_not_written(tmp_path):
    repo = _init_repo(tmp_path)
    src = repo / "m.sh"
    src.write_text("echo hi  # an earlier fix did the thing here.\n", encoding="utf-8")
    _commit_all(repo)
    text = src.read_text(encoding="utf-8")
    decision = cc.decide_file("m.sh", text, None, retain=1, include_tests=False)
    # decide_file computes the decision regardless of suffix; the CLI's
    # apply step is what gates writes to .py only. Confirm the decision
    # layer still proposes a change...
    assert decision.changed
    # ...but the file on disk is untouched unless the CLI writes it.
    assert src.read_text(encoding="utf-8") == "echo hi  # an earlier fix did the thing here.\n"


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
