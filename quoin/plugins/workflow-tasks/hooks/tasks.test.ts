import { describe, expect, test } from 'claude-code/testing'

import {
  deriveStage,
  detectPhaseCompat,
  discoverTasks,
  findProjectRoot,
  flattenTasks,
  gateVerdict,
  isDriveConflict,
  loadRows,
  namesMatch,
  newestGate,
  nextCommand,
  parseTimestamp,
  programCandidates,
  relativeAge,
  sessionContext,
  shortLabel,
  stageCount,
} from './tasks.ts'
import type { Entry, Fs } from './tasks.ts'
import { FIXTURE_BASE_SECONDS, STAGE_FIXTURES } from './stage-fixtures.ts'

const ROOT = '/proj'
const ART = `${ROOT}/.workflow_artifacts`
const BASE_MS = FIXTURE_BASE_SECONDS * 1000
const DEFAULT_OFFSET = 100

type Body = string | { text: string; mtime: number }

/** An in-memory Fs over a tree of paths relative to .workflow_artifacts/. */
function memFs(tree: Record<string, Body>, links: string[] = [], extraDirs: string[] = []) {
  const files = new Map<string, { text: string; mtimeMs: number }>()
  const others = new Set<string>()
  const dirs = new Set<string>(['/'])
  const addDirs = (path: string) => {
    let dir = path
    while (dir && dir !== '/') {
      dirs.add(dir)
      dir = dir.slice(0, dir.lastIndexOf('/')) || '/'
    }
  }
  addDirs(ART)
  for (const dir of extraDirs) addDirs(dir)
  for (const [key, body] of Object.entries(tree)) {
    const path = `${ART}/${key}`.replace(/\/+$/, '')
    if (key.endsWith('/')) {
      addDirs(path)
      continue
    }
    const text = typeof body === 'string' ? body : body.text
    const offset = typeof body === 'string' ? DEFAULT_OFFSET : body.mtime
    files.set(path, { text, mtimeMs: BASE_MS + offset * 1000 })
    addDirs(path.slice(0, path.lastIndexOf('/')))
  }
  for (const key of links) {
    const path = `${ART}/${key}`
    others.add(path)
    addDirs(path.slice(0, path.lastIndexOf('/')))
  }
  const calls = { list: 0, read: 0 }
  const fs: Fs = {
    async list(path: string) {
      calls.list += 1
      const dir = path.replace(/\/+$/, '') || '/'
      if (!dirs.has(dir)) throw new Error(`ENOENT ${dir}`)
      const prefix = dir === '/' ? '/' : `${dir}/`
      const out = new Map<string, Entry>()
      const child = (full: string) => (full.startsWith(prefix) ? full.slice(prefix.length) : null)
      for (const d of dirs) {
        const rest = child(d)
        if (rest && !rest.includes('/')) out.set(rest, { name: rest, kind: 'dir', size: 0, mtimeMs: 0 })
      }
      for (const [f, v] of files) {
        const rest = child(f)
        if (rest && !rest.includes('/')) out.set(rest, { name: rest, kind: 'file', size: v.text.length, mtimeMs: v.mtimeMs })
      }
      for (const o of others) {
        const rest = child(o)
        if (rest && !rest.includes('/')) out.set(rest, { name: rest, kind: 'other', size: 0, mtimeMs: 0 })
      }
      return [...out.values()]
    },
    async read(path: string) {
      calls.read += 1
      const f = files.get(path)
      if (!f) throw new Error(`ENOENT ${path}`)
      return f.text
    },
    async exists(path: string) {
      return files.has(path) || dirs.has(path) || others.has(path)
    },
  }
  return { fs, calls }
}

const file = (name: string, mtimeSeconds = 0): Entry => ({
  name,
  kind: 'file',
  size: 10,
  mtimeMs: BASE_MS + mtimeSeconds * 1000,
})

const gateBody = (verdict: string) => `# Gate\n\n## Verdict\n\n${verdict}\n`

const stageInfo = (key: string, over: Record<string, unknown> = {}) =>
  ({
    key,
    short: key,
    label: key,
    stage: null,
    multi: false,
    stageCount: null,
    basePhase: 'discover',
    reviewRounds: 0,
    scope: 'stage',
    gate: null,
    run: null,
    runStale: false,
    artifact: null,
    ...over,
  }) as Parameters<typeof nextCommand>[0]

describe('stage fixtures', () => {
  for (const f of STAGE_FIXTURES) {
    test(`fixture ${f.id}`, async () => {
      const { fs } = memFs(f.tree)
      const dir = `${ART}/${f.phaseDir ?? f.task}`
      if (f.expect.detectPhase !== null) {
        expect(detectPhaseCompat(dir, await fs.list(dir)).phase).toBe(f.expect.detectPhase)
      }
      const stage = await deriveStage(fs, ROOT, f.task)
      expect(stage.key).toBe(f.expect.stage)
      expect(nextCommand(stage, f.task).command).toBe(f.expect.command)
      if (f.expect.gateVerdict !== undefined) expect(stage.gate?.verdict).toBe(f.expect.gateVerdict)
      if (f.expect.newestGate !== undefined) expect(stage.gate?.name).toBe(f.expect.newestGate)
    })
  }
})

describe('project root', () => {
  test('walks up from a child directory two levels below the root', async () => {
    const { fs } = memFs({ 'foo/spec.md': 'x' }, [], [`${ROOT}/src/lib`])
    expect(await findProjectRoot(fs, `${ROOT}/src/lib`)).toBe(ROOT)
    expect(await findProjectRoot(fs, ROOT)).toBe(ROOT)
    expect(await findProjectRoot(fs, `${ART}/foo`)).toBe(ROOT)
  })

  test('returns null when no ancestor holds .workflow_artifacts', async () => {
    const fs: Fs = {
      list: async () => [file('readme.md')],
      read: async () => '',
      exists: async () => false,
    }
    expect(await findProjectRoot(fs, '/a/b/c')).toBe(null)
  })

  test('a .workflow_artifacts file (not a directory) does not count', async () => {
    const fs: Fs = {
      list: async () => [file('.workflow_artifacts')],
      read: async () => '',
      exists: async () => false,
    }
    expect(await findProjectRoot(fs, '/a/b')).toBe(null)
  })
})

describe('discovery', () => {
  test('excludes finalized, memory, cache, trash, hidden, conflict copies, marker-less folders and symlinks', async () => {
    const { fs } = memFs(
      {
        'ok/spec.md': 'x',
        'finalized/old/spec.md': 'x',
        'memory/spec.md': 'x',
        'cache/spec.md': 'x',
        'trash/spec.md': 'x',
        '.hidden/spec.md': 'x',
        'ok 1/spec.md': 'x',
        'nomarker/readme.md': 'x',
      },
      ['linked/spec.md'],
    )
    const nodes = await discoverTasks(fs, ROOT)
    expect(nodes.map(n => n.name)).toEqual(['ok'])
  })

  test('a symlink entry of kind other is never a candidate', async () => {
    const { fs } = memFs({ 'ok/spec.md': 'x' }, ['spec-link'])
    const nodes = await discoverTasks(fs, ROOT)
    expect(nodes.map(n => n.name)).toEqual(['ok'])
  })

  test('nested children are found to depth 3, never stage folders or finalized', async () => {
    const { fs } = memFs({
      'parent/spec.md': 'x',
      'parent/child/spec.md': 'x',
      'parent/child/grand/spec.md': 'x',
      'parent/child/grand/great/spec.md': 'x',
      'parent/stage-1/spec.md': 'x',
      'parent/finalized/gone/spec.md': 'x',
    })
    const rows = flattenTasks(await discoverTasks(fs, ROOT)).map(r => r.node.arg)
    expect(rows).toEqual(['parent', 'parent/child', 'parent/child/grand'])
  })

  test('a folder holding only stage folders is a task', async () => {
    const { fs } = memFs({ 'staged/stage-1/current-plan.md': 'x' })
    expect((await discoverTasks(fs, ROOT)).map(n => n.name)).toEqual(['staged'])
  })

  test('program lists attach exact, drifted and prefix names; ambiguous and unmatched stay top-level', async () => {
    const { fs } = memFs({
      'adapter-program/program.md':
        '| M2b | `ivg-273-opencode-m2b-workflow-parity-handoff` |\n| M2a | /run large: ivg-272-opencode-m2a-runtime-driver |\n| M3 | `plain-name` |\n',
      'ivg-273-opencode-m2b-workflow-parity/spec.md': 'x',
      'ivg-272-opencode-m2a-runtime-driver/spec.md': 'x',
      'plain-name/spec.md': 'x',
      'unrelated-task/spec.md': 'x',
      'other-program/program.md': 'References `plain-name` too.\n',
    })
    const nodes = await discoverTasks(fs, ROOT)
    const names = nodes.map(n => n.name).sort()
    expect(names).toEqual(['adapter-program', 'other-program', 'plain-name', 'unrelated-task'])
    const program = nodes.find(n => n.name === 'adapter-program')
    expect(program?.children.map(c => c.arg).sort()).toEqual([
      'ivg-272-opencode-m2a-runtime-driver',
      'ivg-273-opencode-m2b-workflow-parity',
    ])
  })

  test('a program never claims itself', async () => {
    const { fs } = memFs({ 'solo-program/program.md': 'This is `solo-program`.\n' })
    const nodes = await discoverTasks(fs, ROOT)
    expect(nodes).toHaveLength(1)
    expect(nodes[0].children).toHaveLength(0)
  })

  test('name matching: equal, shared issue key, hyphen-boundary prefix, and nothing else', () => {
    expect(namesMatch('foo-bar', 'foo-bar')).toBe(true)
    expect(namesMatch('ivg-273-a-b', 'ivg-273-a-b-handoff')).toBe(true)
    expect(namesMatch('ivg-273-a', 'ivg-274-a')).toBe(false)
    expect(namesMatch('foo', 'foo-bar')).toBe(true)
    expect(namesMatch('foo', 'foobar')).toBe(false)
  })

  test('program candidates need a hyphen and come from backticks or /run lines', () => {
    const text = 'Names: `quoin`, `main`, `real-name`, and /run strict: another-name later.'
    expect(programCandidates(text).sort()).toEqual(['another-name', 'real-name'])
  })

  test('orders by latest activity then name; a parent sorts by its subtree', async () => {
    const { fs } = memFs({
      'beta/spec.md': { text: 'x', mtime: 100 },
      'alpha/spec.md': { text: 'x', mtime: 100 },
      'recent/spec.md': { text: 'x', mtime: 500 },
      'old-parent/spec.md': { text: 'x', mtime: 10 },
      'old-parent/child/spec.md': { text: 'x', mtime: 900 },
    })
    const names = (await discoverTasks(fs, ROOT)).map(n => n.name)
    expect(names).toEqual(['old-parent', 'recent', 'alpha', 'beta'])
  })

  test('activity counts non-empty files at the root and in stage folders, not conflict copies', async () => {
    const { fs } = memFs({
      'busy/spec.md': { text: 'x', mtime: 100 },
      'busy/empty.md': { text: '', mtime: 800 },
      'busy/spec 1.md': { text: 'x', mtime: 700 },
      'busy/stage-1/current-plan.md': { text: 'x', mtime: 300 },
    })
    const [node] = await discoverTasks(fs, ROOT)
    expect(node.activity).toBe(BASE_MS + 300 * 1000)
  })

  test('an unreadable task folder becomes a failed node and stage unknown', async () => {
    const base = memFs({ 'good/spec.md': 'x', 'bad/spec.md': 'x' })
    const fs: Fs = {
      list: async path => {
        if (path.endsWith('/bad')) throw new Error('EIO')
        return base.fs.list(path)
      },
      read: base.fs.read,
      exists: base.fs.exists,
    }
    const rows = await loadRows(fs, ROOT)
    const bad = rows.find(r => r.arg === 'bad')
    expect(bad?.stage.key).toBe('unknown')
    expect(bad?.next.command).toBe(null)
    expect(rows.find(r => r.arg === 'good')?.stage.key).toBe('spec')
  })

  test('rows carry stage and command for every discovered task', async () => {
    const { fs } = memFs({
      'one/current-plan.md': 'x',
      'prog/program.md': 'x',
    })
    const rows = await loadRows(fs, ROOT)
    expect(rows.map(r => [r.arg, r.stage.key, r.next.command]).sort()).toEqual([
      ['one', 'planning', '/gate one'],
      ['prog', 'program', null],
    ])
  })
})

describe('gate selection and verdicts', () => {
  test('retry token forms each parse and the higher retry wins', () => {
    const pick = (names: string[]) =>
      newestGate(
        names.map((n, i) => file(n, 100 - i)),
        ['gate-implement-'],
      )?.name
    expect(pick(['gate-implement-2026-09-01.md', 'gate-implement-2026-09-01-r2.md'])).toBe('gate-implement-2026-09-01-r2.md')
    expect(pick(['gate-implement-2026-09-01.md', 'gate-implement-2026-09-01-round2.md'])).toBe('gate-implement-2026-09-01-round2.md')
    expect(pick(['gate-implement-2026-09-01.md', 'gate-implement-round3-2026-09-01.md'])).toBe('gate-implement-round3-2026-09-01.md')
    expect(pick(['gate-implement-2026-09-01.md', 'gate-implement-fix-1-2026-09-01.md', 'gate-implement-fix-2-2026-09-01.md'])).toBe('gate-implement-fix-2-2026-09-01.md')
    expect(pick(['gate-implement-2026-09-01.md', 'gate-implement-fix2-2026-09-01.md'])).toBe('gate-implement-fix2-2026-09-01.md')
  })

  test('a number right after the date is a retry; the day itself never is', () => {
    const rank = (day: string) => {
      const older = file(`gate-implement-${day}-2.md`, 10)
      const newer = file(`gate-implement-${day}.md`, 999)
      return newestGate([newer, older], ['gate-implement-'])?.name
    }
    expect(rank('2026-09-30')).toBe('gate-implement-2026-09-30-2.md')
    expect(rank('2026-10-03')).toBe('gate-implement-2026-10-03-2.md')
    for (const suffix of ['fix2', 'r2', '2']) {
      const names = [`gate-implement-2026-09-30-${suffix}.md`, 'gate-implement-2026-09-30.md']
      const picked = newestGate([file(names[1], 999), file(names[0], 10)], ['gate-implement-'])?.name
      expect(picked).toBe(names[0])
    }
    const day30 = file('gate-implement-2026-09-30.md', 5)
    const day03 = file('gate-implement-2026-10-03.md', 5)
    expect(newestGate([day30, day03], ['gate-implement-'])?.name).toBe('gate-implement-2026-10-03.md')
  })

  test('a date inside a fix name is not read as a retry number', () => {
    const entries = [file('gate-implement-2026-08-15.md', 300), file('gate-implement-fix-2026-08-15.md', 200)]
    expect(newestGate(entries, ['gate-implement-'])?.name).toBe('gate-implement-2026-08-15.md')
  })

  test('the newer date beats a higher retry on an older date', () => {
    const entries = [file('gate-implement-2026-09-10-r3.md', 100), file('gate-implement-2026-09-11.md', 50)]
    expect(newestGate(entries, ['gate-implement-'])?.name).toBe('gate-implement-2026-09-11.md')
  })

  test('a leftover .md.tmp never wins and is not a candidate on its own', () => {
    const both = [file('gate-plan-2026-10-03.md', 100), file('gate-plan-2026-10-03.md.tmp', 900)]
    expect(newestGate(both, ['gate-plan-'])?.name).toBe('gate-plan-2026-10-03.md')
    expect(newestGate([file('gate-plan-2026-10-03.md.tmp', 900)], ['gate-plan-'])).toBe(null)
    expect(newestGate([file('gate-plan-2026-10-03.md.body.tmp', 900)], ['gate-plan-'])).toBe(null)
  })

  test('undated names rank below dated ones; ties fall to mtime then name', () => {
    const undated = [file('gate-plan-amendment.md', 900), file('gate-plan-2026-01-01.md', 1)]
    expect(newestGate(undated, ['gate-plan-'])?.name).toBe('gate-plan-2026-01-01.md')
    const tie = [file('gate-plan-2026-01-01a.md', 5), file('gate-plan-2026-01-01b.md', 5)]
    expect(newestGate(tie, ['gate-plan-'])?.name).toBe('gate-plan-2026-01-01b.md')
    const byTime = [file('gate-plan-2026-01-01a.md', 9), file('gate-plan-2026-01-01b.md', 5)]
    expect(newestGate(byTime, ['gate-plan-'])?.name).toBe('gate-plan-2026-01-01a.md')
  })

  test('conflict copies and other prefixes are not candidates', () => {
    const entries = [file('gate-plan-2026-10-03 1.md', 900), file('gate-fullsuite-2026-10-03.txt', 900), file('gate-spec-2026-10-03.md', 900)]
    expect(newestGate(entries, ['gate-plan-'])).toBe(null)
  })

  test('verdict forms: heading line, heading body, inline, frontmatter, tags and bold', () => {
    expect(gateVerdict('## Verdict: PASS\n')).toBe('pass')
    expect(gateVerdict('## Verdict\n\nFAIL (mechanical)\n')).toBe('fail')
    expect(gateVerdict('## Verdict\n\n**FAIL**\n')).toBe('fail')
    expect(gateVerdict('## Verdict\n\n<verdict>FAIL</verdict>\n')).toBe('fail')
    expect(gateVerdict('**Level:** Full\n**Verdict:** PASS\n')).toBe('pass')
    expect(gateVerdict('Verdict: PASSED\n')).toBe('pass')
    expect(gateVerdict('---\nverdict: FAIL\n---\n# Gate\n')).toBe('fail')
  })

  test('legacy forms: ### heading, bulleted bold, parenthetical label and NO-GO', () => {
    expect(gateVerdict('### Verdict\n\nFAIL\n')).toBe('fail')
    expect(gateVerdict('### Verdict\n\nPASS\n')).toBe('pass')
    expect(gateVerdict('- **Verdict:** FAIL\n')).toBe('fail')
    expect(gateVerdict('- **Verdict:** PASS\n')).toBe('pass')
    expect(gateVerdict('Verdict (automated): FAIL\n')).toBe('fail')
    expect(gateVerdict('Verdict (automated): PASS\n')).toBe('pass')
    expect(gateVerdict('## Verdict: NO-GO\n')).toBe('fail')
    expect(gateVerdict('## Verdict: NO-GO — tests red\n')).toBe('fail')
    expect(gateVerdict('## Verdict: NOTED\n')).toBe('pass')
    expect(gateVerdict('## Verdict: GO\n')).toBe('pass')
  })

  test('NEEDS-DECISION, BLOCKED and PARTIAL are undecided; CONDITIONAL PASS, GO and APPROVED pass', () => {
    expect(gateVerdict(gateBody('NEEDS-DECISION'))).toBe('undecided')
    expect(gateVerdict('---\nverdict: BLOCKED (see note)\n---\n')).toBe('undecided')
    expect(gateVerdict('---\nverdict: PARTIAL — scope limited\n---\n')).toBe('undecided')
    expect(gateVerdict(gateBody('CONDITIONAL PASS — tests deferred'))).toBe('pass')
    expect(gateVerdict('---\nverdict: GO\n---\n')).toBe('pass')
    expect(gateVerdict('## Verdict: APPROVED\n')).toBe('pass')
  })

  test('a missing verdict and a section named Verdict rationale read as passed', () => {
    expect(gateVerdict('# Gate\n\nno verdict here\n')).toBe('pass')
    expect(gateVerdict('## Verdict rationale\n\nFAIL talk\n')).toBe('pass')
  })

  test('a heading verdict wins over a frontmatter key', () => {
    expect(gateVerdict('---\nverdict: PASS\n---\n## Verdict\n\nFAIL\n')).toBe('fail')
  })
})

describe('stage derivation details', () => {
  test('a failed implement gate adds the re-gate hint', async () => {
    const { fs } = memFs({ 'foo/gate-implement-2026-10-03.md': gateBody('FAIL') })
    const stage = await deriveStage(fs, ROOT, 'foo')
    const next = nextCommand(stage, 'foo')
    expect(next.command).toBe('/implement foo')
    expect(next.note).toContain('/gate foo')
  })

  test('the stage decomposition row count ignores other sections and null without rows', () => {
    expect(stageCount('# A\n\n## Stage decomposition\n\n1. ⏳ S-1: a\n2. ✅ S-2: b\n\n## Other\n\n3. ⏳ S-3: c\n')).toBe(2)
    expect(stageCount('# A\n\n## Stage decomposition\n\nnothing\n')).toBe(null)
    expect(stageCount('# A\n')).toBe(null)
  })

  test('drive conflict names match status_graph', () => {
    expect(isDriveConflict('gate-review-2026-10-03 1.md')).toBe(true)
    expect(isDriveConflict('foo 12')).toBe(true)
    expect(isDriveConflict('foo-1.md')).toBe(false)
    expect(isDriveConflict('review-1.md')).toBe(false)
  })

  test('detectPhaseCompat reports review and critic rounds', () => {
    const entries = [file('review-1.md'), file('review-3.md'), file('critic-response-2.md')]
    const result = detectPhaseCompat(`${ART}/foo`, entries)
    expect(result).toEqual({ phase: 'review', criticRounds: 2, reviewRounds: 3 })
    expect(shortLabel({ key: 'review', reviewRounds: 3 })).toBe('review 3')
  })

  test('relative age formats seconds to months', () => {
    const now = 1_000_000_000_000
    expect(relativeAge(0, now)).toBe('-')
    expect(relativeAge(now - 20_000, now)).toBe('now')
    expect(relativeAge(now - 5 * 60_000, now)).toBe('5m')
    expect(relativeAge(now - 3 * 3_600_000, now)).toBe('3h')
    expect(relativeAge(now - 2 * 86_400_000, now)).toBe('2d')
    expect(relativeAge(now - 95 * 86_400_000, now)).toBe('3mo')
  })
})

describe('run state', () => {
  const record = (over: Record<string, unknown> = {}) =>
    JSON.stringify({
      schema: 1,
      task: 'foo',
      session_id: '53d8ccf1-5831-402d-9d79-733b97a9df39',
      active: true,
      phase: 'implement',
      phase_index: 4,
      subphase: 'task-3',
      step: 'T-03',
      at_stage_boundary: false,
      route: 'full',
      profile: 'medium',
      artifacts: [],
      next_action: 'continue T-03',
      resume_command: '/run --resume foo',
      notes_path: '/x/run-notes-foo.md',
      updated_at: '2026-10-02T05:17:54.362899+00:00',
      ...over,
    })
  const updatedMs = Date.parse('2026-10-02T05:17:54.362+00:00')
  const tree = (stateText: string, artifactMtime = 100) => ({
    'foo/current-plan.md': { text: 'x', mtime: artifactMtime },
    'memory/run-state-foo.json': stateText,
  })

  test('an active record decides the stage and the label shows phase and subphase', async () => {
    const { fs } = memFs(tree(record()))
    const stage = await deriveStage(fs, ROOT, 'foo')
    expect(stage.key).toBe('run-active')
    expect(stage.label).toBe('run: implement/task-3')
    expect(stage.run?.nextAction).toBe('continue T-03')
    expect(stage.artifact?.key).toBe('planning')
    expect(nextCommand(stage, 'foo').command).toBe('/run --resume foo')
  })

  test('the resume command from the record is used when it names exactly this task', async () => {
    const { fs } = memFs(tree(record({ resume_command: '/run --resume foo' })))
    expect(nextCommand(await deriveStage(fs, ROOT, 'foo'), 'foo').command).toBe('/run --resume foo')
  })

  for (const cmd of ['/run --resume --autonomous', '/run --resume ../x', '/run --resume foo-2', '/run --resume foo --autonomous']) {
    test(`a resume command that is not exactly this task is replaced: ${cmd}`, async () => {
      const { fs } = memFs(tree(record({ resume_command: cmd })))
      expect(nextCommand(await deriveStage(fs, ROOT, 'foo'), 'foo').command).toBe('/run --resume foo')
    })
  }

  test('an invalid resume command is replaced by /run --resume TASK', async () => {
    const { fs } = memFs(tree(record({ resume_command: '/run --resume foo; rm -rf /' })))
    expect(nextCommand(await deriveStage(fs, ROOT, 'foo'), 'foo').command).toBe('/run --resume foo')
  })

  test('an inactive record is ignored', async () => {
    const { fs } = memFs(tree(record({ active: false })))
    expect((await deriveStage(fs, ROOT, 'foo')).key).toBe('planning')
  })

  test('an active record for a finalized task is ignored', async () => {
    const { fs } = memFs({ ...tree(record()), 'finalized/foo/review-1.md': 'x' })
    expect((await deriveStage(fs, ROOT, 'foo')).key).toBe('planning')
  })

  test('malformed JSON falls through to the artifacts', async () => {
    const { fs } = memFs(tree('{ not json'))
    expect((await deriveStage(fs, ROOT, 'foo')).key).toBe('planning')
  })

  test('the record is flagged when the artifacts are newer, with microsecond timestamps', async () => {
    const newer = (updatedMs - BASE_MS) / 1000 + 60
    const stale = await deriveStage(memFs(tree(record(), newer)).fs, ROOT, 'foo')
    expect(stale.runStale).toBe(true)
    const older = (updatedMs - BASE_MS) / 1000 - 3600
    const fresh = await deriveStage(memFs(tree(record(), older)).fs, ROOT, 'foo')
    expect(fresh.runStale).toBe(false)
  })

  test('an unparseable updated_at sets no flag', async () => {
    const { fs } = memFs(tree(record({ updated_at: 'yesterday-ish' })))
    const stage = await deriveStage(fs, ROOT, 'foo')
    expect(stage.key).toBe('run-active')
    expect(stage.runStale).toBe(false)
    expect(Number.isNaN(parseTimestamp('yesterday-ish'))).toBe(true)
  })

  test('microsecond fractions are cut to milliseconds before parsing', () => {
    expect(parseTimestamp('2026-10-02T05:17:54.362899+00:00')).toBe(updatedMs)
    expect(parseTimestamp('2026-10-02T05:17:54+00:00')).toBe(Date.parse('2026-10-02T05:17:54+00:00'))
  })

  test('nested tasks have no run record', async () => {
    const { fs } = memFs({
      'parent/spec.md': 'x',
      'parent/child/current-plan.md': 'x',
      'memory/run-state-parent/child.json': record(),
    })
    expect((await deriveStage(fs, ROOT, 'parent/child')).key).toBe('planning')
  })
})

describe('session context', () => {
  const sessions = (files: Record<string, Body>) =>
    Object.fromEntries(Object.entries(files).map(([name, body]) => [`memory/sessions/${name}`, body]))

  test('a plain session file is preferred over the orchestrator file', async () => {
    const { fs } = memFs(
      sessions({
        '2026-10-01-foo.md': '---\n---\n## Status\nin_progress\n\n## Current stage: implement task 2 of 5\n',
        '2026-10-02-foo-orchestrator.md': '## Current stage\nthorough-plan:round-2-revise\n',
      }),
    )
    expect(await sessionContext(fs, ROOT, 'foo')).toBe('implement task 2 of 5')
  })

  test('the orchestrator file is used when no plain file exists, bare heading form', async () => {
    const { fs } = memFs(sessions({ '2026-10-02-foo-orchestrator.md': '# S\n\n## Current stage\n\nthorough-plan:round-2-revise\n' }))
    expect(await sessionContext(fs, ROOT, 'foo')).toBe('thorough-plan:round-2-revise')
  })

  test('the newest date wins and non-markdown files are ignored', async () => {
    const { fs } = memFs(
      sessions({
        '2026-09-30-foo.md': '## Current stage: older\n',
        '2026-10-03-foo.md': '## Current stage: newer\n',
        '2026-10-04-foo.pidfile.lock': 'pid\n',
        '2026-10-05-other.md': '## Current stage: elsewhere\n',
      }),
    )
    expect(await sessionContext(fs, ROOT, 'foo')).toBe('newer')
  })

  test('no session file gives null', async () => {
    const { fs } = memFs(sessions({ '2026-10-03-other.md': '## Current stage: x\n' }))
    expect(await sessionContext(fs, ROOT, 'foo')).toBe(null)
  })
})

describe('next command table', () => {
  const cmd = (key: string, over: Record<string, unknown> = {}, task = 'foo') => nextCommand(stageInfo(key, over), task).command

  test('stages before planning', () => {
    expect(cmd('started')).toBe('/specify foo')
    expect(cmd('pre-spec')).toBe('/specify foo')
    expect(cmd('spec')).toBe('/gate foo')
    expect(cmd('spec-approved')).toBe('/architect foo')
    expect(cmd('architecture')).toBe('/gate foo')
    expect(cmd('architecture-approved')).toBe('/thorough_plan foo')
    expect(cmd('architecture-approved', { multi: true, stage: 2 })).toBe('/thorough_plan stage 2 of foo')
  })

  test('planning through review, single and multi-stage', () => {
    expect(cmd('planning')).toBe('/gate foo')
    expect(cmd('plan-approved')).toBe('/implement foo')
    expect(cmd('implement-done')).toBe('/review foo')
    expect(cmd('review')).toBe('/gate foo')
    expect(cmd('planning', { multi: true, stage: 3 })).toBe('/gate stage 3 of foo')
    expect(cmd('plan-approved', { multi: true, stage: 3 })).toBe('/implement stage 3 of foo')
    expect(cmd('implement-done', { multi: true, stage: 3 })).toBe('/review stage 3 of foo')
  })

  test('review-approved: single stage, non-final stage, final stage, unknown count', () => {
    expect(cmd('review-approved')).toBe('/end_of_task foo')
    const middle = nextCommand(stageInfo('review-approved', { multi: true, stage: 1, stageCount: 3 }), 'foo')
    expect(middle.command).toBe('/thorough_plan stage 2 of foo')
    expect(middle.note).toContain('/end_of_task stage 1 of foo')
    expect(cmd('review-approved', { multi: true, stage: 3, stageCount: 3 })).toBe('/end_of_task stage 3 of foo')
    expect(cmd('review-approved', { multi: true, stage: 2, stageCount: null })).toBe('/end_of_task stage 2 of foo')
  })

  test('failed gates send the task back to the phase that produced the artifact', () => {
    expect(cmd('spec-gate-failed')).toBe('/specify foo')
    expect(cmd('architecture-gate-failed')).toBe('/architect foo')
    expect(cmd('plan-gate-failed')).toBe('/thorough_plan foo')
    expect(cmd('implement-gate-failed')).toBe('/implement foo')
    expect(cmd('review-gate-failed')).toBe('/review foo')
    expect(cmd('plan-gate-failed', { multi: true, stage: 2 })).toBe('/thorough_plan stage 2 of foo')
  })

  test('an undecided gate refills /gate at the right level', () => {
    expect(cmd('gate-undecided', { scope: 'task', multi: true, stage: 1 })).toBe('/gate foo')
    expect(cmd('gate-undecided', { scope: 'stage', multi: true, stage: 1 })).toBe('/gate stage 1 of foo')
    expect(cmd('gate-undecided')).toBe('/gate foo')
  })

  test('terminal and unfillable stages', () => {
    expect(cmd('end-of-task-done')).toBe('/pr')
    expect(cmd('stages-done')).toBe(null)
    expect(nextCommand(stageInfo('stages-done'), 'foo').note).toBe('all stages archived')
    expect(cmd('program')).toBe(null)
    expect(cmd('done')).toBe(null)
    expect(cmd('unknown')).toBe(null)
  })

  test('run-active uses a valid resume command and falls back otherwise', () => {
    const run = (resumeCommand: string) => ({ run: { phase: 'p', subphase: '', step: '', nextAction: '', profile: '', updatedAt: '', resumeCommand } })
    expect(cmd('run-active', run('/run --resume foo'))).toBe('/run --resume foo')
    expect(cmd('run-active', run('/run --resume foo && ls'))).toBe('/run --resume foo')
    expect(cmd('run-active')).toBe('/run --resume foo')
  })

  test('an unusual task name gives no command and a reason', () => {
    const bad = nextCommand(stageInfo('spec'), 'foo bar; rm')
    expect(bad.command).toBe(null)
    expect(bad.note).toBe('no command (unusual folder name)')
    expect(cmd('spec', {}, '../escape')).toBe(null)
    expect(cmd('spec', {}, 'parent/child')).toBe('/gate parent/child')
  })
})
