import type {
  GateVerdict,
  NextCommand,
  RunState,
  StageInfo,
  StageKey,
  TaskNode,
  TaskRow,
} from '../types'

// Pure logic for the workflow-tasks mod. Every function takes a small Fs and
// plain data, so the tests can drive it with an in-memory tree. The base phase
// detection mirrors core/scripts/status_graph.py (detect_phase without the git
// probe); the stage refinement and the next command build on top of it.

export type Entry = {
  name: string
  kind: 'file' | 'dir' | 'other'
  size: number
  mtimeMs: number
}

export type Fs = {
  list(path: string): Promise<Entry[]>
  read(path: string): Promise<string>
  exists(path: string): Promise<boolean>
}

export type Phase =
  | 'done'
  | 'review-gated'
  | 'review'
  | 'implement-gated'
  | 'plan-gated'
  | 'planning'
  | 'architecture'
  | 'discover'

export type PhaseResult = { phase: Phase; criticRounds: number; reviewRounds: number }

const ARTIFACTS = '.workflow_artifacts'
const MAX_DEPTH = 3
const EXCLUDED_DIRS = new Set(['finalized', 'memory', 'cache', 'trash'])
const MARKER_FILES = [
  'task-brief.md',
  'task-description.md',
  'enriched-prompt.md',
  'spec.md',
  'architecture.md',
  'current-plan.md',
  'cost-ledger.md',
  'program.md',
]
const STAGE_DIR_RE = /^stage-(\d+)$/

// The gate prefixes detect_phase keys on. detectPhaseCompat and newestGate
// share these so a new prefix cannot be added to one and missed by the other.
export const REVIEW_GATE_PREFIXES = ['gate-review-', 'gate-post-review-']
export const IMPLEMENT_GATE_PREFIXES = ['gate-implement-', 'gate-post-implement-']
export const PLAN_GATE_PREFIXES = ['gate-post-plan-', 'gate-plan-']
export const ARCHITECT_GATE_PREFIXES = ['gate-architect-']
export const SPECIFY_GATE_PREFIXES = ['gate-specify-']

const TASK_ARG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/

// ---------------------------------------------------------------- basics

const joinPath = (...parts: string[]) => parts.join('/').replace(/\/{2,}/g, '/')

/** Same anchored pattern status_graph uses to drop Google Drive conflict copies. */
const DRIVE_CONFLICT_RE = / \d{1,3}(\.[^ ]*)?$/
export const isDriveConflict = (name: string) => DRIVE_CONFLICT_RE.test(name)

const isLiveFile = (e: Entry) => e.kind === 'file' && !isDriveConflict(e.name)

async function readSafe(fs: Fs, path: string): Promise<string> {
  try {
    return await fs.read(path)
  } catch {
    return ''
  }
}

async function listSafe(fs: Fs, path: string): Promise<Entry[]> {
  try {
    return await fs.list(path)
  } catch {
    return []
  }
}

/** Walks up from `cwd` to the first directory holding a `.workflow_artifacts` directory. */
export async function findProjectRoot(fs: Fs, cwd: string): Promise<string | null> {
  let dir = cwd.replace(/\/+$/, '') || '/'
  for (let guard = 0; guard < 128; guard += 1) {
    const entries = await listSafe(fs, dir)
    if (entries.some(e => e.name === ARTIFACTS && e.kind === 'dir')) return dir
    if (dir === '/') return null
    const cut = dir.lastIndexOf('/')
    dir = cut <= 0 ? '/' : dir.slice(0, cut)
  }
  return null
}

// ---------------------------------------------------------------- discovery

/** Newest change among non-empty files in the folder and its stage-N folders. */
async function ownActivity(fs: Fs, path: string, entries: Entry[]): Promise<number> {
  let newest = 0
  for (const e of entries) {
    if (isDriveConflict(e.name)) continue
    if (e.kind === 'file' && e.size > 0) newest = Math.max(newest, e.mtimeMs)
  }
  for (const e of entries) {
    if (e.kind !== 'dir' || isDriveConflict(e.name) || !STAGE_DIR_RE.test(e.name)) continue
    for (const sub of await listSafe(fs, joinPath(path, e.name))) {
      if (isDriveConflict(sub.name)) continue
      if (sub.kind === 'file' && sub.size > 0) newest = Math.max(newest, sub.mtimeMs)
    }
  }
  return newest
}

const hasMarker = (entries: Entry[]) =>
  entries.some(
    e =>
      (e.kind === 'file' && MARKER_FILES.includes(e.name)) ||
      (e.kind === 'dir' && STAGE_DIR_RE.test(e.name)),
  )

async function scanTask(
  fs: Fs,
  path: string,
  name: string,
  arg: string,
  depth: number,
): Promise<TaskNode | null> {
  let entries: Entry[]
  try {
    entries = await fs.list(path)
  } catch {
    return { name, arg, kind: 'task', activity: 0, subtree: 0, children: [], failed: true }
  }
  if (!hasMarker(entries)) return null
  const kind = entries.some(e => e.kind === 'file' && e.name === 'program.md') ? 'program' : 'task'
  const activity = await ownActivity(fs, path, entries)
  const children: TaskNode[] = []
  if (depth < MAX_DEPTH) {
    for (const e of entries) {
      if (e.kind !== 'dir') continue
      if (e.name.startsWith('.') || e.name === 'finalized') continue
      if (STAGE_DIR_RE.test(e.name) || isDriveConflict(e.name)) continue
      const child = await scanTask(fs, joinPath(path, e.name), e.name, `${arg}/${e.name}`, depth + 1)
      if (child) children.push(child)
    }
  }
  const subtree = Math.max(activity, ...children.map(c => c.subtree))
  return { name, arg, kind, activity, subtree, children, failed: false }
}

const PROGRAM_NAME_RE = /^[a-z0-9][a-z0-9._-]*-[a-z0-9._-]*$/
const ISSUE_KEY_RE = /^[a-z]+-\d+-/

/** Folder names a program.md points at: backticked hyphenated names and `/run` arguments. */
export function programCandidates(text: string): string[] {
  const found: string[] = []
  for (const m of text.matchAll(/`([^`\n]+)`/g)) {
    if (PROGRAM_NAME_RE.test(m[1])) found.push(m[1])
  }
  for (const m of text.matchAll(/\/run\s+(?:(?:small|medium|large|strict|fast):\s*)?([a-z0-9][a-z0-9._-]*)/g)) {
    if (PROGRAM_NAME_RE.test(m[1])) found.push(m[1])
  }
  return found
}

/** Whether a folder name matches a name a program lists: equal, same issue key, or a hyphen-boundary prefix. */
export function namesMatch(folder: string, candidate: string): boolean {
  if (folder === candidate) return true
  const a = folder.match(ISSUE_KEY_RE)
  const b = candidate.match(ISSUE_KEY_RE)
  if (a && b && a[0] === b[0]) return true
  return candidate.startsWith(`${folder}-`) || folder.startsWith(`${candidate}-`)
}

function sortNodes(nodes: TaskNode[]): TaskNode[] {
  for (const n of nodes) sortNodes(n.children)
  nodes.sort((a, b) => b.subtree - a.subtree || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  return nodes
}

/**
 * Lists the project's active tasks as a tree: nested child folders under their
 * parent, matched top-level tasks under the program that names them. Sorted most
 * recent activity first, ties by name.
 */
export async function discoverTasks(fs: Fs, root: string): Promise<TaskNode[]> {
  const base = joinPath(root, ARTIFACTS)
  const top: TaskNode[] = []
  for (const e of await listSafe(fs, base)) {
    if (e.kind !== 'dir') continue
    if (EXCLUDED_DIRS.has(e.name) || e.name.startsWith('.') || isDriveConflict(e.name)) continue
    const node = await scanTask(fs, joinPath(base, e.name), e.name, e.name, 1)
    if (node) top.push(node)
  }

  const programs = top.filter(n => n.kind === 'program' && !n.failed)
  if (programs.length > 0) {
    const candidates = new Map<string, string[]>()
    for (const p of programs) {
      candidates.set(p.name, programCandidates(await readSafe(fs, joinPath(base, p.name, 'program.md'))))
    }
    const attached = new Set<string>()
    for (const node of top) {
      if (node.kind === 'program') continue
      const owners = programs.filter(p => (candidates.get(p.name) ?? []).some(c => namesMatch(node.name, c)))
      if (owners.length === 1) {
        owners[0].children.push(node)
        attached.add(node.name)
      }
    }
    for (const p of programs) {
      p.subtree = Math.max(p.subtree, ...p.children.map(c => c.subtree))
    }
    return sortNodes(top.filter(n => !attached.has(n.name)))
  }
  return sortNodes(top)
}

/** The tree as rows in display order, each with its depth. */
export function flattenTasks(nodes: TaskNode[], depth = 0): { node: TaskNode; depth: number }[] {
  const out: { node: TaskNode; depth: number }[] = []
  for (const n of nodes) {
    out.push({ node: n, depth })
    out.push(...flattenTasks(n.children, depth + 1))
  }
  return out
}

// ---------------------------------------------------------------- base phase

const maxN = (re: RegExp, names: string[]) =>
  names.reduce((best, f) => {
    const m = f.match(re)
    return m ? Math.max(best, Number(m[1])) : best
  }, 0)

const hasPrefix = (names: string[], prefixes: string[]) =>
  names.some(f => prefixes.some(p => f.startsWith(p)))

/** Port of status_graph.detect_phase with the git probe off. */
export function detectPhaseCompat(dirPath: string, entries: Entry[]): PhaseResult {
  if (dirPath.split('/').includes('finalized')) return { phase: 'done', criticRounds: 0, reviewRounds: 0 }
  const names = entries.filter(isLiveFile).map(e => e.name)
  const criticRounds = maxN(/^critic-response-(\d+)\.md$/, names)
  const reviewRounds = maxN(/^review-(\d+)\.md$/, names)
  if (hasPrefix(names, REVIEW_GATE_PREFIXES)) return { phase: 'review-gated', criticRounds, reviewRounds }
  if (reviewRounds >= 1) return { phase: 'review', criticRounds, reviewRounds }
  if (hasPrefix(names, IMPLEMENT_GATE_PREFIXES)) return { phase: 'implement-gated', criticRounds, reviewRounds: 0 }
  if (hasPrefix(names, PLAN_GATE_PREFIXES)) return { phase: 'plan-gated', criticRounds, reviewRounds: 0 }
  if (names.includes('current-plan.md')) return { phase: 'planning', criticRounds, reviewRounds: 0 }
  if (names.includes('architecture.md')) return { phase: 'architecture', criticRounds, reviewRounds: 0 }
  return { phase: 'discover', criticRounds: 0, reviewRounds: 0 }
}

// ---------------------------------------------------------------- stages and gates

const STAGE_SECTION_RE = /^## Stage decomposition\s*$/m
const NEXT_H2_RE = /^## /m
const STAGE_ROW_RE = /^[0-9]+\.\s+(?:[✅✓✗⏳⛔⚠️\s])*S-([0-9]+):\s*(.+?)\s*$/gm

/** Rows in the architecture's `## Stage decomposition` section, or null when it has none. */
export function stageCount(architectureText: string): number | null {
  const start = architectureText.match(STAGE_SECTION_RE)
  if (!start || start.index === undefined) return null
  const from = start.index + start[0].length
  const rest = architectureText.slice(from)
  const next = rest.match(NEXT_H2_RE)
  const body = next && next.index !== undefined ? rest.slice(0, next.index) : rest
  const rows = body.match(STAGE_ROW_RE)
  return rows && rows.length > 0 ? rows.length : null
}

const firstWord = (raw: string) => {
  const cleaned = raw.replace(/<[^>]*>/g, ' ').replace(/[*`]/g, '').toUpperCase()
  const m = cleaned.match(/[A-Z]+/)
  return m ? m[0] : ''
}

/**
 * Reads a gate file's outcome: the text after `## Verdict:`, else the first
 * non-blank line under a `## Verdict` heading, else an inline `Verdict:` line,
 * else a frontmatter `verdict:` key. A first word starting FAIL is a fail;
 * NEEDS, BLOCKED and PARTIAL are undecided (the gate has to be run again);
 * anything else, a missing verdict included, reads as passed, as detect_phase
 * does by never looking inside gate files.
 */
export function gateVerdict(text: string): GateVerdict {
  const lines = text.split(/\r?\n/)
  let raw: string | null = null
  for (let i = 0; i < lines.length && raw === null; i += 1) {
    const inline = lines[i].match(/^##\s+Verdict\s*:\s*(\S.*)$/)
    if (inline) {
      raw = inline[1]
      break
    }
    if (/^##\s+Verdict\s*:?\s*$/.test(lines[i])) {
      for (let j = i + 1; j < lines.length; j += 1) {
        if (lines[j].trim() !== '') {
          raw = lines[j]
          break
        }
      }
      break
    }
  }
  if (raw === null) {
    for (const line of lines) {
      const m = line.match(/^\s*\**Verdict\**\s*:\s*\**\s*(\S.*)$/i)
      if (m) {
        raw = m[1]
        break
      }
    }
  }
  if (raw === null && lines[0] === '---') {
    for (let i = 1; i < lines.length && lines[i] !== '---'; i += 1) {
      const m = lines[i].match(/^verdict:\s*(\S.*)$/)
      if (m) {
        raw = m[1]
        break
      }
    }
  }
  if (raw === null) return 'pass'
  const word = firstWord(raw)
  if (word.startsWith('FAIL')) return 'fail'
  if (word === 'NEEDS' || word === 'BLOCKED' || word === 'PARTIAL') return 'undecided'
  return 'pass'
}

const DATE_RE = /\d{4}-\d{2}-\d{2}/
// First retry marker anywhere in a gate name: `-r2`, `round3`, `fix-1`, `fix2`,
// or a bare number just before the date. Each number is one or two digits
// that no digit follows, so the date in `fix-2026-08-15` is not read as a retry.
const RETRY_RE =
  /(?:^|[^A-Za-z])r(\d{1,2})(?!\d)|round(\d{1,2})(?!\d)|fix-?(\d{1,2})(?!\d)|-(\d{1,2})-(?=\d{4}-\d{2}-\d{2})/

const gateDate = (name: string) => (name.match(DATE_RE) ?? [''])[0]
const gateRetry = (name: string) => {
  const m = name.match(RETRY_RE)
  if (!m) return 1
  return Number(m[1] ?? m[2] ?? m[3] ?? m[4])
}

/**
 * The newest gate file among entries whose name starts with one of `prefixes`.
 * Only real `.md` files count, so a leftover `.md.tmp` is never a verdict.
 * Order: the date in the name (undated below dated), the retry number, the
 * modification time, then the name.
 */
export function newestGate(entries: Entry[], prefixes: string[]): Entry | null {
  const candidates = entries.filter(
    e => isLiveFile(e) && e.name.endsWith('.md') && prefixes.some(p => e.name.startsWith(p)),
  )
  if (candidates.length === 0) return null
  return candidates.reduce((best, e) => {
    const a = gateDate(e.name)
    const b = gateDate(best.name)
    if (a !== b) return a > b ? e : best
    const ra = gateRetry(e.name)
    const rb = gateRetry(best.name)
    if (ra !== rb) return ra > rb ? e : best
    if (e.mtimeMs !== best.mtimeMs) return e.mtimeMs > best.mtimeMs ? e : best
    return e.name > best.name ? e : best
  })
}

// ---------------------------------------------------------------- labels

const SHORT: Record<StageKey, string> = {
  'run-active': 'run',
  started: 'new',
  'pre-spec': 'brief',
  spec: 'spec',
  'spec-approved': 'spec ✓',
  'spec-gate-failed': 'spec ✗',
  architecture: 'arch',
  'architecture-approved': 'arch ✓',
  'architecture-gate-failed': 'arch ✗',
  planning: 'plan',
  'plan-approved': 'plan ✓',
  'plan-gate-failed': 'plan ✗',
  'implement-done': 'impl ✓',
  'implement-gate-failed': 'impl ✗',
  review: 'review',
  'review-approved': 'review ✓',
  'review-gate-failed': 'review ✗',
  'gate-undecided': 'gate ?',
  'end-of-task-done': 'shipped',
  'stages-done': 'stages done',
  program: 'program',
  done: 'done',
  unknown: '?',
}

const LONG: Record<StageKey, string> = {
  'run-active': 'run in progress',
  started: 'started, nothing written yet',
  'pre-spec': 'task brief written, no spec yet',
  spec: 'spec written, not gated',
  'spec-approved': 'spec approved',
  'spec-gate-failed': 'spec gate failed',
  architecture: 'architecture written, not gated',
  'architecture-approved': 'architecture approved',
  'architecture-gate-failed': 'architecture gate failed',
  planning: 'plan written, not gated',
  'plan-approved': 'plan approved',
  'plan-gate-failed': 'plan gate failed',
  'implement-done': 'implemented, gate passed',
  'implement-gate-failed': 'implement gate failed',
  review: 'review written, not gated',
  'review-approved': 'review approved',
  'review-gate-failed': 'review gate failed',
  'gate-undecided': 'gate left undecided',
  'end-of-task-done': 'shipped, pull request next',
  'stages-done': 'every stage archived',
  program: 'program folder',
  done: 'finalized',
  unknown: 'folder could not be read',
}

export function shortLabel(info: Pick<StageInfo, 'key' | 'reviewRounds'>): string {
  if (info.key === 'review' && info.reviewRounds > 0) return `review ${info.reviewRounds}`
  return SHORT[info.key]
}

/** `now`, `5m`, `3h`, `2d`, `4mo`; `-` when there is no timestamp. */
export function relativeAge(ms: number, now: number): string {
  if (!ms) return '-'
  const seconds = Math.max(0, Math.round((now - ms) / 1000))
  if (seconds < 60) return 'now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d`
  return `${Math.floor(days / 30)}mo`
}

// ---------------------------------------------------------------- stage derivation

type StageCtx = { multi: boolean; stage: number | null; count: number | null }

function makeStage(
  key: StageKey,
  base: PhaseResult | null,
  ctx: StageCtx,
  extra: Partial<StageInfo> = {},
): StageInfo {
  const reviewRounds = base?.reviewRounds ?? 0
  return {
    key,
    short: shortLabel({ key, reviewRounds }),
    label: LONG[key],
    stage: ctx.stage,
    multi: ctx.multi,
    stageCount: ctx.count,
    basePhase: base?.phase ?? 'discover',
    reviewRounds,
    scope: 'stage',
    gate: null,
    run: null,
    runStale: false,
    artifact: null,
    ...extra,
  }
}

type GateRead = { entry: Entry | null; verdict: GateVerdict }

async function readGate(fs: Fs, dirPath: string, entries: Entry[], prefixes: string[]): Promise<GateRead> {
  const entry = newestGate(entries, prefixes)
  if (!entry) return { entry: null, verdict: 'undecided' }
  try {
    return { entry, verdict: gateVerdict(await fs.read(joinPath(dirPath, entry.name))) }
  } catch {
    return { entry, verdict: 'pass' }
  }
}

const fileMtime = (entries: Entry[], name: string) =>
  entries.find(e => isLiveFile(e) && e.name === name)?.mtimeMs ?? 0

const latestReviewMtime = (entries: Entry[]) =>
  entries.reduce(
    (best, e) => (isLiveFile(e) && /^review-\d+\.md$/.test(e.name) ? Math.max(best, e.mtimeMs) : best),
    0,
  )

const gateInfo = (g: GateRead) => (g.entry ? { name: g.entry.name, verdict: g.verdict } : null)

/** Refines the base phase of one directory (a task folder or a stage folder) into a stage. */
async function evaluateDir(fs: Fs, dirPath: string, entries: Entry[], ctx: StageCtx): Promise<StageInfo> {
  const base = detectPhaseCompat(dirPath, entries)
  const names = entries.filter(isLiveFile).map(e => e.name)
  const has = (name: string) => names.includes(name)

  // A gate left failed or undecided, cleared when its producing artifact is newer.
  const refine = (
    g: GateRead,
    producedAt: number,
    key: { pass: StageKey; fail: StageKey; cleared: StageKey },
    scope: 'task' | 'stage',
  ): StageInfo => {
    if (g.entry && g.verdict === 'pass') return makeStage(key.pass, base, ctx, { gate: gateInfo(g), scope })
    if (g.entry && producedAt > g.entry.mtimeMs) return makeStage(key.cleared, base, ctx, { gate: gateInfo(g), scope })
    if (g.entry && g.verdict === 'fail') return makeStage(key.fail, base, ctx, { gate: gateInfo(g), scope })
    return makeStage('gate-undecided', base, ctx, { gate: gateInfo(g), scope })
  }

  switch (base.phase) {
    case 'done':
      return makeStage('done', base, ctx)
    case 'review-gated': {
      const g = await readGate(fs, dirPath, entries, REVIEW_GATE_PREFIXES)
      return refine(
        g,
        latestReviewMtime(entries),
        { pass: 'review-approved', fail: 'review-gate-failed', cleared: 'review' },
        'stage',
      )
    }
    case 'review':
      return makeStage('review', base, ctx)
    case 'implement-gated': {
      const g = await readGate(fs, dirPath, entries, IMPLEMENT_GATE_PREFIXES)
      if (g.entry && g.verdict === 'pass') return makeStage('implement-done', base, ctx, { gate: gateInfo(g) })
      if (g.entry && g.verdict === 'fail') return makeStage('implement-gate-failed', base, ctx, { gate: gateInfo(g) })
      return makeStage('gate-undecided', base, ctx, { gate: gateInfo(g) })
    }
    case 'plan-gated': {
      const g = await readGate(fs, dirPath, entries, PLAN_GATE_PREFIXES)
      return refine(
        g,
        fileMtime(entries, 'current-plan.md'),
        { pass: 'plan-approved', fail: 'plan-gate-failed', cleared: 'planning' },
        'stage',
      )
    }
    case 'planning':
      return makeStage('planning', base, ctx)
    case 'architecture': {
      const hasGateFile = names.some(n => ARCHITECT_GATE_PREFIXES.some(p => n.startsWith(p)))
      if (!hasGateFile) return makeStage('architecture', base, ctx, { scope: 'task' })
      const g = await readGate(fs, dirPath, entries, ARCHITECT_GATE_PREFIXES)
      return refine(
        g,
        fileMtime(entries, 'architecture.md'),
        { pass: 'architecture-approved', fail: 'architecture-gate-failed', cleared: 'architecture' },
        'task',
      )
    }
    default: {
      if (ctx.multi && ctx.stage !== null) {
        return makeStage('architecture-approved', base, ctx)
      }
      if (has('program.md')) return makeStage('program', base, ctx)
      if (has('spec.md')) {
        const hasGateFile = names.some(n => SPECIFY_GATE_PREFIXES.some(p => n.startsWith(p)))
        if (!hasGateFile) return makeStage('spec', base, ctx, { scope: 'task' })
        const g = await readGate(fs, dirPath, entries, SPECIFY_GATE_PREFIXES)
        return refine(
          g,
          fileMtime(entries, 'spec.md'),
          { pass: 'spec-approved', fail: 'spec-gate-failed', cleared: 'spec' },
          'task',
        )
      }
      if (has('task-brief.md') || has('enriched-prompt.md') || has('task-description.md')) {
        return makeStage('pre-spec', base, ctx)
      }
      return makeStage('started', base, ctx)
    }
  }
}

const stageNumbers = (entries: Entry[]) =>
  entries
    .filter(e => e.kind === 'dir' && !isDriveConflict(e.name))
    .map(e => e.name.match(STAGE_DIR_RE))
    .filter((m): m is RegExpMatchArray => m !== null)
    .map(m => Number(m[1]))
    .sort((a, b) => a - b)

/** What the artifacts alone say, with multi-stage tasks resolved to the stage in play. */
async function artifactStage(fs: Fs, taskPath: string, entries: Entry[]): Promise<StageInfo> {
  const single: StageCtx = { multi: false, stage: null, count: null }
  const names = entries.filter(isLiveFile).map(e => e.name)
  const architecture = names.includes('architecture.md') ? await readSafe(fs, joinPath(taskPath, 'architecture.md')) : ''
  const closed = stageNumbers(await listSafe(fs, joinPath(taskPath, 'finalized')))
  const live = stageNumbers(entries).filter(n => !closed.includes(n))
  const hasArchitectGate = names.some(n => ARCHITECT_GATE_PREFIXES.some(p => n.startsWith(p)))
  const multi =
    STAGE_SECTION_RE.test(architecture) && (live.length > 0 || closed.length > 0 || hasArchitectGate)
  if (!multi) return evaluateDir(fs, taskPath, entries, single)

  const count = stageCount(architecture)
  if (live.length > 0) {
    const n = live[live.length - 1]
    const stagePath = joinPath(taskPath, `stage-${n}`)
    return evaluateDir(fs, stagePath, await listSafe(fs, stagePath), { multi: true, stage: n, count })
  }
  if (count !== null && closed.length > 0 && Array.from({ length: count }, (_, i) => i + 1).every(n => closed.includes(n))) {
    return makeStage('stages-done', null, { multi: true, stage: null, count })
  }
  const next = Math.max(0, ...closed) + 1
  if (closed.length === 0) return evaluateDir(fs, taskPath, entries, { multi: true, stage: 1, count })
  return makeStage('architecture-approved', null, { multi: true, stage: next, count }, { scope: 'task' })
}

// Run-state record: stage `run-active` when a run owns the task.
function parseRunState(text: string): RunState | null {
  try {
    const data = JSON.parse(text)
    if (!data || typeof data !== 'object' || data.active !== true) return null
    const str = (v: unknown) => (typeof v === 'string' ? v : '')
    return {
      phase: str(data.phase),
      subphase: str(data.subphase),
      step: str(data.step),
      nextAction: str(data.next_action),
      profile: str(data.profile),
      updatedAt: str(data.updated_at),
      resumeCommand: str(data.resume_command),
    }
  } catch {
    return null
  }
}

/** Parses a run-state timestamp; microsecond fractions are cut to milliseconds first. NaN when unparseable. */
export function parseTimestamp(value: string): number {
  return Date.parse(value.replace(/(\.\d{3})\d+/, (_all, ms) => ms))
}

const unknownStage = (): StageInfo => makeStage('unknown', null, { multi: false, stage: null, count: null })

/**
 * The stage of a task: an active run record first (top-level tasks), else the
 * artifacts. Any read failure gives stage `unknown` for this task alone.
 */
export async function deriveStage(fs: Fs, root: string, task: string): Promise<StageInfo> {
  try {
    const artifacts = joinPath(root, ARTIFACTS)
    const taskPath = joinPath(artifacts, task)
    const entries = await fs.list(taskPath)
    const fromArtifacts = await artifactStage(fs, taskPath, entries)
    if (task.includes('/')) return fromArtifacts

    const recordPath = joinPath(artifacts, 'memory', `run-state-${task}.json`)
    if (!(await fs.exists(recordPath))) return fromArtifacts
    const run = parseRunState(await readSafe(fs, recordPath))
    if (!run || (await fs.exists(joinPath(artifacts, 'finalized', task)))) return fromArtifacts

    const updated = parseTimestamp(run.updatedAt)
    const newest = await ownActivity(fs, taskPath, entries)
    const label = `run: ${run.phase}${run.subphase ? `/${run.subphase}` : ''}`
    return makeStage('run-active', null, { multi: fromArtifacts.multi, stage: fromArtifacts.stage, count: fromArtifacts.stageCount }, {
      label,
      run,
      runStale: !Number.isNaN(updated) && newest > updated,
      artifact: { key: fromArtifacts.key, short: fromArtifacts.short, label: fromArtifacts.label },
    })
  } catch {
    return unknownStage()
  }
}

// ---------------------------------------------------------------- next command

const THEN: Partial<Record<StageKey, string>> = {
  started: 'then /architect',
  'pre-spec': 'then /architect',
  spec: 'then /architect',
  'spec-approved': 'then /thorough_plan',
  architecture: 'then /thorough_plan',
  'architecture-approved': 'then /implement',
  planning: 'then /implement',
  'plan-approved': 'then /review',
  'implement-done': 'then /gate',
  review: 'then /end_of_task',
  'review-approved': 'then /pr',
  'end-of-task-done': 'then merge',
}

/** The command that moves a task forward, validated before it is shown. `task` is the task argument. */
export function nextCommand(stage: StageInfo, task: string): NextCommand {
  if (!TASK_ARG_RE.test(task)) return { command: null, then: null, note: 'no command (unusual folder name)' }
  const stageArg = stage.multi && stage.stage !== null ? `stage ${stage.stage} of ${task}` : task
  const then = THEN[stage.key] ?? null
  const make = (command: string | null, note: string | null = null): NextCommand => ({ command, then, note })

  switch (stage.key) {
    case 'run-active': {
      // Only the exact resume command for this task is ever shown; a record's own text is not trusted.
      return make(`/run --resume ${task}`)
    }
    case 'started':
    case 'pre-spec':
    case 'spec-gate-failed':
      return make(`/specify ${task}`, stage.key === 'spec-gate-failed' ? 'revise the spec, then run /gate again' : null)
    case 'spec':
    case 'architecture':
      return make(`/gate ${task}`)
    case 'spec-approved':
    case 'architecture-gate-failed':
      return make(`/architect ${task}`)
    case 'architecture-approved':
      return make(`/thorough_plan ${stageArg}`)
    case 'planning':
    case 'review':
      return make(`/gate ${stageArg}`)
    case 'plan-approved':
      return make(`/implement ${stageArg}`)
    case 'implement-done':
      return make(`/review ${stageArg}`)
    case 'review-approved': {
      if (!stage.multi || stage.stage === null) return make(`/end_of_task ${task}`)
      const archive = `/end_of_task stage ${stage.stage} of ${task}`
      if (stage.stageCount !== null && stage.stage < stage.stageCount) {
        return make(`/thorough_plan stage ${stage.stage + 1} of ${task}`, `or ${archive} to archive this stage now`)
      }
      return make(archive)
    }
    case 'plan-gate-failed':
      return make(`/thorough_plan ${stageArg}`, 'revise the plan, then run /gate again')
    case 'implement-gate-failed':
      return make(`/implement ${stageArg}`, `after fixing, run /gate ${stageArg}`)
    case 'review-gate-failed':
      return make(`/review ${stageArg}`, 'revise the review, then run /gate again')
    case 'gate-undecided':
      return make(`/gate ${stage.scope === 'task' ? task : stageArg}`, 'the last gate left no decision')
    case 'end-of-task-done':
      return make('/pr')
    case 'stages-done':
      return make(null, 'all stages archived')
    default:
      return make(null)
  }
}

// ---------------------------------------------------------------- session context

const headingValue = (lines: string[], i: number) => {
  const inline = lines[i].match(/^## Current stage:\s*(\S.*)$/)
  if (inline) return inline[1].trim()
  if (/^## Current stage\s*$/.test(lines[i])) {
    for (let j = i + 1; j < lines.length; j += 1) {
      if (lines[j].trim() !== '') return lines[j].trim()
    }
  }
  return null
}

/** The `Current stage` line of the task's newest session file, or null. */
export async function sessionContext(fs: Fs, root: string, task: string): Promise<string | null> {
  const dir = joinPath(root, ARTIFACTS, 'memory', 'sessions')
  const key = task.replace(/\//g, '-')
  const plain: Entry[] = []
  const orchestrator: Entry[] = []
  for (const e of await listSafe(fs, dir)) {
    if (e.kind !== 'file' || isDriveConflict(e.name)) continue
    const m = e.name.match(/^\d{4}-\d{2}-\d{2}-(.+)\.md$/)
    if (!m) continue
    if (m[1] === key) plain.push(e)
    else if (m[1] === `${key}-orchestrator`) orchestrator.push(e)
  }
  const pool = plain.length > 0 ? plain : orchestrator
  if (pool.length === 0) return null
  const newest = pool.reduce((best, e) => (e.name > best.name ? e : best))
  const lines = (await readSafe(fs, joinPath(dir, newest.name))).split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    const value = headingValue(lines, i)
    if (value !== null) return value
  }
  return null
}

// ---------------------------------------------------------------- rows

/** Every active task with its stage and next command, in display order. */
export async function loadRows(fs: Fs, root: string): Promise<TaskRow[]> {
  const flat = flattenTasks(await discoverTasks(fs, root))
  return Promise.all(
    flat.map(async ({ node, depth }) => {
      const stage =
        node.failed
          ? unknownStage()
          : node.kind === 'program'
            ? makeStage('program', null, { multi: false, stage: null, count: null })
            : await deriveStage(fs, root, node.arg)
      return {
        arg: node.arg,
        name: node.name,
        depth,
        kind: node.kind,
        activity: node.subtree,
        stage,
        next: nextCommand(stage, node.arg),
      }
    }),
  )
}
