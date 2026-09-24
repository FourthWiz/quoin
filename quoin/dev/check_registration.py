#!/usr/bin/env python3
"""Registration roster consistency check — IVG-118.

Adding a new quoin skill or a new wrapped script requires updating several
independent, hand-maintained lists (installer.py rosters, per-file test
rosters, generator rosters). Because none of these lists is derived from a
single source of truth, forgetting to update one produces a silent failure
(see spec.md Context section for the three known failure modes). This script
cross-validates those lists against each other and against the filesystem,
and reports every inconsistency loudly, naming the offending roster + entry.

Lives in quoin/dev/ (not the core/scripts + scripts wrapper convention, see
plan D-01): the check operates on the SOURCE TREE, is never invoked from a
deployed install, and reads Claude-adapter-only rosters (portable core must
not depend on adapter registration data, FR-7). quoin/dev/ is scanned by no
installer roster and no wrapper-parity rule, so this check registers in ZERO
rosters and cannot false-positive on itself.

Boundary with validate_adapter_drift.py: no overlap. drift-check owns
SKILL.md structure / file-existence / wrapper file-parity / manifest shape;
this check owns installer-tuple membership + Claude test/generator roster
consistency.

FR-6 note (T-08): a wrapper sharing a module name with its core/scripts/<name>
twin must register the module in sys.modules BEFORE exec_module runs, or a
second same-process import of the same module name silently fails/aliases —
the third known silent-failure mode alongside missing DEPLOYED_SCRIPTS/
CORE_SCRIPTS registration. This is documented (not statically checked here,
per plan FR-6 SHOULD / A-6) in quoin/docs/runtime-portability.md, section
"Wrapper template (IVG-118 FR-6)".

Usage:
    python3 check_registration.py [--repo-root PATH] [--manifest PATH] [--json]

Exit codes:
    0   clean — no findings
    2   findings present
    64  usage — bad CLI arguments
    65  data — manifest/installer unreadable or invalid
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Finding codes (stable contract — tests assert by code, not by prose)
# ---------------------------------------------------------------------------
RG_CANON = "RG-CANON"                  # AC-1: skill dir <-> CANONICAL_SKILLS
RG_MANIFEST = "RG-MANIFEST"            # oracle 3-way integrity + count
RG_RUNTIME_BREAK = "RG-RUNTIME-BREAK"  # AC-5: DEPLOYED wrapper w/ core twin missing from CORE_SCRIPTS
RG_STALE_CORE = "RG-STALE-CORE"        # AC-7: CORE_SCRIPTS entry w/ no file on disk
RG_DEPLOY = "RG-DEPLOY"                # AC-4: wrapper file missing from DEPLOYED_SCRIPTS
RG_OVERRIDES = "RG-OVERRIDES"          # AC-2: SKILL_OVERRIDES <-> CANONICAL_SKILLS
RG_TESTROSTER = "RG-TESTROSTER"        # AC-3: cross-file-agreeing test rosters vs skills.json class
RG_GENROSTER = "RG-GENROSTER"          # AC-3: generator rosters vs skills.json class
RG_CENSUS = "RG-CENSUS"                # mechanical roster-population census
RG_MIGRATED = "RG-MIGRATED"            # T-06: MIGRATED_TO_ADAPTER literal-vs-derive drift guard

# T-06 gate (R-05): flipped to True only after T-05's reconciliation lands and
# the full suite is verified green. T-05 landed (all 5 MIGRATED_TO_ADAPTER
# copies now use the filesystem-derive comprehension form) and the affected
# test files plus the new roster-registration tests are green — see plan
# T-05 acceptance / R-05 mitigation.
_RG_MIGRATED_WIRED = True


def _default_repo_pkg() -> Path:
    """quoin/dev/check_registration.py -> parents[1] = the inner `quoin/` package root."""
    return Path(__file__).resolve().parents[1]


def _default_manifest(repo_pkg: Path) -> Path:
    return repo_pkg / "core" / "workflow" / "skills.json"


def _default_installer(repo_pkg: Path) -> Path:
    """repo_pkg is the inner `quoin/` package (parents[1]); installer.py lives
    in the src-layout distribution package at <git-root>/src/quoin/installer.py."""
    return repo_pkg.parent / "src" / "quoin" / "installer.py"


# ---------------------------------------------------------------------------
# Manifest / installer loading
# ---------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"DATA: cannot read manifest {path}: {exc}", file=sys.stderr)
        sys.exit(65)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"DATA: manifest is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(65)
    if "skills" not in data or not isinstance(data["skills"], list):
        print("DATA: manifest missing 'skills' list", file=sys.stderr)
        sys.exit(65)
    return data


def load_installer_rosters(installer_path: Path):
    """Read the four installer rosters from src/quoin/installer.py by AST,
    WITHOUT importing the quoin package.

    CRIT-1 (IVG-118 review-1): the former `from quoin.installer import ...`
    exited 65 in a bare CI checkout (no `pip install` -> ModuleNotFoundError),
    which silently disabled the only CI surface for this check. Reading the
    source as text — the same technique the sibling validate_adapter_drift.py
    uses — makes the check runnable from a plain `python3 check_registration.py`
    with stdlib alone. The AST-derived member sets are membership-identical to
    the former import: CANONICAL_SKILLS / DEPLOYED_SCRIPTS / CORE_SCRIPTS are
    string tuples (-> set of strings) and SKILL_OVERRIDES is a dict (-> set of
    its keys, which is all the checker's rules consume). A missing or
    unparseable installer roster is a loud DATA error (exit 65), never a silent
    skip.

    Returns (canonical_skills, deployed_scripts, core_scripts, skill_overrides)
    as sets of strings.
    """
    if not installer_path.is_file():
        print(f"DATA: installer.py not found at {installer_path}", file=sys.stderr)
        sys.exit(65)
    rosters: dict[str, set] = {}
    for name in ("CANONICAL_SKILLS", "DEPLOYED_SCRIPTS", "CORE_SCRIPTS", "SKILL_OVERRIDES"):
        node = parse_roster(installer_path, name)
        if node is None:
            print(
                f"DATA: installer roster {name} not found in {installer_path}",
                file=sys.stderr,
            )
            sys.exit(65)
        try:
            members = eval_collection(node)
        except ValueError as exc:
            print(
                f"DATA: could not parse installer roster {name} in {installer_path}: {exc}",
                file=sys.stderr,
            )
            sys.exit(65)
        if members is DERIVED:
            print(
                f"DATA: installer roster {name} has an unexpected derived shape",
                file=sys.stderr,
            )
            sys.exit(65)
        rosters[name] = members
    return (
        rosters["CANONICAL_SKILLS"],
        rosters["DEPLOYED_SCRIPTS"],
        rosters["CORE_SCRIPTS"],
        rosters["SKILL_OVERRIDES"],
    )


# ---------------------------------------------------------------------------
# T-03: AST roster parser
#
# parse_roster() finds the value node of a module-level Assign/AnnAssign
# binding. eval_collection() turns that node into a set[str] of members, or
# returns the DERIVED sentinel for the canonical MIGRATED_TO_ADAPTER
# filesystem-derive comprehension shape (T-05's reconciled form). Reused
# as-is by RG-CENSUS's scan (T-04) — one parser, no duplication.
# ---------------------------------------------------------------------------

DERIVED = "DERIVED"


def parse_roster(file_path: Path, name: str) -> Optional[ast.AST]:
    """Return the value AST node of the first module-level Assign/AnnAssign
    binding `name` in file_path. None if no such top-level binding exists."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tree = ast.parse(text, filename=str(file_path))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
                return node.value
    return None


def _is_migrated_derive_genexp(call_node: ast.Call) -> bool:
    """True iff call_node is the canonical
    frozenset(p.name for p in ADAPTER_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
    shape (T-05's reconciled MIGRATED_TO_ADAPTER form)."""
    if not (isinstance(call_node.func, ast.Name) and call_node.func.id == "frozenset"):
        return False
    if len(call_node.args) != 1 or not isinstance(call_node.args[0], ast.GeneratorExp):
        return False
    genexp = call_node.args[0]
    dumped = ast.dump(genexp)
    return "ADAPTER_SKILLS_DIR" in dumped and "SKILL.md" in dumped and "is_file" in dumped


def eval_collection(node: ast.AST):
    """Evaluate a roster value AST node to a set[str] of members, or return
    the DERIVED sentinel for the T-05 filesystem-derive comprehension shape.

    Raises ValueError for any node shape that is neither a literal
    set/list/tuple/dict container of strings (optionally wrapped in a
    frozenset()/set()/tuple()/list() call, or a list-of-tuples keyed by a
    leading string) nor the DERIVED comprehension. Any evaluation failure
    (unhashable/non-literal/non-string elements, unsupported node shapes) is
    normalized to ValueError so callers have one exception type to catch —
    it simply means "this binding is not a skill-name roster".
    """
    try:
        return _eval_collection_inner(node)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        raise ValueError(f"could not evaluate roster node: {exc}") from exc


def _eval_collection_inner(node: ast.AST):
    if node is None:
        raise ValueError("no value node to evaluate")

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
        "frozenset", "set", "tuple", "list",
    }:
        if len(node.args) == 1 and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            if _is_migrated_derive_genexp(node):
                return DERIVED
            raise ValueError(f"unrecognized comprehension-wrapped call: {ast.dump(node)[:200]}")
        if not node.args:
            return set()
        return _eval_collection_inner(node.args[0])

    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        members: set = set()
        for elt in node.elts:
            try:
                value = ast.literal_eval(elt)
            except (ValueError, SyntaxError):
                raise ValueError(f"element not a literal: {ast.dump(elt)[:120]}")
            if isinstance(value, tuple) and value and isinstance(value[0], str):
                # list-of-tuples roster (e.g. SECTION0_TARGETS): first element is the skill name
                members.add(value[0])
            elif isinstance(value, str):
                members.add(value)
            else:
                # non-string member (e.g. a list-of-dicts roster) — not a skill-name
                # roster at all; bail so the caller treats this binding as unrecognized.
                raise ValueError(f"non-string element: {value!r}")
        return members

    if isinstance(node, ast.Dict):
        members = set()
        for key in node.keys:
            if key is None:  # dict unpacking (**x) — no literal key to add
                continue
            try:
                key_value = ast.literal_eval(key)
            except (ValueError, SyntaxError):
                raise ValueError(f"dict key not a literal: {ast.dump(key)[:120]}")
            if not isinstance(key_value, str):
                raise ValueError(f"non-string dict key: {key_value!r}")
            members.add(key_value)
        return members

    raise ValueError(f"unrecognized roster value node shape: {type(node).__name__}")


# ---------------------------------------------------------------------------
# Phase 1 (T-02): filesystem-grounded rules (AC-1, AC-5, AC-7)
# ---------------------------------------------------------------------------

def rg_canon(fs_names: set, canonical_set: set, findings: list) -> None:
    """AC-1: bidirectional skill-dir <-> CANONICAL_SKILLS set-equality."""
    for name in sorted(fs_names - canonical_set):
        findings.append({
            "rule": RG_CANON,
            "roster": "CANONICAL_SKILLS",
            "entry": name,
            "detail": f"quoin/skills/{name}/ exists on disk but is not registered in CANONICAL_SKILLS",
        })
    for name in sorted(canonical_set - fs_names):
        findings.append({
            "rule": RG_CANON,
            "roster": "quoin/skills/",
            "entry": name,
            "detail": f"CANONICAL_SKILLS lists {name!r} but quoin/skills/{name}/ directory is missing",
        })


def rg_manifest(fs_names: set, canonical_set: set, manifest_names: set, findings: list) -> None:
    """Oracle integrity: skills.json names == CANONICAL_SKILLS == filesystem dirs == 31.

    Subsumes the runtime `== len(CANONICAL_SKILLS)` / `== 31` count-asserts scattered
    across test_install_fresh_clone.py, test_quoin_cli.py, test_runtime_portability_docs.py,
    test_codex_installable_feature.py — guards the oracle so later skills.json-derived
    rules cannot silently drift.
    """
    all_names = fs_names | canonical_set | manifest_names
    for name in sorted(all_names):
        missing_from = []
        if name not in fs_names:
            missing_from.append("filesystem (quoin/skills/)")
        if name not in canonical_set:
            missing_from.append("CANONICAL_SKILLS")
        if name not in manifest_names:
            missing_from.append("skills.json")
        if missing_from:
            findings.append({
                "rule": RG_MANIFEST,
                "roster": ", ".join(missing_from),
                "entry": name,
                "detail": f"{name!r} missing from: {', '.join(missing_from)}",
            })
    counts = {
        "filesystem": len(fs_names),
        "CANONICAL_SKILLS": len(canonical_set),
        "skills.json": len(manifest_names),
    }
    if len(set(counts.values())) > 1:
        findings.append({
            "rule": RG_MANIFEST,
            "roster": "counts",
            "entry": "n/a",
            "detail": f"oracle count mismatch across the three sources: {counts}",
        })


def rg_runtime_break(deployed_scripts, core_scripts_set: set, core_scripts_dir: Path, findings: list) -> None:
    """AC-5: a wrapper in DEPLOYED_SCRIPTS with a core/scripts/<name> twin on disk
    MUST be in CORE_SCRIPTS, or the deployed wrapper's parents[1]/core/scripts/<name>
    dynamic loader fails at runtime. Keyed on DEPLOYED so it never fires on a
    non-deployed core/scripts/ twin (dev/CI-only category, RG-CORE side)."""
    for name in deployed_scripts:
        if not name.endswith(".py"):
            continue
        twin = core_scripts_dir / name
        if twin.is_file() and name not in core_scripts_set:
            findings.append({
                "rule": RG_RUNTIME_BREAK,
                "roster": "CORE_SCRIPTS",
                "entry": name,
                "detail": (
                    f"wrapper {name} is in DEPLOYED_SCRIPTS and has a core/scripts/{name} twin "
                    f"but is missing from CORE_SCRIPTS — the deployed wrapper's "
                    f"parents[1]/core/scripts/{name} dynamic loader will fail at runtime"
                ),
            })


def rg_stale_core(core_scripts_set: set, core_scripts_dir: Path, findings: list) -> None:
    """AC-7: every CORE_SCRIPTS entry must have a core/scripts/<name> file on disk."""
    for name in sorted(core_scripts_set):
        if not (core_scripts_dir / name).is_file():
            findings.append({
                "rule": RG_STALE_CORE,
                "roster": "CORE_SCRIPTS",
                "entry": name,
                "detail": f"CORE_SCRIPTS lists {name!r} but quoin/core/scripts/{name} does not exist on disk",
            })


# ---------------------------------------------------------------------------
# Phase 2 (T-04): classification + deploy rules (AC-2, AC-3, AC-4)
# ---------------------------------------------------------------------------

# Q-03: adapter-only wrappers with no core/scripts/ twin, seeded at authorship
# time as a flat annotated allow-list (verified members, D-05).
NON_DEPLOYED_WRAPPERS: frozenset = frozenset({
    "validate_adapter_drift.py",   # dev/CI-only
    "validate_discovery_map.py",   # dev/CI-only
    "cost_event.py",               # core-only-deploy (required by dashboard_model.py)
    "dashboard_model.py",          # core-only-deploy
})


def rg_deploy(scripts_dir: Path, deployed_scripts_set: set, findings: list) -> None:
    """AC-4: every wrapper file in quoin/scripts/ is in DEPLOYED_SCRIPTS unless
    it is on the NON_DEPLOYED_WRAPPERS allow-list. __init__.py is excluded."""
    if not scripts_dir.is_dir():
        return
    for f in sorted(scripts_dir.glob("*.py")):
        name = f.name
        if name == "__init__.py" or name in NON_DEPLOYED_WRAPPERS:
            continue
        if name not in deployed_scripts_set:
            findings.append({
                "rule": RG_DEPLOY,
                "roster": "DEPLOYED_SCRIPTS",
                "entry": name,
                "detail": f"quoin/scripts/{name} exists but is not registered in DEPLOYED_SCRIPTS",
            })
    for f in sorted(scripts_dir.glob("*.sh")):
        name = f.name
        if name in NON_DEPLOYED_WRAPPERS:
            continue
        if name not in deployed_scripts_set:
            findings.append({
                "rule": RG_DEPLOY,
                "roster": "DEPLOYED_SCRIPTS",
                "entry": name,
                "detail": f"quoin/scripts/{name} exists but is not registered in DEPLOYED_SCRIPTS",
            })


# Q-01: non-quoin SKILL_OVERRIDES keys (settings.json accepts entries for
# skills quoin does not own; skillOverrides applies by name regardless).
NON_QUOIN_OVERRIDE_KEYS: frozenset = frozenset({"init", "keybindings-help"})

# Q-01: seeded allow-list of CANONICAL_SKILLS entries intentionally omitted
# from SKILL_OVERRIDES (computed at authorship time as the exact diff —
# CANONICAL_SKILLS minus the quoin keys of SKILL_OVERRIDES on `main`).
SKILL_OVERRIDES_OPTIONAL: frozenset = frozenset({
    "architect", "capture_insight", "discover", "end_of_task", "revise", "revise-fast",
})


def rg_overrides(canonical_set: set, skill_overrides: dict, findings: list) -> None:
    """AC-2: (a) every SKILL_OVERRIDES key is a quoin skill in CANONICAL_SKILLS,
    unless it is a documented non-quoin key. (b) every CANONICAL_SKILLS entry is
    in SKILL_OVERRIDES or on the SKILL_OVERRIDES_OPTIONAL allow-list."""
    for key in skill_overrides:
        if key in NON_QUOIN_OVERRIDE_KEYS:
            continue
        if key not in canonical_set:
            findings.append({
                "rule": RG_OVERRIDES,
                "roster": "CANONICAL_SKILLS",
                "entry": key,
                "detail": f"SKILL_OVERRIDES has key {key!r} which is neither in CANONICAL_SKILLS nor NON_QUOIN_OVERRIDE_KEYS",
            })
    for name in sorted(canonical_set):
        if name not in skill_overrides and name not in SKILL_OVERRIDES_OPTIONAL:
            findings.append({
                "rule": RG_OVERRIDES,
                "roster": "SKILL_OVERRIDES",
                "entry": name,
                "detail": (
                    f"{name!r} is in CANONICAL_SKILLS but missing from SKILL_OVERRIDES "
                    f"and not on the documented SKILL_OVERRIDES_OPTIONAL allow-list"
                ),
            })


# ---------------------------------------------------------------------------
# RG-TESTROSTER / RG-GENROSTER: cross-file-agreeing rosters vs skills.json class
# ---------------------------------------------------------------------------

def _class_from_manifest(manifest_skills: list, predicate) -> set:
    return {rec["name"] for rec in manifest_skills if predicate(rec)}


def _opus_leaf_class(manifest_skills: list) -> set:
    return _class_from_manifest(
        manifest_skills,
        lambda r: r.get("section_0") is False and r["name"] not in {"run", "thorough_plan"},
    )


def _all_section0_class(manifest_skills: list) -> set:
    return _class_from_manifest(manifest_skills, lambda r: r.get("section_0") is True)


def _sonnet_cheap_class(manifest_skills: list) -> set:
    return _class_from_manifest(
        manifest_skills,
        lambda r: r.get("claude_model") == "sonnet" and r.get("section_0") is True,
    )


def _opus_tier_full_class(manifest_skills: list) -> set:
    return _class_from_manifest(manifest_skills, lambda r: r.get("section_0") is False)


def _read_roster(dev_tests_dir: Path, generators: dict, file_name: str, roster_name: str, findings: list, rule: str):
    """Locate + parse a named roster in either quoin/dev/tests/<file_name> or a
    generator script. Returns a set[str] (DERIVED coerced to None, callers that
    need DERIVED handle it themselves) or None + emits no finding of its own
    when the roster cannot be found/parsed (caller decides how to react)."""
    if file_name in generators:
        path = generators[file_name]
    else:
        path = dev_tests_dir / file_name
    node = parse_roster(path, roster_name)
    if node is None:
        return None
    try:
        result = eval_collection(node)
    except ValueError:
        return None
    return result


def rg_testroster(dev_tests_dir: Path, manifest_skills: list, findings: list) -> None:
    """AC-3: GLOBAL, cross-file-AGREEING test rosters, each == its skills.json-
    derived class, and all copies mutually agreeing."""
    opus_leaf = _opus_leaf_class(manifest_skills)
    all_s0 = _all_section0_class(manifest_skills)
    sonnet_cheap = _sonnet_cheap_class(manifest_skills)

    def _check_group(group_name: str, expected: set, copies: list):
        """copies: list of (file_name, roster_name)."""
        resolved = {}
        for file_name, roster_name in copies:
            val = _read_roster(dev_tests_dir, {}, file_name, roster_name, findings, RG_TESTROSTER)
            if val is None or val == DERIVED:
                continue
            resolved[(file_name, roster_name)] = val
        for (file_name, roster_name), val in resolved.items():
            missing = expected - val
            extra = val - expected
            for skill in sorted(missing):
                findings.append({
                    "rule": RG_TESTROSTER,
                    "roster": f"{roster_name}@{file_name}",
                    "entry": skill,
                    "detail": f"{group_name} class requires {skill!r} but {roster_name} in {file_name} is missing it",
                })
            for skill in sorted(extra):
                findings.append({
                    "rule": RG_TESTROSTER,
                    "roster": f"{roster_name}@{file_name}",
                    "entry": skill,
                    "detail": f"{roster_name} in {file_name} has extra entry {skill!r} not in the {group_name} class",
                })

    _check_group("opus-leaf", opus_leaf, [
        ("test_1m_context_precheck.py", "SECTION0PRIME_TARGETS"),
        ("test_quoin_pollution_preamble.py", "SKILL_DISTINCTIVE_TOKENS"),
        ("test_quoin_pollution_preamble.py", "POLLUTION_TARGET_SKILLS"),
        ("test_inject_pollution_dispatch.py", "SKILL_DISTINCTIVE_TOKENS"),
        ("test_mintier_guard.py", "MINTIER_SKILLS"),
        ("test_pollution_score_extraction.py", "TARGET_SKILLS"),
    ])
    _check_group("all-section_0", all_s0, [
        ("test_1m_context_precheck.py", "SECTION0_TARGETS"),
        ("test_1m_proactive_precheck.py", "SECTION0_TARGETS"),
    ])
    _check_group("sonnet-cheap", sonnet_cheap, [
        ("test_sonnet_mintier_guard.py", "SONNET_MINTIER_SKILLS"),
    ])


def rg_genroster(dev_tests_dir: Path, scripts_dir: Path, manifest_skills: list, findings: list) -> None:
    """AC-3 (Q-04 hard-fail): generator rosters vs skills.json-derived classes."""
    opus_leaf = _opus_leaf_class(manifest_skills)
    sonnet_cheap = _sonnet_cheap_class(manifest_skills)
    opus_full = _opus_tier_full_class(manifest_skills)
    all_s0 = _all_section0_class(manifest_skills)
    canonical_names = {rec["name"] for rec in manifest_skills}

    generators = {
        "build_preambles.py": scripts_dir / "build_preambles.py",
        "inject_pollution_dispatch.py": scripts_dir / "inject_pollution_dispatch.py",
    }

    def _get(file_name: str, roster_name: str):
        path = generators.get(file_name, dev_tests_dir / file_name)
        node = parse_roster(path, roster_name)
        if node is None:
            return None
        try:
            return eval_collection(node)
        except ValueError:
            return None

    def _check(roster_name: str, file_name: str, expected: set, label: str):
        val = _get(file_name, roster_name)
        if val is None or val == DERIVED:
            return
        for skill in sorted(expected - val):
            findings.append({
                "rule": RG_GENROSTER,
                "roster": f"{roster_name}@{file_name}",
                "entry": skill,
                "detail": f"{label} requires {skill!r} but {roster_name} in {file_name} is missing it",
            })
        for skill in sorted(val - expected):
            findings.append({
                "rule": RG_GENROSTER,
                "roster": f"{roster_name}@{file_name}",
                "entry": skill,
                "detail": f"{roster_name} in {file_name} has extra entry {skill!r} not in the {label}",
            })

    # SPAWN_TARGETS: dict keys == {spawn_target==true}
    spawn_class = _class_from_manifest(manifest_skills, lambda r: r.get("spawn_target") is True)
    _check("SPAWN_TARGETS", "build_preambles.py", spawn_class, "{spawn_target==true} class")

    # POLLUTION_TARGET_SKILLS / MINTIER_TARGET_SKILLS == opus-leaf class; mutual agreement
    pollution_val = _get("inject_pollution_dispatch.py", "POLLUTION_TARGET_SKILLS")
    mintier_val = _get("inject_pollution_dispatch.py", "MINTIER_TARGET_SKILLS")
    _check("POLLUTION_TARGET_SKILLS", "inject_pollution_dispatch.py", opus_leaf, "opus-leaf class")
    _check("MINTIER_TARGET_SKILLS", "inject_pollution_dispatch.py", opus_leaf, "opus-leaf class")
    if pollution_val not in (None, DERIVED) and mintier_val not in (None, DERIVED) and pollution_val != mintier_val:
        findings.append({
            "rule": RG_GENROSTER,
            "roster": "POLLUTION_TARGET_SKILLS/MINTIER_TARGET_SKILLS@inject_pollution_dispatch.py",
            "entry": "n/a",
            "detail": "POLLUTION_TARGET_SKILLS and MINTIER_TARGET_SKILLS disagree with each other",
        })

    # MINTIER_SONNET_TARGET_SKILLS == sonnet-cheap class; cross-check against
    # the test-side SONNET_MINTIER_SKILLS copy (RG-TESTROSTER checks that one
    # against the class too — here we additionally assert the two agree).
    _check("MINTIER_SONNET_TARGET_SKILLS", "inject_pollution_dispatch.py", sonnet_cheap, "sonnet-cheap class")
    gen_sonnet = _get("inject_pollution_dispatch.py", "MINTIER_SONNET_TARGET_SKILLS")
    test_sonnet = _get("test_sonnet_mintier_guard.py", "SONNET_MINTIER_SKILLS")
    if gen_sonnet not in (None, DERIVED) and test_sonnet not in (None, DERIVED) and gen_sonnet != test_sonnet:
        findings.append({
            "rule": RG_GENROSTER,
            "roster": "MINTIER_SONNET_TARGET_SKILLS/SONNET_MINTIER_SKILLS",
            "entry": "n/a",
            "detail": "generator's MINTIER_SONNET_TARGET_SKILLS disagrees with test_sonnet_mintier_guard.py's SONNET_MINTIER_SKILLS",
        })

    # OPUS_TIER_SKILLS == full {section_0==false} complement (no exceptions)
    _check("OPUS_TIER_SKILLS", "test_quoin_stage1_preamble.py", opus_full, "{section_0==false} class (full complement)")

    # SECTION0_TARGET_SKILLS == all-section_0 class (IVG-165 — the §0 generator's
    # own 20-skill roster; mirrors the MINTIER_SONNET_TARGET_SKILLS pattern above).
    # Dict-keyed roster (skill -> (tier, proceed_ref, variant, clause, comment));
    # eval_collection extracts the key set.
    _check("SECTION0_TARGET_SKILLS", "inject_pollution_dispatch.py", all_s0, "all-section_0 class")

    # ZC_SKILLS / WORKTREE_FALLBACK_SKILLS / SOURCE_MUTATING_WORKTREE_SKILLS:
    # no skills.json-derivable field — sanity-check members ⊆ CANONICAL_SKILLS only.
    for file_name, roster_name in [
        ("inject_pollution_dispatch.py", "ZC_SKILLS"),
        ("test_quoin_stage1_worktree_fallback.py", "WORKTREE_FALLBACK_SKILLS"),
        ("test_quoin_stage1_worktree_fallback.py", "SOURCE_MUTATING_WORKTREE_SKILLS"),
    ]:
        val = _get(file_name, roster_name)
        if val is None or val == DERIVED:
            continue
        for skill in sorted(val - canonical_names):
            findings.append({
                "rule": RG_GENROSTER,
                "roster": f"{roster_name}@{file_name}",
                "entry": skill,
                "detail": f"{roster_name} in {file_name} contains {skill!r} which is not a CANONICAL_SKILLS skill",
            })


# ---------------------------------------------------------------------------
# RG-CENSUS (T-04, round 3, D-07): mechanical, terminating roster population census
# ---------------------------------------------------------------------------

# Rosters actually checked by RG-TESTROSTER / RG-GENROSTER above — one entry
# per (roster, file) pair each rule reads. Keyed "NAME@basename.py".
COVERED_ROSTERS: frozenset = frozenset({
    # RG-TESTROSTER: opus-leaf class (10 skills), 6 agreeing copies
    "SECTION0PRIME_TARGETS@test_1m_context_precheck.py",
    "SKILL_DISTINCTIVE_TOKENS@test_quoin_pollution_preamble.py",
    "POLLUTION_TARGET_SKILLS@test_quoin_pollution_preamble.py",
    "SKILL_DISTINCTIVE_TOKENS@test_inject_pollution_dispatch.py",
    "MINTIER_SKILLS@test_mintier_guard.py",
    "TARGET_SKILLS@test_pollution_score_extraction.py",
    # RG-TESTROSTER: all-section_0 class (19 skills), 2 agreeing copies
    "SECTION0_TARGETS@test_1m_context_precheck.py",
    "SECTION0_TARGETS@test_1m_proactive_precheck.py",
    # RG-TESTROSTER: sonnet-cheap class (10 skills)
    "SONNET_MINTIER_SKILLS@test_sonnet_mintier_guard.py",
    # RG-GENROSTER
    "SPAWN_TARGETS@build_preambles.py",
    "POLLUTION_TARGET_SKILLS@inject_pollution_dispatch.py",
    "MINTIER_TARGET_SKILLS@inject_pollution_dispatch.py",
    "MINTIER_SONNET_TARGET_SKILLS@inject_pollution_dispatch.py",
    "SECTION0_TARGET_SKILLS@inject_pollution_dispatch.py",
    "OPUS_TIER_SKILLS@test_quoin_stage1_preamble.py",
    "ZC_SKILLS@inject_pollution_dispatch.py",
    "WORKTREE_FALLBACK_SKILLS@test_quoin_stage1_worktree_fallback.py",
    "SOURCE_MUTATING_WORKTREE_SKILLS@test_quoin_stage1_worktree_fallback.py",
    # RG-MIGRATED (T-06): glob-discovered MIGRATED_TO_ADAPTER copies, all 5 files
    "MIGRATED_TO_ADAPTER@test_1m_context_precheck.py",
    "MIGRATED_TO_ADAPTER@test_1m_proactive_precheck.py",
    "MIGRATED_TO_ADAPTER@test_quoin_stage1_preamble.py",
    "MIGRATED_TO_ADAPTER@test_quoin_stage1_recursion_abort.py",
    "MIGRATED_TO_ADAPTER@test_quoin_stage1_worktree_fallback.py",
})

# Rosters the census discovers but this task's confirmed scope explicitly
# excludes from cross-file agreement / classification-derivation (D-04, D-06,
# D-07) — mapped to a one-line reason. "NAME" (no @file) applies to every file
# discovered under that name; "NAME@file" disambiguates when the same name
# means two different things in two files.
KNOWN_DEFERRED_ROSTERS: dict = {
    # D-04: CHEAP_TIER_SKILLS is drifted 4 ways (12/12/18/15 members) — same
    # defect class as MIGRATED_TO_ADAPTER but NOT opted into this task's scope
    # (only Q-02/MIGRATED_TO_ADAPTER was opted in).
    "CHEAP_TIER_SKILLS": "drifted 4-way roster, same defect class as MIGRATED_TO_ADAPTER, not in confirmed Q-02 scope (D-04)",
    # D-07: MIGRATED_SKILLS_DIR_OVERRIDES is drifted 4 ways (9/9/9-different/6
    # members) — same defect class, also not in confirmed scope.
    "MIGRATED_SKILLS_DIR_OVERRIDES": "drifted 4-way roster, same defect class as MIGRATED_TO_ADAPTER, not in confirmed Q-02 scope (D-07)",
    # D-07: TARGET_SKILLS in test_pitfall_preamble_in_class_b.py is a
    # DIFFERENT, 8-member, non-skills.json-derivable "Class-B pitfall target"
    # roster that merely shares a variable name with the covered copy.
    "TARGET_SKILLS@test_pitfall_preamble_in_class_b.py": (
        "distinct 8-member Class-B pitfall-injection-target roster (editorial "
        "criterion, no skills.json field) — shares a name with, but is not "
        "the same population as, the covered opus-leaf TARGET_SKILLS copy (D-07)"
    ),
    # ORCHESTRATOR_SKILLS: 2-member {run, thorough_plan} exclusion list, no
    # skills.json field distinguishes "orchestrator" — editorial, single concept
    # duplicated verbatim in the two §0' pollution-dispatch test files.
    "ORCHESTRATOR_SKILLS": "editorial 2-member orchestrator-exclusion list, no skills.json field, verbatim-duplicated (not independently authored, no drift risk)",
    # CLASS_B_WRITERS: 6-member "Class B pattern writer" grouping, editorial
    # (per Stage 4 D-04-rev2 criterion), no skills.json field, single-file.
    "CLASS_B_WRITERS@test_tmp_cleanup_contract.py": "editorial Class-B-writer grouping (Stage 4 D-04-rev2 criterion), no skills.json field, single-file",

    # --- Discovered by the T-04 authorship-time run of the mechanical scan
    # (below): each is a same-shaped incidental fixture — a list/dict/tuple
    # whose members happen to overlap valid skill names but encodes a
    # narrower, editorial, or structural concept (a specific test's
    # parametrization, a doc's phase list, a rendering pipeline's node
    # labels) rather than a general skill-classification roster this task's
    # scope covers. This is exactly the false-positive class T-04's design
    # anticipated ("status-graph pipeline-node lists, section-heading
    # anchors, which happen to contain strings that overlap with skill
    # names but are never iterated as a skill roster") — the census still
    # discovers them (the syntactic usage filter cannot fully distinguish
    # them from a true roster), so they are classified here explicitly
    # rather than silently passing.
    "ORCHESTRATOR_SKILLS@test_quoin_pollution_preamble.py": "editorial 2-member orchestrator-exclusion list, no skills.json field, verbatim-duplicated",
    "ORCHESTRATOR_SKILLS@test_inject_pollution_dispatch.py": "editorial 2-member orchestrator-exclusion list, no skills.json field, verbatim-duplicated",

    # PRIOR_PHASE_SKILLS: a cumulative "skills that existed at rollout phase N"
    # snapshot, DIFFERENT per adapter-pilot smoke-test file by design (each
    # file's list reflects that skill's position in the migration order, not
    # a drift bug) — verified non-identical across files by direct read.
    # Deferred file-independently: the concept, not any one file's snapshot,
    # is what's excluded.
    "PRIOR_PHASE_SKILLS": "cumulative rollout-order snapshot, intentionally different per adapter-pilot smoke-test file, no skills.json field",

    "SKILLS@test_architecture_planning_adapter_pilot.py": "list-of-tuples pilot-migration smoke-test parametrization (2 skills), not a general roster",
    "SKILLS@test_planning_loop_adapter_pilot.py": "list-of-tuples pilot-migration smoke-test parametrization (4 skills), not a general roster",

    "_TRANSITIVE_SKILLS@test_autonomous_mode_memory.py": "editorial autonomous-mode.md doc-coverage list, no skills.json field, single-file",
    "REFERENCING_SKILLS@test_branch_recovery_recipe.py": "editorial branch-recovery-recipe cross-reference list (4 skills), no skills.json field, single-file",
    "CORE_WORKFLOW_PHASES@test_codex_installable_feature.py": "workflow-PHASE list (discover/plan/implement/review/gate) for Codex procedure-doc coverage, not a skill-registration roster",
    "STEP5_SKILLS@test_fallback_increment_in_skills.py": "editorial Class-B Step-5 pattern grouping, no skills.json field, single-file",
    "STEP2_SKILLS@test_fallback_increment_in_skills.py": "editorial Class-B Step-2 pattern grouping, no skills.json field, single-file",
    "NEGATIVE_SKILLS@test_fallback_increment_in_skills.py": "negative-control list paired with STEP5/STEP2_SKILLS, no skills.json field, single-file",
    "NON_TARGET_SKILLS@test_pitfall_preamble_in_class_b.py": "negative-control list paired with the deferred Class-B TARGET_SKILLS copy in the same file (D-07)",
    "SKILLS_WITH_WRITE_SITE@test_preamble_bootstrap_step.py": "editorial format-kit write-site grouping (6 skills), no skills.json field, single-file",
    "LOAD_BEARING_HEADINGS@test_quoin_stage1_preamble.py": "dict of 3 skills -> a load-bearing heading string, regression-guard fixture, not a skill roster",
    "_REACHABLE_ARTIFACT_ONLY_SKILLS@test_quoin_stage1_worktree_fallback.py": "2-skill autonomous-clause reachability fixture, no skills.json field, single-file",
    "_REACHABLE_SOURCE_MUTATING_SKILLS@test_quoin_stage1_worktree_fallback.py": "2-skill autonomous-clause reachability fixture, no skills.json field, single-file",
    "_PIPELINE_NODES@test_status_graph.py": "status-graph pipeline-node render labels — the literal example T-04's design anticipated, not a skill roster",
    "EXPECTED@test_budget_roster_census.py": "IVG-141 budget-check roster: editorial 4-skill grouping (run/implement/thorough_plan/review) self-guarded by set-equality in the same file, no skills.json field, single-file",
    "SECTION0_SKILLS@test_footprint_ceilings.py": (
        "IVG-162 T-02 byte-ceiling target roster: the full 20-skill §0-carrying "
        "set (source of truth: quoin/CLAUDE.md '### §0 Model dispatch preamble' "
        "prose list), used only to key the CEILINGS dict for per-file byte "
        "assertions. A DIFFERENT, smaller population from the 19-member "
        "SECTION0_TARGETS@test_1m_*.py copies by design — SECTION0_TARGETS "
        "mirrors propagate_1m_s0_edit.py's 19-file pilot-patch scope (one "
        "skill was patched separately, per T-09's investigation), while "
        "SECTION0_SKILLS is the full 20-file §0-heading set. No skills.json "
        "field distinguishes '§0-carrying', so this is editorial, single-file, "
        "self-guarded by test_section0_skills_set_matches_ceiling_keys in the "
        "same file — not a candidate for RG-TESTROSTER cross-file agreement."
    ),
    "SECTION0_SKILLS@test_section0_marker.py": (
        "IVG-165 T-02 marker-placement-assertion roster: a verbatim copy of "
        "the same 20-skill §0-carrying population as "
        "SECTION0_SKILLS@test_footprint_ceilings.py, duplicated (not "
        "independently authored) so this structural-invariant test has no "
        "cross-file import dependency on the ceilings test. Same rationale "
        "applies: no skills.json field distinguishes '§0-carrying', editorial, "
        "single-file, not a candidate for RG-TESTROSTER cross-file agreement."
    ),
    "_ADAPTER_POINTER_COUNTS@test_authored_content_rule_pointers.py": (
        "the expected per-file pointer-site count for one guard test's own "
        "assertion, not a skill-classification roster — no skills.json field "
        "backs it, it is read directly against the five adapter SKILL.md "
        "files it names in the same test, and no other file duplicates it."
    ),
    "SHARE_CEILINGS@test_section0_provenance_guard.py": (
        "prompt-audit-anthropic-skills stage-5 T-03 per-file §0-family share "
        "ceiling roster: all 32 adapter skill names, used only to key the "
        "SHARE_CEILINGS dict for per-file percentage-share assertions "
        "(measured §0-family line share + a flat headroom). Same shape as "
        "SECTION0_SKILLS@test_footprint_ceilings.py above — no skills.json "
        "field distinguishes it, editorial, single-file, self-guarded by "
        "test_share_ceilings_key_set_matches_adapter_skills in the same "
        "file — not a candidate for RG-TESTROSTER cross-file agreement."
    ),
    "DENSITY_EXCLUDED_BELOW_FLOOR@test_section0_provenance_guard.py": (
        "prompt-audit-anthropic-skills stage-5 T-06 pressure-density roster: "
        "the 18 manifest-33 files measuring below DENSITY_MATERIALITY_FLOOR "
        "(5 caps tokens), the complement of the DENSITY_CEILINGS dict's 15 "
        "keys. No skills.json field distinguishes 'below the density "
        "materiality floor', editorial, single-file, self-guarded by "
        "test_density_ceiling_and_excluded_sets_cover_manifest_33_exactly "
        "in the same file — not a candidate for RG-TESTROSTER cross-file "
        "agreement. (DENSITY_CEILINGS itself is not discovered by this "
        "census: it also carries the non-skill key 'claude_md', so it fails "
        "the census's members-subset-of-CANONICAL_SKILLS filter.)"
    ),
}

_ALLCAPS_RE = None  # populated lazily to avoid import cost at module load


def _is_all_caps(name: str) -> bool:
    return name.isupper() and any(c.isalpha() for c in name)


def _name_used_as_roster(tree: ast.Module, name: str) -> bool:
    """Syntactic usage-context filter (T-04 RG-CENSUS): NAME is used elsewhere
    in the same module as the receiver of pytest.mark.parametrize(...), the
    iterable of a `for ... in NAME:` loop (incl. comprehensions), the object of
    `NAME.get(`, or the right-hand side of `x in NAME` / `x not in NAME`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "parametrize":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == name:
                    return True
        if isinstance(node, (ast.For, ast.comprehension)):
            iter_node = node.iter
            if isinstance(iter_node, ast.Name) and iter_node.id == name:
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == name:
                return True
        if isinstance(node, ast.Compare):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Name) and comparator.id == name:
                        return True
    return False


def _discover_rosters(dev_tests_dir: Path, scripts_dir: Path, canonical_names: set) -> list:
    """Mechanical scan: every module-level Assign/AnnAssign with an ALL-CAPS
    target name across quoin/dev/tests/test_*.py plus the 2 generators, that
    (a) evaluates to a non-empty set of members all ⊆ CANONICAL_SKILLS with
    length >= 2, AND (b) is used elsewhere in the same module as a roster
    (parametrize / for-loop / .get / in-membership). Returns [(name, file_name)].
    """
    files = sorted(dev_tests_dir.glob("test_*.py"))
    files += [scripts_dir / "build_preambles.py", scripts_dir / "inject_pollution_dispatch.py"]

    discovered = []
    for path in files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError):
            continue

        candidate_names = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and _is_all_caps(target.id):
                        candidate_names.append(target.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and _is_all_caps(node.target.id) and node.value is not None:
                    candidate_names.append(node.target.id)

        for name in candidate_names:
            value_node = parse_roster(path, name)
            try:
                members = eval_collection(value_node)
            except ValueError:
                continue
            if members == DERIVED:
                member_set = None  # DERIVED counts as non-empty by construction
            else:
                if not members or len(members) < 2:
                    continue
                if not all(isinstance(m, str) for m in members):
                    continue
                if not members <= canonical_names:
                    continue
                member_set = members
            if _name_used_as_roster(tree, name):
                discovered.append((name, path.name))
    return discovered


def rg_census(dev_tests_dir: Path, scripts_dir: Path, canonical_names: set, findings: list) -> None:
    """T-04, round 3 (D-07): every roster the mechanical scan discovers must be
    either in COVERED_ROSTERS or KNOWN_DEFERRED_ROSTERS. Anything in neither
    FAILS loud, naming the file + roster — the convergence point that replaces
    hand-enumeration (which missed real rosters twice, per D-06/D-07)."""
    for name, file_name in _discover_rosters(dev_tests_dir, scripts_dir, canonical_names):
        key_with_file = f"{name}@{file_name}"
        if key_with_file in COVERED_ROSTERS or key_with_file in KNOWN_DEFERRED_ROSTERS:
            continue
        if name in KNOWN_DEFERRED_ROSTERS:
            continue
        findings.append({
            "rule": RG_CENSUS,
            "roster": key_with_file,
            "entry": name,
            "detail": (
                f"roster {name!r} discovered in {file_name} is neither in COVERED_ROSTERS nor "
                f"KNOWN_DEFERRED_ROSTERS — classify it in quoin/dev/check_registration.py"
            ),
        })


# ---------------------------------------------------------------------------
# T-06: RG-MIGRATED — literal-vs-derive drift guard (added AFTER T-05 is green)
# ---------------------------------------------------------------------------

def _canonical_migrated_set(adapter_skills_dir: Path) -> set:
    """The single filesystem-derived canonical MIGRATED_TO_ADAPTER set (D-02):
    every skill with an adapter SKILL.md — matches T-05's reconciled form."""
    if not adapter_skills_dir.is_dir():
        return set()
    return {p.name for p in adapter_skills_dir.iterdir() if (p / "SKILL.md").is_file()}


def rg_migrated(dev_tests_dir: Path, adapter_skills_dir: Path, findings: list) -> None:
    """Glob every quoin/dev/tests/test_*.py for a module-level MIGRATED_TO_ADAPTER
    assignment (discovery via glob, not a frozen file list — a future 6th copy is
    picked up automatically). DERIVED-shaped copies are canonical-by-construction;
    literal copies are compared against the filesystem-derived canonical set."""
    canonical = _canonical_migrated_set(adapter_skills_dir)
    for path in sorted(dev_tests_dir.glob("test_*.py")):
        node = parse_roster(path, "MIGRATED_TO_ADAPTER")
        if node is None:
            continue
        try:
            value = eval_collection(node)
        except ValueError:
            continue
        if value == DERIVED:
            continue
        missing = canonical - value
        extra = value - canonical
        for skill in sorted(missing):
            findings.append({
                "rule": RG_MIGRATED,
                "roster": f"MIGRATED_TO_ADAPTER@{path.name}",
                "entry": skill,
                "detail": f"{skill!r} has an adapter SKILL.md but is missing from the hardcoded MIGRATED_TO_ADAPTER in {path.name}",
            })
        for skill in sorted(extra):
            findings.append({
                "rule": RG_MIGRATED,
                "roster": f"MIGRATED_TO_ADAPTER@{path.name}",
                "entry": skill,
                "detail": f"{skill!r} is listed in {path.name}'s MIGRATED_TO_ADAPTER but has no adapter SKILL.md",
            })


# ---------------------------------------------------------------------------
# Emit findings
# ---------------------------------------------------------------------------

def emit_findings(findings: list, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"findings": findings}, indent=2))
    else:
        for f in findings:
            print(f"{f['rule']} {f['roster']}: {f['entry']} — {f['detail']}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate skill/script registration roster consistency (IVG-118).",
    )
    parser.add_argument("--repo-root", type=Path, default=None, help="Path to the quoin inner package root (default: auto-detected)")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to skills.json manifest (default: auto-detected)")
    parser.add_argument("--json", action="store_true", default=False, help="Emit findings as JSON to stdout instead of human-readable lines on stderr")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # --help / --version legitimately exit 0 via argparse; only a real
        # usage error (missing/bad argument) should surface as USAGE (64).
        sys.exit(0 if exc.code in (0, None) else 64)

    repo_pkg: Path = args.repo_root if args.repo_root is not None else _default_repo_pkg()
    manifest_path: Path = args.manifest if args.manifest is not None else _default_manifest(repo_pkg)

    skills_dir = repo_pkg / "skills"
    adapter_skills_dir = repo_pkg / "adapters" / "claude" / "skills"
    scripts_dir = repo_pkg / "scripts"
    core_scripts_dir = repo_pkg / "core" / "scripts"
    dev_tests_dir = repo_pkg / "dev" / "tests"

    manifest = load_manifest(manifest_path)
    manifest_skills = manifest["skills"]
    manifest_names = {rec["name"] for rec in manifest_skills}

    installer_path = _default_installer(repo_pkg)
    canonical_skills, deployed_scripts, core_scripts, skill_overrides = load_installer_rosters(installer_path)
    canonical_set = set(canonical_skills)
    deployed_set = set(deployed_scripts)
    core_scripts_set = set(core_scripts)

    fs_names = {d.name for d in skills_dir.iterdir() if d.is_dir()} if skills_dir.is_dir() else set()

    findings: list = []

    # Phase 1 (T-02)
    rg_canon(fs_names, canonical_set, findings)
    rg_manifest(fs_names, canonical_set, manifest_names, findings)
    rg_runtime_break(deployed_set, core_scripts_set, core_scripts_dir, findings)
    rg_stale_core(core_scripts_set, core_scripts_dir, findings)

    # Phase 2 (T-04)
    rg_deploy(scripts_dir, deployed_set, findings)
    rg_overrides(canonical_set, skill_overrides, findings)
    rg_testroster(dev_tests_dir, manifest_skills, findings)
    rg_genroster(dev_tests_dir, scripts_dir, manifest_skills, findings)
    rg_census(dev_tests_dir, scripts_dir, canonical_set, findings)

    # T-06 placeholder: rg_migrated() is defined above and unit-testable
    # standalone, but is NOT wired into main() until T-05's filesystem-derive
    # reconciliation lands and the full suite is green (plan strict T-05 ->
    # T-06 ordering, R-05) — see _RG_MIGRATED_WIRED below.
    if _RG_MIGRATED_WIRED:
        rg_migrated(dev_tests_dir, adapter_skills_dir, findings)

    emit_findings(findings, args.json)

    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
