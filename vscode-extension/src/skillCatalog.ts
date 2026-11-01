import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

export type GroupName = 'Planning' | 'Execution' | 'Lifecycle' | 'Other';

export interface CatalogEntry {
  command: string;
  label: string;
  group: GroupName;
}

export interface SkillGroup {
  group: GroupName;
  entries: CatalogEntry[];
}

/**
 * Explicit ordered catalog of curated skill entries (D-01, D-07).
 *
 * Order within this array IS the display order within each group.
 * Curated entries are shown in the panel regardless of whether the
 * corresponding skill directory exists on disk (R-02: panel is a fixed
 * curated menu; missing skill → Claude shows "unknown command", non-fatal).
 */
const CURATED: CatalogEntry[] = [
  // Planning
  { command: 'architect',      label: 'Architect',      group: 'Planning' },
  { command: 'thorough_plan',  label: 'Thorough Plan',  group: 'Planning' },

  // Execution
  { command: 'implement',      label: 'Implement',      group: 'Execution' },
  { command: 'review',         label: 'Review',         group: 'Execution' },
  { command: 'end_of_task',    label: 'End of Task',    group: 'Execution' },
  { command: 'pr',             label: 'PR',             group: 'Execution' },

  // Lifecycle
  { command: 'init_workflow',       label: 'Init Workflow',    group: 'Lifecycle' },
  { command: 'end_of_day',          label: 'End of Day',       group: 'Lifecycle' },
  { command: 'start_of_day',        label: 'Start of Day',     group: 'Lifecycle' },
  { command: 'weekly_review',       label: 'Weekly Review',    group: 'Lifecycle' },
  { command: 'discover',            label: 'Discover',         group: 'Lifecycle' },
];

/**
 * The stable display order for groups.
 * Empty groups are omitted from the returned array.
 */
const GROUP_ORDER: GroupName[] = ['Planning', 'Execution', 'Lifecycle', 'Other'];

/**
 * Enumerate skill names from the given directory.
 *
 * Returns only entries that are:
 *   1. Directories (not files)
 *   2. Match `^[a-z][a-z0-9_-]*$` (CRIT-1 fix: includes hyphen, so 'revise-fast' passes;
 *      'next_steps 2' fails on the embedded space)
 *
 * Falls back to an empty array if the directory is unreadable (R-02 rollback).
 *
 * @param skillsDir - Directory to scan. Defaults to ~/.claude/skills
 */
export function enumerateSkills(
  skillsDir: string = path.join(os.homedir(), '.claude', 'skills')
): string[] {
  try {
    const entries = fs.readdirSync(skillsDir, { withFileTypes: true });
    const pattern = /^[a-z][a-z0-9_-]*$/;
    return entries
      .filter(e => e.isDirectory() && pattern.test(e.name))
      .map(e => e.name);
  } catch {
    // R-02 rollback: unreadable directory → empty list (UI shows no skill buttons)
    return [];
  }
}

/**
 * Group skill entries into ordered SkillGroup buckets.
 *
 * Curated entries (from CURATED array) are always included in the output,
 * in their defined display order, regardless of whether they appear in
 * `skillNames` (enumeration-independent). This makes the panel a fixed
 * curated menu, not a mirror of the skills directory.
 *
 * The 'Other' bucket is built from enumerated names NOT found in the curated
 * command set (bare ids only
 * from curatedCommands since it contains a space). This satisfies D-02
 * no-silent-drops: every enumerated name appears either as a curated command
 * or in the Other bucket.
 *
 * Empty groups are omitted from the result.
 * Groups are returned in stable order: Planning → Execution → Lifecycle → Other.
 *
 * @param skillNames - Skill names to group (typically from enumerateSkills())
 */
export function groupSkills(skillNames: string[]): SkillGroup[] {
  // Build set of bare curated command ids
  const curatedCommands = new Set(
    CURATED.map(e => e.command).filter(c => !c.includes(' '))
  );

  // Build per-group ordered entry lists from CURATED (preserves intra-group order)
  const buckets: Record<GroupName, CatalogEntry[]> = {
    Planning: [],
    Execution: [],
    Lifecycle: [],
    Other: [],
  };

  // Add curated entries in array order (enumeration-independent)
  for (const entry of CURATED) {
    buckets[entry.group].push(entry);
  }

  // Other bucket: enumerated names not in curated command set
  for (const name of skillNames) {
    if (!curatedCommands.has(name)) {
      buckets['Other'].push({ command: name, label: name, group: 'Other' });
    }
  }

  // Return stable order, omitting empty groups
  return GROUP_ORDER
    .filter(g => buckets[g].length > 0)
    .map(g => ({ group: g, entries: buckets[g] }));
}
