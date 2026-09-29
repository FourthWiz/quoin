"""IVG-258 stage-4 T-05: drift guard tying installer.py's stanza roster to
the five prose/doc surfaces that name its count or the WorktreeCreate
ordinal (R-09 mitigation).

Rationale: nothing pinned "the printed count equals the actual number of
_append_stanza calls" before this file, which is why three of the count
surfaces were free to disagree before stage 4 started (see R-12). The
ordered (event, matcher) tuple list below is itself a hardcoded literal
roster, not AST-derived like the count and the WorktreeCreate ordinal are
— a deliberate trade-off (round-1 MIN-6): a bare cardinality check would
miss a call whose event/matcher pair silently changed while the count
stayed the same, and the list-equality form catches that. This makes the
tuple list an extra surface needing an update at every new stanza,
alongside the five below.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "src" / "quoin" / "installer.py"
CLAUDE_MD = REPO_ROOT / "quoin" / "CLAUDE.md"
WORKFLOW_CATALOG = REPO_ROOT / "quoin" / "memory" / "workflow-catalog.md"
HOOKS_TABLE = REPO_ROOT / "quoin" / "memory" / "hooks-table.md"
HOOKS_GUIDE = REPO_ROOT / "quoin" / "docs" / "hooks-guide.md"

EXPECTED_STANZAS = [
    ("UserPromptSubmit", "*"),
    ("PreCompact", "auto"),
    ("PostCompact", "auto"),
    ("SessionStart", "startup"),
    ("SessionStart", "resume"),
    ("SessionStart", "compact"),
    ("SessionEnd", "*"),
    ("WorktreeCreate", "*"),
    ("Stop", "*"),
]
EXPECTED_COUNT = len(EXPECTED_STANZAS)  # 9


def _append_stanza_calls():
    """Return the ordered (event, matcher) tuples from every _append_stanza(...)
    call in installer.py's source, in source order, via AST (not a grep)."""
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"), filename=str(INSTALLER))
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_append_stanza"
        ):
            event_arg, matcher_arg = node.args[0], node.args[1]
            assert isinstance(event_arg, ast.Constant) and isinstance(event_arg.value, str), (
                "_append_stanza's event argument must be a string literal"
            )
            assert isinstance(matcher_arg, ast.Constant) and isinstance(matcher_arg.value, str), (
                "_append_stanza's matcher argument must be a string literal"
            )
            calls.append((event_arg.value, matcher_arg.value, node.lineno))
    # Source-order: AST walk order for sibling statements in a function body
    # is already source order (ast.walk is a BFS/DFS over the tree, but
    # sibling Expr statements inside deploy_hooks appear in body-list order
    # since ast.walk yields a node's children before descending further into
    # each) — sort explicitly by lineno to make this independent of walk order.
    calls.sort(key=lambda c: c[2])
    return calls


def test_stanza_call_count_and_order_match_expected_roster():
    calls = _append_stanza_calls()
    assert len(calls) == EXPECTED_COUNT, (
        f"installer.py registers {len(calls)} _append_stanza call(s); expected "
        f"{EXPECTED_COUNT}. If this is an intentional new stanza, update "
        f"EXPECTED_STANZAS here AND the five prose surfaces this test also checks."
    )
    actual = [(event, matcher) for event, matcher, _ in calls]
    assert actual == EXPECTED_STANZAS, (
        f"installer.py's (event, matcher) call order is {actual}; expected "
        f"{EXPECTED_STANZAS} (list equality, not membership — order and content "
        "both matter)."
    )


def test_printed_stanza_count_is_derived_not_literal():
    text = INSTALLER.read_text(encoding="utf-8")
    # The print's f-string must interpolate a variable, never a bare digit for
    # the count — "Merged 8 hook stanzas" (or any other digit) would mean the
    # count was re-literalised instead of read from _stanza_count.
    assert re.search(r"Merged \d+ hook stanzas", text) is None, (
        "installer.py's printed stanza count has been re-literalised as a bare "
        "digit; it must read from the _append_stanza call counter instead "
        "(D-02) so R-09's drift cannot recur."
    )
    assert "Merged {" in text or re.search(r"Merged \{[^}]+\} hook stanzas", text), (
        "installer.py's stanza-count print statement is missing entirely."
    )


def test_claude_md_and_catalog_name_nine_stanzas():
    for path in (CLAUDE_MD, WORKFLOW_CATALOG):
        text = path.read_text(encoding="utf-8")
        assert f"registers {EXPECTED_COUNT} (event, matcher) stanzas" in text, (
            f"{path} does not name {EXPECTED_COUNT} (event, matcher) stanzas"
        )


def test_hooks_table_names_nine_stanzas_in_words():
    text = HOOKS_TABLE.read_text(encoding="utf-8")
    assert "registers nine (event, matcher) stanzas" in text, (
        "hooks-table.md does not name the stanza count as the word 'nine'"
    )


def test_hooks_guide_worktreecreate_ordinal_matches_ast_index():
    calls = _append_stanza_calls()
    worktree_indices = [
        i for i, (event, matcher, _) in enumerate(calls, start=1) if event == "WorktreeCreate"
    ]
    assert len(worktree_indices) == 1, "expected exactly one WorktreeCreate stanza call"
    ordinal_index = worktree_indices[0]  # 1-based position in the AST call list

    ordinals = {
        1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
        6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
    }
    expected_word = ordinals[ordinal_index]

    text = HOOKS_GUIDE.read_text(encoding="utf-8")
    assert f"Registered as the {expected_word} stanza (`WorktreeCreate`" in text, (
        f"hooks-guide.md's WorktreeCreate ordinal does not match its "
        f"AST call-list position ({ordinal_index} -> '{expected_word}')"
    )


def test_hooks_guide_stop_ordinal_matches_ast_index():
    calls = _append_stanza_calls()
    stop_indices = [
        i for i, (event, matcher, _) in enumerate(calls, start=1) if event == "Stop"
    ]
    assert len(stop_indices) == 1, "expected exactly one Stop stanza call"
    ordinal_index = stop_indices[0]  # 1-based position in the AST call list

    ordinals = {
        1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
        6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
    }
    expected_word = ordinals[ordinal_index]

    text = HOOKS_GUIDE.read_text(encoding="utf-8")
    assert f"Registered as the {expected_word} stanza (`Stop`" in text, (
        f"hooks-guide.md's Stop ordinal does not match its "
        f"AST call-list position ({ordinal_index} -> '{expected_word}')"
    )


def test_hooks_table_has_nine_data_rows_including_compact():
    text = HOOKS_TABLE.read_text(encoding="utf-8")
    lines = text.splitlines()
    # The table's data rows are the pipe-delimited lines after the header
    # separator row (the `|---|---|...` line); stop at the first blank line.
    sep_idx = next(i for i, line in enumerate(lines) if re.match(r"^\|[-\s|]+\|$", line))
    data_rows = []
    for line in lines[sep_idx + 1:]:
        if not line.strip():
            break
        if line.startswith("|"):
            data_rows.append(line)
    assert len(data_rows) == EXPECTED_COUNT, (
        f"hooks-table.md's events table has {len(data_rows)} data row(s); "
        f"expected {EXPECTED_COUNT}"
    )
    assert any("SessionStart" in row and "`compact`" in row for row in data_rows), (
        "hooks-table.md's events table has no SessionStart / `compact` row"
    )
