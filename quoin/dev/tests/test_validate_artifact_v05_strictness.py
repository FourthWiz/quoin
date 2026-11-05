"""
V-05 false-positive regression tests for validate_artifact.py.

Each case reproduces a writer following the documented ID convention and
getting a spurious "reference has no definition" failure:

  * definitions written with status glyphs or common markdown decoration
    (bold, italics, strikethrough, checkboxes, headings with no trailing text,
    table cells, YAML-style ``id:`` keys, label words such as "Decision");
  * references to IDs defined in a sibling artifact of the same task folder
    (or the task root, for stage-N folders);
  * explicitly qualified references (``plan.md#T-03``, "see architecture D-02");
  * mid-sentence prose mentions of IDs that are defined in one of the above ways.

The true-positive cases (an ID defined nowhere) must still fail.
Artifacts are built under tmp_path in a ``.workflow_artifacts/<task>/`` layout
and validated through the CLI, the same way skills invoke it.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

pytest.importorskip("yaml", reason="pyyaml required: pip install -e '.[dev]'")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
QUOIN_DIR = os.path.dirname(os.path.dirname(TEST_DIR))
VALIDATOR = os.path.join(QUOIN_DIR, 'core', 'scripts', 'validate_artifact.py')
TEST_SIDECAR = os.path.join(TEST_DIR, 'fixtures', 'format-kit.sections.json')


def _load_module():
    spec = importlib.util.spec_from_file_location('validate_artifact_v05', VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(path, artifact_type='default'):
    cmd = [sys.executable, VALIDATOR, '--sections-json', TEST_SIDECAR,
           '--type', artifact_type, str(path)]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode, res.stderr.decode('utf-8', errors='replace')


def task_dir(tmp_path, *parts):
    d = tmp_path / '.workflow_artifacts' / 'my-task'
    for p in parts:
        d = d / p
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(d, name, body):
    p = d / name
    p.write_text(body, encoding='utf-8')
    return p


def v05_failures(stderr):
    return [ln for ln in stderr.splitlines() if ln.startswith('FAIL V-05')]


# ── Definition forms ─────────────────────────────────────────────────────────

DEF_FORMS = [
    ('glyph-hourglass-no-list', '⏳ D-01 adopt the cache', 'D-01'),
    ('glyph-check-no-list', '✓ T-03 wire the endpoint', 'T-03'),
    ('glyph-cross', '✗ T-04: dropped', 'T-04'),
    ('glyph-blocked', '🚫 T-05: blocked on infra', 'T-05'),
    ('glyph-with-variation-selector', '1. ⏳️ T-06: pending', 'T-06'),
    ('glyph-after-bullet', '- ✓ T-07 done', 'T-07'),
    ('bold-bullet', '- **D-01**: use Postgres', 'D-01'),
    ('bold-bullet-dash', '- **D-02** — keep the API stable', 'D-02'),
    ('bold-colon-inside', '**R-03:** data loss on migrate', 'R-03'),
    ('bold-numbered-glyph', '1. ⏳ **T-08**: write tests', 'T-08'),
    ('table-cell', '| T-02 | write parser | done |', 'T-02'),
    ('table-bold-cell', '| **T-09** | write parser |', 'T-09'),
    ('table-second-column', '| 1 | T-10 | write docs |', 'T-10'),
    ('heading-bare', '### R-01', 'R-01'),
    ('heading-with-dash', '#### F-02 — stale cache', 'F-02'),
    ('heading-bold', '### **Q-01** Which region?', 'Q-01'),
    ('checkbox-open', '- [ ] T-11: migrate', 'T-11'),
    ('checkbox-done', '- [x] T-12: migrate', 'T-12'),
    ('strikethrough', '- ~~T-13~~ dropped', 'T-13'),
    ('italic', '_S-01_: discovery stage', 'S-01'),
    ('paren-numbered', '1) T-14: deploy', 'T-14'),
    ('label-word', 'Decision D-03: ship behind a flag', 'D-03'),
    ('bold-label-word', '- **Risk R-04:** quota exhaustion', 'R-04'),
    ('yaml-id-key', '- id: T-15', 'T-15'),
    ('id-then-period', '- T-16. Write the migration', 'T-16'),
]


@pytest.mark.parametrize('label,line,expected', DEF_FORMS, ids=[c[0] for c in DEF_FORMS])
def test_def_regex_recognises_documented_form(label, line, expected):
    mod = _load_module()
    m = mod.V05_DEF_RE.match(line)
    assert m is not None, f'{label}: {line!r} not recognised as a definition'
    assert m.group(1) == expected


@pytest.mark.parametrize('label,line,expected', DEF_FORMS, ids=[c[0] for c in DEF_FORMS])
def test_definition_form_resolves_end_to_end(tmp_path, label, line, expected):
    d = task_dir(tmp_path)
    p = write(d, 'notes.md', f'# Notes\n\n{line}\n\nLater prose cites {expected} mid-sentence.\n')
    rc, stderr = run(p)
    assert rc == 0, f'{label}: {stderr}'
    assert not v05_failures(stderr)


NOT_DEFS = [
    ('backticked-bullet', '- `T-05`: a cross-artifact ref, not a def'),
    ('backticked-bare', '`T-05`'),
    ('ascii-substitute-glyph', '5. ☑ T-05: substituted'),
    ('mid-sentence', 'We followed T-05 here.'),
    ('id-prefix-of-longer-token', '- T-05-alpha build'),
    ('bare-id-alone-on-line', 'T-97'),
]


@pytest.mark.parametrize('label,line', NOT_DEFS, ids=[c[0] for c in NOT_DEFS])
def test_def_regex_rejects_non_definitions(label, line):
    mod = _load_module()
    assert mod.V05_DEF_RE.match(line) is None, f'{label}: {line!r} wrongly treated as a def'


# ── Cross-artifact references ────────────────────────────────────────────────

def test_ref_to_id_defined_in_sibling_artifact_passes(tmp_path):
    d = task_dir(tmp_path)
    write(d, 'current-plan.md', '## Tasks\n\n1. ⏳ T-03: build it\n2. ⏳ T-04: test it\n')
    write(d, 'architecture.md', '## Decisions\n\n- **D-02**: use a queue\n')
    p = write(d, 'critic-response-1.md',
              '## Issues\n\n- MAJ-1: T-03 ignores the queue chosen in D-02.\n')
    rc, stderr = run(p)
    assert rc == 0, stderr
    assert not v05_failures(stderr)


def test_stage_folder_sees_task_root_artifacts(tmp_path):
    root = task_dir(tmp_path)
    write(root, 'architecture.md', '## Decisions\n\n| D-05 | event sourcing |\n')
    stage = task_dir(tmp_path, 'stage-2')
    p = write(stage, 'current-plan.md',
              '## Tasks\n\n1. ⏳ T-01: implement the projection required by D-05\n\nT-01 follows D-05.\n')
    rc, stderr = run(p)
    assert rc == 0, stderr
    assert not v05_failures(stderr)


def test_sibling_stage_folders_are_not_scanned(tmp_path):
    other = task_dir(tmp_path, 'stage-1')
    write(other, 'current-plan.md', '1. ✓ T-07: old stage task\n')
    stage = task_dir(tmp_path, 'stage-2')
    p = write(stage, 'current-plan.md', '1. ⏳ T-01: new\n\nSee T-07.\n')
    rc, stderr = run(p)
    assert rc == 1
    assert any('T-07' in ln for ln in v05_failures(stderr))


def test_tmp_write_ignores_previous_version_of_same_artifact(tmp_path):
    """Validating current-plan.md.tmp must not resolve IDs from the old current-plan.md."""
    d = task_dir(tmp_path)
    write(d, 'current-plan.md', '1. ⏳ T-09: removed in the new version\n')
    p = write(d, 'current-plan.md.tmp', '1. ⏳ T-01: kept\n\nDepends on T-09.\n')
    rc, stderr = run(p)
    assert rc == 1
    assert any('T-09' in ln for ln in v05_failures(stderr))


def test_siblings_outside_workflow_artifacts_are_not_scanned(tmp_path):
    d = tmp_path / 'scratch'
    d.mkdir()
    write(d, 'other.md', '- T-03: defined over here\n')
    p = write(d, 'note.md', 'Refers to T-03.\n')
    rc, stderr = run(p)
    assert rc == 1
    assert any('T-03' in ln for ln in v05_failures(stderr))


QUALIFIED = [
    ('md-anchor', 'Mirrors plan.md#T-03 exactly.', 'T-03'),
    ('md-path-space', 'Same as current-plan.md T-04.', 'T-04'),
    ('see-architecture', 'See architecture D-02 for the reasoning.', 'D-02'),
    ('possessive', "Covered by the parent plan's T-06.", 'T-06'),
    ('stage-qualified', 'Carried over from stage-1 T-02.', 'T-02'),
]


@pytest.mark.parametrize('label,line,ref', QUALIFIED, ids=[c[0] for c in QUALIFIED])
def test_qualified_reference_is_not_an_error(tmp_path, label, line, ref):
    d = task_dir(tmp_path)
    p = write(d, 'review-1.md', f'Findings:\n\n{line}\n')
    rc, stderr = run(p)
    assert rc == 0, f'{label}: {stderr}'
    assert not v05_failures(stderr)
    # Defined nowhere, so it is surfaced as a warning rather than silently dropped.
    assert any(ln.startswith('WARN V-05') and ref in ln for ln in stderr.splitlines()), stderr


# ── Mid-sentence prose ───────────────────────────────────────────────────────

def test_mid_sentence_mention_of_local_def_passes(tmp_path):
    d = task_dir(tmp_path)
    p = write(d, 'notes.md',
              '## Decisions\n\n- **D-01**: managed DB\n\n'
              'Because of D-01, and as the risk list notes (R-01), we pin versions.\n\n'
              '## Risks\n\n### R-01\n')
    rc, stderr = run(p)
    assert rc == 0, stderr


def test_inline_prose_definition_is_a_warning_not_error(tmp_path):
    """An ID introduced mid-sentence ("... decision D-07: keep X") is ambiguous: warn."""
    d = task_dir(tmp_path)
    p = write(d, 'notes.md',
              'After the review we took decision D-07: keep the cache warm.\n\n'
              'Revisit D-07 next quarter.\n')
    rc, stderr = run(p)
    assert rc == 0, stderr
    assert not v05_failures(stderr)
    assert any(ln.startswith('WARN V-05') and 'D-07' in ln for ln in stderr.splitlines())


# ── True positives still fail ────────────────────────────────────────────────

def test_mid_sentence_undefined_id_still_fails(tmp_path):
    d = task_dir(tmp_path)
    write(d, 'current-plan.md', '1. ⏳ T-01: only task\n')
    p = write(d, 'review-1.md', 'Findings:\n\nThe fix for T-42 is missing.\n')
    rc, stderr = run(p)
    assert rc == 1
    assert any('T-42' in ln for ln in v05_failures(stderr))


def test_unreadable_sibling_does_not_crash(tmp_path):
    d = task_dir(tmp_path)
    (d / 'broken.md').write_bytes(b'\xff\xfe\x00 not utf-8 \xff')
    p = write(d, 'notes.md', '- T-01: x\n\nSee T-01.\n')
    rc, stderr = run(p)
    assert rc == 0, stderr
