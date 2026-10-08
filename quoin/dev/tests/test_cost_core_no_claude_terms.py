"""Guard test: quoin/core/ cost files must not contain Claude-specific vocabulary.

Enforces the runtime-neutrality invariant for cost_event.py and cost-ledger.md
so future phases cannot silently drag Claude vocabulary into quoin/core/.

The guard excludes the '## Claude-Specific Capture' section of cost-ledger.md
(that section is explicitly allowed to reference ccusage and JSONL).
"""

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module-scope constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]

WORDLIST = [
    "ccusage",
    "jsonl",
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
    "npx ccusage",
]

EXCLUDED_SECTION = "## Claude-Specific Capture"

SCANNED_FILES = [
    REPO_ROOT / "quoin" / "core" / "scripts" / "cost_event.py",
    REPO_ROOT / "quoin" / "core" / "workflow" / "cost-ledger.md",
]

# Adapter-import terms whose presence in cost_event.py would mean the module
# is co-importing a Claude-adapter-owned helper.
IMPORT_BLACKLIST = [
    "cost_from_jsonl",
    "session_age_guard",
    # IVG-111 stage 6 (T-07): the nested-transcript resolver/pricer and the
    # historical backfill script are Claude-adapter-owned (D-04/R-05) — core
    # must never import either.
    "agent_transcript_cost",
    "backfill_cost_attribution",
    # The finalize-time ledger summary reads Claude transcripts and prices; core
    # may call neither it nor its helpers.
    "cost_ledger_summary",
]

CORE_SCRIPTS_DIR = REPO_ROOT / "quoin" / "core" / "scripts"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _scan_outside_excluded(path: Path) -> list:
    """Scan `path` for WORDLIST terms, skipping lines at/after EXCLUDED_SECTION.

    Returns a list of (lineno, matched_word, full_line) tuples for every hit
    found before the excluded section. Matching is case-insensitive.
    """
    hits = []
    in_excluded = False

    with open(path, encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip("\n")
            if line.strip() == EXCLUDED_SECTION:
                in_excluded = True
            if in_excluded:
                continue
            for word in WORDLIST:
                if re.search(re.escape(word), line, re.IGNORECASE):
                    hits.append((lineno, word, line))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cost_event_py_has_no_claude_terms():
    """cost_event.py must contain no Claude-specific vocabulary."""
    path = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_event.py"
    hits = _scan_outside_excluded(path)
    assert hits == [], (
        f"Claude vocabulary found in {path}:\n"
        + "\n".join(f"  line {ln}: {word!r} in {text!r}" for ln, word, text in hits)
    )


def test_cost_ledger_md_has_no_claude_terms_outside_excluded():
    """cost-ledger.md must contain no Claude vocabulary outside ## Claude-Specific Capture."""
    path = REPO_ROOT / "quoin" / "core" / "workflow" / "cost-ledger.md"
    hits = _scan_outside_excluded(path)
    assert hits == [], (
        f"Claude vocabulary found outside excluded section in {path}:\n"
        + "\n".join(f"  line {ln}: {word!r} in {text!r}" for ln, word, text in hits)
    )


def test_cost_ledger_md_excluded_section_exists():
    """The '## Claude-Specific Capture' heading must be present in cost-ledger.md.

    Without this heading, _scan_outside_excluded would never enter excluded mode
    and the entire file (including the Claude-vocabulary section) would be scanned,
    causing false failures. This test guards against the section being silently removed.
    """
    path = REPO_ROOT / "quoin" / "core" / "workflow" / "cost-ledger.md"
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    heading_lines = [ln.strip() for ln in lines]
    assert EXCLUDED_SECTION in heading_lines, (
        f"Required heading '{EXCLUDED_SECTION}' not found in {path}. "
        "The exclusion guard would fail silently without it."
    )


def test_cost_event_py_has_no_adapter_imports():
    """cost_event.py must not import from Claude-adapter-owned modules.

    Checks for bare substrings that would appear in any import form:
        import cost_from_jsonl
        from cost_from_jsonl import ...
        from quoin.scripts.cost_from_jsonl import ...
    This enforces the module docstring's runtime-neutrality promise programmatically.
    """
    path = REPO_ROOT / "quoin" / "core" / "scripts" / "cost_event.py"
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    for term in IMPORT_BLACKLIST:
        assert term not in source, (
            f"Adapter-owned import term '{term}' found in {path}. "
            "cost_event.py must not import from Claude-adapter-owned modules."
        )


# ---------------------------------------------------------------------------
# T-09 (stage 4, MINOR-1 core-purity method): per-file "no NEW adapter
# import" guards for the two other stage-4-touched core files. spend_monitor.py
# is a documented EXCEPTION (D-6) — it legitimately cross-loads cost_from_jsonl
# via _load_sibling for JSONL parsing, so a bare zero-import assertion would
# be spuriously red. The guard here is narrower: no import term OTHER than the
# already-documented cost_from_jsonl sibling load, and the new stage-4
# classify_attribution loader (_load_core) must be core->core, not core->adapter.
# ---------------------------------------------------------------------------

def test_spend_monitor_py_no_new_adapter_import_beyond_documented_sibling():
    """spend_monitor.py may import cost_from_jsonl (documented D-6 exception)
    but must introduce NO OTHER adapter-owned import term, and its stage-4
    classify_attribution loader (_load_core) must stay core->core."""
    path = REPO_ROOT / "quoin" / "core" / "scripts" / "spend_monitor.py"
    source = path.read_text(encoding="utf-8")

    for term in IMPORT_BLACKLIST:
        if term == "cost_from_jsonl":
            continue  # documented D-6 exception — pre-existing, allowed
        assert term not in source, (
            f"NEW adapter-owned import term '{term}' found in {path} — "
            "spend_monitor.py's only allowed adapter dependency is the "
            "documented cost_from_jsonl sibling load (D-6)."
        )

    # The stage-4 _load_core("cost_event") loader must resolve within
    # core/scripts/ (core->core), not traverse into an adapter directory.
    assert "_load_core(" in source, "expected the stage-4 _load_core helper to be present"
    assert '"core" / "scripts"' not in source or "core_dir = Path(__file__).resolve().parent" in source, (
        "_load_core must resolve relative to this file's own (core/scripts/) "
        "directory, not traverse into an adapter path"
    )
    assert "adapters" not in source, (
        "spend_monitor.py must not reference the adapters/ directory (core-purity)"
    )


def test_dashboard_model_py_has_no_adapter_imports():
    """dashboard_model.py (stage-4 T-10 touched it) must remain fully
    core-pure: zero ACTUAL import statements naming an adapter-owned module.
    T-10's edit is a string-literal + dict-key change only (partial default +
    merge whitelist) — it adds no import.

    Anchored to real `import`/`from` statement lines only (MINOR-1) — a bare
    substring check would spuriously flag the module's own docstring, which
    explicitly documents what it does NOT import (e.g. "NO cost_from_jsonl,
    NO analyze_cost_ledger...").
    """
    path = REPO_ROOT / "quoin" / "core" / "scripts" / "dashboard_model.py"
    source = path.read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines()
        if re.match(r"^\s*(from|import)\s+", line)
    ]
    for term in IMPORT_BLACKLIST:
        offending = [ln for ln in import_lines if term in ln]
        assert offending == [], (
            f"Adapter-owned import term '{term}' found in an actual import "
            f"statement in {path}: {offending}. "
            "dashboard_model.py must stay core-pure (no adapter imports)."
        )


# ---------------------------------------------------------------------------
# T-07 (stage 6): directory-wide core-purity guard for the IVG-111
# cost-attribution surface. Walks EVERY file in quoin/core/scripts/ (not just
# the three files targeted above) and asserts no actual import statement
# names the adapter-only resolver/pricer (`agent_transcript_cost`) or the
# historical backfill script (`backfill_cost_attribution`) — the invariant
# stated in architecture D-04/R-05 and documented in runtime-portability.md.
# Anchored to real import/from lines only, mirroring
# test_dashboard_model_py_has_no_adapter_imports's anchoring discipline (a
# bare substring scan would spuriously flag this test file's own docstrings
# elsewhere in the suite, and any future doc comment inside a core script).
# ---------------------------------------------------------------------------

_COST_ATTRIBUTION_TERMS = ["agent_transcript_cost", "backfill_cost_attribution", "cost_ledger_summary"]


def test_core_scripts_directory_never_imports_cost_attribution_adapter_modules():
    assert CORE_SCRIPTS_DIR.exists(), f"expected core scripts dir at {CORE_SCRIPTS_DIR}"
    offenses = []
    for py_file in sorted(CORE_SCRIPTS_DIR.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        import_lines = [
            line for line in source.splitlines()
            if re.match(r"^\s*(from|import)\s+", line)
        ]
        for term in _COST_ATTRIBUTION_TERMS:
            for line in import_lines:
                if term in line:
                    offenses.append((py_file.name, term, line.strip()))
    assert offenses == [], (
        "quoin/core/scripts/ must never import the Claude-adapter-owned "
        "cost-attribution resolver/pricer/backfill modules (D-04/R-05):\n"
        + "\n".join(f"  {f}: {term!r} in {ln!r}" for f, term, ln in offenses)
    )
