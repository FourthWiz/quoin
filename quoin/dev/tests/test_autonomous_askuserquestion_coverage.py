"""Structural-canary coverage test for `[autonomous]` mode propagation (IVG-153, T-19).

This is the PRIMARY load-bearing test for the autonomous-run-mode feature (Stage 1).
It replaces any hardcoded roster of "skills that need autonomous coverage" with a
LIVE-DERIVED graph traversal: it parses the actual spawn edges out of every SKILL.md
source at test time, computes the transitive closure reachable from `/run`, and asserts
that closure is exactly matched by the set of skills carrying a genuine `[autonomous]`
parse-and-branch. NO count is ever hardcoded in the assertion logic -- a newly added
spawnable skill (or a missed existing one, as `/end_of_task` was in planning rounds 1-2)
makes this test FAIL until it is covered, by construction.

Two guards:
  (a) test_propagation_set_equals_transitive_spawn_closure -- STRUCTURAL set-equality
      guard over the live-derived spawn graph.
  (b) test_every_askuserquestion_site_in_closure_has_autonomous_resolution -- PER-SITE
      coverage lint: every literal `AskUserQuestion` occurrence in the closure must have
      an autonomous resolution in its enclosing (##-heading-bounded) section.

m-2 LIMITATION (documented per plan acceptance): the per-site lint in (b) keys on the
literal token `AskUserQuestion`. Decision points implemented as PROSE ONLY -- thorough_plan's
same-class escalation (rule 3 / "Same-class detection", source lines ~L303/L320) and its
auto-classification confirm (1c, source line ~L149) -- are NOT `AskUserQuestion` call sites
and are therefore invisible to this lint. Those two prose sites are covered manually by
`test_thorough_plan_autonomous.py` (T-05) and must be kept in sync by hand if their wording
changes. A future prompt mechanism that does not use the literal `AskUserQuestion` token
would likewise escape this lint's per-site guard (though NOT the structural set-equality
guard in (a), which is independent of how a prompt is implemented).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"

SEED_SKILL = "run_hard"

# ---------------------------------------------------------------------------
# Part 1: live spawn-edge parsing + transitive closure
# ---------------------------------------------------------------------------
#
# Recognized spawn-edge phrasings (verified against the actual SKILL.md corpus,
# 2026-07-20): `spawn \`/X\``, `invoke \`/X\``, `Spawn \`/X\` as a subagent`,
# `dispatch ... /X`, the review -> `/security_review` fan-out ("...deeper subagent
# spawn prompt ... the Large-only `/security_review` OWASP-pass spawn..."), and the
# architect Phase-4 `/critic` spawn ("`/architect` spawns `/critic --target=...`").
#
# The parser is intentionally conservative about DIRECTION: many SKILL.md files
# describe being spawned BY another skill ("spawned by `/run`", "invoked by
# `/architect`") or narrate a THIRD skill's spawn behavior ("`thorough_plan`
# re-prefixes `[autonomous]` onto its `/plan` spawn") without that being an edge
# owned by the file currently being parsed. Both patterns are excluded below so the
# graph reflects only each skill's OWN outgoing spawns.

_VERB_RE = re.compile(r"\b(spawn|invoke|dispatch)\w*\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"`/(\w[\w-]*)[^`\n]*`")
_BARE_ACTOR_RE = re.compile(r"`([a-zA-Z][\w-]*)`")
_PASSIVE_BEFORE_RE = re.compile(r"\b(is|was|being|has been|have been)\s*$", re.IGNORECASE)
_TOKEN_IMMEDIATELY_BEFORE_RE = re.compile(r"`\s*$")
_REVERSE_CONNECTOR_RE = re.compile(r"^\s*(by|from)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(not|never|n't)\b", re.IGNORECASE)
_FORWARD_WINDOW = 180  # chars scanned forward from a verb, cut at the first newline


def parse_spawn_edges(skill_name: str, text: str) -> set[str]:
    """Return the set of skill names `skill_name`'s SKILL.md documents spawning.

    For every occurrence of spawn/invoke/dispatch, scan forward (same line only --
    real spawn documentation is same-line/same-bullet in this corpus) for backtick
    `/token` targets, honoring three exclusions: (1) passive voice ("is/was spawned")
    -- the CURRENT skill is the one being spawned, not the spawner; (2) a bare-word
    actor name (e.g. `` `thorough_plan` ``, no leading slash) appearing between the
    verb and the token, naming a DIFFERENT skill as the true spawner (third-party
    narration); (3) a `by `/`from ` connector immediately after the verb ("spawned by
    `/run`"). A trailing negation ("do not spawn", "never invoke") within the
    verb-to-token span drops that specific match. A bare "dispatch" immediately
    preceded by a closing backtick (e.g. "a fresh `/implement` dispatch is needed") is
    treated as a noun, not an action verb, and is skipped entirely.
    """
    edges: set[str] = set()
    for vm in _VERB_RE.finditer(text):
        vstart, vend = vm.start(), vm.end()
        pre = text[max(0, vstart - 20):vstart]
        if _PASSIVE_BEFORE_RE.search(pre):
            continue
        verb_word = vm.group(1).lower()
        if verb_word == "dispatch" and _TOKEN_IMMEDIATELY_BEFORE_RE.search(pre):
            continue

        fwd_end = min(len(text), vend + _FORWARD_WINDOW)
        fwd_text = text[vend:fwd_end]
        newline_pos = fwd_text.find("\n")
        if newline_pos != -1:
            fwd_text = fwd_text[:newline_pos]
        if _REVERSE_CONNECTOR_RE.match(fwd_text[:15]):
            continue

        events = [("token", m) for m in _TOKEN_RE.finditer(fwd_text)]
        events += [("actor", m) for m in _BARE_ACTOR_RE.finditer(fwd_text)]
        events.sort(key=lambda e: e[1].start())

        current_actor = None
        for kind, m in events:
            if kind == "actor":
                current_actor = m.group(1)
                continue
            token_end_abs = vend + m.end()
            neg_lo = max(0, vstart - 25)
            if _NEGATION_RE.search(text[neg_lo:token_end_abs]):
                continue
            if current_actor is not None and current_actor != skill_name:
                continue  # a different named skill is the documented spawner here
            target = m.group(1)
            if target == skill_name:
                continue
            edges.add(target)
    return edges


def load_skill_text(skill_name: str) -> str | None:
    path = SKILLS_DIR / skill_name / "SKILL.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def build_spawn_closure(seed: str = SEED_SKILL, loader=load_skill_text) -> set[str]:
    """BFS the spawn graph to fixpoint starting from `seed` (default: `/run`)."""
    visited: set[str] = set()
    frontier = [seed]
    while frontier:
        name = frontier.pop()
        if name in visited:
            continue
        visited.add(name)
        text = loader(name)
        if text is None:
            continue  # dead end: spawned name has no SKILL.md (e.g. an injected fake edge)
        for target in parse_spawn_edges(name, text):
            if target not in visited:
                frontier.append(target)
    return visited


# ---------------------------------------------------------------------------
# Part 2: [autonomous] propagation coverage
# ---------------------------------------------------------------------------

_BRANCH_MARKER_RE = re.compile(
    r"\bUnder\b[^\n]{0,25}\[autonomous\]"
    r"|\bUnder\b[^\n]{0,25}AUTONOMOUS"
    r"|Autonomous[- ](class|fail-OPEN|mode|re-prefix|sentinel|auto-approve|hard.stop)"
    r"|_AUTONOMOUS",
    re.IGNORECASE,
)


def skill_has_autonomous_coverage(text: str) -> bool:
    """True if `text` carries both an `[autonomous]` sentinel mention and at least one
    recognizable branch marker (`Under [autonomous]`, `Autonomous mode:`, `_AUTONOMOUS`
    state, etc.)."""
    return ("[autonomous]" in text) and bool(_BRANCH_MARKER_RE.search(text))


def build_propagation_set(candidates, loader=load_skill_text) -> set[str]:
    """Independently verify autonomous coverage for each candidate skill.

    SCOPE NOTE (deliberate, not a shortcut): candidates are the derived spawn closure's
    own members, not a blind scan of every SKILL.md in the repo. This is required by
    the codebase's own byte-identity sync contracts: T-23's generated `§0'`/`§0''`
    fail-open clause is propagated byte-identically to ALL 10 Opus-tier leaf skills,
    including `init_workflow` (never reached by `/run`'s autonomous spawn graph), and
    T-25's hand-synced `§0`-worktree-fallback/sidecar clause is propagated
    byte-identically to all 12 artifact-only + 4 source-mutating skills (e.g. `pr`,
    `rollback`, `sleep`, `checkpoint` -- none of them reachable under autonomous
    either) purely to keep two drift tests green (`test_quoin_pollution_preamble.py`,
    `test_quoin_stage1_worktree_fallback.py`). A full-repo scan for "carries an
    `[autonomous]` branch" therefore returns a near-universal superset (~28/29 skills)
    that is NOT the propagation set the architecture describes -- it would make this
    test permanently red on a correctly implemented tree. The live-derived spawn
    closure is the authoritative "what must be covered" signal; every one of ITS
    members is independently re-verified against its own source text below, which is
    exactly what makes both required failure modes bite (see the two guard-verification
    tests further down): a closure member losing its branch, or a newly-reachable skill
    never gaining one.
    """
    covered = set()
    for name in candidates:
        text = loader(name)
        if text is not None and skill_has_autonomous_coverage(text):
            covered.add(name)
    return covered


# ---------------------------------------------------------------------------
# (a) Structural set-equality guard
# ---------------------------------------------------------------------------


def test_propagation_set_equals_transitive_spawn_closure():
    # NOTE: this comment is documentation only, NEVER read by the assertions below.
    # The closure currently resolves to 15 skills (run, discover, enrich, specify,
    # architect, thorough_plan, plan, critic, revise, revise-fast, implement, gate,
    # review, security_review, end_of_task) -- but the graph is re-derived live on
    # every run; no count is frozen anywhere in this file's logic.
    closure = build_spawn_closure()
    assert closure, "spawn closure came back empty -- parser regression (seed unreachable?)"
    assert SEED_SKILL in closure

    propagation = build_propagation_set(closure)

    missing_coverage = closure - propagation
    extra_coverage = propagation - closure  # trivially empty by construction (see scope note)

    assert not missing_coverage, (
        "skills in the live-derived transitive spawn closure lack `[autonomous]` "
        f"parse+branch coverage: {sorted(missing_coverage)}"
    )
    assert not extra_coverage, (
        f"propagation set contains skills outside the derived closure: {sorted(extra_coverage)}"
    )


def test_guard_catches_injected_fake_spawn_edge():
    """Verification (i): a fabricated `spawn \\`/foo\\`` edge added to `/run`'s spawn
    text must make the derived closure gain `foo`, which cannot be covered (no such
    skill exists) -- `closure - propagation` becomes non-empty. Purely in-memory via a
    custom loader; no real SKILL.md is read from or written to."""
    real_run_text = load_skill_text("run_hard")
    assert real_run_text is not None
    tampered_run_text = real_run_text + "\n\nSpawn `/foo` as a subagent for a fabricated phase.\n"

    def tampered_loader(name: str) -> str | None:
        if name == "run_hard":
            return tampered_run_text
        return load_skill_text(name)

    closure = build_spawn_closure(loader=tampered_loader)
    assert "foo" in closure, "injected fake spawn edge did not reach the closure -- parser regression"

    propagation = build_propagation_set(closure, loader=tampered_loader)
    assert closure - propagation, (
        "expected the fabricated 'foo' spawn target to be uncovered (closure - "
        "propagation non-empty); the guard failed to catch the injected edge"
    )


def test_guard_catches_missing_autonomous_branch():
    """Verification (ii): stripping `end_of_task`'s `[autonomous]`/`AUTONOMOUS` branch
    markers (simulating a revert of T-24) must drop it from the propagation set while it
    remains in the spawn closure (still reachable via `/run`'s direct spawn edge, which
    lives in `run`'s own text and is unaffected) -- `closure - propagation` becomes
    non-empty. Purely in-memory via a custom loader; no real SKILL.md is touched."""
    real_eot_text = load_skill_text("end_of_task")
    assert real_eot_text is not None
    stripped = real_eot_text
    for token in ("[autonomous]", "_AUTONOMOUS", "AUTONOMOUS", "Autonomous"):
        stripped = stripped.replace(token, "")

    def tampered_loader(name: str) -> str | None:
        if name == "end_of_task":
            return stripped
        return load_skill_text(name)

    closure = build_spawn_closure(loader=tampered_loader)
    assert "end_of_task" in closure, "end_of_task must still be reachable via run's direct spawn edge"

    propagation = build_propagation_set(closure, loader=tampered_loader)
    assert "end_of_task" not in propagation, "stripping the autonomous markers should have removed coverage"
    assert closure - propagation, (
        "expected closure - propagation to be non-empty after stripping end_of_task's "
        "autonomous branch coverage; the guard failed to catch the regression"
    )


# ---------------------------------------------------------------------------
# (b) Per-site coverage lint
# ---------------------------------------------------------------------------

_H2_RE = re.compile(r"^##\s", re.MULTILINE)
_SITE_MARKER_RE = re.compile(
    r"autonomous|non-interactive dispatch|do not call|never (auto-)?block",
    re.IGNORECASE,
)


def _h2_sections(text: str):
    idxs = [m.start() for m in _H2_RE.finditer(text)]
    idxs.append(len(text))
    bounds = []
    if idxs and idxs[0] != 0:
        bounds.append((0, idxs[0]))
    for i in range(len(idxs) - 1):
        bounds.append((idxs[i], idxs[i + 1]))
    return bounds


def find_uncovered_askuserquestion_sites(text: str) -> list[int]:
    """Return the char offsets of every `AskUserQuestion` occurrence whose enclosing
    `##`-bounded section lacks an autonomous resolution marker."""
    sections = _h2_sections(text)
    uncovered = []
    for m in re.finditer(r"AskUserQuestion", text):
        idx = m.start()
        section_text = next((text[s:e] for s, e in sections if s <= idx < e), text)
        if not _SITE_MARKER_RE.search(section_text):
            uncovered.append(idx)
    return uncovered


@pytest.fixture(scope="module")
def closure_texts() -> dict[str, str]:
    closure = build_spawn_closure()
    return {name: text for name in closure if (text := load_skill_text(name)) is not None}


def test_every_askuserquestion_site_in_closure_has_autonomous_resolution(closure_texts):
    failures = {}
    for name, text in closure_texts.items():
        uncovered = find_uncovered_askuserquestion_sites(text)
        if uncovered:
            line_nos = [text.count("\n", 0, idx) + 1 for idx in uncovered]
            failures[name] = line_nos
    assert not failures, f"uncovered AskUserQuestion sites (by line number): {failures}"


# ---------------------------------------------------------------------------
# Special cases explicitly named by the plan (T-19 acceptance)
# ---------------------------------------------------------------------------


def test_discover_repo_spec_offer_autonomous_skip():
    """discover's two GENUINE non-dispatch sites (DRAFT/REFRESH repo-spec offer) must be
    auto-SKIPPED under autonomous, and the repo main spec must never be auto-written."""
    text = load_skill_text("discover")
    assert text is not None
    normalized = " ".join(text.split())
    assert "skip this entire offer" in normalized
    assert "DRAFT prompt" in normalized and "REFRESH prompt" in normalized
    assert "do NOT call `AskUserQuestion`" in normalized
    assert "NEVER auto-write or auto-modify" in normalized


def test_end_of_task_four_body_prompts_autonomous_covered():
    """end_of_task's four terminal body prompts (garbage/commit/lessons/archive) each
    need their own autonomous branch -- the general per-site lint above already proves
    this structurally; this test pins the specific step markers so a future rename
    can't silently widen the H2-section window past an uncovered step."""
    text = load_skill_text("end_of_task")
    assert text is not None
    step_markers = [
        "**Step 1b: Working-tree cleanup scan**",
        "**Step 2: Commit decision",
        "**Step 3: Lessons learned",
        "**Step 4: Archive type",
    ]
    for i, marker in enumerate(step_markers):
        idx = text.index(marker)
        end = text.index(step_markers[i + 1]) if i + 1 < len(step_markers) else idx + 1500
        window = text[idx:end]
        assert "**Autonomous mode:**" in window, f"missing autonomous branch near {marker!r}"


def test_end_of_task_worktree_sidecar_covered():
    """end_of_task's §0-worktree sidecar prompt requires the T-25 fail-OPEN clause."""
    text = load_skill_text("end_of_task")
    assert text is not None
    assert "<!-- §0-sidecar-begin -->" in text and "<!-- §0-sidecar-end -->" in text
    start = text.index("<!-- §0-sidecar-begin -->")
    end = text.index("<!-- §0-sidecar-end -->")
    sidecar = text[start:end]
    assert "[autonomous]" in sidecar


def test_revise_fast_worktree_site_covered():
    """revise-fast's only reachable interactive prompt is the worktree-class site;
    requires the T-25 fail-OPEN clause."""
    text = load_skill_text("revise-fast")
    assert text is not None
    assert "<!-- §0-worktree-fallback-begin -->" in text
    start = text.index("<!-- §0-worktree-fallback-begin -->")
    end = text.index("<!-- §0-worktree-fallback-end -->")
    block = text[start:end]
    assert "[autonomous]" in block
    assert "AskUserQuestion" in block


def test_thorough_plan_resume_selects_resume_not_new_session():
    """§1b resume auto-selects "Resume", never the "new session"/STOP option."""
    text = load_skill_text("thorough_plan")
    assert text is not None
    normalized = " ".join(text.split())
    assert "auto-select option **(a) Resume**" in normalized
    assert "NEVER auto-select **(c) Resume in a new session**" in normalized


def test_end_of_task_commit_selects_commit_not_abort():
    """end_of_task's commit prompt selects "Commit", never "Abort"."""
    text = load_skill_text("end_of_task")
    assert text is not None
    idx = text.index("**Step 2: Commit decision")
    end = text.index("**Step 3: Lessons learned")
    section = text[idx:end]
    autonomous_idx = section.index("**Autonomous mode:**")
    clause = section[autonomous_idx: autonomous_idx + 400]
    assert '"Commit"' in clause
    assert "NEVER" in clause
    assert '"Abort"' in clause


# ---------------------------------------------------------------------------
# Floor counts (secondary sanity only -- the guards above are primary)
# ---------------------------------------------------------------------------

# Per-skill floor counts asserted `>=` the live grep totals recorded when this plan's
# census was taken (2026-07-20). These are a FLOOR, not a frozen target: current counts
# are expected to be >= these numbers as the corpus grows. Never treat this dict as the
# source of truth for closure membership -- that is exclusively `build_spawn_closure()`.
_FLOOR_COUNTS = {
    "run_hard": 2,
    "discover": 6,
    "enrich": 5,
    "specify": 6,
    "architect": 10,
    "thorough_plan": 8,
    "plan": 4,
    "critic": 4,
    "revise": 4,
    "revise-fast": 3,
    "implement": 7,
    "gate": 4,
    "review": 4,
    "security_review": 4,
    "end_of_task": 11,
}


def test_askuserquestion_floor_counts(closure_texts):
    for name, floor in _FLOOR_COUNTS.items():
        text = closure_texts.get(name)
        assert text is not None, f"{name} missing from the live-derived closure"
        count = len(re.findall(r"AskUserQuestion", text))
        assert count >= floor, f"{name}: live AskUserQuestion count {count} fell below floor {floor}"
