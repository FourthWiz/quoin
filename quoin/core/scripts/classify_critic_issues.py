#!/usr/bin/env python3
"""
classify_critic_issues.py — Deterministic critic-issue classifier for the
pipeline-efficiency-improvements Stage 1 critic-loop intelligence layer.

Parses a critic-response Markdown file (Class A format), classifies each
CRITICAL and MAJOR issue as structural or mechanical, and optionally decides
whether the orchestrator may short-circuit the critic loop (BAIL-TO-IMPLEMENT)
when only mechanical issues remain and a canary precondition holds.

Usage:
  python3 classify_critic_issues.py \\
      --critic-response <path-to-critic-response.md> \\
      [--plan <path-to-plan.md>] \\
      [--enable-bailout]

Output (stdout):
  Line 1: verdict token — BAIL-TO-IMPLEMENT | CONTINUE-LOOP
  Line 2+: JSON array of classified issues

Exit codes:
  0 — success (verdict emitted)
  1 — parse error (malformed critic-response or unreadable file)
  2 — missing required argument

Bailout decision logic:
  Emits BAIL-TO-IMPLEMENT IFF ALL of:
    (a) --enable-bailout flag is present
    (b) zero structural CRIT/MAJ issues
    (c) ≥1 mechanical CRIT/MAJ issue
    (d) canary_precondition(plan_text, mechanical_issues) returns True

Default when --enable-bailout is absent: verdict is always CONTINUE-LOOP.
This matches --enable-bailout=false semantics.

POST-MERGE: flip to --enable-bailout in orchestrator SKILL.md edits ONLY after
the held-out 5+ post-merge regression corpus shows ≥95% classifier agreement.
See pipeline-efficiency-improvements/architecture.md Stage 1 acceptance row.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

# ── Bullet-shape regexes (first-match-wins: A → B → C → D → E) ───────────────

# Shape A: bracket form  - **[CRIT-1] title** or - **[MAJ-1-r2 (NEW)] title**
_SHAPE_A = re.compile(
    r'^-\s+\*\*\[(?P<sev_a>CRIT|MAJ|MIN|NIT)-(?P<id_a>[0-9]+(?:[A-Za-z0-9-]*(?:\s*\([^)]*\))?)?)\]\s+(?P<title_a>.+?)\*\*',
    re.IGNORECASE,
)

# Shape B: no-bracket em-dash  - **CRIT-2 — title**  (round suffix tolerated)
_SHAPE_B = re.compile(
    r'^-\s+\*\*(?P<sev_b>CRIT|MAJ|MIN|NIT)-(?P<id_b>[0-9]+(?:[A-Za-z0-9-]*(?:\s*\([^)]*\))?)?)\s*[—–-]\s+(?P<title_b>.+?)\*\*',
    re.IGNORECASE,
)

# Shape C: Issue prefix  - **Issue C1 — title**
_SHAPE_C = re.compile(
    r'^-\s+\*\*Issue\s+(?P<id_c>[A-Za-z]+[A-Za-z0-9-]*)\s*[—–-]\s+(?P<title_c>.+?)\*\*',
    re.IGNORECASE,
)

# Shape D: full-word severity  - **MAJOR — title.**
_SHAPE_D = re.compile(
    r'^-\s+\*\*(?P<sev_d>CRITICAL|MAJOR|MINOR)\s*[—–-]\s+(?P<title_d>.+?)\.?\*\*',
    re.IGNORECASE,
)

# Shape E: colon separator  - **MIN-1: title.**  (seen in stage-3 critic-response-5)
_SHAPE_E = re.compile(
    r'^-\s+\*\*(?P<sev_e>CRIT|MAJ|MIN|NIT)-(?P<id_e>[0-9]+[A-Za-z0-9-]*?)\s*:\s+(?P<title_e>.+?)\*\*',
    re.IGNORECASE,
)

# Standard severity H3 headings
_SEVERITY_H3 = re.compile(r'^###\s+(?P<sev>Critical|Major|Minor)\b', re.IGNORECASE)
_ANY_H3 = re.compile(r'^###\s+\S')
_H2 = re.compile(r'^##\s+\S')
# Top-level issue bullet (no leading indent)
_TOP_BULLET = re.compile(r'^-\s+\*\*')
# Class: sub-bullet
_CLASS_LINE = re.compile(r'^\s+-\s+Class:\s+(\S+)\s*$')
# mechanical [tag] in title
_MECHANICAL_TAG = re.compile(r'\[mechanical\]', re.IGNORECASE)

# Mechanical indicators in issue title (complement to body keyword check)
# Patterns here are specific enough not to fire on structural issue titles.
_MECHANICAL_TITLE_KEYWORD = re.compile(
    r'\b(files?\s+do\s+not\s+have|does?\s+NOT\s+include|not\s+actually\s+provided|has\s+no\s+acceptance\s+grep|too\s+narrow|has\s+gaps?|Tier\s+1\s+carve-out\s+documentation|missing\s+rows?|missing\s+entr(?:y|ies)|missing\s+reference)\b',
    re.IGNORECASE,
)

# Severity abbreviation → canonical name
_SEV_MAP = {
    'crit': 'CRITICAL', 'critical': 'CRITICAL',
    'maj': 'MAJOR', 'major': 'MAJOR',
    'min': 'MINOR', 'minor': 'MINOR',
    'nit': 'MINOR',
}

# Valid surface family values (from format-kit and plan spec)
_VALID_FAMILIES = frozenset({
    'enumeration', 'regex-breadth', 'audit-method',
    'integration', 'risk-coverage', 'testability',
    'implementability', 'structural-fallback', 'other', 'unknown',
})

# Surface family inference: title keyword → family (first match wins)
# Anchored at title text only (body text NOT scanned for surface inference).
_SURFACE_FAMILIES = [
    ('regex-breadth', re.compile(
        r'\b(alternation|regex|widen.*regex|broaden.*regex|re-grep|extend.*regex)\b',
        re.IGNORECASE,
    )),
    ('enumeration', re.compile(
        r'\b(row|enumerate|enumeration|missing.*row|\d+-(file|row|item|skill|task)|count|skill\s+list)\b',
        re.IGNORECASE,
    )),
    ('audit-method', re.compile(
        r'\b(audit|audit-grep|narrower.*regex|subset.*audit|grep\s+-rE)\b',
        re.IGNORECASE,
    )),
]

# Mechanical keyword regex (applied to issue body when no [mechanical] tag)
_MECHANICAL_KEYWORD = re.compile(
    r'\b(broaden|broadened|broadening|enumerate|enumeration|missing\s+row|alternation|widen|widened|widening|extending?\s+the\s+regex|miss(?:ed|ing)\s+(?:row|skill|case|file|anchor|enumeration|cross-reference)|missing\s+rows?|missing\s+entr(?:y|ies)|missing\s+reference|not\s+actually\s+provided|has\s+no\s+\w+\s+(?:grep|assertion|acceptance))\b',
    re.IGNORECASE,
)

# Canary pattern keywords per surface family
_CANARY_PATTERNS = {
    'regex-breadth': re.compile(r'grep|re-grep|re_grep', re.IGNORECASE),
    'enumeration': re.compile(r'len\s*\(|count\s*==|>=\s*\d+\s*rows|>=\s*\d+\s*entries|row.count', re.IGNORECASE),
    'audit-method': re.compile(r'grep\s+-rE|grep\s+-rn', re.IGNORECASE),
}
_CANARY_FALLBACK = re.compile(r'grep|audit|re-grep|enumerate|row.count|assert.*count', re.IGNORECASE)


@dataclass
class Issue:
    id: str
    severity: str          # CRITICAL | MAJOR | MINOR
    title: str
    body: str = ''
    class_field: Optional[str] = None   # Value of "- Class:" line if present
    source: str = 'default'             # tag | keyword | default
    surface_family: str = 'structural-fallback'
    surface_source: str = 'default-structural'  # class-field | title-keyword | default-structural


def _infer_surface_family(issue: Issue) -> Tuple[str, str]:
    """
    Returns (surface_family, surface_source) for an issue.

    Priority:
    1. Class: field (post-T-05 schema) → class-field
    2. Title keyword match → title-keyword
    3. Default → structural-fallback, default-structural
    """
    if issue.class_field:
        family = issue.class_field.lower().strip()
        if family not in _VALID_FAMILIES:
            family = 'other'
        return family, 'class-field'

    title = issue.title
    for family_name, pattern in _SURFACE_FAMILIES:
        if pattern.search(title):
            return family_name, 'title-keyword'

    return 'structural-fallback', 'default-structural'


def _is_mechanical(issue: Issue) -> bool:
    """
    Classify issue as mechanical (True) or structural (False).

    Classification priority:
    1. [mechanical] tag in title → mechanical
    2. Mechanical keyword in body → mechanical
    3. Mechanical title keyword → mechanical
    4. Default → structural (ambiguity-defaults-structural per R-1 mitigation)
    """
    if _MECHANICAL_TAG.search(issue.title):
        issue.source = 'tag'
        return True
    if _MECHANICAL_KEYWORD.search(issue.body):
        issue.source = 'keyword'
        return True
    if _MECHANICAL_TITLE_KEYWORD.search(issue.title):
        issue.source = 'keyword'
        return True
    issue.source = 'default'
    return False


def _skip_frontmatter(lines: List[str]) -> List[str]:
    """Strip YAML frontmatter (between opening and closing ---)."""
    if not lines or lines[0].strip() != '---':
        return lines
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            return lines[i + 1:]
    return lines


def _parse_severity_h3(line: str) -> Optional[str]:
    m = _SEVERITY_H3.match(line)
    if not m:
        return None
    return _SEV_MAP.get(m.group('sev').lower())


def _parse_bullet(line: str, current_severity: str) -> Optional[Tuple[str, str, str]]:
    """
    Try each shape A–E on a top-level issue bullet line.
    Returns (severity, id, title) or None if no shape matches.
    """
    # Shape A
    m = _SHAPE_A.match(line)
    if m:
        sev = _SEV_MAP.get(m.group('sev_a').lower(), 'MINOR')
        return sev, m.group('id_a'), m.group('title_a').strip()

    # Shape B
    m = _SHAPE_B.match(line)
    if m:
        sev = _SEV_MAP.get(m.group('sev_b').lower(), 'MINOR')
        return sev, m.group('id_b'), m.group('title_b').strip()

    # Shape C — severity from enclosing H3
    m = _SHAPE_C.match(line)
    if m:
        sev = current_severity if current_severity else 'MAJOR'
        return sev, m.group('id_c'), m.group('title_c').strip()

    # Shape D — no ID token; use severity abbreviation as placeholder
    m = _SHAPE_D.match(line)
    if m:
        sev = _SEV_MAP.get(m.group('sev_d').lower(), 'MINOR')
        # Shape D has no numeric ID; use severity abbreviation as placeholder id
        sev_abbr = m.group('sev_d')[:3].upper()
        return sev, sev_abbr, m.group('title_d').strip()

    # Shape E (colon separator)
    m = _SHAPE_E.match(line)
    if m:
        sev = _SEV_MAP.get(m.group('sev_e').lower(), 'MINOR')
        return sev, m.group('id_e'), m.group('title_e').strip()

    return None


def parse_critic_response(path: str) -> List[Issue]:
    """
    Parse a critic-response file and return all issues found.
    Only CRITICAL and MAJOR issues are classified for bailout; MINOR/NIT are
    included in the JSON sidecar but do not influence the verdict.
    """
    try:
        with open(path, encoding='utf-8') as fh:
            raw = fh.readlines()
    except OSError as exc:
        print(f'ERROR: cannot read critic-response: {exc}', file=sys.stderr)
        sys.exit(1)

    lines = _skip_frontmatter([ln.rstrip('\n') for ln in raw])
    issues: List[Issue] = []
    in_issues_h2 = False
    in_severity_h3 = False
    current_severity: Optional[str] = None
    current_issue: Optional[Issue] = None

    for idx, line in enumerate(lines, start=1):
        if re.match(r'^##\s+Issues\s*$', line):
            in_issues_h2 = True
            in_severity_h3 = False
            current_severity = None
            current_issue = None
            continue
        if in_issues_h2 and _H2.match(line):
            in_issues_h2 = False
            in_severity_h3 = False
            current_severity = None
            current_issue = None
            continue

        if not in_issues_h2:
            continue

        if _ANY_H3.match(line):
            sev = _parse_severity_h3(line)
            in_severity_h3 = sev is not None
            current_severity = sev
            current_issue = None
            continue

        if not in_severity_h3:
            continue

        if _TOP_BULLET.match(line):
            parsed = _parse_bullet(line, current_severity)
            if parsed is None:
                print(
                    f'WARN: unrecognized bullet shape at {path}:{idx}',
                    file=sys.stderr,
                )
                current_issue = None
                continue
            sev, issue_id, title = parsed
            current_issue = Issue(id=issue_id, severity=sev, title=title)
            issues.append(current_issue)
            continue

        if current_issue is not None and line.strip().startswith('-'):
            class_m = _CLASS_LINE.match(line)
            if class_m:
                current_issue.class_field = class_m.group(1)
            else:
                current_issue.body += ' ' + line.strip()
        elif current_issue is not None and line.strip():
            current_issue.body += ' ' + line.strip()

    for issue in issues:
        issue.surface_family, issue.surface_source = _infer_surface_family(issue)

    return issues


def canary_precondition(plan_text: str, mechanical_issues: List[Issue]) -> bool:
    """
    Check whether every mechanical CRIT/MAJ issue has at least one matching
    canary task in the parent plan's ## Tasks section.

    Conservative-by-design: when uncertain → returns False.

    Per D-05: matching is per-issue, not aggregate. A mechanical issue with
    surface_source == 'default-structural' is treated as structural (blocks
    bailout), not matched against any canary.
    """
    tasks_match = re.search(
        r'^##\s+Tasks\s*$(.+?)(?=^##\s+\S|\Z)',
        plan_text,
        re.MULTILINE | re.DOTALL,
    )
    if not tasks_match:
        return False
    tasks_text = tasks_match.group(1)

    for issue in mechanical_issues:
        if issue.severity not in ('CRITICAL', 'MAJOR'):
            continue
        # Per D-05: surface_source == default-structural blocks bailout
        if issue.surface_source == 'default-structural':
            return False

        family = issue.surface_family
        pattern = _CANARY_PATTERNS.get(family, _CANARY_FALLBACK)

        if not pattern.search(tasks_text):
            return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            'Classify critic-response issues as structural vs. mechanical '
            'and decide whether the orchestrator may bail to implement.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Bailout logic:\n'
            '  BAIL-TO-IMPLEMENT is emitted ONLY when:\n'
            '    (a) --enable-bailout flag is present\n'
            '    (b) zero structural CRIT/MAJ issues\n'
            '    (c) ≥1 mechanical CRIT/MAJ issue\n'
            '    (d) canary precondition holds (every mechanical issue has a\n'
            '        matching canary task in the parent plan)\n'
            '  Otherwise: CONTINUE-LOOP\n'
            '\n'
            'Default (--enable-bailout absent): always CONTINUE-LOOP.\n'
            'POST-MERGE: flip to --enable-bailout in SKILL.md only after\n'
            'held-out corpus ≥95% accuracy. See architecture.md.'
        ),
    )
    parser.add_argument(
        '--critic-response',
        required=True,
        metavar='PATH',
        help='Path to the critic-response Markdown file to classify.',
    )
    parser.add_argument(
        '--plan',
        metavar='PATH',
        default=None,
        help='Path to the plan Markdown file (required for canary precondition).',
    )
    parser.add_argument(
        '--enable-bailout',
        action='store_true',
        default=False,
        help=(
            'Enable the BAIL-TO-IMPLEMENT verdict when bailout conditions are met. '
            'Default: disabled (verdict is always CONTINUE-LOOP).'
        ),
    )
    args = parser.parse_args()

    issues = parse_critic_response(args.critic_response)

    crit_maj = [i for i in issues if i.severity in ('CRITICAL', 'MAJOR')]

    structural = []
    mechanical = []
    for issue in crit_maj:
        if _is_mechanical(issue):
            mechanical.append(issue)
        else:
            structural.append(issue)

    verdict = 'CONTINUE-LOOP'
    if args.enable_bailout and len(structural) == 0 and len(mechanical) >= 1:
        plan_text = ''
        if args.plan:
            try:
                with open(args.plan, encoding='utf-8') as fh:
                    plan_text = fh.read()
            except OSError as exc:
                print(f'WARN: cannot read plan file: {exc}', file=sys.stderr)
        if canary_precondition(plan_text, mechanical):
            verdict = 'BAIL-TO-IMPLEMENT'

    print(verdict)

    structural_count = len(structural)
    mechanical_count = len(mechanical)
    issues_json = []
    for issue in issues:
        is_mech = issue in mechanical
        issues_json.append({
            'id': issue.id,
            'severity': issue.severity,
            'title': issue.title,
            'surface_family': issue.surface_family,
            'class': 'mechanical' if is_mech else (
                'structural' if issue.severity in ('CRITICAL', 'MAJOR') else 'unclassified'
            ),
            'source': issue.source,
            'surface_source': issue.surface_source,
        })

    summary = {
        'structural_count': structural_count,
        'mechanical_count': mechanical_count,
        'issues': issues_json,
    }
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
