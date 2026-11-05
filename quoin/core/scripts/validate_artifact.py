#!/usr/bin/env python3
"""
validate_artifact.py — deterministic structural validator for quoin artifacts.

Stage 1 deliverable of artifact-format-architecture v3 (see architecture §5.3.2).
No LLM calls. No network. Pure stdlib + pyyaml.

Invariants implemented:
  V-01  Frontmatter parses as valid YAML (if frontmatter is present)
  V-02  All ## headings are from the artifact type's allowed-section set
  V-03  Markdown tables have a separator row
  V-04  XML tags balance (outside fenced code blocks and inline-code spans)
  V-05  ID references (D-NN, T-NN, R-NN, F-NN, Q-NN, S-NN) resolve to definitions
        in the artifact or its sibling artifacts (ambiguous cases warn, exit unaffected)
  V-06  ## For human section present, first, ≤12 non-blank lines (Class B only)
  V-07  Required sections (per format-kit.sections.json) are present

CLI:
  Usage: validate_artifact.py [--sections-json PATH] [--type TYPE] [--quiet] [--verbose] <artifact-file>
  Exit:  0 = all invariants pass; 1 = at least one failed; 2 = invocation error

Sidecar (format-kit.sections.json) resolution order:
  (a) --sections-json <path>
  (b) ~/.claude/memory/format-kit.sections.json
  (c) <quoin-dir>/memory/format-kit.sections.json  (development fallback)
"""

import argparse
import json
import os
import re
import sys

# ── Named regex constants ─────────────────────────────────────────────────────

# V-02: top-level headings (## ) — skip inside fenced code blocks
HEADING_RE = re.compile(r'^## .+', re.MULTILINE)

# V-03: table detection
TABLE_ROW_RE = re.compile(r'^\|.*\|$')
TABLE_SEP_RE = re.compile(r'^\|[ \-:|]+\|$')

# V-04: XML tag patterns (names only — content is variable)
XML_OPEN_RE = re.compile(r'<([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>(?!</)')
XML_CLOSE_RE = re.compile(r'</([a-zA-Z][a-zA-Z0-9-]*)>')
XML_SELF_RE = re.compile(r'<([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*/?)/>/')

# HTML void elements — not balanced (no closing tag)
HTML_VOID = frozenset([
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
])

# V-05: definition regex.
# A line defines an ID when the ID is its first real token. Allowed decoration before the
# ID: whitespace, table pipes, blockquote '>', list markers (-, *, +, 1., 1)), heading '#',
# bold/italic/strikethrough markers (* _ ~), a task checkbox ([ ] / [x]), the four
# canonical status glyphs (with or without the emoji variation selector U+FE0F), and an
# optional label word ("Decision D-01:", "id: T-03"). The ID must not continue into a
# longer token ("T-05-alpha"). A line holding nothing but a bare ID is a reference,
# not a definition. Backticks are deliberately not allowed in the prefix:
# a backticked ID is a cross-artifact reference, never a local definition.
# Known limitation: a discussion bullet that starts with an ID ("- R-12 on line 455")
# is also read as a definition; this keeps table cells and terse lists working.
V05_DEF_RE = re.compile(
    r'^(?!\s*[DTRFQS]-\d+\s*$)'
    r'[\s|>*+#.\-\d✓✗⏳🚫\ufe0f_~)]*'
    r'(?:\[[ xX]\][\s*_~]*)?'
    r'(?:(?i:decision|task|risk|finding|question|stage|id)\s*:?\s+[*_~]*)?'
    r'([DTRFQS]-\d+)(?![A-Za-z0-9\-`])'
)

# V-05: body reference regex — any ID-shaped string in body text
V05_REF_RE = re.compile(r'\b([DTRFQS]-\d+)\b')

# V-05: an ID introduced mid-sentence with a label and a colon/dash
# ("... we took decision D-07: keep the cache"). Ambiguous as a definition, so
# references to IDs that are only introduced this way are reported as warnings.
V05_SOFT_DEF_RE = re.compile(
    r'(?i:\b(?:decision|task|risk|finding|question|stage))\s+[*_]*([DTRFQS]-\d+)[*_]*\s*[:—–]'
)

# V-05: text that, when it immediately precedes an ID, marks the ID as an explicit
# reference into another artifact: "plan.md#T-03", "current-plan.md T-04",
# "see architecture D-02", "the parent plan's T-06", "stage-1 T-02".
V05_QUALIFIER_RE = re.compile(
    r'(?:[\w./-]+\.md(?:#|\s*§?\s*)'
    r'|(?i:\b(?:architecture|arch|current-plan|plan|spec|brief|critic-response|critic'
    r'|review|gate|parent|sibling|upstream|previous|prior|stage[- ]?\d+))'
    r'(?:[\'’]s)?\s+'
    r'(?:(?i:plan|task|decision|risk|finding|question|stage|spec|doc)\s+)?)$'
)

# V-05: sibling-artifact scanning limits (keeps the check cheap and bounded)
V05_SIBLING_MAX_FILES = 500
V05_SIBLING_MAX_BYTES = 2 * 1024 * 1024
V05_STAGE_DIR_RE = re.compile(r'^stage-\d+$')

# V-06: ## For human heading
FOR_HUMAN_RE = re.compile(r'^## For human\s*$', re.MULTILINE)

# Fenced code block delimiter
FENCE_RE = re.compile(r'^```', re.MULTILINE)

# Inline-code span — handles single and multi-backtick runs per CommonMark:
# N opening backticks, any non-newline content (lazy), same N closing backticks.
INLINE_CODE_RE = re.compile(r'(`+)[^\n]+?\1')


# ── Utility: fence-aware line iterator ───────────────────────────────────────

def iter_lines_with_fence(text):
    """Yield (line_number_1based, line_text, in_fence) for each line."""
    in_fence = False
    for i, line in enumerate(text.split('\n'), 1):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            yield i, stripped, True  # fence delimiter line itself is inside-fence
            continue
        yield i, stripped, in_fence


def strip_inline_code(line):
    """Return line with inline-code spans replaced by placeholder spaces."""
    return INLINE_CODE_RE.sub(lambda m: ' ' * len(m.group()), line)


# ── Sidecar resolution ────────────────────────────────────────────────────────

def resolve_sidecar(cli_path):
    """Find format-kit.sections.json via the three-tier fallback."""
    if cli_path:
        if not os.path.isfile(cli_path):
            print(f"validate_artifact.py: --sections-json path not found: {cli_path}", file=sys.stderr)
            sys.exit(2)
        return cli_path

    # T-11 / MAJ-4: __QUOIN_HOME__ is substituted to dest_root at deploy time, so this
    # path resolves correctly in both user mode (~/.claude/) and project mode (<proj>/.claude/).
    deployed = os.path.join('__QUOIN_HOME__', 'memory', 'format-kit.sections.json')
    if os.path.isfile(deployed):
        return deployed

    script_dir = os.path.dirname(os.path.abspath(__file__))
    dev_path = os.path.normpath(os.path.join(script_dir, '..', '..', 'memory', 'format-kit.sections.json'))
    if os.path.isfile(dev_path):
        return dev_path

    print(
        "validate_artifact.py: format-kit.sections.json not found.\n"
        "  Tried (a) --sections-json CLI, (b) __QUOIN_HOME__/memory/, (c) <quoin-dir>/memory/\n"
        "  Run install.sh to deploy or pass --sections-json <path>.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_sidecar(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"validate_artifact.py: failed to load sidecar {path}: {e}", file=sys.stderr)
        sys.exit(2)


# ── Artifact-type detection ───────────────────────────────────────────────────

def detect_type(filepath, type_override):
    """Determine artifact type from filename (deterministic, overridable)."""
    if type_override:
        return type_override
    name = os.path.basename(filepath)
    parent = os.path.basename(os.path.dirname(filepath))
    if re.match(r'^current-plan', name):
        return 'current-plan'
    if re.match(r'^architecture-critic-', name):
        return 'critic-response'   # architecture-critic-N.md uses critic-response section set
    if re.match(r'^architecture-overview', name):
        return 'architecture-overview'
    if re.match(r'^architecture', name):
        return 'architecture'
    if re.match(r'^security-review-', name):
        return 'security-review'
    if re.match(r'^review-', name):
        return 'review'
    if re.match(r'^critic-response-', name):
        return 'critic-response'
    if parent in ('session', 'sessions') or re.match(r'^\d{4}-\d{2}-\d{2}-', name):
        return 'session'
    if re.match(r'^gate-', name):
        return 'gate'
    if re.match(r'^format-kit-pitfalls', name):
        return 'pitfalls'
    if re.match(r'^repos-inventory', name):
        return 'repos-inventory'
    if re.match(r'^dependencies-map', name):
        return 'dependencies-map'
    if re.match(r'^git-log', name):
        return 'git-log'
    if re.match(r'^spec', name):
        return 'spec'
    return 'default'


def get_type_config(sidecar, artifact_type):
    """Return the config dict for the artifact type, falling back to default."""
    types = sidecar.get('artifact_types', {})
    if artifact_type in types:
        return types[artifact_type]
    return sidecar.get('default', {'class': 'A', 'required_sections': [], 'allowed_sections': []})


# ── Invariant implementations ─────────────────────────────────────────────────

def check_v01(text, failures):
    """V-01: frontmatter parses as valid YAML (if present)."""
    try:
        import yaml
    except ImportError:
        # pyyaml not installed — skip this check with a warning
        print("validate_artifact.py: WARNING: pyyaml not installed; skipping V-01", file=sys.stderr)
        return

    if not text.startswith('---'):
        # No frontmatter — V-01 passes (missing frontmatter is allowed)
        return

    # Find closing ---
    rest = text[3:]
    end = rest.find('\n---')
    if end == -1:
        failures.append('FAIL V-01: frontmatter has opening --- but no closing --- marker')
        return
    frontmatter_text = rest[:end]
    try:
        yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        failures.append(f'FAIL V-01: frontmatter YAML parse error: {e}')


def check_v02(text, allowed_sections, failures):
    """V-02: all ## headings are from the allowed-section set."""
    if not allowed_sections:
        return  # no constraint — skip
    allowed_set = set(allowed_sections)
    in_fence = False
    for i, line in enumerate(text.split('\n'), 1):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith('## '):
            heading = stripped.rstrip()
            if heading not in allowed_set:
                failures.append(f'FAIL V-02: heading "{heading}" not in allowed set for artifact type')


def check_v03(text, failures):
    """V-03: markdown tables have a separator row."""
    in_fence = False
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r')
        if line.startswith('```'):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        # Detect a potential table: ≥2 consecutive |…| lines
        if TABLE_ROW_RE.match(line):
            if i + 1 < len(lines):
                next_line = lines[i + 1].rstrip('\r')
                if TABLE_ROW_RE.match(next_line):
                    # We have a 2+-row table block. Check if the second row is a separator.
                    if not TABLE_SEP_RE.match(next_line):
                        failures.append(
                            f'FAIL V-03: table missing header separator on line {i + 2}'
                        )
                    # Advance past the table block
                    while i < len(lines) and TABLE_ROW_RE.match(lines[i].rstrip('\r')):
                        i += 1
                    continue
        i += 1


def check_v04(text, failures):
    """V-04: XML tags balance (skip fenced code blocks and inline-code spans)."""
    stack = []  # list of (tag_name, line_number)
    in_fence = False
    for lineno, line in enumerate(text.split('\n'), 1):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Strip inline-code spans before scanning for XML
        clean = strip_inline_code(stripped)

        # Skip self-closing tags like <tag/>
        clean_no_self = re.sub(r'<[a-zA-Z][a-zA-Z0-9-]*/>', '', clean)

        # Find opens and closes in order
        # Build a merged list of (position, type, tag_name)
        events = []
        for m in XML_OPEN_RE.finditer(clean_no_self):
            tag = m.group(1).lower()
            if tag not in HTML_VOID:
                events.append((m.start(), 'open', m.group(1)))
        for m in XML_CLOSE_RE.finditer(clean_no_self):
            events.append((m.start(), 'close', m.group(1)))
        events.sort(key=lambda x: x[0])

        for _, kind, tag in events:
            if kind == 'open':
                stack.append((tag, lineno))
            else:
                # close
                if stack and stack[-1][0].lower() == tag.lower():
                    stack.pop()
                else:
                    failures.append(
                        f'FAIL V-04: closing tag </{tag}> at line {lineno} has no matching open'
                    )

    # Any unclosed opens
    for tag, lineno in stack:
        failures.append(
            f'FAIL V-04: unbalanced XML tag <{tag}> opened at line {lineno} (no matching </{tag}>)'
        )


def collect_v05_defs(text):
    """Return (defs, soft_defs, def_line_indices) for an artifact body.

    Fenced code blocks are skipped. def_line_indices are 0-based.
    """
    defs = set()
    soft_defs = set()
    def_line_indices = set()
    in_fence = False
    for idx, line in enumerate(text.split('\n')):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = V05_DEF_RE.match(stripped)
        if m:
            defs.add(m.group(1))
            def_line_indices.add(idx)
            continue
        for sm in V05_SOFT_DEF_RE.finditer(strip_inline_code(stripped)):
            soft_defs.add(sm.group(1))
    return defs, soft_defs, def_line_indices


def _v05_base_name(name):
    """Strip writer temp suffixes: current-plan.md.body.tmp -> current-plan.md."""
    while True:
        if name.endswith('.tmp'):
            name = name[:-4]
        elif name.endswith('.body'):
            name = name[:-5]
        else:
            return name


def collect_sibling_defs(artifact_path):
    """IDs defined in sibling artifacts of the same task (or stage) folder.

    Only applies inside a .workflow_artifacts tree, where a folder holds one task's
    artifacts. For a stage-N folder the task root is scanned too (architecture.md
    lives there); other stage folders are not. The previous version of the artifact
    being validated (current-plan.md when validating current-plan.md.tmp) and temp
    files are excluded. Unreadable files are skipped, which can only make the check
    stricter, never silently looser.
    """
    abs_path = os.path.abspath(artifact_path)
    if '.workflow_artifacts' not in abs_path.split(os.sep):
        return set()
    own_dir = os.path.dirname(abs_path)
    dirs = [own_dir]
    if V05_STAGE_DIR_RE.match(os.path.basename(own_dir)):
        dirs.append(os.path.dirname(own_dir))
    own_name = os.path.basename(abs_path)
    base_name = _v05_base_name(own_name)

    defs = set()
    scanned = 0
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith('.md') or name in (own_name, base_name):
                continue
            if scanned >= V05_SIBLING_MAX_FILES:
                return defs
            path = os.path.join(d, name)
            try:
                if not os.path.isfile(path) or os.path.getsize(path) > V05_SIBLING_MAX_BYTES:
                    continue
                with open(path, encoding='utf-8', errors='replace') as f:
                    sibling_text = f.read()
            except OSError:
                continue
            scanned += 1
            defs |= collect_v05_defs(sibling_text)[0]
    return defs


def check_v05(text, failures, warnings=None, external_defs=None):
    """V-05: ID references resolve to definitions.

    A bare reference resolves against definitions in this artifact or in
    external_defs (sibling artifacts of the same task folder). Unresolved
    references fail, except two ambiguous cases that are reported as warnings:
    an ID only introduced mid-sentence ("decision D-07: ..."), and an explicitly
    qualified cross-artifact reference ("plan.md#T-03", "see architecture D-02").
    """
    if warnings is None:
        warnings = []
    external_defs = external_defs or set()
    defs, soft_defs, def_line_indices = collect_v05_defs(text)
    known = defs | external_defs

    in_fence = False
    warned = set()
    for idx, line in enumerate(text.split('\n')):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if idx in def_line_indices:
            # References on a definition line are not checked.
            continue
        # Backtick-quoted IDs are explicit cross-artifact references and are exempt.
        scan_line = strip_inline_code(stripped)
        for m in V05_REF_RE.finditer(scan_line):
            ref_id = m.group(1)
            if ref_id in known:
                continue
            if ref_id in soft_defs:
                key = (ref_id, 'soft')
                if key not in warned:
                    warned.add(key)
                    warnings.append(
                        f'WARN V-05: {ref_id} is only introduced mid-sentence '
                        f'(first seen near line {idx + 1}); prefer a list, table or heading definition'
                    )
                continue
            if V05_QUALIFIER_RE.search(scan_line[:m.start()]):
                warnings.append(
                    f'WARN V-05: qualified reference {ref_id} at line {idx + 1} '
                    f'is not defined in this task folder'
                )
                continue
            failures.append(
                f'FAIL V-05: reference {ref_id} at line {idx + 1} has no definition'
            )


def check_v06(text, artifact_class, failures):
    """V-06: ## For human section presence, position, length (Class B only)."""
    if artifact_class == 'A':
        return  # skip for Class A

    lines = text.split('\n')

    # Find where frontmatter ends (if present)
    body_start = 0
    if text.startswith('---'):
        try:
            end_idx = text.index('\n---', 3)
            # Count lines up to end of frontmatter
            fm_text = text[:end_idx + 4]
            body_start = fm_text.count('\n')
        except ValueError:
            pass

    # Find first non-blank, non-empty line after frontmatter
    first_heading = None
    for i in range(body_start, len(lines)):
        line = lines[i].rstrip('\r')
        if line.startswith('## '):
            first_heading = (i, line.rstrip())
            break

    if first_heading is None or first_heading[1] != '## For human':
        failures.append(
            'FAIL V-06: ## For human section missing or not first heading after frontmatter'
        )
        return

    # Count non-blank lines from ## For human until next ## heading
    fh_line_idx = first_heading[0]
    non_blank = 0
    for j in range(fh_line_idx + 1, len(lines)):
        line = lines[j].rstrip('\r')
        if line.startswith('## '):
            break
        if line.strip():
            non_blank += 1

    if non_blank > 12:
        failures.append(
            f'FAIL V-06: ## For human section exceeds 12 non-blank lines (got {non_blank})'
        )


def check_v07(text, required_sections, failures):
    """V-07: required sections (per sidecar) are present."""
    if not required_sections:
        return
    # Collect all top-level headings (## )
    in_fence = False
    found_headings = set()
    for line in text.split('\n'):
        stripped = line.rstrip('\r')
        if stripped.startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith('## '):
            found_headings.add(stripped.rstrip())
    for req in required_sections:
        # Accept heading-line-form: "## Verdict: PASS" satisfies required "## Verdict"
        satisfied = any(h == req or h.startswith(req + ':') or h.startswith(req + ' ') for h in found_headings)
        if not satisfied:
            failures.append(
                f'FAIL V-07: required section "{req}" missing for this artifact type'
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Validate a quoin artifact against format-kit.sections.json invariants.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Usage: validate_artifact.py <artifact-file-path>\n'
            'Exit:  0 = all invariants pass; 1 = at least one failed; 2 = invocation error\n'
        ),
    )
    parser.add_argument('artifact', help='Path to the artifact file to validate')
    parser.add_argument(
        '--sections-json', metavar='PATH',
        help='Path to format-kit.sections.json (overrides auto-discovery)'
    )
    parser.add_argument(
        '--type', metavar='TYPE',
        help='Artifact type key (overrides filename-based detection)'
    )
    parser.add_argument(
        '--quiet', action='store_true',
        help='Suppress the PASS confirmation line on stdout'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Emit per-invariant pass/fail lines on stdout'
    )
    args = parser.parse_args()

    # Load artifact
    artifact_path = args.artifact
    if not os.path.isfile(artifact_path):
        print(f'validate_artifact.py: file not found: {artifact_path}', file=sys.stderr)
        sys.exit(2)

    try:
        with open(artifact_path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        print(f'validate_artifact.py: cannot read file: {e}', file=sys.stderr)
        sys.exit(2)

    # Load sidecar
    sidecar_path = resolve_sidecar(args.sections_json)
    sidecar = load_sidecar(sidecar_path)

    # Detect artifact type
    artifact_type = detect_type(artifact_path, args.type)
    type_config = get_type_config(sidecar, artifact_type)
    artifact_class = type_config.get('class', 'A')
    allowed_sections = type_config.get('allowed_sections', [])
    required_sections = type_config.get('required_sections', [])

    failures = []
    warnings = []

    # Run all invariants independently
    check_v01(text, failures)
    check_v02(text, allowed_sections, failures)
    check_v03(text, failures)
    check_v04(text, failures)
    check_v05(text, failures, warnings, collect_sibling_defs(artifact_path))
    check_v06(text, artifact_class, failures)
    check_v07(text, required_sections, failures)

    for line in warnings:
        print(line, file=sys.stderr)

    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        sys.exit(1)
    else:
        if not args.quiet:
            print(f'PASS: {artifact_path}')
        sys.exit(0)


if __name__ == '__main__':
    main()
