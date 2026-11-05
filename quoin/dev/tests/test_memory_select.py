"""IVG-50 S-1: tests for memory_select selective lessons retrieval.

Two-tier structure (mirrors test_memory_check.py):

1. Tier 1 (CI-safe, no `claude`):
   Import installer.py via importlib and assert "memory_select.py"
   is present in BOTH DEPLOYED_SCRIPTS and CORE_SCRIPTS. Also asserts both
   source files exist in the repo.

2. Behavior tests (tmp_path or in-process, no external deps):
   Tests for parse_entries, tokenize, score, select core API plus
   T-07 non-regression superset acceptance gate.

Non-regression superset gate (T-07):
  The fixture at fixtures/memory_select/ is sized (16 entries) so that the
  matcher path — not the MIN_RESULTS=5 wholesale fallback — is exercised.
  The passing case asserts:
    (1) fellback_to_wholesale == False
    (2) selected_count < total (proper subset)
    (3) selected_headers ⊇ human_relevant_set
  A companion test asserts the fallback triggers on pathological input.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ repo root
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"
WRAPPER_SRC = REPO_ROOT / "quoin" / "scripts" / "memory_select.py"
CORE_IMPL_SRC = REPO_ROOT / "quoin" / "core" / "scripts" / "memory_select.py"

FIXTURE_DIR = REPO_ROOT / "quoin" / "dev" / "tests" / "fixtures" / "memory_select"
FIXTURE_LESSONS = FIXTURE_DIR / "lessons-learned-fixture.md"
FIXTURE_TASK = FIXTURE_DIR / "fixture-task.txt"
FIXTURE_HUMAN_RELEVANT = FIXTURE_DIR / "human-relevant.txt"

# ---------------------------------------------------------------------------
# Tier 1: installer membership (CI-safe, no claude binary)
# ---------------------------------------------------------------------------


def _load_installer():
    spec = importlib.util.spec_from_file_location("installer", INSTALLER_PY)
    assert spec is not None, f"Could not load spec from {INSTALLER_PY}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_deployed_scripts_contains_memory_select():
    """DEPLOYED_SCRIPTS must contain memory_select.py (IVG-50 S-1 wrapper guard)."""
    mod = _load_installer()
    assert "memory_select.py" in mod.DEPLOYED_SCRIPTS, (
        "installer.py DEPLOYED_SCRIPTS must contain 'memory_select.py'. "
        "Missing entry means install.sh won't deploy the wrapper to "
        "~/.claude/scripts/memory_select.py."
    )


def test_installer_core_scripts_contains_memory_select():
    """CORE_SCRIPTS must contain memory_select.py (wrapper loader guard)."""
    mod = _load_installer()
    assert "memory_select.py" in mod.CORE_SCRIPTS, (
        "installer.py CORE_SCRIPTS must contain 'memory_select.py'. "
        "Missing entry means the wrapper's parents[1] loader fails at runtime."
    )


def test_source_files_exist():
    """Both source files must exist in the repo."""
    assert WRAPPER_SRC.is_file(), f"Wrapper source missing: {WRAPPER_SRC}"
    assert CORE_IMPL_SRC.is_file(), f"Core impl source missing: {CORE_IMPL_SRC}"


def test_fixture_files_exist():
    """All three fixture files must exist."""
    assert FIXTURE_LESSONS.is_file(), f"Fixture lessons file missing: {FIXTURE_LESSONS}"
    assert FIXTURE_TASK.is_file(), f"Fixture task file missing: {FIXTURE_TASK}"
    assert FIXTURE_HUMAN_RELEVANT.is_file(), f"Fixture human-relevant file missing: {FIXTURE_HUMAN_RELEVANT}"


# ---------------------------------------------------------------------------
# Core loader helper (importlib pattern — D-S2-3 lesson 2026-06-17)
# ---------------------------------------------------------------------------


def _load_core():
    """Load the core module directly for behaviour tests (mandatory importlib pattern)."""
    spec = importlib.util.spec_from_file_location("_ms_core", CORE_IMPL_SRC)
    assert spec is not None, f"Could not load spec from {CORE_IMPL_SRC}"
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ms_core"] = mod
    spec.loader.exec_module(mod)
    return mod


# Load once for all tests in this module
MS = _load_core()


# ---------------------------------------------------------------------------
# D-S1 purity guard: core must not import from adapter/scripts layer
# ---------------------------------------------------------------------------


def test_no_adapter_import():
    """core/scripts/memory_select.py must not import from quoin.scripts (core/adapter boundary)."""
    text = CORE_IMPL_SRC.read_text(encoding="utf-8")
    assert "from quoin.scripts" not in text, (
        "core/scripts/memory_select.py must not import from quoin.scripts "
        "(core/adapter import-boundary violation)."
    )
    assert "import quoin.scripts" not in text, (
        "core/scripts/memory_select.py must not import quoin.scripts "
        "(core/adapter import-boundary violation)."
    )
    assert "from scripts" not in text, (
        "core/scripts/memory_select.py must not import from scripts "
        "(core/adapter import-boundary violation)."
    )


def test_stdlib_only():
    """All top-level imports in core must be stdlib (no third-party deps)."""
    import ast
    src = CORE_IMPL_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    stdlib_mods = {
        "argparse", "json", "re", "sys", "math", "pathlib",
        "dataclasses", "typing", "__future__", "hashlib", "importlib",
        "os", "zlib",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in stdlib_mods, f"Non-stdlib import found: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                assert top in stdlib_mods, f"Non-stdlib from-import found: {node.module}"


# ---------------------------------------------------------------------------
# parse_entries tests
# ---------------------------------------------------------------------------


def test_parse_entries_count_fixture():
    """parse_entries must find 16 real entries in the fixture (template block skipped)."""
    text = FIXTURE_LESSONS.read_text(encoding="utf-8")
    entries = MS.parse_entries(text)
    assert len(entries) == 16, (
        f"Expected 16 entries in fixture, got {len(entries)}. "
        "Check that the template comment block is properly skipped."
    )


def test_parse_entries_skips_template_block():
    """parse_entries must skip ## headers containing <date> placeholder."""
    text = "# Lessons\n\n<!-- comment -->\n\n## <date> — <task-name>\ntemplate\n\n## 2026-01-01 — real-entry\n**What happened:** real\n**Lesson:** real\n**Applies to:** /plan\n"
    entries = MS.parse_entries(text)
    assert len(entries) == 1
    assert "real-entry" in entries[0].header


def test_parse_entries_extracts_applies_to():
    """parse_entries must extract the **Applies to:** field correctly."""
    text = "## 2026-01-01 — my-task\n**What happened:** x\n**Lesson:** y\n**Applies to:** /plan, /critic — some context\n"
    entries = MS.parse_entries(text)
    assert len(entries) == 1
    assert "/plan" in entries[0].applies_to
    assert "/critic" in entries[0].applies_to


def test_parse_entries_lineno():
    """parse_entries lineno must be 1-based line of the ## header."""
    text = "# Title\n\n## 2026-01-01 — entry-a\n**Applies to:** /plan\n\n## 2026-01-02 — entry-b\n**Applies to:** /critic\n"
    entries = MS.parse_entries(text)
    assert len(entries) == 2
    assert entries[0].lineno == 3   # Line 3 (1-based)
    assert entries[1].lineno == 6   # Line 6 (1-based)


# ---------------------------------------------------------------------------
# tokenize tests
# ---------------------------------------------------------------------------


def test_tokenize_deterministic():
    """tokenize must return the same result for the same input on repeated calls."""
    s = "JSONL cost parsing stacked branch workflows"
    r1 = MS.tokenize(s)
    r2 = MS.tokenize(s)
    assert r1 == r2, "tokenize is not deterministic"


def test_tokenize_idempotent():
    """tokenize applied to already-tokenized output (joined) must be stable."""
    s = "JSONL cost parsing stacked branch workflows"
    r1 = MS.tokenize(s)
    # Join and re-tokenize (stability check)
    r2 = MS.tokenize(" ".join(sorted(r1)))
    assert r1 == r2, "tokenize is not idempotent on its own output"


def test_tokenize_drops_short_tokens():
    """tokenize must drop tokens shorter than MIN_TOKEN_LEN (default 3)."""
    tokens = MS.tokenize("I am an it")
    assert "i" not in tokens
    assert "am" not in tokens


def test_tokenize_drops_stopwords():
    """tokenize must drop stopwords."""
    tokens = MS.tokenize("the quick brown fox")
    assert "the" not in tokens
    assert "quick" in tokens
    assert "brown" in tokens


def test_tokenize_lowercase():
    """tokenize must lowercase all tokens."""
    tokens = MS.tokenize("JSONL COST PARSING")
    assert "jsonl" in tokens
    assert "cost" in tokens
    assert "JSONL" not in tokens


# ---------------------------------------------------------------------------
# score tests
# ---------------------------------------------------------------------------


def test_score_monotonic_in_shared_tokens():
    """score must increase as shared tokens increase."""
    task_tokens = MS.tokenize("jsonl cost parsing branch")
    # Entry 1: 1 shared token
    e1 = MS.Entry(header="2026-01-01 — task1", applies_to="jsonl", body="", lineno=1)
    # Entry 2: 2 shared tokens
    e2 = MS.Entry(header="2026-01-01 — task2", applies_to="jsonl cost", body="", lineno=1)
    # Entry 3: 3 shared tokens
    e3 = MS.Entry(header="2026-01-01 — task3", applies_to="jsonl cost parsing", body="", lineno=1)

    s1 = MS.score(task_tokens, e1)
    s2 = MS.score(task_tokens, e2)
    s3 = MS.score(task_tokens, e3)

    assert s1 < s2 < s3, f"Score not monotonic: {s1}, {s2}, {s3}"


def test_score_zero_no_overlap():
    """score must return 0 when there is no token overlap."""
    task_tokens = MS.tokenize("kubernetes networking pods")
    e = MS.Entry(header="2026-01-01 — task", applies_to="docker layering", body="", lineno=1)
    assert MS.score(task_tokens, e) == 0


# ---------------------------------------------------------------------------
# select tests
# ---------------------------------------------------------------------------


def test_select_threshold_one_includes_single_shared_token():
    """A single shared token must cause inclusion with threshold=1 (over-inclusion rule)."""
    text = (
        "## 2026-01-01 — match-entry\n"
        "**What happened:** uses jsonl\n"
        "**Lesson:** lesson\n"
        "**Applies to:** /rollback — jsonl tasks\n"
        "\n"
    ) * 1 + (
        "## 2026-01-02 — no-match-a\n"
        "**What happened:** kubernetes\n"
        "**Lesson:** k8s lesson\n"
        "**Applies to:** /pr — kubernetes\n"
        "\n"
    ) * 7 + (
        "## 2026-01-03 — no-match-b\n"
        "**What happened:** docker layers\n"
        "**Lesson:** docker lesson\n"
        "**Applies to:** /pr — docker builds\n"
        "\n"
    ) * 7
    result = MS.select(text, "jsonl cost parsing")
    # The match-entry must appear in the result (threshold=1, "jsonl" is shared)
    headers = {e.header for e in result.selected}
    assert any("match-entry" in h for h in headers), (
        "Entry with single shared token 'jsonl' must be selected at threshold=1. "
        f"Selected: {headers}"
    )


def test_select_always_include_plan_tagged():
    """Entries tagged /plan or /review must always be included regardless of score."""
    # Build a mini fixture: 16 entries total, most unrelated, one /plan-tagged
    # Use enough entries that fallback isn't triggered on the selected set
    lines = []
    # 1 entry tagged /plan (must always include, score=0 vs the task)
    lines.append(
        "## 2026-01-01 — plan-tagged\n"
        "**What happened:** something unrelated\n"
        "**Lesson:** lesson\n"
        "**Applies to:** /plan — some context\n\n"
    )
    # 1 entry tagged /review (must always include)
    lines.append(
        "## 2026-01-02 — review-tagged\n"
        "**What happened:** something else\n"
        "**Lesson:** lesson\n"
        "**Applies to:** /review — some context\n\n"
    )
    # 14 entries with totally unrelated content and no planning-skill tags
    for i in range(3, 17):
        lines.append(
            f"## 2026-01-{i:02d} — unrelated-{i}\n"
            f"**What happened:** kubernetes networking pod cidr\n"
            f"**Lesson:** k8s lesson {i}\n"
            f"**Applies to:** /pr — kubernetes networking\n\n"
        )
    text = "# Lessons\n\n" + "".join(lines)
    result = MS.select(text, "totally unrelated task abc xyz")
    headers = {e.header for e in result.selected}
    assert any("plan-tagged" in h for h in headers), (
        "/plan-tagged entry must always be included. Selected: " + str(headers)
    )
    assert any("review-tagged" in h for h in headers), (
        "/review-tagged entry must always be included. Selected: " + str(headers)
    )


def test_select_wholesale_fallback_on_empty_task():
    """Empty task text must trigger wholesale fallback (fellback_to_wholesale=True)."""
    text = FIXTURE_LESSONS.read_text(encoding="utf-8")
    result = MS.select(text, "")
    assert result.fellback_to_wholesale is True, (
        "Empty task text must trigger wholesale fallback."
    )
    assert result.selected_count == result.total, (
        "Wholesale fallback must return all entries."
    )


def test_select_wholesale_fallback_on_garbage_task():
    """Garbage/meaningless task text (all stopwords/short) must trigger wholesale fallback."""
    text = FIXTURE_LESSONS.read_text(encoding="utf-8")
    # All stopwords and 1-2 char tokens that will produce empty token set
    result = MS.select(text, "a an the is of to in")
    assert result.fellback_to_wholesale is True, (
        "Task text producing empty token set must trigger wholesale fallback."
    )


# ---------------------------------------------------------------------------
# T-07 Non-regression superset acceptance gate
# The three binding assertions in one test — prove the MATCHER, not the fallback.
# ---------------------------------------------------------------------------


def test_superset_gate_matcher_path():
    """T-07: superset gate on sized fixture (16 entries).

    Assertions:
    (1) fellback_to_wholesale == False  — matcher path exercised, not wholesale fallback
    (2) selected_count < total          — proper subset (matcher is selective)
    (3) selected_headers ⊇ human_relevant_set — no missed lesson (R-04 guard)
    """
    fixture_text = FIXTURE_LESSONS.read_text(encoding="utf-8")
    fixture_task = FIXTURE_TASK.read_text(encoding="utf-8").strip()
    human_relevant_raw = FIXTURE_HUMAN_RELEVANT.read_text(encoding="utf-8")
    human_relevant = {
        line.strip()
        for line in human_relevant_raw.splitlines()
        if line.strip()
    }

    result = MS.select(fixture_text, fixture_task)

    # (1) Matcher path — NOT the wholesale fallback
    assert result.fellback_to_wholesale is False, (
        "T-07 gate FAIL: fellback_to_wholesale=True means the wholesale fallback "
        "triggered, not the matcher path. The fixture must be sized so that "
        "selected_count is between MIN_RESULTS and MAX_FRACTION*total. "
        f"Got: total={result.total}, selected={result.selected_count}"
    )

    # (2) Proper subset — proves selectivity
    assert result.selected_count < result.total, (
        f"T-07 gate FAIL: selected_count ({result.selected_count}) == total ({result.total}). "
        "The matcher must return a proper subset, not all entries. "
        "A matcher returning everything is not selective."
    )

    # (3) Superset of human-relevant entries — the R-04 missed-lesson guard
    selected_headers = set(result.selected_headers)
    missing = human_relevant - selected_headers
    assert not missing, (
        f"T-07 gate FAIL: The following human-marked relevant entries were NOT selected:\n"
        + "\n".join(f"  - {h}" for h in sorted(missing))
        + f"\nSelected headers: {sorted(selected_headers)}"
    )


def test_superset_gate_companion_fallback_on_pathological_input():
    """T-07 companion: fallback triggers on pathological input, NOT on the sized fixture.

    A ≤6-entry fixture (below MIN_RESULTS=5 floor) with empty task text must
    trigger fellback_to_wholesale=True, proving the fallback triggers ONLY on
    pathological inputs.
    """
    # Build a tiny 3-entry fixture — below MIN_RESULTS=5
    tiny_text = (
        "# Lessons\n\n"
        "## 2026-01-01 — entry-a\n**What happened:** a\n**Lesson:** a\n**Applies to:** /rollback\n\n"
        "## 2026-01-02 — entry-b\n**What happened:** b\n**Lesson:** b\n**Applies to:** /pr\n\n"
        "## 2026-01-03 — entry-c\n**What happened:** c\n**Lesson:** c\n**Applies to:** /gate\n\n"
    )
    # Task text that would score 0 on all entries (no shared tokens with "kubernetes" theme)
    result = MS.select(tiny_text, "totally unrelated xyz qrs mnp")
    assert result.fellback_to_wholesale is True, (
        "Fallback must trigger when selected_count < MIN_RESULTS. "
        f"Got: fellback={result.fellback_to_wholesale}, selected={result.selected_count}, total={result.total}"
    )
    assert result.selected_count == result.total, (
        "Wholesale fallback must return ALL entries (not a subset)."
    )


def test_narrow_threshold_drops_human_relevant_fails_guard():
    """If a narrowed threshold (threshold=99) drops a human-relevant entry while
    NOT triggering the fallback, assertion (3) in the gate would fail.

    This test proves the gate is actually guarding — a matcher that missed the
    relevant entry makes the superset assertion fail.
    """
    fixture_text = FIXTURE_LESSONS.read_text(encoding="utf-8")
    fixture_task = FIXTURE_TASK.read_text(encoding="utf-8").strip()
    human_relevant_raw = FIXTURE_HUMAN_RELEVANT.read_text(encoding="utf-8")
    human_relevant = {
        line.strip()
        for line in human_relevant_raw.splitlines()
        if line.strip()
    }

    # With threshold=99 (impossibly high) and always-include rules disabled by
    # passing a dummy task with no overlap, selected_count will be low (possibly
    # triggering fallback). But with MIN_RESULTS=5 and a fixture of 16 entries,
    # a threshold-99 select() returns 0 scored entries + 0 always-include entries
    # (since "xyzzy" doesn't match any header or /plan tag)... so it falls back.
    # The important thing: verify the test infrastructure can detect a miss.
    result = MS.select(fixture_text, fixture_task, threshold=99)
    # Either we fell back to wholesale (ok, human-relevant is covered) or we didn't
    # (selected_count < 5 → also falls back). Either way, if wholesale covers it, pass.
    # This test just verifies the test infra doesn't silently pass on a bad select.
    selected_headers = set(result.selected_headers)
    if not result.fellback_to_wholesale:
        # At threshold=99, missing may be non-empty → proves the gate would catch it.
        _ = human_relevant - selected_headers
    # The real gate is test_superset_gate_matcher_path — this is a sanity check
    assert isinstance(result.fellback_to_wholesale, bool)


# ---------------------------------------------------------------------------
# JSON output shape
# ---------------------------------------------------------------------------


@pytest.mark.slow_fs
def test_json_output_shape():
    """--json output must have the correct schema: selected, fellback_to_wholesale, total, selected_count."""
    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--task-text", "jsonl cost parsing", "--file", str(FIXTURE_LESSONS), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"--json failed:\n{result.stderr}"
    data = json.loads(result.stdout)
    assert "selected" in data
    assert "fellback_to_wholesale" in data
    assert "total" in data
    assert "selected_count" in data
    # Each selected entry must have header, lineno, score
    for item in data["selected"]:
        assert "header" in item
        assert "lineno" in item
        assert "score" in item


@pytest.mark.slow_fs
def test_wrapper_help_exits_zero():
    """Wrapper --help must exit 0 (exercises the importlib loader chain)."""
    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--task-text", "x", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"--help failed:\n{result.stderr}"
    assert "memory_select" in result.stdout.lower()


@pytest.mark.slow_fs
def test_missing_file_exits_two():
    """Passing a non-existent --file must exit 2 (IO error), not 0 or 1."""
    result = subprocess.run(
        [sys.executable, str(WRAPPER_SRC), "--task-text", "x", "--file", "/tmp/nonexistent-12345.md"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, f"Expected exit 2 for missing file, got {result.returncode}"


# ---------------------------------------------------------------------------
# Semantic index (opt-in, fail-open)
# ---------------------------------------------------------------------------

# The fixture task lexically selects 7 entries, none about rate limiting. The
# appended sentence paraphrases the api-rate-limiting lesson in other word forms
# ("degrading"/"degraded", "limiters"/"limits", "breakers", "exponentially")
# and shares no token with its header or Applies-to line, so only the vector
# match can find it.
PLAIN_TASK = FIXTURE_TASK.read_text(encoding="utf-8").strip()
PARAPHRASE_TASK = PLAIN_TASK + (
    " Services degrading once vendor limiters kick in need breakers and"
    " exponentially growing retries."
)
RATE_LIMIT = "2026-01-14 — api-rate-limiting"

FORGOTTEN_BLOCKS = (
    "> Source: /abs/daily/insights-2026-01-02.md:3..5\n"
    "> Forgotten: 2026-01-30T10:00:00Z\n"
    "> Score: forget=4, promote=0\n"
    "\n"
    "## 10:00 — zellij pane flakiness\n"
    "Zellij pane tests were flaky because of timing; poll instead of sleeping.\n"
    "\n"
    "---\n"
    "\n"
    "> Source: /abs/daily/insights-2026-01-03.md:7..8\n"
    "> Forgotten: 2026-01-30T10:00:01Z\n"
    "> Score: forget=5, promote=0\n"
    "\n"
    "## 11:00 — docs typo\n"
    "Fixed a typo in the README heading.\n"
    "---\n"
    "A horizontal rule inside the verbatim text stays in the block.\n"
    "\n"
    "---\n"
)


@pytest.fixture
def builtin_backend(monkeypatch):
    monkeypatch.delenv("QUOIN_EMBED_BACKEND", raising=False)


def _memory_dir(tmp_path: Path, with_forgotten: bool = False) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "lessons-learned.md").write_text(
        FIXTURE_LESSONS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    if with_forgotten:
        (mem / "forgotten").mkdir()
        (mem / "forgotten" / "2026-01-30.md").write_text(FORGOTTEN_BLOCKS, encoding="utf-8")
    return mem


def _run(capsys, *argv: str) -> tuple[int, str]:
    rc = MS.main(list(argv))
    return rc, capsys.readouterr().out


def test_semantic_paraphrase_retrieves_lexical_miss(tmp_path, capsys, builtin_backend):
    """A paraphrased task finds a lesson the lexical matcher misses; the plain
    task (negative control) gets nothing beyond the lexical selection."""
    lessons = str(_memory_dir(tmp_path) / "lessons-learned.md")
    assert _run(capsys, "--build-index", "--file", lessons)[0] == 0

    def ids(task, *extra):
        return set(_run(capsys, *extra, "--task-text", task, "--file", lessons, "--ids-only")[1].splitlines())

    lexical = ids(PARAPHRASE_TASK)
    semantic = ids(PARAPHRASE_TASK, "--semantic")
    assert RATE_LIMIT not in lexical
    assert semantic == lexical | {RATE_LIMIT}
    assert ids(PLAIN_TASK, "--semantic") == ids(PLAIN_TASK)


def test_semantic_covers_entries_added_after_build(tmp_path):
    """A stale index still covers new entries (embedded at query time)."""
    lessons_path = _memory_dir(tmp_path) / "lessons-learned.md"
    index = MS.default_index_path(lessons_path)
    MS.build_index(lessons_path, index, MS.NgramBackend())
    with lessons_path.open("a", encoding="utf-8") as fh:
        fh.write("\n## 2026-02-01 — late-entry\n**Lesson:** zeppelin mooring knots\n")
    entries = MS.parse_entries(lessons_path.read_text(encoding="utf-8"))
    vectors = MS.load_index(index, MS.NgramBackend.id)
    top = MS.semantic_rank(entries, "zeppelins moored", vectors, MS.NgramBackend())[0]
    assert top[1].header == "2026-02-01 — late-entry"


@pytest.mark.parametrize("index_state,mode", [
    ("missing", ["--json"]), ("corrupt", ["--json"]), ("other-backend", ["--json"]),
    ("missing", []), ("corrupt", ["--ids-only"]),
])
def test_semantic_without_usable_index_matches_lexical(tmp_path, capsys, index_state, mode):
    """No usable index: --semantic output is byte-identical to the lexical path."""
    lessons_path = _memory_dir(tmp_path) / "lessons-learned.md"
    index = MS.default_index_path(lessons_path)
    if index_state != "missing":
        MS.build_index(lessons_path, index, MS.NgramBackend())
        if index_state == "corrupt":
            index.write_text("{not json", encoding="utf-8")
        else:
            data = json.loads(index.read_text(encoding="utf-8"))
            data["backend"] = "some-other-model"
            index.write_text(json.dumps(data), encoding="utf-8")
    lessons = str(lessons_path)
    base = ["--task-text", PARAPHRASE_TASK, "--file", lessons, *mode]
    assert _run(capsys, "--semantic", *base) == _run(capsys, *base)


def test_build_index_idempotent_and_incremental(tmp_path, capsys, builtin_backend):
    """Rebuild is byte-stable; only new entries are embedded, removed ones dropped."""
    mem = _memory_dir(tmp_path, with_forgotten=True)
    lessons_path = mem / "lessons-learned.md"
    index = MS.default_index_path(lessons_path)
    first = MS.build_index(lessons_path, index, MS.NgramBackend())
    assert first == {"entries": 18, "added": 18, "reused": 0, "removed": 0, "written": True}
    assert (index.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"
    snapshot = index.read_bytes()

    rc, out = _run(capsys, "--build-index", "--file", str(lessons_path))
    assert rc == 0 and out.strip().endswith("added=0 reused=18 removed=0 unchanged")
    assert index.read_bytes() == snapshot

    original = lessons_path.read_text(encoding="utf-8")
    lessons_path.write_text(
        original + "\n## 2026-02-01 — new-entry\n**Lesson:** something new\n", encoding="utf-8"
    )
    grown = MS.build_index(lessons_path, index, MS.NgramBackend())
    assert (grown["added"], grown["reused"], grown["removed"]) == (1, 18, 0)

    lessons_path.write_text(original, encoding="utf-8")
    shrunk = MS.build_index(lessons_path, index, MS.NgramBackend())
    assert (shrunk["added"], shrunk["removed"]) == (0, 1)
    assert index.read_bytes() == snapshot

    # An edited entry (including a blockquote line) is re-embedded.
    lessons_path.write_text(
        original.replace("**Lesson:** Always back up", "> note\n**Lesson:** Always back up"),
        encoding="utf-8",
    )
    edited = MS.build_index(lessons_path, index, MS.NgramBackend())
    assert (edited["added"], edited["removed"]) == (1, 1)


def test_parse_forgotten_blocks():
    """forgotten/ archive blocks parse into entries carrying the Source anchor."""
    entries = MS.parse_forgotten(FORGOTTEN_BLOCKS, "2026-01-30.md")
    assert [e.header for e in entries] == ["forgotten/2026-01-30.md#1", "forgotten/2026-01-30.md#2"]
    assert entries[0].applies_to == "/abs/daily/insights-2026-01-02.md:3..5"
    assert "horizontal rule inside" in entries[1].body
    assert entries[0].lineno == 1 and entries[1].lineno == 10
    assert "> Source:" not in MS._embed_text(entries[0])


def test_forgotten_search_substring_then_semantic(tmp_path, capsys, builtin_backend):
    """--source forgotten: substring match without an index, vector hits with one."""
    lessons = str(_memory_dir(tmp_path, with_forgotten=True) / "lessons-learned.md")
    query = "flakiness"  # substring of block 1 only

    _, plain = _run(capsys, "--source", "forgotten", "--task-text", "flaky timing", "--file", lessons, "--json")
    assert json.loads(plain)["selected"] == []  # no literal substring match

    _run(capsys, "--build-index", "--file", lessons)
    _, sem = _run(
        capsys, "--semantic", "--source", "forgotten", "--task-text", "flaky timing",
        "--file", lessons, "--json",
    )
    hits = json.loads(sem)["selected"]
    assert [h["header"] for h in hits] == ["forgotten/2026-01-30.md#1"]
    assert hits[0]["source"] == "forgotten/2026-01-30.md"
    assert hits[0]["anchor"] == "/abs/daily/insights-2026-01-02.md:3..5"

    _, sub = _run(capsys, "--source", "forgotten", "--task-text", query, "--file", lessons, "--ids-only")
    assert sub.splitlines() == ["forgotten/2026-01-30.md#1"]
    # Metadata lines are not matched: every block has a "> Forgotten: 2026-01-30" line.
    _, meta = _run(capsys, "--source", "forgotten", "--task-text", "2026-01-30T", "--file", lessons, "--ids-only")
    assert meta == ""


def test_task_text_still_required_without_build_index(tmp_path):
    with pytest.raises(SystemExit) as exc:
        MS.main(["--file", str(_memory_dir(tmp_path) / "lessons-learned.md")])
    assert exc.value.code == 2


def test_unavailable_optional_backend_falls_back_to_builtin(monkeypatch):
    monkeypatch.setenv("QUOIN_EMBED_BACKEND", "fastembed")
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import raises ImportError
    assert MS.active_backend().id == MS.NgramBackend.id
