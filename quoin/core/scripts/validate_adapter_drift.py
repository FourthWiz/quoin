#!/usr/bin/env python3
"""Adapter drift validator — Phase 22 runtime-portability work.

Reads quoin/core/workflow/skills.json and asserts per-skill structural
invariants across the core/ and adapters/claude/ trees.
Also validates that quoin/scripts/ has a wrapper for every core script
(AD-WR), that wrappers are delegation-only shims (AD-WD), that pyproject
force-include ships all required core asset dirs (AD-PK), and that the
Codex adapter surface is complete (AD-CX). A manifest entry with
"claude_adapter": false is checked for AD-CO and AD-CX only, and AD-AD fires if
a Claude adapter exists for it.

Lives in core/scripts/ because the manifest and the artifact-path
conventions are runtime-neutral; references to ## §0 dispatch and
adapters/claude/ are Claude-specific by necessity — see Decisions D-09
in the Phase 22 plan.

revise-fast uses a See-also form for AD-PT because it shares the
revise.md intent doc; the substring check preserves the Phase 21 contract.
A future phase may tighten to a canonical single-line pointer.

capture_insight is the lead if-branch (Phase 6 migration) in install.sh.
Normalizing to elif would break bash control flow.

Newer installer wrappers may delegate skill deployment to `quoin install`
instead of carrying per-skill shell branches. In that shape, AD-IV/AD-IE/AD-IO
are satisfied when install.sh delegates to the Python installer and
src/quoin/installer.py deploys skills from adapters/claude/skills/<name>/SKILL.md.

Usage:
    python3 validate_adapter_drift.py [--manifest PATH] [--repo-root PATH] [--json]

Exit codes:
    0   PASS — no violations
    2   DRIFT — one or more violations detected
    64  USAGE — bad CLI arguments
    65  DATA — manifest unreadable or malformed
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Invariant IDs (stable contract — tests assert by ID, not by prose)
#
# Violations are emitted as one of the following prefixes:
# DRIFT AD-CO  (core-doc-exists)
# DRIFT AD-AD  (adapter-doc-exists)
# DRIFT AD-FN  (frontmatter-name)
# DRIFT AD-FM  (frontmatter-model)
# DRIFT AD-PT  (pointer-text)
# DRIFT AD-S0P (section-0-present)
# DRIFT AD-S0A (section-0-absent)
# DRIFT AD-PE  (preamble-present)
# DRIFT AD-PX  (preamble-not-present-for-non-spawn)
# DRIFT AD-IV  (install.sh-variable)
# DRIFT AD-IE  (install.sh-branch)
# DRIFT AD-IO  (install.sh-ordering)
# DRIFT AD-WR  (core-script-has-wrapper) — every quoin/core/scripts/*.py needs a quoin/scripts/ shim
# DRIFT AD-WD  (wrapper-delegation-only) — wrappers that shadow a core script must be shims only
# DRIFT AD-PK  (pip-manifest-key) — pyproject force-include must ship all required core asset dirs
# DRIFT AD-CX  (codex-surface) — every manifest skill needs a Codex README; feature-manifest must be valid
# ---------------------------------------------------------------------------
AD_CO = "AD-CO"   # core-doc-exists
AD_AD = "AD-AD"   # adapter-doc-exists
AD_FN = "AD-FN"   # frontmatter-name
AD_FM = "AD-FM"   # frontmatter-model
AD_PT = "AD-PT"   # pointer-text (Phase 21 substring contract)
AD_S0P = "AD-S0P" # section-0-present
AD_S0A = "AD-S0A" # section-0-absent
AD_PE = "AD-PE"   # preamble-present (spawn targets)
AD_PX = "AD-PX"   # preamble-not-present-for-non-spawn
AD_IV = "AD-IV"   # install.sh-variable
AD_IE = "AD-IE"   # install.sh-branch
AD_IO = "AD-IO"   # install.sh-ordering
AD_WR = "AD-WR"   # core-script-has-wrapper
AD_WD = "AD-WD"   # wrapper-delegation-only
AD_PK = "AD-PK"   # pip-manifest-key
AD_CX = "AD-CX"   # codex-surface


# ---------------------------------------------------------------------------
# Section-0 regex (line-anchored per D-13)
# ---------------------------------------------------------------------------
_S0_PATTERN = re.compile(r"^## §0 Model dispatch \(FIRST STEP", re.MULTILINE)


def _default_repo_root() -> Path:
    """Two parents above this script puts us at the quoin package's parent (the repo root)."""
    return Path(__file__).resolve().parents[3]


def _default_manifest(repo_root: Path) -> Path:
    return repo_root / "quoin" / "core" / "workflow" / "skills.json"


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def load_manifest(path: Path) -> Dict[str, Any]:
    """Load and minimally validate the skills manifest. Exit 65 on errors."""
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


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _extract_frontmatter(text: str) -> Optional[str]:
    """Extract the raw YAML between the first pair of --- fences."""
    parts = text.replace("\r\n", "\n").split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1]


def _parse_frontmatter_field(frontmatter: str, field: str) -> Optional[str]:
    """Extract a simple scalar field from YAML frontmatter via regex."""
    m = re.search(rf"^{re.escape(field)}:\s*(\S+)", frontmatter, re.MULTILINE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Per-skill check functions
# ---------------------------------------------------------------------------

def check_files_exist(
    name: str,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> Tuple[bool, bool]:
    """AD-CO, AD-AD: verify the core doc and the Claude adapter SKILL.md exist.

    Returns (core_ok, adapter_ok) so callers can skip dependent checks.
    """
    core_path = repo_root / "quoin" / "core" / "skills" / f"{name}.md"
    adapter_path = repo_root / "quoin" / "adapters" / "claude" / "skills" / name / "SKILL.md"

    core_ok = core_path.is_file()
    adapter_ok = adapter_path.is_file()

    if not core_ok:
        violations.append({
            "invariant": AD_CO,
            "skill": name,
            "detail": f"missing quoin/core/skills/{name}.md",
        })
    if not adapter_ok:
        violations.append({
            "invariant": AD_AD,
            "skill": name,
            "detail": f"missing quoin/adapters/claude/skills/{name}/SKILL.md",
        })

    return core_ok, adapter_ok


def check_no_claude_surface(
    name: str,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-CO, AD-AD for a claude_adapter=false skill: core doc present, Claude files absent."""
    core_path = repo_root / "quoin" / "core" / "skills" / f"{name}.md"
    if not core_path.is_file():
        violations.append({
            "invariant": AD_CO,
            "skill": name,
            "detail": f"missing quoin/core/skills/{name}.md",
        })
    adapter_dir = repo_root / "quoin" / "adapters" / "claude" / "skills" / name
    if adapter_dir.exists():
        violations.append({
            "invariant": AD_AD,
            "skill": name,
            "detail": "claude_adapter=false but a Claude adapter is present",
        })


def check_frontmatter(
    name: str,
    claude_model: str,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-FN, AD-FM: adapter frontmatter name and model fields."""
    adapter_path = repo_root / "quoin" / "adapters" / "claude" / "skills" / name / "SKILL.md"
    text = adapter_path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(text)
    if fm is None:
        violations.append({
            "invariant": AD_FN,
            "skill": name,
            "detail": "adapter SKILL.md has no YAML frontmatter",
        })
        return

    fm_name = _parse_frontmatter_field(fm, "name")
    if fm_name != name:
        violations.append({
            "invariant": AD_FN,
            "skill": name,
            "detail": f"adapter frontmatter name={fm_name!r} expected {name!r}",
        })

    fm_model = _parse_frontmatter_field(fm, "model")
    if fm_model != claude_model:
        violations.append({
            "invariant": AD_FM,
            "skill": name,
            "detail": f"adapter frontmatter model={fm_model!r} expected {claude_model!r} (from manifest)",
        })


def check_pointer(
    name: str,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-PT: adapter body contains 'quoin/core/skills/<name>.md' as substring.

    This is the Phase 21 substring contract (relaxed from an exact pointer-line regex).
    For revise-fast, the substring appears on the See-also line, not on the primary
    pointer line (which points to quoin/core/skills/revise.md). Both are intentional.
    """
    adapter_path = repo_root / "quoin" / "adapters" / "claude" / "skills" / name / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    needle = f"quoin/core/skills/{name}.md"
    if needle not in content:
        violations.append({
            "invariant": AD_PT,
            "skill": name,
            "detail": f"adapter does not contain substring {needle!r}",
        })


def check_section_0(
    name: str,
    section_0: bool,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-S0P / AD-S0A: section-0 presence matches manifest declaration."""
    adapter_path = repo_root / "quoin" / "adapters" / "claude" / "skills" / name / "SKILL.md"
    content = adapter_path.read_text(encoding="utf-8")
    has_s0 = bool(_S0_PATTERN.search(content))

    if section_0 and not has_s0:
        violations.append({
            "invariant": AD_S0P,
            "skill": name,
            "detail": (
                "manifest declares section_0=true but adapter is missing "
                "'## §0 Model dispatch (FIRST STEP' (line-anchored)"
            ),
        })
    elif not section_0 and has_s0:
        violations.append({
            "invariant": AD_S0A,
            "skill": name,
            "detail": (
                "manifest declares section_0=false but adapter contains "
                "'## §0 Model dispatch (FIRST STEP' at line start"
            ),
        })


def check_preamble(
    name: str,
    spawn_target: bool,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-PE, AD-PX: preamble presence matches spawn_target declaration."""
    adapter_preamble = repo_root / "quoin" / "adapters" / "claude" / "skills" / name / "preamble.md"

    if spawn_target:
        # AD-PE: the installer copies preamble.md from the adapter skill folder
        if not adapter_preamble.is_file():
            violations.append({
                "invariant": AD_PE,
                "skill": name,
                "detail": (
                    f"spawn_target=true but missing "
                    f"quoin/adapters/claude/skills/{name}/preamble.md"
                ),
            })
    elif adapter_preamble.is_file():
        # AD-PX: non-spawn skills must not carry a preamble.md
        violations.append({
            "invariant": AD_PX,
            "skill": name,
            "detail": (
                f"spawn_target=false but preamble.md exists at "
                f"quoin/adapters/claude/skills/{name}/preamble.md"
            ),
        })


# ---------------------------------------------------------------------------
# install.sh checks
# ---------------------------------------------------------------------------

def _name_upper(name: str) -> str:
    """Convert skill name to UPPER_CASE var name (revise-fast → REVISE_FAST)."""
    return name.upper().replace("-", "_")


def check_install_sh(
    name: str,
    install_sh_content: str,
    violations: List[Dict[str, str]],
) -> bool:
    """AD-IV, AD-IE: install.sh has the preflight var and branch for this skill.

    Returns True if AD-IV passed (preflight var found), so check_install_sh_ordering
    knows whether to also check ordering for this skill.
    """
    name_up = _name_upper(name)

    # AD-IV: ADAPTER_<NAME_UPPER>_SRC= assignment exists
    iv_needle = f"ADAPTER_{name_up}_SRC="
    iv_ok = iv_needle in install_sh_content
    if not iv_ok:
        violations.append({
            "invariant": AD_IV,
            "skill": name,
            "detail": f"install.sh missing '{iv_needle}' preflight assignment",
        })

    # AD-IE: if/elif branch exists
    # capture_insight is the lead `if` (Phase 6); all others use `elif`
    ie_pattern = re.compile(
        rf'^\s*(if|elif) \[ "\$skill_name" = "{re.escape(name)}" \]',
        re.MULTILINE,
    )
    if not ie_pattern.search(install_sh_content):
        violations.append({
            "invariant": AD_IE,
            "skill": name,
            "detail": (
                f"install.sh missing if/elif branch for skill '{name}'. "
                f"Note: capture_insight is the lead if-branch (Phase 6 migration)."
            ),
        })

    return iv_ok


def check_install_sh_ordering(
    skills: List[Dict[str, Any]],
    install_sh_content: str,
    skills_with_iv: List[str],
    violations: List[Dict[str, str]],
) -> None:
    """AD-IO: each ADAPTER_<NAME>_SRC= assignment appears before the for-loop start.

    Only checked for skills that passed AD-IV (those in skills_with_iv).
    """
    for_loop_needle = 'for skill_dir in "$SCRIPT_DIR/skills"/*/'
    for_loop_offset = install_sh_content.find(for_loop_needle)
    if for_loop_offset == -1:
        # Cannot locate for-loop — skip ordering check
        return

    for name in skills_with_iv:
        name_up = _name_upper(name)
        iv_needle = f"ADAPTER_{name_up}_SRC="
        preflight_offset = install_sh_content.find(iv_needle)
        if preflight_offset == -1:
            # Should not happen if skills_with_iv is correct, but be safe
            continue
        if preflight_offset > for_loop_offset:
            violations.append({
                "invariant": AD_IO,
                "skill": name,
                "detail": (
                    f"preflight ADAPTER_{name_up}_SRC= at byte offset {preflight_offset} "
                    f"is after for-loop start at byte offset {for_loop_offset}"
                ),
            })


def _install_sh_delegates_to_python_installer(install_sh_content: str) -> bool:
    """Return True when install.sh delegates deployment to `python -m quoin install`.

    This supports the current thin-wrapper installer shape while preserving the
    older per-skill shell-branch checks for synthetic fixtures and legacy
    layouts.
    """
    required_tokens = [
        'INSTALL_ARGS=("install" "--source-dir" "$SCRIPT_DIR")',
        '-m quoin "${INSTALL_ARGS[@]}"',
    ]
    return all(token in install_sh_content for token in required_tokens)


def _python_installer_prefers_claude_adapter(repo_root: Path) -> bool:
    """Return True when src/quoin/installer.py deploys skills from the Claude adapter tree.

    deploy_skills must enumerate source_dir/adapters/claude/skills/ and copy each
    skill folder's SKILL.md from there.
    """
    installer_path = repo_root / "src" / "quoin" / "installer.py"
    try:
        content = installer_path.read_text(encoding="utf-8")
    except OSError:
        return False

    func_start = content.find("def deploy_skills(")
    if func_start == -1:
        return False
    next_def = content.find("\ndef ", func_start + 1)
    func_body = content[func_start:next_def] if next_def != -1 else content[func_start:]

    required_tokens = [
        'src_adapter = source_dir / "adapters" / "claude" / "skills"',
        "for skill_dir in sorted(src_adapter.iterdir()):",
        'skill_md = skill_dir / "SKILL.md"',
    ]
    return all(token in func_body for token in required_tokens)


# ---------------------------------------------------------------------------
# T-02: AD-WR and AD-WD checks
# ---------------------------------------------------------------------------

# Regex to detect a top-level def or class in wrapper text
_TOPLEVEL_DEF_CLASS = re.compile(r"^(def|class)\s", re.MULTILINE)


def check_core_wrapper_parity(
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-WR: every quoin/core/scripts/*.py must have a quoin/scripts/<name>.py wrapper.
    AD-WD: wrappers that shadow a core script must be delegation-only shims.

    AD-WD scope is intentionally restricted to core-matched wrappers only (wrappers
    that have a corresponding core script). Adapter-only scripts in quoin/scripts/
    (analyze_cost_ledger, build_preambles, etc.) are never iterated here.

    Guard: if quoin/core/scripts is not a dir, skip silently so synthetic fixture
    tests with no core/scripts directory still pass.
    """
    core_scripts_dir = repo_root / "quoin" / "core" / "scripts"
    if not core_scripts_dir.is_dir():
        return

    wrapper_dir = repo_root / "quoin" / "scripts"

    for core_script in sorted(core_scripts_dir.iterdir()):
        if not core_script.suffix == ".py":
            continue  # skip __pycache__/, dashboard_assets/, non-.py items
        name = core_script.name

        wrapper_path = wrapper_dir / name

        # AD-WR: wrapper must exist
        if not wrapper_path.is_file():
            violations.append({
                "invariant": AD_WR,
                "skill": name,
                "detail": f"core script {name} has no wrapper in quoin/scripts/",
            })
            continue  # can't check AD-WD if wrapper is absent

        # AD-WD: wrapper must be delegation-only (importlib shim present, no top-level def/class)
        try:
            wrapper_text = wrapper_path.read_text(encoding="utf-8")
        except OSError:
            continue  # can't read wrapper — skip AD-WD for this script

        missing_shim = "spec_from_file_location" not in wrapper_text
        has_toplevel = bool(_TOPLEVEL_DEF_CLASS.search(wrapper_text))

        if missing_shim:
            violations.append({
                "invariant": AD_WD,
                "skill": name,
                "detail": (
                    f"wrapper quoin/scripts/{name} is missing importlib shim "
                    "(spec_from_file_location not found)"
                ),
            })
        if has_toplevel:
            violations.append({
                "invariant": AD_WD,
                "skill": name,
                "detail": (
                    f"wrapper quoin/scripts/{name} defines a top-level def or class "
                    "(must be a delegation-only shim)"
                ),
            })


# ---------------------------------------------------------------------------
# T-03: AD-PK check (pip force-include manifest)
# ---------------------------------------------------------------------------

# Required core-asset keys that must appear in pyproject force-include section
_REQUIRED_FORCE_INCLUDE_KEYS = {
    "quoin/scripts",
    "quoin/core",
    "quoin/adapters/claude",
    "quoin/adapters/codex",
    "quoin/hooks",
    "quoin/memory",
}

# Regex to extract quoted left-hand keys in force-include section lines
_FORCE_INCLUDE_KEY_RE = re.compile(r'^\s*"([^"]+)"\s*=')


def _parse_force_include_keys(pyproject_text: str) -> set:
    """Extract the set of quoted left-hand keys from the force-include section.

    Uses scoped regex (no tomllib) to stay compatible with Python 3.10.
    Locates [tool.hatch.build.targets.wheel.force-include], skips past
    the header line, then collects keys until the next TOML section header.
    """
    header = "[tool.hatch.build.targets.wheel.force-include]"
    lines = pyproject_text.splitlines()

    # Find the header line index
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            header_idx = i
            break

    if header_idx is None:
        return set()

    keys: set = set()
    # Start scanning AFTER the header line
    for line in lines[header_idx + 1:]:
        # Stop at the next TOML section header
        if line.strip().startswith("["):
            break
        m = _FORCE_INCLUDE_KEY_RE.match(line)
        if m:
            keys.add(m.group(1))

    return keys


def check_pip_manifest(
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-PK: pyproject.toml force-include must contain all required core asset dirs.

    Guard: if pyproject.toml is absent, skip silently (keeps synthetic repos clean).
    Only checks presence of the known-required set; never complains about extra keys.
    """
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return

    try:
        pyproject_text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return

    present_keys = _parse_force_include_keys(pyproject_text)

    for required_key in sorted(_REQUIRED_FORCE_INCLUDE_KEYS):
        if required_key not in present_keys:
            violations.append({
                "invariant": AD_PK,
                "skill": required_key,
                "detail": f"pyproject force-include missing core asset dir {required_key}",
            })


# ---------------------------------------------------------------------------
# T-04: AD-CX check (Codex adapter surface)
# ---------------------------------------------------------------------------

def check_codex_surface(
    name: str,
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-CX (per-skill): manifest skill must have quoin/adapters/codex/skills/<name>/README.md.

    Caller is responsible for the codex_enabled guard — this function must only
    be called when quoin/adapters/codex/skills/ is a directory.
    """
    readme_path = repo_root / "quoin" / "adapters" / "codex" / "skills" / name / "README.md"
    if not readme_path.is_file():
        violations.append({
            "invariant": AD_CX,
            "skill": name,
            "detail": f"missing quoin/adapters/codex/skills/{name}/README.md",
        })


def check_codex_manifest(
    repo_root: Path,
    violations: List[Dict[str, str]],
) -> None:
    """AD-CX (manifest): quoin/adapters/codex/feature-manifest.json must exist and be valid JSON.

    Caller is responsible for the codex_enabled guard — this function must only
    be called when quoin/adapters/codex/skills/ is a directory.
    """
    manifest_path = repo_root / "quoin" / "adapters" / "codex" / "feature-manifest.json"
    if not manifest_path.is_file():
        violations.append({
            "invariant": AD_CX,
            "skill": "feature-manifest",
            "detail": "missing quoin/adapters/codex/feature-manifest.json",
        })
        return

    try:
        text = manifest_path.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        violations.append({
            "invariant": AD_CX,
            "skill": "feature-manifest",
            "detail": f"quoin/adapters/codex/feature-manifest.json is not valid JSON: {exc}",
        })


# ---------------------------------------------------------------------------
# Emit violations
# ---------------------------------------------------------------------------

def emit_violations(violations: List[Dict[str, str]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"violations": violations}, indent=2))
    else:
        for v in violations:
            print(
                f"DRIFT {v['invariant']} {v['skill']}: {v['detail']}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate adapter drift across core/, adapters/claude/, and skills/ trees.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to skills.json manifest (default: auto-detected from repo root)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Path to the quoin repo root (default: auto-detected from script location)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit violations as JSON to stdout instead of human-readable lines on stderr",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        sys.exit(64)

    repo_root: Path = args.repo_root if args.repo_root is not None else _default_repo_root()
    manifest_path: Path = args.manifest if args.manifest is not None else _default_manifest(repo_root)

    manifest = load_manifest(manifest_path)

    # Load install.sh once for all per-skill checks
    install_sh_path = repo_root / "quoin" / "install.sh"
    try:
        install_sh_content = install_sh_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"DATA: cannot read install.sh at {install_sh_path}: {exc}", file=sys.stderr)
        sys.exit(65)
    install_routing_delegated = (
        _install_sh_delegates_to_python_installer(install_sh_content)
        and _python_installer_prefers_claude_adapter(repo_root)
    )

    violations: List[Dict[str, str]] = []
    skills_with_iv: List[str] = []  # skills that passed AD-IV (for AD-IO ordering check)

    # AD-CX: compute codex_enabled once — skip all Codex checks when dir is absent
    codex_enabled: bool = (repo_root / "quoin" / "adapters" / "codex" / "skills").is_dir()

    claude_skills = [s for s in manifest["skills"] if s.get("claude_adapter", True)]

    for skill in manifest["skills"]:
        name: str = skill["name"]
        if not skill.get("claude_adapter", True):
            check_no_claude_surface(name, repo_root, violations)
            if codex_enabled:
                check_codex_surface(name, repo_root, violations)
            continue
        claude_model: str = skill["claude_model"]
        section_0: bool = bool(skill.get("section_0", False))
        spawn_target: bool = bool(skill.get("spawn_target", False))

        core_ok, adapter_ok = check_files_exist(name, repo_root, violations)

        if adapter_ok:
            check_frontmatter(name, claude_model, repo_root, violations)
            check_pointer(name, repo_root, violations)
            check_section_0(name, section_0, repo_root, violations)

        check_preamble(name, spawn_target, repo_root, violations)

        if not install_routing_delegated:
            iv_ok = check_install_sh(name, install_sh_content, violations)
            if iv_ok:
                skills_with_iv.append(name)

        # AD-CX: per-skill Codex README check
        if codex_enabled:
            check_codex_surface(name, repo_root, violations)

    if not install_routing_delegated:
        check_install_sh_ordering(
            claude_skills, install_sh_content, skills_with_iv, violations
        )

    # AD-WR / AD-WD: core-to-wrapper parity
    check_core_wrapper_parity(repo_root, violations)

    # AD-PK: pyproject force-include completeness
    check_pip_manifest(repo_root, violations)

    # AD-CX: feature-manifest.json presence and validity
    if codex_enabled:
        check_codex_manifest(repo_root, violations)

    emit_violations(violations, args.json)

    return 2 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
