"""IVG-164 stage 1 T-09: fail-closed CLAUDE.md-citation disposition sweep (architecture D-04b).

Enumerates every live `CLAUDE.md`-mentioning line across the three in-scope
deployed corpora (adapter `SKILL.md` files, `quoin/memory/*.md`,
`quoin/scripts/*.py`) and checks it against a committed disposition fixture,
fail-closed in BOTH directions:

  - a live mention line with no matching fixture record FAILS (a new,
    unclassified CLAUDE.md citation cannot ship silently);
  - a fixture record with no matching live mention line FAILS (the fixture
    cannot silently accumulate stale entries after an edit).

Fixture keys are `{file, sha256(normalized_line_text)}` (plan D-04): line
numbers rot on the first unrelated edit, content hashes survive relocation
and still fail loudly on a genuine text change.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ (repo root)
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_SCRIPTS = _SOURCE_ROOT / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import build_claude_slim as bcs  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_md_citation_dispositions.json"

_ADAPTER_SKILLS = _SOURCE_ROOT / "adapters" / "claude" / "skills"
_MEMORY = _SOURCE_ROOT / "memory"
_SCRIPTS_DIR = _SOURCE_ROOT / "scripts"

_MENTION_RE = re.compile(r"CLAUDE\.md")

_VALID_DISPOSITIONS = {"resolves", "rewritten", "non-citation", "known-exception"}
_VALID_PATH_FORMS = {
    "quoin_home_claude_md",
    "quoin_home_memory",
    "project_relative",
    "repo_source",
    "none",
}


def _norm(line: str) -> str:
    return line.strip()


def _relpath(p: Path) -> str:
    return str(p.relative_to(_REPO_ROOT))


def _enumerate_live_mentions() -> dict[tuple[str, str], tuple[str, int, str]]:
    """Return {(file, line_hash): (file, lineno, line_text)} for every live mention line."""
    out: dict[tuple[str, str], tuple[str, int, str]] = {}
    corpora = (
        sorted(_ADAPTER_SKILLS.glob("*/SKILL.md"))
        + sorted(_MEMORY.glob("*.md"))
        + sorted(_SCRIPTS_DIR.glob("*.py"))
    )
    for fp in corpora:
        relpath = _relpath(fp)
        text = fp.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.split("\n"), start=1):
            if _MENTION_RE.search(line):
                key = (relpath, hashlib.sha256(_norm(line).encode("utf-8")).hexdigest())
                out[key] = (relpath, lineno, line)
    return out


def _load_fixture() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["records"]


def test_every_live_mention_line_is_classified_in_fixture():
    """Fail-closed direction 1: a new, unclassified CLAUDE.md mention makes this fail."""
    live = _enumerate_live_mentions()
    fixture_keys = {(r["file"], r["line_hash"]) for r in _load_fixture()}
    unclassified = sorted(
        f"{file}:{lineno}: {text.strip()[:120]!r}"
        for (file, _hash), (file2, lineno, text) in live.items()
        if (file, _hash) not in fixture_keys
    )
    assert not unclassified, (
        f"{len(unclassified)} live CLAUDE.md mention line(s) have no fixture "
        "record (fail-closed, new-citation direction). Classify each in "
        f"{FIXTURE}:\n" + "\n".join(unclassified)
    )


def test_every_fixture_record_matches_a_live_mention_line():
    """Fail-closed direction 2 (stale-entry direction): a record with no live line makes this fail."""
    live_keys = set(_enumerate_live_mentions().keys())
    stale = [
        f"{r['file']}#{r['line_hash'][:12]}"
        for r in _load_fixture()
        if (r["file"], r["line_hash"]) not in live_keys
    ]
    assert not stale, (
        f"{len(stale)} fixture record(s) have no matching live mention line "
        "(stale-entry direction) — the cited line was edited or removed; "
        f"update or delete the fixture record: {stale}"
    )


def test_fixture_records_well_formed():
    for r in _load_fixture():
        assert r["heading_disposition"] in _VALID_DISPOSITIONS, r
        assert r["path_form"] in _VALID_PATH_FORMS, r
        assert r.get("note"), f"record missing a note: {r}"


def test_no_resolves_record_has_quoin_home_claude_md_path_form():
    """Cross-axis clause (D-10/MAJ-3): resolves + quoin_home_claude_md is forbidden.

    __QUOIN_HOME__/CLAUDE.md never resolves under project scope (substitute_
    quoin_home replaces __QUOIN_HOME__ with dest_root, but merge_workflow_
    rules writes the deployed CLAUDE.md to dest_root.parent/CLAUDE.md) — so a
    citation asserting BOTH "this resolves" AND "via that exact path" is
    self-contradictory and must never ship.
    """
    violations = [
        r
        for r in _load_fixture()
        if r["heading_disposition"] == "resolves" and r["path_form"] == "quoin_home_claude_md"
    ]
    assert not violations, violations


def test_known_exception_records_have_a_nonempty_owner():
    """Every known-exception record names the stage/follow-up that will resolve it."""
    violations = [
        f"{r['file']}#{r['line_hash'][:12]}"
        for r in _load_fixture()
        if r["heading_disposition"] == "known-exception" and not r.get("owner", "").strip()
    ]
    assert not violations, (
        f"{len(violations)} known-exception record(s) have no owner: {violations}"
    )


def test_resolves_records_cite_a_catalog_reachable_heading():
    """Every resolves record cites a real source heading that a reader can reach.

    The cited heading must be a CLASSIFICATION key (kept in slim, or dropped and
    therefore verbatim in the catalog), and the committed slim file must carry
    the sentence that points readers at the catalog.
    """
    headings = set(bcs.CLASSIFICATION)
    violations = []
    records = [r for r in _load_fixture() if r["heading_disposition"] == "resolves"]
    assert records, "no resolves records; the check would be vacuous"
    catalog = (_MEMORY / "workflow-catalog.md").read_text(encoding="utf-8")
    for r in records:
        heading = r.get("cited_heading")
        if heading not in headings:
            violations.append((r["file"], r["line_hash"][:12], heading))
        elif CLASSIFICATION_DROP(heading) and f"\n{heading}\n" not in catalog:
            violations.append((r["file"], r["line_hash"][:12], heading, "not in catalog"))
    assert not violations, violations
    slim = (_SOURCE_ROOT / "CLAUDE.slim.md").read_text(encoding="utf-8")
    assert "workflow-catalog.md" in slim and "means that section there" in slim


def CLASSIFICATION_DROP(heading):
    return bcs.CLASSIFICATION[heading][0] == "drop"


# --- Dangler-family fixes shipped in this task -------------------------------

def test_dangler_families_gone():
    """The five path/heading-dangler families named in the plan no longer appear."""
    gate_text = (_ADAPTER_SKILLS / "gate" / "SKILL.md").read_text(encoding="utf-8")
    assert 'CLAUDE.md "User-facing rendered output"' not in gate_text

    eot_text = (_ADAPTER_SKILLS / "end_of_task" / "SKILL.md").read_text(encoding="utf-8")
    assert 'CLAUDE.md says "deleted by /end_of_task before archive"' not in eot_text

    sleep_text = (_ADAPTER_SKILLS / "sleep" / "SKILL.md").read_text(encoding="utf-8")
    assert "__QUOIN_HOME__/CLAUDE.md`; if missing, use hardcoded defaults" not in sleep_text

    architect_text = (_ADAPTER_SKILLS / "architect" / "SKILL.md").read_text(encoding="utf-8")
    assert "per CLAUDE.md model assignments" not in architect_text

    iw_text = (_ADAPTER_SKILLS / "init_workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "See __QUOIN_HOME__/CLAUDE.md for the full rules." not in iw_text

    mm_text = (_MEMORY / "memory-maintenance.md").read_text(encoding="utf-8")
    assert "__QUOIN_HOME__/CLAUDE.md" not in mm_text


def test_init_workflow_frontmatter_citation_retained():
    """IVG-164 S-4 (plan D-03): stage 4 owns and has now trimmed init_workflow/SKILL.md:3;
    the __QUOIN_HOME__/CLAUDE.md citation is RETAINED as the template's single permitted
    constraint clause, and the fixture record tracks the trimmed line's hash."""
    iw_text = (_ADAPTER_SKILLS / "init_workflow" / "SKILL.md").read_text(encoding="utf-8")
    line3 = iw_text.split("\n")[2]
    assert "__QUOIN_HOME__/CLAUDE.md" in line3
    fixture_keys = {(r["file"], r["line_hash"]) for r in _load_fixture()}
    key = (
        "quoin/adapters/claude/skills/init_workflow/SKILL.md",
        hashlib.sha256(_norm(line3).encode("utf-8")).hexdigest(),
    )
    assert key in fixture_keys, "init_workflow/SKILL.md:3 must be present in the fixture"
    rec = next(r for r in _load_fixture() if (r["file"], r["line_hash"]) == key)
    assert rec["heading_disposition"] == "known-exception"
    assert rec["owner"] == "stage 4"


def test_injecting_unclassified_mention_would_fail(tmp_path):
    """Mutation proof: an unclassified CLAUDE.md mention in an in-scope corpus reds the sweep."""
    scratch = tmp_path / "scratch_skill" / "SKILL.md"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("A brand-new unclassified mention of CLAUDE.md here.\n", encoding="utf-8")
    text = scratch.read_text(encoding="utf-8")
    live_line = next(ln for ln in text.split("\n") if _MENTION_RE.search(ln))
    fixture_keys = {(r["file"], r["line_hash"]) for r in _load_fixture()}
    key = ("scratch_skill/SKILL.md", hashlib.sha256(_norm(live_line).encode("utf-8")).hexdigest())
    assert key not in fixture_keys


def test_claude_md_has_zero_yaml_fences():
    """review-1.md MINOR 8: guards the sleep_score.py section-parser leg staying inert.

    sleep_score.py's load_config searches for a fenced ```yaml``` block under
    `### /sleep importance signals`; CLAUDE.md has never carried one (round-3
    critic finding), which is why that parsing leg is already dead under the
    FULL variant too, not just the slim variant that drops the section. If a
    yaml fence is ever added here, the leg revives and needs re-examination
    under the slim variant (where the section is gone).
    """
    claude_md_text = (_SOURCE_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "```yaml" not in claude_md_text

