"""
test_critic_response_class_field.py — Verify per-issue `Class:` sub-bullet
parsing (T-05 Edit 1 schema; review-1 MAJ-3).

Two cases:
1. Critic response WITH `- Class:` sub-bullets on CRIT/MAJ → classifier
   populates Issue.class_field and surface_source becomes 'class-field'.
2. Critic response WITHOUT `- Class:` sub-bullets → class_field stays None
   and surface_source falls back to 'title-keyword' or 'default-structural'.
"""
import os
import sys
import textwrap

import pytest

# Make the runtime script importable
SCRIPT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
)
sys.path.insert(0, SCRIPT_DIR)
import classify_critic_issues  # noqa: E402

FIXTURE_WITH_CLASS = os.path.join(
    os.path.dirname(__file__),
    'fixtures', 'critic_response_class_field', 'critic-response-with-class.md',
)


def test_class_field_populated_when_present():
    """Every CRIT/MAJ issue with `- Class:` sub-bullet has class_field set."""
    issues = classify_critic_issues.parse_critic_response(FIXTURE_WITH_CLASS)

    crit_maj = [i for i in issues if i.severity in ('CRITICAL', 'MAJOR')]
    assert len(crit_maj) == 4, (
        f'expected 4 CRIT+MAJ issues in fixture, got {len(crit_maj)}'
    )

    expected = {
        'CRIT-1': 'enumeration',
        'CRIT-2': 'regex-breadth',
        'MAJ-1': 'integration',
        'MAJ-2': 'testability',
    }
    for issue in crit_maj:
        sev_prefix = 'CRIT' if issue.severity == 'CRITICAL' else 'MAJ'
        key = f'{sev_prefix}-{issue.id}'
        assert key in expected, f'unexpected issue id: {key}'
        assert issue.class_field == expected[key], (
            f'{key}: expected class_field={expected[key]!r}, '
            f'got {issue.class_field!r}'
        )
        assert issue.surface_source == 'class-field', (
            f'{key}: expected surface_source=class-field, '
            f'got {issue.surface_source}'
        )
        assert issue.surface_family == expected[key], (
            f'{key}: expected surface_family={expected[key]!r}, '
            f'got {issue.surface_family!r}'
        )


def test_minor_issue_without_class_field_falls_through(tmp_path):
    """MIN-1 in the fixture has no Class: line — class_field stays None."""
    issues = classify_critic_issues.parse_critic_response(FIXTURE_WITH_CLASS)
    minors = [i for i in issues if i.severity == 'MINOR']
    assert len(minors) == 1
    assert minors[0].class_field is None
    assert minors[0].surface_source in ('title-keyword', 'default-structural')


def test_class_field_absent_falls_through_to_title_keyword(tmp_path):
    """Critic response without any `- Class:` lines: surface inference falls
    back to title-keyword match or default-structural."""
    body = textwrap.dedent("""\
        ## Issues

        ### Critical

        - **[CRIT-1] Widen the audit regex to cover stage-3 alternation**
          - Body: regex coverage gap noted by reviewer.
          - Suggestion: extend the regex.

        - **[CRIT-2] Plan misses cross-repo deployment ordering**
          - Body: The deployment ordering between services is undefined.
          - Suggestion: Document explicitly.
    """)
    fp = tmp_path / 'critic-response-no-class.md'
    fp.write_text(body, encoding='utf-8')

    issues = classify_critic_issues.parse_critic_response(str(fp))
    by_id = {i.id: i for i in issues}

    # CRIT-1 should hit a title keyword (regex-breadth or audit-method via 'audit'/'regex')
    crit1 = by_id['1']
    assert crit1.class_field is None
    assert crit1.surface_source == 'title-keyword', (
        f'CRIT-1: expected title-keyword fallback, got {crit1.surface_source}'
    )

    # CRIT-2 has no recognised keyword → defaults to structural-fallback
    crit2 = by_id['2']
    assert crit2.class_field is None
    assert crit2.surface_source == 'default-structural'
    assert crit2.surface_family == 'structural-fallback'


def test_fixture_passes_v01_v07():
    """The class-field fixture must pass all validate_artifact.py checks (V-01 through V-07)."""
    import subprocess
    result = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), '..', '..', 'core', 'scripts', 'validate_artifact.py'), FIXTURE_WITH_CLASS],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f'validate_artifact.py reported failures for fixture:\n'
        f'stdout: {result.stdout}\n'
        f'stderr: {result.stderr}'
    )


def test_invalid_class_value_normalized_to_other(tmp_path):
    """A `Class:` value not in the whitelist is normalized to 'other'."""
    body = textwrap.dedent("""\
        ## Issues

        ### Critical

        - **[CRIT-1] Some issue title**
          - Body: details.
          - Class: bogus-value-not-in-whitelist
    """)
    fp = tmp_path / 'critic-response-bad-class.md'
    fp.write_text(body, encoding='utf-8')

    issues = classify_critic_issues.parse_critic_response(str(fp))
    assert len(issues) == 1
    assert issues[0].class_field == 'bogus-value-not-in-whitelist'
    assert issues[0].surface_family == 'other'
    assert issues[0].surface_source == 'class-field'
