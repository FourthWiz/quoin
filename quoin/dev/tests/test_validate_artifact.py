"""
Black-box pytest tests for validate_artifact.py.

Tests call the validator via subprocess against synthetic .md fixtures in
quoin/dev/tests/fixtures/. Tests are hermetic: each test passes
--sections-json pointing at the test fixture sidecar (content-identical to
the production sidecar at test creation time).

No imports of validator internals — tests exercise the CLI contract only.
Run: pytest quoin/dev/tests/test_validate_artifact.py -v
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("yaml", reason="pyyaml required: pip install -e '.[dev]'")

# ── Path setup ────────────────────────────────────────────────────────────────

# Resolve from the location of this test file upward to the project root
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(TEST_DIR, 'fixtures')
DEV_DIR = os.path.dirname(TEST_DIR)              # quoin/dev/
QUOIN_DIR = os.path.dirname(DEV_DIR)             # quoin/
CORE_SCRIPTS_DIR = os.path.join(QUOIN_DIR, 'core', 'scripts')
PROJECT_ROOT = os.path.dirname(QUOIN_DIR)        # project root

VALIDATOR = os.path.join(CORE_SCRIPTS_DIR, 'validate_artifact.py')
TEST_SIDECAR = os.path.join(FIXTURES_DIR, 'format-kit.sections.json')


def run_validator(*extra_args, artifact=None, artifact_type=None):
    """Helper: run the validator and return (returncode, stderr_text)."""
    cmd = [sys.executable, VALIDATOR, '--sections-json', TEST_SIDECAR]
    if artifact_type:
        cmd += ['--type', artifact_type]
    cmd += list(extra_args)
    if artifact:
        cmd.append(artifact)
    result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
    return result.returncode, result.stderr.decode('utf-8', errors='replace')


def fixture(name):
    return os.path.join(FIXTURES_DIR, name)


# ── V-01: Frontmatter YAML ────────────────────────────────────────────────────

def test_v01_pass_valid_frontmatter():
    rc, _ = run_validator(artifact=fixture('v01-pass.md'), artifact_type='default')
    assert rc == 0


def test_v01_fail_invalid_frontmatter():
    rc, stderr = run_validator(artifact=fixture('v01-fail-bad-yaml.md'), artifact_type='default')
    assert rc == 1
    assert 'FAIL V-01' in stderr


def test_v01_pass_no_frontmatter():
    rc, stderr = run_validator(artifact=fixture('v01-pass-no-frontmatter.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-01' not in stderr


# ── V-02: Allowed sections ────────────────────────────────────────────────────

def test_v02_pass_allowed_sections():
    rc, _ = run_validator(artifact=fixture('v02-pass.md'), artifact_type='current-plan')
    assert rc == 0


def test_v02_fail_unknown_section():
    rc, stderr = run_validator(artifact=fixture('v02-fail.md'), artifact_type='current-plan')
    assert rc == 1
    assert 'FAIL V-02' in stderr
    assert '## Made-Up Section' in stderr


# ── V-03: Table separator ─────────────────────────────────────────────────────

def test_v03_pass_well_formed_table():
    rc, _ = run_validator(artifact=fixture('v03-pass.md'), artifact_type='default')
    assert rc == 0


def test_v03_fail_missing_separator():
    rc, stderr = run_validator(artifact=fixture('v03-fail.md'), artifact_type='default')
    assert rc == 1
    assert 'FAIL V-03' in stderr
    assert 'table missing header separator' in stderr


# ── V-04: XML tag balance ─────────────────────────────────────────────────────

def test_v04_pass_balanced_xml():
    rc, _ = run_validator(artifact=fixture('v04-pass.md'), artifact_type='default')
    assert rc == 0


def test_v04_fail_orphan_open():
    rc, stderr = run_validator(artifact=fixture('v04-fail-open.md'), artifact_type='default')
    assert rc == 1
    assert 'FAIL V-04' in stderr
    assert '<verdict>' in stderr or 'verdict' in stderr


def test_v04_fail_orphan_close():
    rc, stderr = run_validator(artifact=fixture('v04-fail-close.md'), artifact_type='default')
    assert rc == 1
    assert 'FAIL V-04' in stderr
    assert 'verdict' in stderr


def test_v04_pass_inline_code():
    """V-04 must NOT fire on XML-looking strings inside backtick inline-code spans."""
    rc, stderr = run_validator(artifact=fixture('v04-pass-inline-code.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-04' not in stderr


# ── V-05: ID references resolve ───────────────────────────────────────────────

def test_v05_pass_resolved_ids():
    rc, _ = run_validator(artifact=fixture('v05-pass.md'), artifact_type='default')
    assert rc == 0


def test_v05_pass_table_defined_ids():
    """IDs defined as markdown table rows must resolve."""
    rc, stderr = run_validator(artifact=fixture('v05-pass-table.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-05' not in stderr


def test_v05_pass_bullet_defined_ids():
    """IDs defined as bullet list items must resolve."""
    rc, stderr = run_validator(artifact=fixture('v05-pass-bullet.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-05' not in stderr


def test_v05_pass_heading_defined_ids():
    """IDs defined as ### heading lines must resolve."""
    rc, stderr = run_validator(artifact=fixture('v05-pass-heading.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-05' not in stderr


def test_v05_pass_numbered_defined_ids():
    """IDs defined as numbered-list items (1. T-01: …) must resolve."""
    rc, stderr = run_validator(artifact=fixture('v05-pass-numbered.md'), artifact_type='default')
    assert rc == 0
    assert 'FAIL V-05' not in stderr


def test_v05_pass_self_ref_on_def_line():
    """Cross-refs on def lines are skipped (interpretation (a) per MIN-2-R2)."""
    rc, stderr = run_validator(
        artifact=fixture('v05-pass-self-ref-on-def-line.md'), artifact_type='default'
    )
    assert rc == 0
    assert 'FAIL V-05' not in stderr


def test_v05_fail_undefined_ref():
    rc, stderr = run_validator(artifact=fixture('v05-fail.md'), artifact_type='default')
    assert rc == 1
    assert 'FAIL V-05' in stderr
    assert 'T-99' in stderr


# ── V-05: status-glyph definition matching (Stage 4 T-02) ────────────────────

def test_v05_def_re_matches_check_glyph():
    """✓ glyph between list-marker and T-NN must be a valid def."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.V05_DEF_RE.match('1. ✓ T-01: completed')
    assert m is not None
    assert m.group(1) == 'T-01'


def test_v05_def_re_matches_cross_glyph():
    """✗ glyph between list-marker and T-NN must be a valid def."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.V05_DEF_RE.match('2. ✗ T-02: failed')
    assert m is not None
    assert m.group(1) == 'T-02'


def test_v05_def_re_matches_hourglass_glyph():
    """⏳ glyph between list-marker and T-NN must be a valid def."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.V05_DEF_RE.match('3. ⏳ T-03: pending')
    assert m is not None
    assert m.group(1) == 'T-03'


def test_v05_def_re_matches_blocked_glyph():
    """🚫 glyph between list-marker and T-NN must be a valid def."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.V05_DEF_RE.match('4. 🚫 T-04: blocked')
    assert m is not None
    assert m.group(1) == 'T-04'


def test_v05_def_re_does_not_match_ascii_glyph_substitute():
    """☑ (out-of-scope ASCII variant) must NOT match as a glyph def."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod.V05_DEF_RE.match('5. ☑ T-05: substituted')
    assert m is None


def test_v05_round_trip_with_canonical_glyphs():
    """current-plan with '1. ⏳ T-01:' form must pass V-05 end-to-end."""
    rc, stderr = run_validator(
        artifact=fixture('v05-pass-glyph-roundtrip.md'), artifact_type='current-plan'
    )
    assert rc == 0, f'Expected V-05 pass; stderr: {stderr}'
    assert 'FAIL V-05' not in stderr


# ── V-05: backtick cross-artifact exemption (IVG-78) ─────────────────────────

def test_v05_pass_backtick_crossref():
    """Backtick-quoted IDs with no local definition must be exempt from V-05."""
    rc, stderr = run_validator(
        artifact=fixture('v05-pass-backtick-crossref.md'), artifact_type='default'
    )
    assert rc == 0, f'Expected V-05 pass; stderr: {stderr}'
    assert 'FAIL V-05' not in stderr


def test_v05_mixed_backtick_and_bare_selective():
    """Selectivity: backtick-quoted IDs exempt; bare undefined IDs still fail.

    Also covers the adjacent-span case: an unrelated backtick span on the same
    line must not shield a bare undefined ID on that line.
    """
    rc, stderr = run_validator(
        artifact=fixture('v05-mixed-backtick-and-bare.md'), artifact_type='default'
    )
    assert rc == 1, f'Expected V-05 fail; stderr: {stderr}'
    assert 'FAIL V-05' in stderr
    # Bare sentinel tokens must appear in failure output
    assert 'T-97' in stderr, f'Expected bare T-97 in stderr; got: {stderr}'
    assert 'T-96' in stderr, f'Expected bare T-96 (adjacent-span case) in stderr; got: {stderr}'
    # Backtick-quoted exempt token must NOT appear in any FAIL V-05 line
    for line in stderr.splitlines():
        if 'FAIL V-05' in line:
            assert 'T-88' not in line, (
                f'Backtick-quoted T-88 must not be flagged; found in: {line}'
            )


def test_v05_def_re_does_not_match_backticked_id():
    """V05_DEF_RE must not treat a backticked ID as a local definition (D-03).

    Guards against a future widening of the leading char class that would
    accidentally treat backticked IDs as defs, silently voiding V-05 for them.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Bullet form with backtick-wrapped ID — must NOT match as definition
    m_bullet = mod.V05_DEF_RE.match('- `T-05`: a cross-artifact ref, not a def')
    assert m_bullet is None, 'V05_DEF_RE must not match a backtick-wrapped ID as a def'
    # Bare backtick-only line — also must not match
    m_bare = mod.V05_DEF_RE.match('`T-05`')
    assert m_bare is None, 'V05_DEF_RE must not match a bare backtick-ID line as a def'


# ── V-06: ## For human section ────────────────────────────────────────────────

def test_v06_pass_class_b_with_summary():
    rc, _ = run_validator(artifact=fixture('v06-pass.md'), artifact_type='current-plan')
    assert rc == 0


def test_v06_fail_class_b_missing_summary():
    rc, stderr = run_validator(artifact=fixture('v06-fail-missing.md'), artifact_type='current-plan')
    assert rc == 1
    assert 'FAIL V-06' in stderr
    assert '## For human' in stderr


def test_v06_fail_class_b_summary_too_long():
    rc, stderr = run_validator(artifact=fixture('v06-fail-long.md'), artifact_type='current-plan')
    assert rc == 1
    assert 'FAIL V-06' in stderr
    assert 'exceeds 12 non-blank lines' in stderr


def test_v06_skip_class_a():
    """Class A artifacts must not be checked for ## For human."""
    rc, stderr = run_validator(artifact=fixture('v06-class-a.md'), artifact_type='critic-response')
    assert rc == 0
    assert 'FAIL V-06' not in stderr


# ── V-07: Required sections present ──────────────────────────────────────────

def test_v07_pass_required_sections_present():
    rc, _ = run_validator(artifact=fixture('v07-pass.md'), artifact_type='current-plan')
    assert rc == 0


def test_v07_fail_missing_required_section():
    rc, stderr = run_validator(artifact=fixture('v07-fail.md'), artifact_type='current-plan')
    assert rc == 1
    assert 'FAIL V-07' in stderr
    assert '## Tasks' in stderr


# ── Invocation errors ─────────────────────────────────────────────────────────

def test_invocation_error_no_file():
    rc, stderr = run_validator(artifact='/tmp/missing-fixture-file-9999.md', artifact_type='default')
    assert rc == 2
    assert 'file not found' in stderr.lower() or 'not found' in stderr.lower()


# ── Multiple failures reported ────────────────────────────────────────────────

def test_multiple_failures_reported():
    """Both V-02 (unknown section) and V-05 (unresolved ref) must appear in stderr."""
    rc, stderr = run_validator(artifact=fixture('multi-fail.md'), artifact_type='current-plan')
    assert rc == 1
    assert 'FAIL V-02' in stderr
    assert 'FAIL V-05' in stderr


# ── T-10: current-plan v2/v3 fixture tests ────────────────────────────────────

def test_v3_current_plan_fixture_passes():
    """v3-current-plan.md must pass all validator checks (auto-detected as current-plan)."""
    rc, stderr = run_validator(artifact=fixture('v3-current-plan.md'))
    assert rc == 0, f"v3 fixture failed:\n{stderr}"


def test_v2_current_plan_fixture_fails_v02_v06_v07():
    """current-plan-v2.md must fail V-02, V-06, and V-07 (auto-detected as current-plan)."""
    rc, stderr = run_validator(artifact=fixture('current-plan-v2.md'))
    assert rc == 1
    assert 'FAIL V-02' in stderr, f"Expected V-02 in:\n{stderr}"
    assert 'FAIL V-06' in stderr, f"Expected V-06 in:\n{stderr}"
    assert 'FAIL V-07' in stderr, f"Expected V-07 in:\n{stderr}"


def test_v3_current_plan_summary_block_position():
    """## For human must appear within the first 50 lines after frontmatter."""
    import pathlib
    content = pathlib.Path(fixture('v3-current-plan.md')).read_text()
    lines = content.splitlines()
    # Skip frontmatter (between first and second ---)
    in_frontmatter = False
    body_start = 0
    fence_count = 0
    for i, line in enumerate(lines):
        if line.strip() == '---':
            fence_count += 1
            if fence_count == 2:
                body_start = i + 1
                break
    body_lines = lines[body_start:]
    for_human_pos = next(
        (i for i, l in enumerate(body_lines) if l.strip() == '## For human'),
        None
    )
    assert for_human_pos is not None, "## For human heading not found"
    assert for_human_pos < 50, (
        f"## For human appears at body line {for_human_pos} (must be < 50)"
    )


def test_v3_current_plan_summary_length_at_boundary():
    """Exactly 12 non-blank summary lines PASS; 13 non-blank lines FAIL V-06."""
    rc12, stderr12 = run_validator(artifact=fixture('current-plan-summary-12.md'))
    assert rc12 == 0, f"12-line summary should pass:\n{stderr12}"

    rc13, stderr13 = run_validator(artifact=fixture('current-plan-summary-13.md'))
    assert rc13 == 1, "13-line summary should fail V-06"
    assert 'FAIL V-06' in stderr13
    assert 'exceeds 12 non-blank lines' in stderr13


def test_assembled_file_no_duplicate_for_human_heading():
    """A correctly assembled v3 file has exactly one ## For human heading.
    The body.tmp has zero. This confirms the §5.3 Step 3(a) dedup is needed."""
    import pathlib, re
    v3 = pathlib.Path(fixture('v3-current-plan.md')).read_text()
    body = pathlib.Path(fixture('v3-current-plan.body.tmp.md')).read_text()

    pattern = re.compile(r'^## For human\s*$', re.MULTILINE)
    v3_count = len(pattern.findall(v3))
    body_count = len(pattern.findall(body))

    assert v3_count == 1, (
        f"Assembled v3 file must have exactly one '## For human', found {v3_count}"
    )
    assert body_count == 0, (
        f"Body.tmp must have zero '## For human' headings (it has {body_count}); "
        "Step 3(a) dedup is critical to prevent duplication on assembly"
    )


# ── T-06: architecture and review v2/v3 fixture round-trip tests ─────────────

def test_v3_architecture_fixture_passes():
    """architecture-v3.md must pass all validator checks (auto-detected as architecture)."""
    rc, stderr = run_validator(artifact=fixture('architecture-v3.md'))
    assert rc == 0, f"v3 architecture fixture failed:\n{stderr}"


def test_v2_architecture_fixture_fails():
    """architecture-v2.md must fail V-02, V-06, and V-07 (auto-detected as architecture).

    Failure set empirically captured during T-02 implementation via:
      python3 ~/.claude/scripts/validate_artifact.py architecture-v2.md
    """
    rc, stderr = run_validator(artifact=fixture('architecture-v2.md'))
    assert rc == 1
    # V-02: three disallowed headings (## Overview, ## Architecture, ## Risks)
    assert 'FAIL V-02: heading "## Overview" not in allowed set' in stderr
    assert 'FAIL V-02: heading "## Architecture" not in allowed set' in stderr
    assert 'FAIL V-02: heading "## Risks" not in allowed set' in stderr
    # V-06: ## For human missing
    assert 'FAIL V-06' in stderr
    # V-07: all six required sections absent
    assert 'FAIL V-07: required section "## For human" missing' in stderr
    assert 'FAIL V-07: required section "## Context" missing' in stderr
    assert 'FAIL V-07: required section "## Current state" missing' in stderr
    assert 'FAIL V-07: required section "## Proposed architecture" missing' in stderr
    assert 'FAIL V-07: required section "## Risk register" missing' in stderr
    assert 'FAIL V-07: required section "## Stage decomposition" missing' in stderr


def test_v3_review_fixture_passes():
    """review-v3-sample.md must pass all validator checks (auto-detected as review)."""
    rc, stderr = run_validator(artifact=fixture('review-v3-sample.md'))
    assert rc == 0, f"v3 review fixture failed:\n{stderr}"


def test_v2_review_fixture_fails():
    """review-v2-sample.md must fail V-02, V-06, and V-07 (auto-detected as review).

    Failure set empirically captured during T-03 implementation.
    """
    rc, stderr = run_validator(artifact=fixture('review-v2-sample.md'))
    assert rc == 1
    # V-02: three disallowed headings
    assert 'FAIL V-02: heading "## Overview" not in allowed set' in stderr
    assert 'FAIL V-02: heading "## Approval" not in allowed set' in stderr
    assert 'FAIL V-02: heading "## Comments" not in allowed set' in stderr
    # V-06: ## For human missing
    assert 'FAIL V-06' in stderr
    # V-07: all eight required sections absent
    assert 'FAIL V-07: required section "## For human" missing' in stderr
    assert 'FAIL V-07: required section "## Summary" missing' in stderr
    assert 'FAIL V-07: required section "## Verdict" missing' in stderr
    assert 'FAIL V-07: required section "## Plan Compliance" missing' in stderr
    assert 'FAIL V-07: required section "## Issues Found" missing' in stderr
    assert 'FAIL V-07: required section "## Integration Safety" missing' in stderr
    assert 'FAIL V-07: required section "## Test Coverage" missing' in stderr
    assert 'FAIL V-07: required section "## Risk Assessment" missing' in stderr


def test_architecture_allowed_section_set_matches_sidecar():
    """Guard: architecture allowed_sections in the test sidecar matches the expected set.

    Detects accidental sidecar edits that silently expand/shrink the allowed set.
    Empirically frozen at T-06 implementation time from format-kit.sections.json.
    """
    import json
    with open(TEST_SIDECAR) as f:
        data = json.load(f)
    arch = data['artifact_types']['architecture']
    expected_allowed = [
        '## For human',
        '## Context',
        '## Current state',
        '## Proposed architecture',
        '## Integration analysis',
        '## Risk register',
        '## De-risking strategy',
        '## Stage decomposition',
        '## Stage Summary Table',
        '## Next Steps',
        '## Open questions',
        '## Appendix',
        '## Revision history',
    ]
    assert arch['allowed_sections'] == expected_allowed, (
        f"architecture allowed_sections drifted from expected:\n"
        f"  expected: {expected_allowed}\n"
        f"  actual:   {arch['allowed_sections']}"
    )


def test_review_allowed_section_set_matches_sidecar():
    """Guard: review allowed_sections in the test sidecar matches the expected set."""
    import json
    with open(TEST_SIDECAR) as f:
        data = json.load(f)
    review = data['artifact_types']['review']
    expected_allowed = [
        '## For human',
        '## Summary',
        '## Verdict',
        '## Plan Compliance',
        '## Spec Compliance',
        '## Issues Found',
        '## Integration Safety',
        '## Test Coverage',
        '## Risk Assessment',
        '## Recommendations',
        '## Dimension Verdicts',
    ]
    assert review['allowed_sections'] == expected_allowed, (
        f"review allowed_sections drifted from expected:\n"
        f"  expected: {expected_allowed}\n"
        f"  actual:   {review['allowed_sections']}"
    )


# ── T-03: ## Convergence Summary in current-plan allowed_sections ─────────────

def test_convergence_summary_in_current_plan_allowed_sections():
    """'## Convergence Summary' must appear in current-plan allowed_sections in the shipped sidecar."""
    import json
    sidecar = os.path.join(os.path.dirname(__file__), '..', '..', 'memory', 'format-kit.sections.json')
    with open(sidecar) as f:
        d = json.load(f)
    allowed = d['artifact_types']['current-plan']['allowed_sections']
    assert '## Convergence Summary' in allowed, (
        f"'## Convergence Summary' not in current-plan allowed_sections: {allowed}"
    )


def test_convergence_summary_heading_passes_validator():
    """current-plan fixture with ## Convergence Summary section must pass V-02."""
    rc, stderr = run_validator(
        artifact=fixture('v03-convergence-summary-pass.md'), artifact_type='current-plan'
    )
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL V-02' not in stderr


# ── IVG-140: V-06 vs. Convergence Summary ordering ─────────────────────────────

def test_v06_fail_convergence_summary_before_for_human():
    """current-plan fixture with ## Convergence Summary BEFORE ## For human must FAIL V-06 only.

    The fixture retains all required current-plan sections (## For human, ## State,
    ## Tasks, ## Risks), so the only tripped invariant is V-06 (ordering), not V-07
    (missing sections) — isolating the ordering signal from a confounding failure.
    """
    rc, stderr = run_validator(
        artifact=fixture('v06-convergence-first-fail.md'), artifact_type='current-plan'
    )
    assert rc == 1, f'Expected validator failure; stderr: {stderr}'
    assert 'FAIL V-06' in stderr
    assert 'FAIL V-07' not in stderr


def test_v06_pass_convergence_summary_after_for_human():
    """current-plan fixture with ## Convergence Summary AFTER ## For human must NOT FAIL V-06."""
    rc, stderr = run_validator(
        artifact=fixture('v03-convergence-summary-pass.md'), artifact_type='current-plan'
    )
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL V-06' not in stderr


# ── T-08: detect_type ordering — architecture-critic- before architecture- ─────

def test_detect_type_architecture_critic_returns_critic_response():
    """architecture-critic-1.md must resolve to critic-response, not architecture."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.detect_type('architecture-critic-1.md', None) == 'critic-response'
    assert mod.detect_type('architecture.md', None) == 'architecture'


# ── IVG-128 T-12: security-review-N.md type + Dimension Verdicts fixtures ──────

def test_detect_type_security_review_returns_security_review():
    """security-review-1.md must resolve to 'security-review', not 'review'."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.detect_type('security-review-1.md', None) == 'security-review'
    assert mod.detect_type('review-1.md', None) == 'review'


def test_v3_security_review_fixture_passes():
    """security-review-v3-sample.md (auto-detected as security-review) must pass
    all V-07 required sections per format-kit.sections.json's security-review type,
    using the pinned APPROVED|CHANGES_REQUESTED|BLOCKED verdict enum shared with
    review-N.md.
    """
    rc, stderr = run_validator(artifact=fixture('security-review-v3-sample.md'))
    assert rc == 0, f"security-review fixture failed:\n{stderr}"


def test_v3_review_dimension_verdicts_fixture_passes():
    """A merged review-N.md fixture WITH ## Dimension Verdicts (Large fan-out
    shape) must pass V-02 (allowed) and V-07 (required), with every required
    section populated per T-08's ownership mapping.
    """
    rc, stderr = run_validator(artifact=fixture('review-v3-dimension-verdicts-sample.md'))
    assert rc == 0, f"merged review-with-dimension-verdicts fixture failed:\n{stderr}"


def test_v3_review_fixture_without_dimension_verdicts_still_passes():
    """A Small single-pass review-N.md WITHOUT ## Dimension Verdicts must still
    pass — the section is OPTIONAL, not required (byte-path-identical Small flow).
    """
    rc, stderr = run_validator(artifact=fixture('review-v3-sample.md'))
    assert rc == 0, f"pre-existing Small-shape review fixture regressed:\n{stderr}"
    assert '## Dimension Verdicts' not in open(fixture('review-v3-sample.md')).read()


# ── T-17: Class A round-trip fixture tests ─────────────────────────────────────

def test_v3_critic_response_fixture_passes():
    """critic-response-v3.md with all required sections must pass validator."""
    rc, stderr = run_validator(artifact=fixture('critic-response-v3.md'))
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL' not in stderr


def test_v2_critic_response_fixture_fails():
    """critic-response-v2.md missing required sections must fail V-07."""
    rc, stderr = run_validator(artifact=fixture('critic-response-v2.md'))
    assert rc == 1, f'Expected validator failure; stderr: {stderr}'
    assert 'FAIL V-07' in stderr
    assert '## Verdict' in stderr


def test_v3_session_fixture_passes():
    """v3 session fixture with all required sections including ## Cost must pass."""
    rc, stderr = run_validator(artifact=fixture('session/2026-04-25-stage4-fixture.md'))
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL' not in stderr


def test_v2_session_fixture_fails():
    """v2 session fixture missing ## Cost must fail V-07."""
    rc, stderr = run_validator(artifact=fixture('session/2026-04-25-stage4-fixture-v2.md'))
    assert rc == 1, f'Expected validator failure; stderr: {stderr}'
    assert 'FAIL V-07' in stderr
    assert '## Cost' in stderr


def test_v3_gate_fixture_passes():
    """gate-implement fixture with ## Automated checks + ## Verdict must pass."""
    rc, stderr = run_validator(artifact=fixture('gate-implement-2026-04-25.md'))
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL' not in stderr


def test_v2_gate_fixture_fails():
    """gate-implement v2 fixture missing required sections must fail V-07."""
    rc, stderr = run_validator(artifact=fixture('gate-implement-2026-04-25-v2.md'))
    assert rc == 1, f'Expected validator failure; stderr: {stderr}'
    assert 'FAIL V-07' in stderr


def test_architecture_overview_fixture_passes():
    """architecture-overview-fixture.md with known heading set must pass — CRIT-1 guard."""
    rc, stderr = run_validator(artifact=fixture('architecture-overview-fixture.md'))
    assert rc == 0, f'Expected validator pass (CRIT-1 regression guard); stderr: {stderr}'
    assert 'FAIL V-02' not in stderr


def test_dependencies_map_fixture_passes():
    """dependencies-map-fixture.md with known heading set must pass — CRIT-1 guard."""
    rc, stderr = run_validator(artifact=fixture('dependencies-map-fixture.md'))
    assert rc == 0, f'Expected validator pass (CRIT-1 regression guard); stderr: {stderr}'
    assert 'FAIL V-02' not in stderr


# ── V-07 prefix-match (heading-line-form) ─────────────────────────────────────
# Dedicated coverage for the heading-line-form prefix-match logic added during
# the fixture-test task (see review-1.md MAJ-2). The check_v07 function accepts
# `## Verdict: PASS` as satisfying required `## Verdict` because critic-response
# format-kit specifies the heading-line-form `## Verdict: PASS | REVISE`.

def _load_validator_module():
    """Helper: import validate_artifact.py as a module for direct function tests."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('validate_artifact', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v07_prefix_match_heading_line_form_satisfies_required():
    """`## Verdict: PASS` heading must satisfy required `## Verdict` (heading-line-form)."""
    mod = _load_validator_module()
    failures = []
    text = (
        '---\nfoo: bar\n---\n\n'
        '## Verdict: PASS\n\n'
        'body content\n'
    )
    mod.check_v07(text, ['## Verdict'], failures)
    assert failures == [], (
        f'## Verdict: PASS should satisfy required ## Verdict via prefix-match; '
        f'got: {failures}'
    )


def test_v07_prefix_match_exact_heading_satisfies_required():
    """`## Verdict` (exact) heading must satisfy required `## Verdict`."""
    mod = _load_validator_module()
    failures = []
    text = (
        '---\nfoo: bar\n---\n\n'
        '## Verdict\n\n'
        'PASS\n'
    )
    mod.check_v07(text, ['## Verdict'], failures)
    assert failures == [], (
        f'## Verdict (exact) should satisfy required ## Verdict; got: {failures}'
    )


def test_v07_prefix_match_rejects_heading_without_separator():
    """`## Verdictian` must NOT satisfy required `## Verdict` (no `:` or ` ` separator)."""
    mod = _load_validator_module()
    failures = []
    text = (
        '---\nfoo: bar\n---\n\n'
        '## Verdictian\n\n'
        'body\n'
    )
    mod.check_v07(text, ['## Verdict'], failures)
    assert len(failures) == 1, (
        f'## Verdictian should NOT satisfy ## Verdict (no separator); '
        f'expected 1 V-07 failure, got: {failures}'
    )
    assert 'FAIL V-07' in failures[0]
    assert '## Verdict' in failures[0]


def test_v07_prefix_match_with_space_separator():
    """`## Verdict REVISE` (space-separator heading) must satisfy required `## Verdict`."""
    mod = _load_validator_module()
    failures = []
    text = (
        '---\nfoo: bar\n---\n\n'
        '## Verdict REVISE\n\n'
        'body\n'
    )
    mod.check_v07(text, ['## Verdict'], failures)
    assert failures == [], (
        f'## Verdict REVISE should satisfy required ## Verdict via space-separator '
        f'prefix-match; got: {failures}'
    )


# ── T-11 / MAJ-4: validate_artifact.py uses __QUOIN_HOME__ for deploy-time substitution ─────


# ── T-06 (specify-skill stage 1): spec artifact type ──────────────────────────
# Class A — no ## For human — shared-minimal required_sections cover both the
# per-task feature-spec and repo-root project-spec variants. See T-01..T-03 in
# .workflow_artifacts/specify-skill/stage-1/current-plan.md.

def test_spec_task_valid_fixture_autodetects_and_passes():
    """spec-task-valid.md auto-detects as `spec` (no --type) and passes validation."""
    rc, stderr = run_validator(artifact=fixture('spec-task-valid.md'))
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL' not in stderr


def test_spec_repo_valid_fixture_autodetects_and_passes():
    """spec-repo-valid.md auto-detects as `spec` (no --type) and passes validation."""
    rc, stderr = run_validator(artifact=fixture('spec-repo-valid.md'))
    assert rc == 0, f'Expected validator pass; stderr: {stderr}'
    assert 'FAIL' not in stderr


def test_spec_missing_acceptance_fails_v07():
    """spec-missing-acceptance.md (no ## Acceptance criteria) must FAIL V-07."""
    rc, stderr = run_validator(artifact=fixture('spec-missing-acceptance.md'))
    assert rc == 1
    assert 'FAIL V-07' in stderr
    assert '## Acceptance criteria' in stderr


def test_spec_valid_fixture_has_no_v06_failure():
    """A valid spec (no ## For human) must NOT fail V-06 — proves Class A (V-06 skipped)."""
    rc, stderr = run_validator(artifact=fixture('spec-task-valid.md'))
    assert rc == 0
    assert 'FAIL V-06' not in stderr


def test_check_v06_class_a_spec_leaves_failures_empty():
    """Direct unit: check_v06 on Class A text (no ## For human) must not append failures."""
    mod = _load_validator_module()
    failures = []
    text = (
        '---\ntask: sample\n---\n\n'
        '## Context\nbody\n\n'
        '## Acceptance criteria\n- a bullet\n'
    )
    mod.check_v06(text, 'A', failures)
    assert failures == [], (
        f'check_v06 must skip Class A artifacts entirely; got: {failures}'
    )


def test_detect_type_spec_cases():
    """detect_type must route spec.md (any parent dir) to `spec`, and leave existing
    types (current-plan, architecture) unaffected — per T-01 acceptance criteria."""
    mod = _load_validator_module()
    assert mod.detect_type('spec.md', None) == 'spec'
    assert mod.detect_type('sample-task/spec.md', None) == 'spec'
    assert mod.detect_type('.workflow_artifacts/spec.md', None) == 'spec'
    assert mod.detect_type('current-plan.md', None) == 'current-plan'
    assert mod.detect_type('architecture.md', None) == 'architecture'


def test_grandfather_task_without_spec_no_error():
    """A task with a valid current-plan.md and no spec.md must never error — the
    spec type is opt-in per-file, never a resolver/validator requirement (R-03/R-09)."""
    import shutil
    import tempfile

    tmp_root = tempfile.mkdtemp(prefix='quoin-spec-grandfather-')
    try:
        task_dir = os.path.join(tmp_root, '.workflow_artifacts', 'legacy-task')
        os.makedirs(task_dir)
        plan_path = os.path.join(task_dir, 'current-plan.md')
        shutil.copyfile(fixture('v3-current-plan.md'), plan_path)

        # (i) validating the existing current-plan.md still passes
        rc, stderr = run_validator(artifact=plan_path)
        assert rc == 0, f'Expected validator pass on legacy current-plan.md; stderr: {stderr}'

        # (ii) detect_type never routes current-plan.md to `spec`
        mod = _load_validator_module()
        assert mod.detect_type(plan_path, None) == 'current-plan'

        # (iii) no spec.md present, and nothing raises when checking for it
        spec_path = os.path.join(task_dir, 'spec.md')
        assert os.path.exists(spec_path) is False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
