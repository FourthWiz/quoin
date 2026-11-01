import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { enumerateSkills, groupSkills, CatalogEntry } from '../skillCatalog';

// ── Fixture helpers ───────────────────────────────────────────────────────────

/** Create a temp directory with fake skill dirs matching a realistic set. */
function makeFixtureDir(): string {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'quoin-skills-'));
  const skillDirs = [
    // Normal skills
    'architect', 'plan', 'critic', 'revise', 'implement', 'review',
    'gate', 'rollback', 'end_of_task', 'pr', 'run',
    'start_of_day', 'end_of_day', 'cleanup', 'sleep',
    'capture_insight', 'triage', 'status', 'expand', 'cost_snapshot',
    // CRIT-1 regression guard: hyphenated name must enumerate
    'revise-fast',
    // Another hyphenated skill
    'thorough_plan',
  ];
  for (const d of skillDirs) {
    fs.mkdirSync(path.join(tmp, d));
  }
  // Stray noise — must NOT be enumerated
  fs.mkdirSync(path.join(tmp, 'next_steps 2'));          // space in name
  fs.writeFileSync(path.join(tmp, 'not-a-dir.txt'), ''); // file not dir
  fs.mkdirSync(path.join(tmp, '123-bad'));               // starts with digit
  return tmp;
}

let fixtureDir = '';

describe('enumerateSkills', () => {
  before(() => { fixtureDir = makeFixtureDir(); });
  after(() => { fs.rmSync(fixtureDir, { recursive: true, force: true }); });

  it('returns revise-fast (CRIT-1: hyphen must be admitted by regex)', () => {
    const names = enumerateSkills(fixtureDir);
    assert.ok(names.includes('revise-fast'),
      '"revise-fast" must be returned — CRIT-1 regression guard');
  });

  it('does NOT return "next_steps 2" (space rejected by regex)', () => {
    const names = enumerateSkills(fixtureDir);
    assert.ok(!names.includes('next_steps 2'),
      '"next_steps 2" must be excluded — embedded space fails the pattern');
  });

  it('does NOT return plain files', () => {
    const names = enumerateSkills(fixtureDir);
    assert.ok(!names.includes('not-a-dir.txt'), 'files must not be enumerated');
  });

  it('does NOT return dirs starting with a digit', () => {
    const names = enumerateSkills(fixtureDir);
    assert.ok(!names.includes('123-bad'), 'dirs starting with digit must be excluded');
  });

  it('returns only strings matching ^[a-z][a-z0-9_-]*$', () => {
    const names = enumerateSkills(fixtureDir);
    const re = /^[a-z][a-z0-9_-]*$/;
    for (const n of names) {
      assert.match(n, re, `${n} should match the skill name pattern`);
    }
  });

  it('returns empty array for a non-existent directory (R-02 rollback)', () => {
    const result = enumerateSkills('/this/path/does/not/exist');
    assert.deepEqual(result, []);
  });
});

// ── groupSkills ───────────────────────────────────────────────────────────────

describe('groupSkills', () => {
  it('places revise-fast in Other (no longer a curated skill)', () => {
    // revise-fast is not in the CURATED list → lands in Other bucket
    const groups = groupSkills(['revise-fast']);
    const other = groups.find(g => g.group === 'Other');
    assert.ok(other, 'Other group must exist when unenumerated skills are passed');
    assert.ok(
      other.entries.some(e => e.command === 'revise-fast'),
      'revise-fast must be in Other with command===revise-fast'
    );
    assert.ok(
      other.entries.find(e => e.command === 'revise-fast')?.label === 'revise-fast',
      'revise-fast Other entry must have label===command (no pretty label)'
    );
  });

  it('places sleep in Other (no longer a curated skill)', () => {
    // sleep is not in the CURATED list → lands in Other bucket
    const groups = groupSkills(['sleep']);
    const other = groups.find(g => g.group === 'Other');
    assert.ok(other, 'Other group must exist');
    assert.ok(
      other.entries.some(e => e.command === 'sleep'),
      'sleep must be in Other'
    );
  });

  it('routes unknown skill names to Other (future-unknown fallback)', () => {
    const groups = groupSkills(['frobnicate']);
    const other = groups.find(g => g.group === 'Other');
    assert.ok(other, 'Other group must exist');
    assert.ok(
      other.entries.some(e => e.command === 'frobnicate'),
      'unknown skill must go to Other, not be dropped'
    );
  });

  it('returns groups in stable order: Planning → Execution → Lifecycle → Other', () => {
    // Pass some enumerated names that will produce an Other bucket
    const names = ['frobnicate', 'cost_snapshot'];
    const groups = groupSkills(names);
    const order = groups.map(g => g.group);
    const positions: Record<string, number> = {};
    order.forEach((g, i) => { positions[g] = i; });
    assert.ok(positions['Planning'] < positions['Execution'],
      'Planning must come before Execution');
    assert.ok(positions['Execution'] < positions['Lifecycle'],
      'Execution must come before Lifecycle');
    assert.ok(positions['Lifecycle'] < positions['Other'],
      'Lifecycle must come before Other');
    // Cost group no longer exists
    assert.ok(!(order as string[]).includes('Cost'), 'Cost group must not appear');
  });

  it('returns exactly the 3 curated groups for empty input (curated groups are enumeration-independent)', () => {
    // NEW SEMANTICS: curated groups (Planning/Execution/Lifecycle) are always present
    // regardless of what skillNames contains. Other is omitted when empty.
    const groups = groupSkills([]);
    const groupNames = groups.map(g => g.group);
    assert.ok(groupNames.includes('Planning'), 'Planning must be present (curated)');
    assert.ok(groupNames.includes('Execution'), 'Execution must be present (curated)');
    assert.ok(groupNames.includes('Lifecycle'), 'Lifecycle must be present (curated)');
    assert.ok(!groupNames.includes('Other'), 'Other must be omitted when no unenumerated skills');
    assert.equal(groups.length, 3, 'exactly 3 curated groups for empty input');
  });

  // ── Curation-specific assertions ─────────────────────────────────────────────

  it('Planning group entries match curated order and labels exactly', () => {
    const groups = groupSkills([]);
    const planning = groups.find(g => g.group === 'Planning');
    assert.ok(planning, 'Planning group must exist');
    const pairs = planning.entries.map(e => ({ command: e.command, label: e.label }));
    assert.deepEqual(pairs, [
      { command: 'architect',     label: 'Architect' },
      { command: 'thorough_plan', label: 'Thorough Plan' },
    ], 'Planning entries must match curated order and pretty labels');
  });

  it('Execution group entries match curated order and labels exactly', () => {
    const groups = groupSkills([]);
    const execution = groups.find(g => g.group === 'Execution');
    assert.ok(execution, 'Execution group must exist');
    const pairs = execution.entries.map(e => ({ command: e.command, label: e.label }));
    assert.deepEqual(pairs, [
      { command: 'implement',   label: 'Implement' },
      { command: 'review',      label: 'Review' },
      { command: 'end_of_task', label: 'End of Task' },
      { command: 'pr',          label: 'PR' },
    ], 'Execution entries must match curated order and pretty labels');
  });

  it('Lifecycle group entries match D-07 curated order exactly', () => {
    const groups = groupSkills([]);
    const lifecycle = groups.find(g => g.group === 'Lifecycle');
    assert.ok(lifecycle, 'Lifecycle group must exist');
    const commands = lifecycle.entries.map(e => e.command);
    assert.deepEqual(commands, [
      'init_workflow',
      'end_of_day',
      'start_of_day',
      'weekly_review',
      'discover',
    ], 'Lifecycle entry order must match D-07 exactly');
  });

  it('cost_snapshot lands in Other (Cost group removed)', () => {
    const groups = groupSkills(['cost_snapshot']);
    // No Cost group
    assert.ok(!groups.some(g => (g.group as string) === 'Cost'), 'Cost group must not exist');
    const other = groups.find(g => g.group === 'Other');
    assert.ok(other, 'Other group must exist');
    assert.ok(
      other.entries.some(e => e.command === 'cost_snapshot'),
      'cost_snapshot must land in Other'
    );
  });

  it('groupSkills([]) leaves Other empty (curated entries are not enumerated into it)', () => {
    const groups = groupSkills([]);
    const other = groups.find(g => g.group === 'Other');
    // Other should be absent entirely for empty input
    assert.ok(!other, 'Other must be absent for empty input');
  });
});

// ── Map-coverage test against real ~/.claude/skills/ ─────────────────────────

describe('map coverage — real ~/.claude/skills/', () => {
  it('every enumerated name appears as a command in some group entries (no silent drops, D-02)', () => {
    const realDir = path.join(os.homedir(), '.claude', 'skills');
    if (!fs.existsSync(realDir)) {
      // Directory doesn't exist in CI or this environment — skip
      return;
    }
    const names = enumerateSkills(realDir);
    const groups = groupSkills(names);
    // Flatten all entry commands across all groups
    const allCommands = groups.flatMap((g: { entries: CatalogEntry[] }) =>
      g.entries.map((e: CatalogEntry) => e.command)
    );

    // Every enumerated name must appear as a command in some group's entries
    for (const n of names) {
      assert.ok(allCommands.includes(n),
        `Skill '${n}' from real ~/.claude/skills/ is missing from groupSkills output`);
    }
  });
});
