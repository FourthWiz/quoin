import { describe, expect, mock, test } from 'claude-code/testing'

const ROOT = '/proj'
const ART = `${ROOT}/.workflow_artifacts`
const BASE = 1_700_000_000_000

const entry = (name: string, kind: 'file' | 'dir', mtimeMs = 0, size = 0) => ({ name, kind, size, mtimeMs, isLink: false })
const spec = (mtimeMs: number) => entry('spec.md', 'file', mtimeMs, 12)

// Two tasks: foo has a spec, bar has a plan and is the more recent.
const LISTINGS: Record<string, ReturnType<typeof entry>[]> = {
  '/': [entry('proj', 'dir')],
  [ROOT]: [entry('.workflow_artifacts', 'dir'), entry('src', 'dir')],
  [`${ROOT}/src`]: [entry('lib', 'dir')],
  [`${ROOT}/src/lib`]: [],
  [ART]: [entry('foo', 'dir'), entry('bar', 'dir'), entry('memory', 'dir')],
  [`${ART}/foo`]: [spec(BASE + 1_000)],
  [`${ART}/bar`]: [entry('current-plan.md', 'file', BASE + 5_000, 12)],
  [`${ART}/memory`]: [entry('sessions', 'dir')],
  [`${ART}/memory/sessions`]: [entry('2026-10-03-bar.md', 'file', BASE, 40)],
}
const FILES: Record<string, string> = {
  [`${ART}/memory/sessions/2026-10-03-bar.md`]: '## Status\nin_progress\n\n## Current stage: plan converged\n',
}

type Harness = {
  lists: number
  fills: { text: string; mode?: string }[]
  closes: string[]
  toasts: string[]
  statuses: (string | undefined)[]
  registered: string[]
  submits: number
  store: Map<string, unknown>
}

/** Answers the engine calls the module makes, and records what it did. */
function wire(
  on: Parameters<Parameters<typeof test>[1]>[1],
  options: { cwd?: string; listings?: typeof LISTINGS; isFilled?: boolean; refusal?: 'no_composer' | 'dialog'; store?: Record<string, unknown> } = {},
): Harness {
  const h: Harness = { lists: 0, fills: [], closes: [], toasts: [], statuses: [], registered: [], submits: 0, store: new Map(Object.entries(options.store ?? {})) }
  const listings = options.listings ?? LISTINGS
  mock.clock(on, { now: BASE + 60_000 })
  on('store.get', (_, e) => ({ value: h.store.get(e.key) }))
  on('store.set', (_, e) => {
    h.store.set(e.key, e.value)
    return { value: undefined }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: options.cwd ?? `${ROOT}/src/lib` }))
  on('fs.list', (_, e) => {
    h.lists += 1
    const found = listings[e.path.replace(/\/+$/, '') || '/']
    if (!found) return { deny: `ENOENT ${e.path}` }
    return { value: found }
  })
  on('fs.read', (_, e) => {
    const text = FILES[e.path]
    if (text === undefined) return { deny: `ENOENT ${e.path}` }
    return { value: text }
  })
  on('fs.exists', (_, e) => ({ value: e.path in FILES || e.path in listings }))
  on('command.register', (_, e) => {
    h.registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('prompt.fill', (_, e) => {
    h.fills.push({ text: e.text, mode: e.mode })
    return options.isFilled === false
      ? { isFilled: false, refusal: options.refusal ?? 'dialog' }
      : { isFilled: true }
  })
  on('prompt.submit', () => {
    h.submits += 1
    return {}
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', (_, e) => {
    h.closes.push(e.id)
    return { value: undefined }
  })
  on('ui.toast', (_, e) => {
    h.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', (_, e) => {
    h.statuses.push(e.text)
    return { value: undefined }
  })
  return h
}

const PANE_PROPS = {
  title: 'Quoin tasks',
  isFocused: true,
  bodyColumns: 100,
  placement: 'inline' as const,
  scroll: { offset: 0, bodyRows: 40 },
  view: {},
}

const mountPane = ($: any, surface: 'terminal' | 'desktop' = 'terminal') =>
  $.ui.mount({
    plugin: 'workflow-tasks',
    surface,
    component: 'Pane',
    requestId: 'quoin-tasks',
    props: PANE_PROPS,
    viewport: { columns: 100, rows: 40 },
  })

const runCommand = ($: any) => $.command.run({ command: 'quoin-tasks', args: '' })

describe('workflow-tasks registration', () => {
  test('session start registers quoin-tasks and reads nothing', async ($, on) => {
    const h = wire(on)
    await $.session.start({ cwd: `${ROOT}/src/lib`, surface: 'terminal', isInteractive: true })
    expect(h.registered).toEqual(['quoin-tasks'])
    expect(h.lists).toBe(0)
    expect(h.statuses).toEqual([])
  })
})

describe('workflow-tasks command and pane', () => {
  test('the command from a child directory opens the pane and summarises the tasks', async ($, on) => {
    const h = wire(on)
    const result = await runCommand($)
    expect(result.text).toContain('2 active tasks in /proj')
    expect(h.lists).toBeGreaterThan(0)
    const pane = await mountPane($)
    expect(await pane.find({ type: 'Text', text: /bar/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /next: \/gate bar/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /last session: plan converged/ })).toBeDefined()
  })

  test('outside any quoin project the command and the pane say so', async ($, on) => {
    wire(on, { cwd: '/elsewhere/deep', listings: { '/': [entry('elsewhere', 'dir')], '/elsewhere': [entry('deep', 'dir')], '/elsewhere/deep': [] } })
    const result = await runCommand($)
    expect(result.text).toContain('No quoin project found above /elsewhere/deep')
    const pane = await mountPane($)
    expect(await pane.find({ type: 'Text', text: /No quoin project found above/ })).toBeDefined()
  })

  test('fill puts the selected row command in the prompt once and never sends it', async ($, on) => {
    const h = wire(on)
    await runCommand($)
    const pane = await mountPane($)
    await pane.select({ key: 'tasks', value: 'foo' })
    await pane.press({ key: 'fill' })
    expect(h.fills).toEqual([{ text: '/gate foo', mode: 'replace' }])
    expect(h.submits).toBe(0)
  })

  test('a successful fill closes the pane', async ($, on) => {
    const h = wire(on)
    await runCommand($)
    const pane = await mountPane($)
    await pane.press({ key: 'fill' })
    expect(h.closes).toEqual(['quoin-tasks'])
  })

  test('a refused fill shows a toast and leaves the pane open', async ($, on) => {
    const h = wire(on, { isFilled: false, refusal: 'dialog' })
    await runCommand($)
    const pane = await mountPane($)
    await pane.press({ key: 'fill' })
    expect(h.closes).toEqual([])
    expect(h.toasts.join(' ')).toContain('Could not fill the prompt')
    expect(h.toasts.join(' ')).toContain('/gate bar')
  })

  test('selection is stored per project root and restored on the next run', async ($, on) => {
    const h = wire(on)
    await runCommand($)
    const pane = await mountPane($)
    await pane.select({ key: 'tasks', value: 'foo' })
    expect(h.store.get('selected:/proj')).toBe('foo')
    await pane.unmount()
    await runCommand($)
    const again = await mountPane($)
    expect(await again.find({ type: 'Text', text: /next: \/gate foo/ })).toBeDefined()
    expect(h.statuses[h.statuses.length - 1]).toBe('quoin foo: spec -> /gate foo')
  })

  test('a stored selection for a task that no longer exists falls back to the first row', async ($, on) => {
    wire(on, { store: { 'selected:/proj': 'gone' } })
    await runCommand($)
    const pane = await mountPane($)
    expect(await pane.find({ type: 'Text', text: /next: \/gate bar/ })).toBeDefined()
  })

  test('refresh reads the project again', async ($, on) => {
    const h = wire(on)
    await runCommand($)
    const pane = await mountPane($)
    const before = h.lists
    await pane.press({ key: 'refresh' })
    expect(h.lists).toBeGreaterThan(before)
  })

  test('the status line names the selected task, its stage and its command', async ($, on) => {
    const h = wire(on)
    await runCommand($)
    expect(h.statuses[h.statuses.length - 1]).toBe('quoin bar: plan -> /gate bar')
  })
})
