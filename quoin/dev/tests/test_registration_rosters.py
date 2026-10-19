"""IVG-118: registration roster consistency check (quoin/dev/check_registration.py).

Covers:
  - T-01: scaffold smoke tests (--help, --json empty envelope, exit codes)
  - T-02: Phase-1 filesystem-grounded rules (RG-CANON, RG-MANIFEST,
    RG-RUNTIME-BREAK, RG-STALE-CORE) + live-tree-clean (AC-1, AC-5, AC-7, AC-8)
  - T-03: AST roster parser completeness (parses every roster in ## References)
  - T-04: Phase-2 classification/deploy rules (RG-DEPLOY, RG-OVERRIDES,
    RG-TESTROSTER, RG-GENROSTER) + RG-CENSUS mechanical roster census
    (AC-2, AC-3, AC-4)
  - T-06: RG-MIGRATED literal-vs-derive drift guard (added after T-05's
    filesystem-derive reconciliation lands)

Import pattern mirrors test_inject_pollution_dispatch.py:27-34 — quoin/dev/ is
NOT a package, so check_registration is imported via sys.path insertion.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_DEV_DIR = _TESTS_DIR.parent  # quoin/dev/
_PKG_DIR = _DEV_DIR.parent    # quoin/quoin/ (the inner package root)
if str(_DEV_DIR) not in sys.path:
    sys.path.insert(0, str(_DEV_DIR))

import check_registration as cr  # noqa: E402


# ---------------------------------------------------------------------------
# T-01: scaffold smoke tests
# ---------------------------------------------------------------------------

def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cr.main(["--help"])
    assert exc_info.value.code == 0


def test_json_empty_findings_envelope(capsys):
    exit_code = cr.main(["--json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == '{\n  "findings": []\n}'


def test_bad_argument_exits_usage():
    with pytest.raises(SystemExit) as exc_info:
        cr.main(["--not-a-real-flag"])
    assert exc_info.value.code == 64


# ---------------------------------------------------------------------------
# T-02 / AC-8: live-tree-clean — the real repo produces 0 findings
# ---------------------------------------------------------------------------

def test_live_tree_clean(capsys):
    exit_code = cr.main([])
    captured = capsys.readouterr()
    assert exit_code == 0, f"expected clean tree, got findings:\n{captured.err}"
    assert captured.err == ""


# ---------------------------------------------------------------------------
# T-02: Phase-1 rule unit + detection tests
# ---------------------------------------------------------------------------

def test_rg_canon_detects_missing_from_canonical_skills():
    findings = []
    fs_names = {"architect", "plan", "ghost_skill"}
    canonical_set = {"architect", "plan"}
    cr.rg_canon(fs_names, canonical_set, findings)
    assert any(
        f["rule"] == cr.RG_CANON and f["entry"] == "ghost_skill" and f["roster"] == "CANONICAL_SKILLS"
        for f in findings
    )


def test_rg_canon_detects_missing_directory():
    findings = []
    fs_names = {"architect"}
    canonical_set = {"architect", "phantom_skill"}
    cr.rg_canon(fs_names, canonical_set, findings)
    assert any(
        f["rule"] == cr.RG_CANON and f["entry"] == "phantom_skill" and f["roster"] == "quoin/skills/"
        for f in findings
    )


def test_rg_canon_clean_on_matching_sets():
    findings = []
    cr.rg_canon({"a", "b"}, {"a", "b"}, findings)
    assert findings == []


def test_rg_manifest_detects_oracle_mismatch():
    findings = []
    cr.rg_manifest({"a", "b"}, {"a", "b", "c"}, {"a", "b"}, findings)
    entries = [f["entry"] for f in findings if f["rule"] == cr.RG_MANIFEST]
    assert "c" in entries


def test_rg_runtime_break_ac5(tmp_path):
    """AC-5: DEPLOYED wrapper with a core/scripts/ twin missing from CORE_SCRIPTS."""
    core_scripts_dir = tmp_path / "core" / "scripts"
    core_scripts_dir.mkdir(parents=True)
    (core_scripts_dir / "orphan_wrapper.py").write_text("# core impl\n")

    findings = []
    cr.rg_runtime_break(
        deployed_scripts=("orphan_wrapper.py",),
        core_scripts_set=set(),  # NOT registered — the exact runtime-loader-break case
        core_scripts_dir=core_scripts_dir,
        findings=findings,
    )
    assert any(
        f["rule"] == cr.RG_RUNTIME_BREAK and f["entry"] == "orphan_wrapper.py" and f["roster"] == "CORE_SCRIPTS"
        for f in findings
    )


def test_rg_runtime_break_ac6_no_false_positive_adapter_only(tmp_path):
    """AC-6: an adapter-only script (DEPLOYED, no core/scripts/ twin, not in
    CORE_SCRIPTS) must NOT be flagged."""
    core_scripts_dir = tmp_path / "core" / "scripts"
    core_scripts_dir.mkdir(parents=True)
    # No twin file created for adapter_only.py — the discriminator.

    findings = []
    cr.rg_runtime_break(
        deployed_scripts=("adapter_only.py",),
        core_scripts_set=set(),
        core_scripts_dir=core_scripts_dir,
        findings=findings,
    )
    assert findings == []


def test_rg_stale_core_ac7(tmp_path):
    core_scripts_dir = tmp_path / "core" / "scripts"
    core_scripts_dir.mkdir(parents=True)
    findings = []
    cr.rg_stale_core({"ghost.py"}, core_scripts_dir, findings)
    assert any(
        f["rule"] == cr.RG_STALE_CORE and f["entry"] == "ghost.py" and f["roster"] == "CORE_SCRIPTS"
        for f in findings
    )


def test_rg_stale_core_clean_when_file_present(tmp_path):
    core_scripts_dir = tmp_path / "core" / "scripts"
    core_scripts_dir.mkdir(parents=True)
    (core_scripts_dir / "present.py").write_text("# ok\n")
    findings = []
    cr.rg_stale_core({"present.py"}, core_scripts_dir, findings)
    assert findings == []


# ---------------------------------------------------------------------------
# T-03: AST roster parser completeness
# ---------------------------------------------------------------------------

# Every roster named in the plan's ## References section, as (file, name)
# pairs. Generators resolve against quoin/scripts/, everything else against
# quoin/dev/tests/.
_GENERATOR_FILES = {"build_preambles.py", "inject_pollution_dispatch.py"}

_ALL_NAMED_ROSTERS = [
    ("test_1m_context_precheck.py", "MIGRATED_TO_ADAPTER"),
    ("test_1m_context_precheck.py", "SECTION0PRIME_TARGETS"),
    ("test_1m_context_precheck.py", "SECTION0_TARGETS"),
    ("test_1m_proactive_precheck.py", "MIGRATED_TO_ADAPTER"),
    ("test_1m_proactive_precheck.py", "SECTION0_TARGETS"),
    ("test_quoin_stage1_preamble.py", "MIGRATED_TO_ADAPTER"),
    ("test_quoin_stage1_preamble.py", "CHEAP_TIER_SKILLS"),
    ("test_quoin_stage1_preamble.py", "OPUS_TIER_SKILLS"),
    ("test_quoin_stage1_recursion_abort.py", "MIGRATED_TO_ADAPTER"),
    ("test_quoin_stage1_recursion_abort.py", "CHEAP_TIER_SKILLS"),
    ("test_quoin_stage1_worktree_fallback.py", "MIGRATED_TO_ADAPTER"),
    ("test_quoin_stage1_worktree_fallback.py", "WORKTREE_FALLBACK_SKILLS"),
    ("test_quoin_stage1_worktree_fallback.py", "SOURCE_MUTATING_WORKTREE_SKILLS"),
    ("test_quoin_pollution_preamble.py", "CHEAP_TIER_SKILLS"),
    ("test_quoin_pollution_preamble.py", "SKILL_DISTINCTIVE_TOKENS"),
    ("test_quoin_pollution_preamble.py", "POLLUTION_TARGET_SKILLS"),
    ("test_inject_pollution_dispatch.py", "CHEAP_TIER_SKILLS"),
    ("test_inject_pollution_dispatch.py", "SKILL_DISTINCTIVE_TOKENS"),
    ("test_mintier_guard.py", "MINTIER_SKILLS"),
    ("test_sonnet_mintier_guard.py", "SONNET_MINTIER_SKILLS"),
    ("test_pollution_score_extraction.py", "TARGET_SKILLS"),
    ("test_pitfall_preamble_in_class_b.py", "TARGET_SKILLS"),
    ("test_pitfall_preamble_in_class_b.py", "MIGRATED_SKILLS_DIR_OVERRIDES"),
    ("test_fallback_increment_in_skills.py", "MIGRATED_SKILLS_DIR_OVERRIDES"),
    ("test_preamble_bootstrap_step.py", "MIGRATED_SKILLS_DIR_OVERRIDES"),
    ("test_tmp_cleanup_contract.py", "MIGRATED_SKILLS_DIR_OVERRIDES"),
    ("build_preambles.py", "SPAWN_TARGETS"),
    ("inject_pollution_dispatch.py", "POLLUTION_TARGET_SKILLS"),
    ("inject_pollution_dispatch.py", "MINTIER_TARGET_SKILLS"),
    ("inject_pollution_dispatch.py", "MINTIER_SONNET_TARGET_SKILLS"),
]


@pytest.mark.parametrize("file_name,roster_name", _ALL_NAMED_ROSTERS)
def test_parser_completeness(file_name, roster_name):
    """Every roster on `## References` parses to a non-empty set, or DERIVED."""
    if file_name in _GENERATOR_FILES:
        path = _PKG_DIR / "scripts" / file_name
    else:
        path = _TESTS_DIR / file_name
    node = cr.parse_roster(path, roster_name)
    assert node is not None, f"{roster_name} not found as a module-level binding in {file_name}"
    result = cr.eval_collection(node)
    if result == cr.DERIVED:
        return
    assert isinstance(result, set) and len(result) > 0, (
        f"{roster_name} in {file_name} evaluated to an empty/non-set result: {result!r}"
    )


def test_eval_collection_unrecognized_shape_raises():
    node = ast.parse("X = some_function_call()").body[0].value
    with pytest.raises(ValueError):
        cr.eval_collection(node)


def test_eval_collection_derived_sentinel():
    src = (
        'MIGRATED_TO_ADAPTER = frozenset(\n'
        '    p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()\n'
        ')'
    )
    node = ast.parse(src).body[0].value
    assert cr.eval_collection(node) == cr.DERIVED


# ---------------------------------------------------------------------------
# T-04: Phase-2 rule unit + detection tests
# ---------------------------------------------------------------------------

def test_rg_deploy_ac4(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "new_unregistered.py").write_text("# wrapper\n")
    findings = []
    cr.rg_deploy(scripts_dir, deployed_scripts_set=set(), findings=findings)
    assert any(
        f["rule"] == cr.RG_DEPLOY and f["entry"] == "new_unregistered.py" and f["roster"] == "DEPLOYED_SCRIPTS"
        for f in findings
    )


def test_rg_deploy_allow_listed_no_false_positive(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    for name in cr.NON_DEPLOYED_WRAPPERS:
        (scripts_dir / name).write_text("# allow-listed\n")
    (scripts_dir / "__init__.py").write_text("")
    findings = []
    cr.rg_deploy(scripts_dir, deployed_scripts_set=set(), findings=findings)
    assert findings == []


def test_rg_overrides_ac2_skill_missing_and_not_allowlisted():
    findings = []
    canonical_set = {"architect", "plan", "brand_new_skill"}
    overrides = {"plan": "on"}  # brand_new_skill missing; architect is on the allow-list
    cr.rg_overrides(canonical_set, overrides, findings)
    assert any(
        f["rule"] == cr.RG_OVERRIDES and f["entry"] == "brand_new_skill" and f["roster"] == "SKILL_OVERRIDES"
        for f in findings
    )
    # architect is on SKILL_OVERRIDES_OPTIONAL — must not be flagged
    assert not any(f["entry"] == "architect" for f in findings)


def test_rg_overrides_flags_key_not_in_canonical_or_allowlisted():
    findings = []
    cr.rg_overrides({"plan"}, {"plan": "on", "totally_unknown_skill": "on"}, findings)
    assert any(
        f["rule"] == cr.RG_OVERRIDES and f["entry"] == "totally_unknown_skill" and f["roster"] == "CANONICAL_SKILLS"
        for f in findings
    )


def test_rg_overrides_non_quoin_keys_allowed():
    findings = []
    cr.rg_overrides({"plan"}, {"plan": "on", "init": "name-only", "keybindings-help": "name-only"}, findings)
    assert findings == []


def test_rg_testroster_ac3_detects_missing_entry(tmp_path):
    """AC-3: a skill removed from SECTION0PRIME_TARGETS is caught."""
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    src = (
        "SECTION0PRIME_TARGETS = [\n"
        '    "architect", "plan", "critic", "revise",\n'
        # "review" deliberately omitted
        '    "init_workflow", "discover", "specify", "security_review", "enrich",\n'
        "]\n"
    )
    (dev_tests_dir / "test_1m_context_precheck.py").write_text(src)

    manifest_skills = [
        {"name": n, "section_0": False}
        for n in ["architect", "plan", "critic", "revise", "review", "init_workflow", "discover", "specify", "security_review", "enrich"]
    ] + [{"name": "run", "section_0": False}, {"name": "thorough_plan", "section_0": False}]

    findings = []
    cr.rg_testroster(dev_tests_dir, manifest_skills, findings)
    assert any(
        f["rule"] == cr.RG_TESTROSTER and f["entry"] == "review" and "SECTION0PRIME_TARGETS" in f["roster"]
        for f in findings
    )


def test_rg_genroster_ac3_detects_missing_entry(tmp_path):
    """AC-3: a skill removed from SPAWN_TARGETS is caught."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    src = (
        "SPAWN_TARGETS = {\n"
        '    "critic": "full",\n'
        '    "revise": "full",\n'
        "    # 'plan' deliberately omitted\n"
        "}\n"
    )
    (scripts_dir / "build_preambles.py").write_text(src)
    dev_tests_dir = scripts_dir  # unused by this specific check but must exist

    manifest_skills = [
        {"name": "critic", "spawn_target": True},
        {"name": "revise", "spawn_target": True},
        {"name": "plan", "spawn_target": True},
    ]

    findings = []
    cr.rg_genroster(dev_tests_dir, scripts_dir, manifest_skills, findings)
    assert any(
        f["rule"] == cr.RG_GENROSTER and f["entry"] == "plan" and "SPAWN_TARGETS" in f["roster"]
        for f in findings
    )


# ---------------------------------------------------------------------------
# RG-CENSUS: mechanical roster-population census
# ---------------------------------------------------------------------------

def test_roster_census_green_on_live_tree():
    dev_tests_dir = _PKG_DIR / "dev" / "tests"
    scripts_dir = _PKG_DIR / "scripts"
    canonical_names = set(cr.load_installer_rosters(cr._default_installer(_PKG_DIR))[0])
    findings = []
    cr.rg_census(dev_tests_dir, scripts_dir, canonical_names, findings)
    assert findings == [], f"unclassified rosters discovered:\n{findings}"


def test_roster_census_ac_new_roster_fails_loud(tmp_path):
    """A synthetic new all-caps skill-name-list roster, iterated via
    pytest.mark.parametrize, absent from COVERED_ROSTERS and
    KNOWN_DEFERRED_ROSTERS, must fail the census naming the file + roster —
    proving the convergence point is real and not decorative."""
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    src = (
        "import pytest\n\n"
        'BRAND_NEW_UNCLASSIFIED_ROSTER = ["architect", "plan", "critic"]\n\n'
        '@pytest.mark.parametrize("skill", BRAND_NEW_UNCLASSIFIED_ROSTER)\n'
        "def test_something(skill):\n"
        "    assert skill\n"
    )
    (dev_tests_dir / "test_synthetic_new_roster.py").write_text(src)

    canonical_names = {"architect", "plan", "critic", "revise", "review"}
    findings = []
    cr.rg_census(dev_tests_dir, scripts_dir, canonical_names, findings)
    assert any(
        f["rule"] == cr.RG_CENSUS and f["entry"] == "BRAND_NEW_UNCLASSIFIED_ROSTER"
        for f in findings
    )


def test_roster_census_covered_roster_no_false_positive(tmp_path):
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    # Same shape as the synthetic test above, but pre-registered in
    # COVERED_ROSTERS via a monkeypatch-free direct name match is awkward
    # (COVERED_ROSTERS is module-level); instead prove the negative case by
    # using a roster that legitimately fails criterion (a): non-skill members.
    src = (
        "import pytest\n\n"
        'INCIDENTAL_FIXTURE = ["not_a_skill_at_all", "also_not_a_skill"]\n\n'
        '@pytest.mark.parametrize("x", INCIDENTAL_FIXTURE)\n'
        "def test_something(x):\n"
        "    assert x\n"
    )
    (dev_tests_dir / "test_incidental.py").write_text(src)

    canonical_names = {"architect", "plan"}
    findings = []
    cr.rg_census(dev_tests_dir, scripts_dir, canonical_names, findings)
    assert findings == []


def test_name_used_as_roster_detects_for_loop():
    src = (
        'ROSTER = ["a", "b"]\n'
        "for skill in ROSTER:\n"
        "    pass\n"
    )
    tree = ast.parse(src)
    assert cr._name_used_as_roster(tree, "ROSTER") is True


def test_name_used_as_roster_detects_membership():
    src = (
        'ROSTER = {"a", "b"}\n'
        "def f(x):\n"
        "    return x in ROSTER\n"
    )
    tree = ast.parse(src)
    assert cr._name_used_as_roster(tree, "ROSTER") is True


def test_name_used_as_roster_false_when_unused():
    src = 'ROSTER = ["a", "b"]\n'
    tree = ast.parse(src)
    assert cr._name_used_as_roster(tree, "ROSTER") is False


# ---------------------------------------------------------------------------
# T-06: RG-MIGRATED literal-vs-derive drift guard
# ---------------------------------------------------------------------------

def test_rg_migrated_clean_on_live_tree():
    """After T-05, all 5 known copies are DERIVED-shaped -> 0 findings on main."""
    dev_tests_dir = _PKG_DIR / "dev" / "tests"
    adapter_skills_dir = _PKG_DIR / "adapters" / "claude" / "skills"
    findings = []
    cr.rg_migrated(dev_tests_dir, adapter_skills_dir, findings)
    assert findings == []


def test_rg_migrated_wired_into_main():
    """T-06 acceptance: main()'s exit code reflects rg_migrated findings."""
    assert cr._RG_MIGRATED_WIRED is True


def test_rg_migrated_detects_drifted_literal(tmp_path):
    """A hardcoded literal missing one skill -> RG-MIGRATED fires naming file + skill."""
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    adapter_skills_dir = tmp_path / "adapter_skills"
    for name in ("architect", "plan", "critic"):
        d = adapter_skills_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n")

    src = 'MIGRATED_TO_ADAPTER = {"architect", "plan"}\n'  # "critic" missing
    (dev_tests_dir / "test_drifted_copy.py").write_text(src)

    findings = []
    cr.rg_migrated(dev_tests_dir, adapter_skills_dir, findings)
    assert any(
        f["rule"] == cr.RG_MIGRATED and f["entry"] == "critic" and "test_drifted_copy.py" in f["roster"]
        for f in findings
    )


def test_rg_migrated_derive_form_no_finding(tmp_path):
    """Positive case: an all-derive-form copy -> no finding."""
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    adapter_skills_dir = tmp_path / "adapter_skills"
    for name in ("architect", "plan"):
        d = adapter_skills_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n")

    src = (
        "ADAPTER_SKILLS_DIR = None  # placeholder, unused by parse_roster/eval_collection\n"
        'MIGRATED_TO_ADAPTER = frozenset(\n'
        '    p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()\n'
        ')\n'
    )
    (dev_tests_dir / "test_derive_copy.py").write_text(src)

    findings = []
    cr.rg_migrated(dev_tests_dir, adapter_skills_dir, findings)
    assert findings == []


def test_rg_migrated_discovers_new_copy_via_glob(tmp_path):
    """MIN-2: a 6th copy in a brand-new file is discovered by the glob scan
    without any code change to check_registration.py."""
    dev_tests_dir = tmp_path / "dev_tests"
    dev_tests_dir.mkdir()
    adapter_skills_dir = tmp_path / "adapter_skills"
    for name in ("architect", "plan", "review"):
        d = adapter_skills_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n")

    src = 'MIGRATED_TO_ADAPTER = {"architect"}\n'  # missing plan + review
    (dev_tests_dir / "test_brand_new_sixth_copy.py").write_text(src)

    findings = []
    cr.rg_migrated(dev_tests_dir, adapter_skills_dir, findings)
    entries = {f["entry"] for f in findings if "test_brand_new_sixth_copy.py" in f["roster"]}
    assert entries == {"plan", "review"}
