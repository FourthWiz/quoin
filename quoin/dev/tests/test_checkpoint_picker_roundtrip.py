"""Characterization harness for checkpoint_picker.select_restore (IVG-139, S-2).

READ THIS FIRST — anti-circularity / authority hierarchy (T-01, MAJ-1)
-----------------------------------------------------------------------
This harness proves `module == S-1 spec` (`quoin/memory/checkpoint-spec.md`),
NOT `module == the live runtime SKILL.md prose`. The live
`quoin/adapters/claude/skills/checkpoint/SKILL.md` restore picker (Step 1.0,
lines ~700-999) is left UNCHANGED in this stage — rewiring it to call this
module is a follow-up stage (S-3). Spec fidelity is therefore the singly
-sourced assumption this harness rests on (S-2 plan T-08 corrected the one
known spec/prose divergence found in round 2, the CRIT-1 anchor-task
precedence).

Deriving expected values purely from the spec is NOT anti-circular with
respect to prose fidelity: the module is written to the spec and the golden
values are read from the same spec, so a spec blind spot is invisible to a
spec-only test. To mitigate this, every EXPECTED-table row below (and every
assertion in the corpus this file grows into, T-05) is tagged with one of
three independent authorities, in DECREASING trust:

  1. "incident"  — the expected Verdict is anchored to an observed
     wrong-then-right behavior resolution (a Linear issue, or a dated
     `fixtures/checkpoint_picker/` scenario) — independent of the spec's own
     prose restatement.
  2. "prose"     — the expected value is pinned to a QUOTED SKILL.md line
     (not a spec paraphrase), so a spec-vs-prose divergence would be caught,
     not encoded. Example (CRIT-1, SKILL.md:886):
       `freshest_task="${_anchor_task}"` (anchor-first; freshest-session is
       only the `-z` fallback at SKILL.md:887-890).
  3. "spec"      — the residual, WEAKEST anchor: cites only the
     `checkpoint-spec.md` section. Used only where no incident or prose line
     is available.

TODO(T-04/T-05): this file currently holds the harness scaffold, the fixture
builder, and a FOUNDATIONAL set of round-trip cases only (T-01/T-02/T-03 of
the S-2 plan). The full incident corpus (anchor-task precedence twin fixture,
B3 Clause-B, staleness-boundary, compact-ordering/dedup, same-sid collision,
etc. — S-2 plan T-05) and the hardened purity/parity guards (no-writes
snapshot, no-env-mutation, static side-effect grep, filename_task parity,
no-local-hash guard — S-2 plan T-04) are intentionally NOT implemented here
yet; a later dispatch extends this file. Search this file for
`# TODO(T-04/T-05)` markers for the exact extension points.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the core module under test + the sibling writer used to build a
# REAL thorough-plan-progress fixture (lesson ivg-84 — direct package import
# raises ModuleNotFoundError from this test layout; use importlib instead).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE = REPO_ROOT / "quoin" / "core" / "scripts" / "checkpoint_picker.py"
_TPC_CORE = REPO_ROOT / "quoin" / "core" / "scripts" / "thorough_plan_checkpoint.py"
_WRAPPER = REPO_ROOT / "quoin" / "scripts" / "checkpoint_picker.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_MOD = _load(_CORE, "_test_checkpoint_picker_core")
_TPC = _load(_TPC_CORE, "_test_checkpoint_picker_tpc")


# ---------------------------------------------------------------------------
# EXPECTED table (T-01) — one row per scenario, authority-tagged.
#
# TODO(T-04/T-05): this table is a STUB this round. The full incident corpus
# (each row anchored to its strongest available authority per the hierarchy
# above, including the CRIT-1 anchor-precedence row and the MIN-4
# int-truncation staleness-boundary row, both of which MUST be `prose`
# -pinned per the S-2 plan) lands in a later dispatch. The two rows below
# demonstrate the required shape and are exercised by the foundational test
# cases further down this file.
# ---------------------------------------------------------------------------

EXPECTED = [
    {
        "name": "tier1_same_task_fastpath",
        "authority": "spec",
        "citation": "checkpoint-spec.md 'Picker tiers' (Tier 1)",
        "expect": {"tier": 1, "reason": "tier1:same-task"},
    },
    {
        "name": "crit1_anchor_precedence_placeholder",
        "authority": "prose",
        "citation": (
            'freshest_task="${_anchor_task}" [SKILL.md:886] (anchor-first; '
            "freshest-session is only the -z fallback, SKILL.md:887-890) "
            "-- full twin fixture deferred to T-05"
        ),
        "expect": None,  # placeholder: real assertion added by T-05
    },
]


def test_expected_table_has_authority():
    """Meta-assertion (T-01 acceptance): every EXPECTED row carries a
    non-empty authority in {incident, prose, spec} and a citation."""
    seen_prose = False
    for row in EXPECTED:
        assert row["authority"] in {"incident", "prose", "spec"}, row["name"]
        assert row.get("citation"), row["name"]
        if row["authority"] == "prose":
            assert "SKILL.md:" in row["citation"], row["name"]
            seen_prose = True
    assert seen_prose, "at least one EXPECTED row must be prose-pinned (CRIT-1 lineage)"


# ---------------------------------------------------------------------------
# Fixture builder (T-01)
# ---------------------------------------------------------------------------

def _write(path: Path, content: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _session_state_content(task: str, sid: str = "") -> str:
    body = f"## Active task: {task}\n\n## Current stage\nimplement\n\n"
    if sid:
        body += f"## Session ID\n{sid}\n\n## Cost\n- Session UUID: {sid}\n"
    return body


def _checkpoint_content(task: str, sid: str = "unknown", branch: str = "main") -> str:
    return (
        "## Status\nvoluntary\n\n"
        "## Current stage\nimplement\n\n"
        f"## Active task\n{task}\n\n"
        f"## Branch\n{branch}\n\n"
        f"## Session ID\n{sid}\n\n"
    )


def _build_memory(tmp_path: Path, spec: dict) -> Path:
    """Materialize a temp `.workflow_artifacts/memory/` tree per `spec`.

    `spec` keys (all optional, each a list of dicts unless noted):
      sessions:        [{"name", "mtime", "task", "sid"}]  -> sessions/*.md
      checkpoints:     [{"name", "mtime", "task", "sid", "branch"}] -> checkpoints/*.md
                        (all three /checkpoint filename shapes are just
                        different `name` values, e.g.
                        "2026-07-01T0930-my-task.md" (timestamped),
                        "2026-07-01-my-task.md" (legacy),
                        "2026-07-01-my-task-precompact.md" (precompact))
      pending_restore: [{"sid", "mtime", "points_to"}] -> pending-restore-<sid>.txt
                        ("points_to" is an absolute path string written as
                        the sentinel's one-line content)
      pending_prompt:  [{"sid", "mtime"}] -> pending-prompt-<sid>.txt (empty body)

    Returns the `memory_dir` Path (`tmp_path/.workflow_artifacts/memory`).
    """
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    (memory_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (memory_dir / "checkpoints").mkdir(parents=True, exist_ok=True)

    for s in spec.get("sessions", []):
        _write(
            memory_dir / "sessions" / s["name"],
            _session_state_content(s.get("task", ""), s.get("sid", "")),
            s["mtime"],
        )

    for c in spec.get("checkpoints", []):
        _write(
            memory_dir / "checkpoints" / c["name"],
            _checkpoint_content(c.get("task", ""), c.get("sid", "unknown"), c.get("branch", "main")),
            c["mtime"],
        )

    for pr in spec.get("pending_restore", []):
        _write(
            memory_dir / f"pending-restore-{pr['sid']}.txt",
            str(pr["points_to"]) + "\n",
            pr["mtime"],
        )

    for pp in spec.get("pending_prompt", []):
        _write(memory_dir / f"pending-prompt-{pp['sid']}.txt", "\n", pp["mtime"])

    return memory_dir


# A fixed, deterministic epoch (R-07 — no wall-clock). 2026-07-14 00:00:00 UTC.
NOW = 1784332800.0
DAY = 86400.0


# ---------------------------------------------------------------------------
# (a) Same-task Tier-1 fast-path hit -> tier=1
# ---------------------------------------------------------------------------

def test_tier1_same_task_fastpath_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    monkeypatch.setenv("QUOIN_RESTORE_SENTINEL_WINDOW", "7")
    monkeypatch.setenv("QUOIN_SESSION_FALLBACK_WINDOW", "7")
    monkeypatch.setenv("QUOIN_PICKER_DEDUP_WINDOW", "7")

    sid = "SID-TIER1"
    task = "my-task"
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-my-task.md", "mtime": NOW - DAY, "task": task}],
        "checkpoints": [{"name": "2026-07-10T0900-my-task.md", "mtime": NOW - 3 * DAY,
                          "task": task, "sid": sid, "branch": "main"}],
    })
    cp_path = memory_dir / "checkpoints" / "2026-07-10T0900-my-task.md"
    _write(memory_dir / f"pending-restore-{sid}.txt", str(cp_path) + "\n", NOW - DAY)

    verdict = _MOD.select_restore(memory_dir, sid, NOW)

    assert verdict["tier"] == 1, verdict
    assert verdict["reason"] == "tier1:same-task", verdict
    assert verdict["selected_path"] == str(cp_path)
    assert verdict["cross_task_ok"] is True
    assert verdict["stale"] is False  # staleness NOT applied at Tier 1 (SKILL.md:708)
    assert verdict["derived_task"] == task
    assert verdict["kind"] == "checkpoint"


# ---------------------------------------------------------------------------
# (b) No-candidates case -> B3 (derived_task template, since a session
#     baseline exists within the fallback window)
# ---------------------------------------------------------------------------

def test_no_candidates_routes_to_b3_with_derived_task_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    monkeypatch.setenv("QUOIN_RESTORE_SENTINEL_WINDOW", "7")
    monkeypatch.setenv("QUOIN_SESSION_FALLBACK_WINDOW", "7")
    monkeypatch.setenv("QUOIN_PICKER_DEDUP_WINDOW", "7")

    task = "solo-task"
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-solo-task.md", "mtime": NOW - DAY, "task": task}],
        # no checkpoints, no sentinels -> Clause A (zero candidates)
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == "4-B3", verdict
    assert verdict["selected_path"] is None
    assert verdict["derived_task"] == task
    assert verdict["reason"] == "b3:clause-a"
    expected_prompt = (
        f"Resume task '{task}': no checkpoint selected (tier 4 / B3). Synthesize a minimal "
        f"restore from the freshest session-state file for '{task}'."
    )
    assert verdict["b3_prompt"] == expected_prompt


def test_no_candidates_and_no_sessions_b3_prompt_is_none(tmp_path, monkeypatch):
    """Total dead end (no candidates, no session baseline at all) -> b3_prompt
    is None and reason narrows to 'none:no-candidates'."""
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    monkeypatch.setenv("QUOIN_RESTORE_SENTINEL_WINDOW", "7")
    monkeypatch.setenv("QUOIN_SESSION_FALLBACK_WINDOW", "7")
    monkeypatch.setenv("QUOIN_PICKER_DEDUP_WINDOW", "7")

    memory_dir = _build_memory(tmp_path, {})

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == "4-B3"
    assert verdict["b3_prompt"] is None
    assert verdict["reason"] == "none:no-candidates"


# ---------------------------------------------------------------------------
# (c) thorough-plan-progress routing case -> reason='route:thorough-plan-progress'
# ---------------------------------------------------------------------------

def test_thorough_plan_progress_routes_out(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    monkeypatch.setenv("QUOIN_RESTORE_SENTINEL_WINDOW", "7")
    monkeypatch.setenv("QUOIN_SESSION_FALLBACK_WINDOW", "7")
    monkeypatch.setenv("QUOIN_PICKER_DEDUP_WINDOW", "7")

    sid = "SID-TPPROGRESS"
    task = "ivg-139-checkpoint-picker"

    # Own session-state file (NOT the --session-state orchestrator file _TPC
    # would create — that filename carries a "-orchestrator" suffix which
    # would NOT match `task` after date-prefix stripping and would spuriously
    # trip the Tier-1 cross-task guard). Keeping this fixture's freshest
    # session filename-task exactly equal to `task` isolates the routing
    # assertion under test.
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": f"2026-07-14-{task}.md", "mtime": NOW, "task": task}],
    })

    # Build the REAL thorough-plan-progress-{sid}.md + pending-restore-{sid}.txt
    # by calling the actual writer (T-01 requirement: schema is real, not
    # hand-faked).
    rc = _TPC.main([
        "--project-root", str(tmp_path),
        "--task", task,
        "--round", "1",
        "--phase", "plan",
        "--sid", sid,
        "--branch", "main",
    ])
    assert rc == 0

    ckpt = memory_dir / "checkpoints" / f"thorough-plan-progress-{sid}.md"
    sentinel = memory_dir / f"pending-restore-{sid}.txt"
    assert ckpt.exists() and sentinel.exists()
    # Pin mtimes so this fixture is deterministic (R-07), not wall-clock-order-dependent.
    os.utime(ckpt, (NOW, NOW))
    os.utime(sentinel, (NOW, NOW))

    verdict = _MOD.select_restore(memory_dir, sid, NOW)

    assert verdict["kind"] == "thorough-plan-progress", verdict
    assert verdict["reason"] == "route:thorough-plan-progress", verdict
    assert verdict["selected_path"] == str(ckpt)


# ---------------------------------------------------------------------------
# (d) Wrapper re-exports select_restore (T-03 acceptance)
# ---------------------------------------------------------------------------

def test_wrapper_reexports_select_restore(tmp_path):
    # The wrapper loads its own copy of the core module via importlib
    # (parents[1]/core/scripts), so its `select_restore` is a DISTINCT object
    # from the harness's independently-loaded `_MOD.select_restore` — an `is`
    # identity check can never hold across two file-based importlib loads.
    # The T-03 contract is faithful RE-EXPORT: the wrapper must expose
    # `select_restore` + `main`, and its `select_restore` must be the same
    # function the wrapper itself loaded from core AND behave identically to
    # the core's on the same input.
    wrapper = _load(_WRAPPER, "_test_checkpoint_picker_wrapper")
    assert hasattr(wrapper, "select_restore")
    assert callable(wrapper.select_restore)
    assert hasattr(wrapper, "main")
    # Re-export fidelity: the wrapper's re-exported name IS the object it loaded
    # from core (same-load identity), and both cores share a qualified name.
    assert wrapper.select_restore is getattr(wrapper._CORE, "select_restore")
    assert wrapper.select_restore.__qualname__ == _MOD.select_restore.__qualname__
    # Behavioral parity: identical Verdict on the same empty-memory input.
    memory_dir = _build_memory(tmp_path, {"sessions": []})
    assert wrapper.select_restore(memory_dir, "unknown", NOW) == _MOD.select_restore(
        memory_dir, "unknown", NOW
    )


# ===========================================================================
# T-04 — purity + parity guard tests (hard acceptance criteria, MAJ-3)
# ===========================================================================

def _tree_snapshot(root: Path):
    """Walk `root` and return {relpath: (mtime, size)} for every file."""
    snap = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(root))] = (st.st_mtime, st.st_size)
    return snap


def test_module_performs_no_writes(tmp_path, monkeypatch):
    """D-03 / T-04: select_restore must not write/create/modify ANY file
    anywhere under tmp_path (not just memory_dir) -- catches out-of-
    memory_dir escapes (tempfile.mkstemp, a stray log, a relative-path
    write). cwd is pinned inside tmp_path so a relative write would land
    inside the walked tree."""
    monkeypatch.chdir(tmp_path)
    sid = "SID-PURITY"
    task = "purity-task"
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-purity-task.md", "mtime": NOW - DAY, "task": task}],
        "checkpoints": [{"name": "2026-07-10T0900-purity-task.md", "mtime": NOW - 3 * DAY,
                          "task": task, "sid": sid}],
    })
    cp_path = memory_dir / "checkpoints" / "2026-07-10T0900-purity-task.md"
    _write(memory_dir / f"pending-restore-{sid}.txt", str(cp_path) + "\n", NOW - DAY)

    before = _tree_snapshot(tmp_path)
    verdict = _MOD.select_restore(memory_dir, sid, NOW)
    after = _tree_snapshot(tmp_path)

    assert verdict["tier"] == 1, verdict  # sanity: the call actually did work
    assert before == after, "select_restore must not write/touch any file under tmp_path"
    assert set(after) == set(before), "no new files may appear anywhere under tmp_path"


def test_module_does_not_mutate_environ(tmp_path, monkeypatch):
    """T-04 / MAJ-3: the module reads knobs via os.environ.get only; it must
    never assign into os.environ."""
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    memory_dir = _build_memory(tmp_path, {"sessions": []})
    before = dict(os.environ)
    _MOD.select_restore(memory_dir, "unknown", NOW)
    after = dict(os.environ)
    assert before == after, "select_restore must not mutate os.environ"


def _module_source_no_docstring_no_comments(path: Path) -> str:
    """Strip the module-level docstring (the only place forbidden purity
    words legitimately appear, since it explains what the module AVOIDS)
    and any full-line `#` comments, leaving only executable source for the
    static side-effect grep."""
    text = path.read_text(encoding="utf-8")
    # Remove the first \"\"\"...\"\"\" block (the module docstring).
    stripped = re.sub(r'"""(.*?)"""', "", text, count=1, flags=re.DOTALL)
    lines = [ln for ln in stripped.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines)


def test_module_issues_no_prompts_or_side_effects():
    """T-04 / MAJ-3 static guard: none of the escape-hatch substrings appear
    in the module's EXECUTABLE source (docstring + comments stripped first,
    since the module docstring legitimately narrates what it avoids)."""
    src = _module_source_no_docstring_no_comments(_CORE)

    forbidden_substrings = [
        "AskUserQuestion", "input(", ".write_text", ".write(",
        "tempfile", "subprocess", "os.system", "shutil.",
        "socket", "urllib", "requests", "http.client",
    ]
    for needle in forbidden_substrings:
        assert needle not in src, f"forbidden purity-violating token found: {needle!r}"

    # open(..., 'w') / open(..., 'a') -- write/append mode opens.
    assert not re.search(r"open\([^)]*['\"][wa]['\"]", src), "found a write/append-mode open()"
    # os.environ[...] = ... assignment.
    assert not re.search(r"os\.environ\[[^\]]*\]\s*=", src), "found an os.environ[...] assignment"


def test_module_imports_no_network_or_subprocess_libs():
    """T-04 / MAJ-3: importlib-load a FRESH copy of the module and assert
    none of the network/subprocess libraries land in sys.modules as a
    result of loading it, and none are bound as attributes on the module."""
    before = set(sys.modules.keys())
    mod = _load(_CORE, "_test_checkpoint_picker_purity_reload")
    after = set(sys.modules.keys())
    delta = after - before

    forbidden_modules = {"subprocess", "socket", "urllib", "urllib.request",
                          "requests", "http.client", "http"}
    assert not (delta & forbidden_modules), f"forbidden modules imported: {delta & forbidden_modules}"

    for name in ("subprocess", "socket", "urllib", "requests"):
        assert not hasattr(mod, name), f"module namespace carries forbidden name: {name}"


def test_filename_task_parity():
    """T-04: the module's inlined `_filename_task` (D-S2-3) must stay
    byte-identical to `verify_claims.filename_task` -- the drift guard for
    the deliberate inline-vs-import choice."""
    vc_path = REPO_ROOT / "quoin" / "core" / "scripts" / "verify_claims.py"
    vc = _load(vc_path, "_test_checkpoint_picker_verify_claims")

    battery = [
        "2026-07-14T0930-foo.md",
        "2026-07-14-foo.md",
        "2026-07-14-foo-precompact.md",
        "2026-07-14-foo",  # no .md extension
        "2026-07-14-foo-orchestrator.md",
    ]
    for name in battery:
        assert _MOD._filename_task(name) == vc.filename_task(name), name


def test_no_local_hash_derivation():
    """T-04 / lesson ivg-84: the module must reuse get_session_uuid for any
    hash derivation rather than re-implementing the project-hash regex
    locally. Build the needle at runtime by concatenation so this test file
    itself never contains the full literal (avoids self-tripping a
    repo-wide grep for the same pattern)."""
    src = _CORE.read_text(encoding="utf-8")
    needle = "re" + ".sub(r'" + "[^A-Za-z0-9-]" + "'"
    assert needle not in src, "module must not locally re-derive the project-hash regex"


# ===========================================================================
# T-05 -- incident corpus + shared-namespace + collision battery
# ===========================================================================

def _default_knobs(monkeypatch, **overrides):
    """Set the env knobs to their spec defaults, with overrides."""
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", str(overrides.get("stale_days", 1)))
    monkeypatch.setenv("QUOIN_RESTORE_SENTINEL_WINDOW", str(overrides.get("sentinel_window", 7)))
    monkeypatch.setenv("QUOIN_SESSION_FALLBACK_WINDOW", str(overrides.get("session_fallback_window", 7)))
    monkeypatch.setenv("QUOIN_PICKER_DEDUP_WINDOW", str(overrides.get("dedup_window", 7)))
    # IVG-160 distinct-task ambiguity window (SECONDS; default 4h = 14400).
    monkeypatch.setenv("QUOIN_RESTORE_AMBIGUITY_WINDOW", str(overrides.get("ambiguity_window", 14400)))


def test_anchor_task_precedence_not_suppressed(tmp_path, monkeypatch):
    # authority:prose 'freshest_task="${_anchor_task}"' [SKILL.md:886] (anchor-first;
    # freshest-session is only the -z fallback, SKILL.md:887-890) -- CRIT-1 / IVG-57 / IVG-30
    _default_knobs(monkeypatch)

    t_anchor = "anchor-task-widgets"
    t_fresh = "fresh-task-zephyr"
    pp_sid = "SID-PP-ANCHOR"
    current_sid = "SID-CURRENT-ANCHOR-MISS"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [
            # (a) MIN-1 r2 fixture invariant: this session carries an explicit
            # Session UUID matching the pending-prompt SID, so the Tier-2
            # SID->session grep (SKILL.md:734-735) actually fires (not the
            # mtime fallback) and seeds anchor_task = t_anchor.
            {"name": "2026-07-08-anchor-task-widgets.md", "mtime": NOW - 2 * DAY,
             "task": t_anchor, "sid": pp_sid},
            # freshest session -> derived_task, DIFFERENT from t_anchor.
            {"name": "2026-07-13-fresh-task-zephyr.md", "mtime": NOW - DAY,
             "task": t_fresh},
        ],
        "checkpoints": [
            # (b) MIN-1 r2: mtime >= max(session mtimes) so B3 Clause-B does
            # not pre-empt the combined gate under test.
            {"name": "2026-07-14T0000-anchor-task-widgets.md", "mtime": NOW,
             "task": t_anchor, "sid": "unknown"},
        ],
        "pending_prompt": [{"sid": pp_sid, "mtime": NOW - 0.1 * DAY}],
        # No pending-restore for pp_sid (else Tier-2 would return an anchor cp
        # directly, tier=2, and never exercise the Tier-3 combined gate under
        # baseline_task). No pending-restore for current_sid (Tier-1 MISS).
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["derived_task"] == t_fresh, verdict  # raw freshest, NOT the anchor
    assert verdict["anchor_task"] == t_anchor, verdict
    assert verdict["baseline_task"] == t_anchor, verdict  # CRIT-1: anchor takes precedence
    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict
    assert verdict["cross_task_ok"] is True, (
        "candidate task == t_anchor == baseline_task -> NOT cross-task-suppressed; "
        "a derived_task-only (freshest) model would have wrongly suppressed this"
    )


def test_anchor_task_precedence_twin_suppresses_and_b3_prompt_uses_derived_task(tmp_path, monkeypatch):
    # authority:prose "freshest_task=\"${_anchor_task}\"" [SKILL.md:886] (gate operand);
    # "TASK = active_task" [SKILL.md:989] (B3 synthesis source) -- CRIT-1 r2 twin
    _default_knobs(monkeypatch)

    t_anchor = "anchor-task-widgets"
    t_fresh = "fresh-task-zephyr"
    pp_sid = "SID-PP-ANCHOR-TWIN"
    current_sid = "SID-CURRENT-ANCHOR-TWIN-MISS"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [
            {"name": "2026-07-08-anchor-task-widgets.md", "mtime": NOW - 2 * DAY,
             "task": t_anchor, "sid": pp_sid},
            {"name": "2026-07-13-fresh-task-zephyr.md", "mtime": NOW - DAY,
             "task": t_fresh},
        ],
        "checkpoints": [
            # SAME shape as the main row, but the candidate's Active task ==
            # t_fresh (NOT t_anchor) -> baseline_task (t_anchor) !=
            # cand.task (t_fresh) -> cross-task suppressed.
            {"name": "2026-07-14T0000-fresh-task-zephyr.md", "mtime": NOW,
             "task": t_fresh, "sid": "unknown"},
        ],
        "pending_prompt": [{"sid": pp_sid, "mtime": NOW - 0.1 * DAY}],
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["baseline_task"] == t_anchor, verdict
    assert verdict["derived_task"] == t_fresh, verdict
    assert verdict["tier"] == "4-B3", verdict
    assert verdict["reason"] == "tier3:gate-suppressed:cross-task", verdict
    assert verdict["cross_task_ok"] is False, verdict

    # DISTINGUISHING assertion (CRIT-1 r2): b3_prompt synthesizes from
    # derived_task (raw freshest), NOT baseline_task (anchor) -- this is the
    # ONLY fixture where the two diverge, so it is the only place this can
    # be caught.
    assert verdict["b3_prompt"] == (
        f"Resume task '{t_fresh}': no checkpoint selected (tier 4 / B3). "
        f"Synthesize a minimal restore from the freshest session-state file for '{t_fresh}'."
    ), verdict
    assert t_fresh in verdict["b3_prompt"]
    assert t_anchor not in verdict["b3_prompt"]


def test_cross_task_rejection_no_anchor(tmp_path, monkeypatch):
    # authority:incident IVG-25/30 (wrong-session restore fixed) -- a
    # timestamp-bumped stale-task checkpoint auto-picked instead of the
    # current, different-task session; SKILL.md:858 notes the real incident
    # is caught by the combined gate specifically when Clause B does NOT
    # fire because the candidate mtime appears fresher than the session's.
    _default_knobs(monkeypatch)

    t_session = "current-real-task"
    t_wrong = "stale-wrong-task"
    current_sid = "SID-CROSS-TASK-MISS"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [
            {"name": "2026-07-12-current-real-task.md", "mtime": NOW - 2 * DAY,
             "task": t_session},
        ],
        "checkpoints": [
            # mtime is NEWER than the session (simulating the incident's
            # timestamp bump) so Clause B does not pre-empt; cross-task is
            # what must catch it.
            {"name": "2026-07-14T0000-stale-wrong-task.md", "mtime": NOW,
             "task": t_wrong, "sid": "unknown"},
        ],
        # No pending-prompt/pending-restore -> no Tier-2 anchor seeded ->
        # baseline_task == derived_task.
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["baseline_task"] == verdict["derived_task"] == t_session, verdict
    assert verdict["tier"] == "4-B3", verdict
    assert verdict["cross_task_ok"] is False, verdict
    assert verdict["reason"] == "tier3:gate-suppressed:cross-task", verdict


def test_multi_candidate_freshest_suppressed_bypasses_valid_older_same_task(tmp_path, monkeypatch):
    # authority:spec-blind-spot -- S-3 plan T-06 / MAJ-2 characterization fixture.
    #
    # This fixture documents an ACCEPTED OUTCOME CHANGE (Q-01), it does NOT prove
    # module == spec for this scenario. checkpoint-spec.md (~lines 300-310) folds
    # the numbered-picker / multi-candidate path into a single-candidate combined
    # -gate description and never models the prose's interactive 2+-candidate
    # user-override (SKILL.md:865-877, the "Two or more" numbered-picker branch).
    # This test exists so the module's actual, current behavior is documented and
    # regression-guarded, not silently untested: this harness's own
    # module==spec equivalence claim does NOT extend to this scenario.
    #
    # Scenario: 2+ candidates exist. The FRESHEST is CROSS-TASK (suppressed by
    # the combined gate). An OLDER candidate is a valid SAME-TASK checkpoint that
    # a human using the Fallback picker's interactive numbered list could select
    # directly. The module only ever evaluates the single freshest candidate
    # overall for the combined gate -- it does not fall back to the next-best
    # (older, valid, same-task) candidate. It picks the freshest, the gate
    # suppresses it, and it routes to B3 (session-state synthesis), bypassing
    # the valid older candidate entirely.
    #
    # MIN-2 (round 2 critic): the suppressed freshest candidate's mtime is
    # deliberately NEWER than the freshest in-window sessions/*.md mtime, so
    # Tier-3 Clause-B (`max_cand_mtime < max_session_mtime`) cannot pre-empt and
    # this fixture actually exercises the freshest-suppressed-bypasses-older
    # -valid path it is meant to characterize (not an unrelated Clause-B route).
    _default_knobs(monkeypatch)

    t_session = "multi-cand-real-task"
    t_wrong = "multi-cand-stale-wrong-task"
    current_sid = "SID-MULTI-CAND-MISS"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [
            {"name": "2026-07-12-multi-cand-real-task.md", "mtime": NOW - 2 * DAY,
             "task": t_session},
        ],
        "checkpoints": [
            # Freshest candidate: CROSS-TASK, mtime NEWER than the session above
            # so Clause B cannot pre-empt (MIN-2).
            {"name": "2026-07-14T0000-multi-cand-stale-wrong-task.md", "mtime": NOW,
             "task": t_wrong, "sid": "unknown"},
            # Older candidate: SAME-TASK as the session -- a genuinely valid,
            # restorable checkpoint present in the same candidate pool
            # (the `_build_memory` fixture builder supports two `checkpoints`
            # entries directly), but never reconsidered once the freshest
            # overall candidate is suppressed.
            {"name": "2026-07-11T0000-multi-cand-real-task.md", "mtime": NOW - 3 * DAY,
             "task": t_session, "sid": "unknown"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    # The module evaluates only the SINGLE freshest candidate for the combined
    # gate; it is cross-task, so it is suppressed and routed to B3 -- the valid
    # older same-task candidate is present in the candidate pool the fixture
    # built (two `checkpoints` entries) but is never selected or reconsidered.
    assert verdict["tier"] == "4-B3", verdict
    assert verdict["cross_task_ok"] is False, verdict
    assert verdict["reason"] == "tier3:gate-suppressed:cross-task", verdict
    assert not verdict["selected_path"], (
        "B3 route must not select any checkpoint file -- confirms the valid "
        "older same-task candidate was bypassed entirely, not silently chosen: "
        f"{verdict}"
    )


def test_staleness_suppresses_tier3_autopick_without_clause_b(tmp_path, monkeypatch):
    # authority:incident IVG-30 (staleness window)
    _default_knobs(monkeypatch)

    task = "staleness-task"
    current_sid = "SID-STALE-MISS"

    memory_dir = _build_memory(tmp_path, {
        # Session sits OUTSIDE the QUOIN_SESSION_FALLBACK_WINDOW (7d default,
        # here 10d old) so it is excluded from Clause-B's in-window session
        # set (max_session_mtime stays None, Clause B never fires) while
        # STILL counting as a session baseline for `freshest_session`/
        # `derived_task` (those use the UNFILTERED session_files list) --
        # this isolates the Tier-3 staleness gate from Clause-B pre-emption.
        "sessions": [{"name": "2026-07-04-staleness-task.md", "mtime": NOW - 10 * DAY,
                       "task": task}],
        "checkpoints": [
            # Same task as baseline (isolates staleness from cross-task);
            # 5 days old -> stale (threshold 1d), clear of the day boundary
            # (MIN-4).
            {"name": "2026-07-09T0000-staleness-task.md", "mtime": NOW - 5 * DAY,
             "task": task, "sid": "unknown"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["tier"] == "4-B3", verdict
    assert verdict["stale"] is True, verdict
    assert verdict["cross_task_ok"] is True, verdict
    assert verdict["reason"] == "tier3:gate-suppressed:stale", verdict


def test_staleness_not_applied_at_tier1_fastpath(tmp_path, monkeypatch):
    # authority:prose "The staleness guard is NOT applied here" [SKILL.md:708]
    _default_knobs(monkeypatch)

    task = "old-fastpath-task"
    sid = "SID-STALE-TIER1"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-old-fastpath-task.md", "mtime": NOW - DAY,
                       "task": task}],
        "checkpoints": [
            {"name": "2026-07-09T0000-old-fastpath-task.md", "mtime": NOW - 5 * DAY,
             "task": task, "sid": sid},
        ],
    })
    cp_path = memory_dir / "checkpoints" / "2026-07-09T0000-old-fastpath-task.md"
    _write(memory_dir / f"pending-restore-{sid}.txt", str(cp_path) + "\n", NOW - DAY)

    verdict = _MOD.select_restore(memory_dir, sid, NOW)

    assert verdict["tier"] == 1, verdict
    assert verdict["reason"] == "tier1:same-task", verdict
    assert verdict["stale"] is False, "staleness must NOT be applied at Tier 1 (SKILL.md:708)"


def test_staleness_int_truncation_boundary_not_stale(tmp_path, monkeypatch):
    # authority:prose "print(int(age))" [SKILL.md:898] -- MIN-4 int-truncation
    # boundary: a candidate 1.5 days old with threshold 1 must NOT be stale
    # (int(1.5) == 1; "1 -gt 1" is false).
    _default_knobs(monkeypatch)

    task = "boundary-task"
    current_sid = "SID-BOUNDARY-MISS"

    memory_dir = _build_memory(tmp_path, {
        # Session older than the candidate (so Clause B: max_cand_mtime <
        # max_session_mtime is False -- candidate is newer) so the combined
        # gate, not Clause B, is what is under test.
        "sessions": [{"name": "2026-07-11-boundary-task.md", "mtime": NOW - 3 * DAY,
                       "task": task}],
        "checkpoints": [
            {"name": "2026-07-12T1200-boundary-task.md", "mtime": NOW - 1.5 * DAY,
             "task": task, "sid": "unknown"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["stale"] is False, verdict
    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict


def test_b3_clause_b_all_candidates_older_than_freshest_session(tmp_path, monkeypatch):
    # authority:incident IVG-57 (overflowed-session recovery)
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-clause-b-task.md", "mtime": NOW - 0.1 * DAY,
                       "task": "clause-b-task"}],
        "checkpoints": [
            # disk-only (no sentinel), older than the freshest session file.
            {"name": "2026-07-11T0000-clause-b-task.md", "mtime": NOW - 3 * DAY,
             "task": "clause-b-task", "sid": "unknown"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == "4-B3", verdict
    assert verdict["reason"] == "b3:clause-b", verdict


def test_same_session_detection(tmp_path, monkeypatch):
    # authority:incident IVG-105 (same-session detection)
    _default_knobs(monkeypatch)

    sid = "SID-SAME-SESSION"
    task = "same-session-task"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-same-session-task.md", "mtime": NOW - DAY,
                       "task": task}],
        "checkpoints": [
            {"name": "2026-07-13T1000-same-session-task.md", "mtime": NOW - DAY,
             "task": task, "sid": sid},
        ],
    })
    cp_path = memory_dir / "checkpoints" / "2026-07-13T1000-same-session-task.md"
    _write(memory_dir / f"pending-restore-{sid}.txt", str(cp_path) + "\n", NOW - DAY)

    verdict = _MOD.select_restore(memory_dir, sid, NOW)

    assert verdict["tier"] == 1, verdict
    assert verdict["same_session"] is True, verdict


def test_empty_and_unknown_sid_skips_tier1(tmp_path, monkeypatch):
    # authority:incident IVG-84 (empty/unknown sid)
    _default_knobs(monkeypatch)

    task = "skip-tier1-task"
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-skip-tier1-task.md", "mtime": NOW - DAY,
                       "task": task}],
        "checkpoints": [
            {"name": "2026-07-10T0900-skip-tier1-task.md", "mtime": NOW - 3 * DAY,
             "task": task, "sid": "unknown"},
        ],
    })
    cp_path = memory_dir / "checkpoints" / "2026-07-10T0900-skip-tier1-task.md"
    # A pending-restore sentinel for BOTH the empty-sid and literal-"unknown"
    # filenames, pointing at a same-task candidate -- if Tier-1 wrongly
    # consulted either, it would return tier=1 immediately. It must not.
    _write(memory_dir / "pending-restore-.txt", str(cp_path) + "\n", NOW - DAY)
    _write(memory_dir / "pending-restore-unknown.txt", str(cp_path) + "\n", NOW - DAY)

    for sid in ("", "unknown"):
        verdict = _MOD.select_restore(memory_dir, sid, NOW)
        assert verdict["tier"] != 1, (sid, verdict)


def test_same_sid_pending_restore_collision_last_writer_wins(tmp_path, monkeypatch):
    # authority:spec "Same-session-id collision" (D-S2-4, MIN-1) -- a
    # /checkpoint voluntary write and a /thorough_plan phase-boundary write
    # both target pending-restore-<sid>.txt; the fixture materializes the
    # POST-collision on-disk state directly (one sentinel, one surviving
    # writer) since the pure reader has no write-ordering to tiebreak.
    _default_knobs(monkeypatch)

    sid = "SID-COLLIDE"
    task = "collide-task"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-14-collide-task.md", "mtime": NOW, "task": task}],
        "checkpoints": [
            {"name": "2026-07-10T0900-collide-task.md", "mtime": NOW - 4 * DAY,
             "task": task, "sid": sid},
        ],
    })
    # (1) Simulate /checkpoint's own write: sentinel points at the voluntary checkpoint.
    voluntary_cp = memory_dir / "checkpoints" / "2026-07-10T0900-collide-task.md"
    sentinel = memory_dir / f"pending-restore-{sid}.txt"
    _write(sentinel, str(voluntary_cp) + "\n", NOW - DAY)

    # (2) Simulate /thorough_plan_checkpoint being the LAST writer to the
    # SAME sentinel path -- it overwrites pending-restore-{sid}.txt to point
    # at its own thorough-plan-progress-{sid}.md.
    rc = _TPC.main([
        "--project-root", str(tmp_path),
        "--task", task,
        "--round", "1",
        "--phase", "plan",
        "--sid", sid,
        "--branch", "main",
    ])
    assert rc == 0
    tp_ckpt = memory_dir / "checkpoints" / f"thorough-plan-progress-{sid}.md"
    assert tp_ckpt.exists()
    os.utime(tp_ckpt, (NOW, NOW))
    os.utime(sentinel, (NOW, NOW))

    # Post-collision snapshot: the sentinel now holds exactly one path --
    # the thorough-plan-progress file's (the last writer).
    assert sentinel.read_text(encoding="utf-8").strip() == str(tp_ckpt)

    verdict = _MOD.select_restore(memory_dir, sid, NOW)

    assert verdict["kind"] == "thorough-plan-progress", verdict
    assert verdict["selected_path"] == str(tp_ckpt), verdict
    assert verdict["reason"] == "route:thorough-plan-progress", verdict


def test_b3_prompt_uses_derived_task_not_anchor_zero_candidates(tmp_path, monkeypatch):
    # authority:prose "TASK = active_task" [SKILL.md:989] -- M-4 deterministic
    # B3 synthesis must use derived_task even when a DIFFERENT Tier-2 anchor
    # was seeded and candidate_count == 0 (Clause A), not just in the
    # zero-anchor case already covered by the T-01/T-02 scaffold test.
    _default_knobs(monkeypatch)

    t_anchor = "b3-anchor-task"
    t_fresh = "b3-fresh-task"
    pp_sid = "SID-PP-B3-ZERO"
    current_sid = "SID-CURRENT-B3-ZERO-MISS"

    memory_dir = _build_memory(tmp_path, {
        "sessions": [
            {"name": "2026-07-08-b3-anchor-task.md", "mtime": NOW - 2 * DAY,
             "task": t_anchor, "sid": pp_sid},
            {"name": "2026-07-13-b3-fresh-task.md", "mtime": NOW - DAY,
             "task": t_fresh},
        ],
        # NO checkpoints at all -> Clause A (zero candidates).
        "pending_prompt": [{"sid": pp_sid, "mtime": NOW - 0.1 * DAY}],
    })

    verdict = _MOD.select_restore(memory_dir, current_sid, NOW)

    assert verdict["anchor_task"] == t_anchor, verdict
    assert verdict["derived_task"] == t_fresh, verdict
    assert verdict["tier"] == "4-B3", verdict
    assert verdict["reason"] == "b3:clause-a", verdict
    expected = (
        f"Resume task '{t_fresh}': no checkpoint selected (tier 4 / B3). "
        f"Synthesize a minimal restore from the freshest session-state file for '{t_fresh}'."
    )
    assert verdict["b3_prompt"] == expected, verdict
    assert t_anchor not in verdict["b3_prompt"]


_FIXTURE_SESSION = (
    REPO_ROOT / "quoin" / "dev" / "tests" / "fixtures" / "checkpoint_picker"
    / "sessions" / "2026-05-17-personal-site-sim-embed.md"
)


def test_dated_fixture_session_as_b3_baseline(tmp_path, monkeypatch):
    # authority:spec dated on-disk fixture corpus (fixtures/checkpoint_picker)
    _default_knobs(monkeypatch)
    assert _FIXTURE_SESSION.is_file(), _FIXTURE_SESSION

    memory_dir = _build_memory(tmp_path, {})
    dest = memory_dir / "sessions" / _FIXTURE_SESSION.name
    dest.write_text(_FIXTURE_SESSION.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(dest, (NOW - DAY, NOW - DAY))
    # No checkpoints -> Clause A -> B3, using this fixture as the session baseline.

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == "4-B3", verdict
    assert verdict["derived_task"] == "personal-site-sim-embed", verdict
    assert verdict["b3_prompt"] is not None
    assert "personal-site-sim-embed" in verdict["b3_prompt"]


def test_corrupt_and_headless_checkpoints_dropped(tmp_path, monkeypatch):
    # authority:prose "[ $(wc -c < \"$cp\") -ge 100 ] || continue" [SKILL.md:823]
    #                 "drop the candidate silently" (parse failure) [SKILL.md:839]
    # Fidelity gap closed post-review (review-1 MINOR-1): the disk-only 30-day
    # enumeration must skip <100-byte corrupt entries and drop candidates whose
    # `## Active task` cannot be extracted. Both would otherwise be selectable.
    monkeypatch.setenv("QUOIN_RESTORE_STALE_DAYS", "1")
    task = "zephyr"
    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": f"2026-07-14-{task}.md", "mtime": NOW, "task": task}],
    })
    ck_dir = memory_dir / "checkpoints"
    # (a) <100-byte same-task checkpoint that WOULD tier-3 auto-pick if not skipped.
    tiny = _write(ck_dir / f"2026-07-14T1000-{task}.md", f"## Active task\n{task}\n", NOW)
    assert tiny.stat().st_size < 100  # fixture invariant: actually under the guard
    # (b) >=100-byte checkpoint with NO `## Active task` heading -> parse-failure drop.
    headless = _write(
        ck_dir / f"2026-07-14T1100-{task}.md",
        "## Status\nvoluntary\n" + ("padding line to exceed one hundred bytes\n" * 4),
        NOW,
    )
    assert headless.stat().st_size >= 100  # fixture invariant: passes the byte guard

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)
    # Both candidates dropped -> nothing selectable -> B3 fallback (NOT the corrupt files).
    assert verdict["selected_path"] is None, verdict
    assert verdict["tier"] == "4-B3", verdict
    assert verdict["reason"].startswith("b3:"), verdict

    # Positive control: a VALID >=100-byte same-task checkpoint IS auto-picked,
    # proving the byte/parse guards are what forced B3 above (not some other cause).
    mem2 = _build_memory(tmp_path / "ctrl", {
        "sessions": [{"name": f"2026-07-14-{task}.md", "mtime": NOW, "task": task}],
        "checkpoints": [{"name": f"2026-07-14T1000-{task}.md", "mtime": NOW, "task": task}],
    })
    v2 = _MOD.select_restore(mem2, "unknown", NOW)
    assert v2["selected_path"] is not None, v2
    assert v2["tier"] == 3, v2
    assert v2["reason"] == "tier3:autopick", v2


# ===========================================================================
# IVG-160 — distinct-task ambiguity gate (architecture §9 items 1-8, AC-1..AC-9)
# ===========================================================================

def test_ambiguity_two_distinct_tasks_recent_returns_picker(tmp_path, monkeypatch):
    # AC-2: two distinct tasks, both checkpoints within the recent window ->
    # tier=="ambiguous", reason=="ambiguous:multi-task-recent", selected_path
    # None, candidates has both, mtime-descending.
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "checkpoints": [
            {"name": "2026-07-14T0000-alpha.md", "mtime": NOW, "task": "alpha"},
            {"name": "2026-07-13T2300-beta.md", "mtime": NOW - 3600, "task": "beta"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == "ambiguous", verdict
    assert verdict["reason"] == "ambiguous:multi-task-recent", verdict
    assert verdict["selected_path"] is None, verdict
    cands = verdict["candidates"]
    assert len(cands) == 2, cands
    assert {c["task"] for c in cands} == {"alpha", "beta"}, cands
    assert cands[0]["mtime"] >= cands[1]["mtime"], cands
    assert cands[0]["task"] == "alpha", cands  # NOW is freshest


def test_ambiguity_candidate_schema(tmp_path, monkeypatch):
    # AC-2: each candidate entry carries task/path/mtime/saved_time/source/
    # sentinel_path/kind; saved_time is deterministic (gmtime of mtime); path is
    # a str (R-08).
    import time as _time
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "checkpoints": [
            {"name": "2026-07-14T0000-alpha.md", "mtime": NOW, "task": "alpha"},
            {"name": "2026-07-13T2300-beta.md", "mtime": NOW - 3600, "task": "beta"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)
    assert verdict["tier"] == "ambiguous", verdict

    for c in verdict["candidates"]:
        assert set(c.keys()) == {
            "task", "path", "mtime", "saved_time", "source", "sentinel_path", "kind"
        }, c
        assert isinstance(c["path"], str), c  # R-08: Path stringified
        assert c["source"] in ("sentinel", "disk-only"), c
        assert c["kind"] in ("checkpoint", "thorough-plan-progress"), c
        # deterministic UTC render of mtime (no wall-clock)
        expected_saved = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime(c["mtime"]))
        assert c["saved_time"] == expected_saved, c


def test_ambiguity_single_task_recent_autopicks(tmp_path, monkeypatch):
    # AC-1: one distinct recent task (even with the session present) -> no
    # ambiguity, tier==3 autopick, verdict["candidates"] == [].
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-solo.md", "mtime": NOW - DAY, "task": "solo"}],
        "checkpoints": [
            {"name": "2026-07-14T0000-solo.md", "mtime": NOW, "task": "solo"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict
    assert verdict["candidates"] == [], verdict


def test_ambiguity_same_task_multiplicity_collapses(tmp_path, monkeypatch):
    # AC-4: several recent checkpoints of the SAME task -> auto-pick most recent,
    # tier != "ambiguous" (same-task multiplicity is NOT ambiguity).
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "checkpoints": [
            {"name": "2026-07-14T0000-solo.md", "mtime": NOW, "task": "solo"},
            {"name": "2026-07-13T2300-solo.md", "mtime": NOW - 3600, "task": "solo"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] != "ambiguous", verdict
    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict
    assert verdict["candidates"] == [], verdict


def test_ambiguity_window_env_tunable_wide_vs_narrow(tmp_path, monkeypatch):
    # AC-5: same fixture (two distinct tasks 1h apart). Wide window -> both in
    # the recent set -> ambiguous; narrow window -> only the freshest is recent
    # -> collapse to one distinct task -> auto-pick.
    spec = {
        "checkpoints": [
            {"name": "2026-07-14T0000-alpha.md", "mtime": NOW, "task": "alpha"},
            {"name": "2026-07-13T2300-beta.md", "mtime": NOW - 3600, "task": "beta"},
        ],
    }

    # Wide (2 days) -> ambiguous.
    memory_wide = _build_memory(tmp_path / "wide", spec)
    _default_knobs(monkeypatch, ambiguity_window=172800)
    v_wide = _MOD.select_restore(memory_wide, "unknown", NOW)
    assert v_wide["tier"] == "ambiguous", v_wide

    # Narrow (60s) -> only the NOW candidate is recent -> not ambiguous.
    memory_narrow = _build_memory(tmp_path / "narrow", spec)
    _default_knobs(monkeypatch, ambiguity_window=60)
    v_narrow = _MOD.select_restore(memory_narrow, "unknown", NOW)
    assert v_narrow["tier"] != "ambiguous", v_narrow


def test_ambiguity_stale_cross_task_never_offered(tmp_path, monkeypatch):
    # AC-3/FR-7: a days-old cross-task checkpoint + a recent same-task checkpoint
    # -> not ambiguous; the days-old task is absent from any offered set; routes
    # exactly as the pre-fix stale/cross-task path (tier-3 autopick of the recent
    # same-task candidate).
    _default_knobs(monkeypatch)

    memory_dir = _build_memory(tmp_path, {
        "sessions": [{"name": "2026-07-13-current.md", "mtime": NOW - DAY, "task": "current"}],
        "checkpoints": [
            {"name": "2026-07-14T0000-current.md", "mtime": NOW, "task": "current"},
            # 5 days old, different task -> outside the 4h window, never recent.
            {"name": "2026-07-09T0000-old-other.md", "mtime": NOW - 5 * DAY, "task": "old-other"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] != "ambiguous", verdict
    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict
    assert verdict["candidates"] == [], verdict
    # The days-old cross-task checkpoint is never surfaced or selected.
    assert "old-other" not in (verdict["selected_path"] or ""), verdict


def test_ambiguity_window_zero_disables(tmp_path, monkeypatch):
    # Opt-out: QUOIN_RESTORE_AMBIGUITY_WINDOW=0 -> gate disabled -> legacy
    # behaviour (no ambiguity route even with two distinct recent tasks).
    _default_knobs(monkeypatch, ambiguity_window=0)

    memory_dir = _build_memory(tmp_path, {
        "checkpoints": [
            {"name": "2026-07-14T0000-alpha.md", "mtime": NOW, "task": "alpha"},
            {"name": "2026-07-13T2300-beta.md", "mtime": NOW - 3600, "task": "beta"},
        ],
    })

    verdict = _MOD.select_restore(memory_dir, "unknown", NOW)

    assert verdict["tier"] != "ambiguous", verdict
    assert verdict["tier"] == 3, verdict
    assert verdict["reason"] == "tier3:autopick", verdict


def test_empty_verdict_has_candidates_key(tmp_path, monkeypatch):
    # FR-9/AC-7: the `candidates` key is present as [] on every non-ambiguity
    # Verdict, AND the ambiguity path performs no writes / no env mutation.
    _default_knobs(monkeypatch)

    # (1) Non-ambiguity Verdict (empty memory -> B3) carries candidates == [].
    empty_mem = _build_memory(tmp_path / "empty", {})
    v_b3 = _MOD.select_restore(empty_mem, "unknown", NOW)
    assert "candidates" in v_b3, v_b3
    assert v_b3["candidates"] == [], v_b3

    # (2) Purity of the ambiguity path: no writes anywhere under tmp_path and no
    # os.environ mutation (mirrors test_module_performs_no_writes /
    # test_module_does_not_mutate_environ for the new route).
    monkeypatch.chdir(tmp_path)
    amb_mem = _build_memory(tmp_path / "amb", {
        "checkpoints": [
            {"name": "2026-07-14T0000-alpha.md", "mtime": NOW, "task": "alpha"},
            {"name": "2026-07-13T2300-beta.md", "mtime": NOW - 3600, "task": "beta"},
        ],
    })
    before_tree = _tree_snapshot(tmp_path)
    before_env = dict(os.environ)
    v_amb = _MOD.select_restore(amb_mem, "unknown", NOW)
    after_tree = _tree_snapshot(tmp_path)
    after_env = dict(os.environ)

    assert v_amb["tier"] == "ambiguous", v_amb  # sanity: really the ambiguity path
    assert before_tree == after_tree, "ambiguity path must not write/touch any file"
    assert before_env == after_env, "ambiguity path must not mutate os.environ"
