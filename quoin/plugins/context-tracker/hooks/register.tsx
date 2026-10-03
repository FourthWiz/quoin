import { atom, read, update } from 'claude-code'
import type { Register, EngineInterface } from 'claude-code'

import type { Breakdown, Sample, SampleKind, SkillEvent, View } from '../types'


const PANE = 'context-tracker'
const MAX_SAMPLES = 600
const MAX_SKILL_EVENTS = 300

const samples = atom({ plugin: 'context-tracker', key: 'samples' } as const, [] as Sample[])
const breakdown = atom({ plugin: 'context-tracker', key: 'breakdown' } as const, null as Breakdown | null)
const skillEvents = atom({ plugin: 'context-tracker', key: 'skillEvents' } as const, [] as SkillEvent[])
const view = atom({ plugin: 'context-tracker', key: 'view' } as const, 'categories' as View)

// ---------------------------------------------------------------- palette

const CATEGORY_COLORS: Record<string, string> = {
  'System prompt': '#7aa2f7',
  'System tools': '#bb9af7',
  'MCP tools': '#ff9e64',
  'Custom agents': '#e0af68',
  'Memory files': '#9ece6a',
  'Skills': '#2ac3de',
  'Messages': '#f7768e',
  'Free space': '#3b4261',
  'Autocompact buffer': '#565f89',
}
const KIND_COLORS = { used: '#c0caf5', free: '#3b4261', buffer: '#565f89', deferred: '#414868' }
const SPIKE_RED = '#f7768e'
const SPIKE_YELLOW = '#e0af68'
const CALM_GREEN = '#9ece6a'
const COMPACT_BLUE = '#7dcfff'

const colorOf = (name: string, kind: keyof typeof KIND_COLORS) =>
  CATEGORY_COLORS[name] ?? KIND_COLORS[kind]

const fillColor = (percent: number) =>
  percent >= 80 ? SPIKE_RED : percent >= 50 ? SPIKE_YELLOW : CALM_GREEN

/** Red for a big jump, yellow for a notable one, dim otherwise. */
const deltaColor = (delta: number, window: number) => {
  if (delta >= Math.max(10_000, window * 0.05)) return SPIKE_RED
  if (delta >= 3_000) return SPIKE_YELLOW
  if (delta < 0) return COMPACT_BLUE
  return undefined
}

// ---------------------------------------------------------------- formatting

const fmtK = (n: number) => {
  const abs = Math.abs(n)
  if (abs < 1_000) return `${Math.round(n)}`
  if (abs < 1_000_000) return `${(n / 1_000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}
const fmtDelta = (n: number) => (n >= 0 ? `+${fmtK(n)}` : `-${fmtK(-n)}`)
const pad2 = (n: number) => String(n).padStart(2, '0')
const fmtTime = (t: number) => {
  const d = new Date(t)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}
const fmtMs = (ms: number) => (ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m` : `${Math.round(ms / 1000)}s`)
const padEnd = (s: string, n: number) => (s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length))
const padStart = (s: string, n: number) => (s.length >= n ? s : ' '.repeat(n - s.length) + s)

const bar = (ratio: number, width: number) => {
  const r = Math.max(0, Math.min(1, ratio))
  const filled = Math.round(r * width)
  return '█'.repeat(filled) + '░'.repeat(Math.max(0, width - filled))
}

const SPARK = '▁▂▃▄▅▆▇█'
const sparkline = (values: number[], max: number) =>
  values
    .map(v => SPARK[Math.min(SPARK.length - 1, Math.max(0, Math.round((v / Math.max(1, max)) * (SPARK.length - 1))))])
    .join('')

const toolsLabel = (tools: Record<string, number>, limit = 3) =>
  Object.entries(tools)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([name, n]) => (n > 1 ? `${name}×${n}` : name))
    .join(' ')

// ---------------------------------------------------------------- recording

type Recorder = {
  /** The current main-thread turn's tool calls, by name. */
  tools: Record<string, number>
  /** Skills invoked in the current main-thread turn. */
  skills: string[]
  /** Main-thread turns seen since load. */
  turn: number
  /** Last context size recorded, to compute deltas without a read. */
  lastTokens: number
  window: number
}

async function addSample(
  $: EngineInterface,
  rec: Recorder,
  input: { tokens: number; percent: number; kind: SampleKind; durationMs?: number; fromTokens?: number },
) {
  const t = await $.clock.now()
  let isNew = false
  await update($, samples, list => {
    const last = list[list.length - 1]
    // A measurement right after a turn repeats the turn's own figure: keep one point.
    if (last && input.kind === 'measure' && last.tokens === input.tokens) return list
    isNew = true
    const sample: Sample = {
      t,
      tokens: input.tokens,
      percent: input.percent,
      kind: input.kind,
      turn: rec.turn,
      tools: input.kind === 'turn' ? { ...rec.tools } : {},
      skills: input.kind === 'turn' ? [...rec.skills] : [],
      durationMs: input.durationMs,
      fromTokens: input.fromTokens,
    }
    return [...list, sample].slice(-MAX_SAMPLES)
  })
  if (isNew) {
    await update($, skillEvents, list =>
      list.map(ev => (ev.tokensAfter === null ? { ...ev, tokensAfter: input.tokens } : ev)),
    )
  }
  rec.lastTokens = input.tokens
}

async function refreshBreakdown($: EngineInterface) {
  try {
    const usage = await $.session.usage({ breakdown: 'summary', columns: 80 })
    const b = usage.context.breakdown
    if (!b) return
    const snapshot: Breakdown = {
      at: await $.clock.now(),
      total: b.totalTokens,
      max: b.rawMaxTokens,
      percent: b.percentage,
      categories: b.categories.map(c => ({ name: c.name, tokens: c.tokens, kind: c.kind })),
    }
    await update($, breakdown, () => snapshot)
  } catch {
    // No session bound (a -p run, a test without the op answered): keep what we had.
  }
}

// ---------------------------------------------------------------- register

export const register: Register = on => {
  const rec: Recorder = { tools: {}, skills: [], turn: 0, lastTokens: 0, window: 0 }

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'ctx',
      description: 'Open the context tracker pane (categories, timeline, skills)',
    })
    void $.ui.open({ id: PANE, title: 'Context' })
    void refreshBreakdown($)
    return next(e)
  })

  on('command.run', { command: 'ctx' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Context', focus: true })
    await refreshBreakdown($)
    return { text: 'Context tracker pane opened. Hotkeys 1/2/3 switch views, r refreshes.' }
  })

  on('prompt.submit', ($, e, next) => {
    rec.tools = {}
    rec.skills = []
    return next(e)
  })

  on('tool.call', ($, e, next) => {
    if (!e.agentId) rec.tools[e.tool] = (rec.tools[e.tool] ?? 0) + 1
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    rec.skills.push(e.skill)
    const ev: SkillEvent = {
      t: await $.clock.now(),
      skill: e.skill,
      turn: rec.turn + 1,
      promptTokens: Math.round(e.text.length / 4),
      tokensBefore: rec.lastTokens,
      tokensAfter: null,
    }
    await update($, skillEvents, list => [...list, ev].slice(-MAX_SKILL_EVENTS))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      rec.turn += 1
      const u = e.usage
      if (u) {
        const tokens = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        const percent = rec.window > 0 ? Math.round((tokens / rec.window) * 100) : 0
        await addSample($, rec, { tokens, percent, kind: 'turn', durationMs: e.durationMs })
      }
      rec.tools = {}
      rec.skills = []
    }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    rec.window = e.context.window
    if (e.changed.includes('context') && e.context.tokens !== undefined) {
      const tokens = e.context.tokens
      const percent = e.context.percent ?? Math.round((tokens / e.context.window) * 100)
      const delta = tokens - rec.lastTokens
      await addSample($, rec, { tokens, percent, kind: 'measure' })
      await refreshBreakdown($)
      const tag = delta === 0 || rec.lastTokens === tokens ? '' : ` ${fmtDelta(delta)}`
      $.ui.status(`ctx ${percent}%${tag}`)
    }
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    const result = await next(e)
    if (!e.agentId && e.trigger !== 'precompute' && 'messages' in result && result.messages) {
      const from = result.tokensBefore ?? rec.lastTokens
      const to = result.tokensAfter ?? 0
      const percent = rec.window > 0 ? Math.round((to / rec.window) * 100) : 0
      await addSample($, rec, { tokens: to, percent, kind: 'compact', fromTokens: from })
      await refreshBreakdown($)
    }
    return result
  })

  // ---------------------------------------------------------------- drawing

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const width = Math.max(40, e.props.bodyColumns ?? e.viewport?.columns ?? 80)
    const [list, snap, events, current] = await Promise.all([
      read($, samples),
      read($, breakdown),
      read($, skillEvents),
      read($, view),
    ])
    const last = list[list.length - 1]
    const prev = list[list.length - 2]
    const window = snap?.max ?? rec.window
    const tokensNow = last?.tokens ?? snap?.total ?? 0
    const pctNow = last?.percent ?? snap?.percent ?? 0
    const lastDelta = last && prev ? last.tokens - prev.tokens : 0

    const tab = (key: View, hotkey: string, label: string) => (
      <Button
        key={`view-${key}`}
        hotkey={hotkey}
        plain
        label={label}
        dimColor={current !== key}
        onPress={() => update($, view, () => key)}
      />
    )

    const header = (
      <Box flexDirection="column">
        <Box>
          <Text bold>Context </Text>
          <Text color={fillColor(pctNow)} bold>{fmtK(tokensNow)}</Text>
          <Text dimColor> / {fmtK(window)} ({pctNow}%)</Text>
          {last && prev && (
            <Text color={deltaColor(lastDelta, window)}> last {fmtDelta(lastDelta)}</Text>
          )}
          <Text dimColor> · {list.length} pts · {events.length} skill runs</Text>
        </Box>
        <Box gap={2}>
          {tab('categories', '1', 'Categories')}
          {tab('timeline', '2', 'Timeline')}
          {tab('skills', '3', 'Skills')}
          <Button key="refresh" hotkey="r" plain dimColor label="Refresh" onPress={() => refreshBreakdown($)} />
        </Box>
      </Box>
    )

    let body
    if (current === 'categories') body = drawCategories()
    else if (current === 'timeline') body = drawTimeline()
    else body = drawSkills()

    return (
      <Box flexDirection="column">
        {header}
        <Text> </Text>
        {body}
      </Box>
    )

    // ------------------------------------------------ categories view
    function drawCategories() {
      if (!snap) return <Text dimColor>No breakdown yet. Press r after the first response.</Text>
      const inWindow = snap.categories.filter(c => c.kind !== 'deferred')
      const deferred = snap.categories.filter(c => c.kind === 'deferred')
      const stackWidth = width - 2
      const stack = inWindow
        .map(c => ({ c, cells: Math.round((c.tokens / Math.max(1, snap.max)) * stackWidth) }))
        .filter(x => x.cells > 0)
      const nameW = Math.min(20, Math.max(...inWindow.map(c => c.name.length), 8))
      const barW = Math.max(10, width - nameW - 20)
      return (
        <Box flexDirection="column">
          <Box>
            {stack.map(({ c, cells }) => (
              <Text color={colorOf(c.name, c.kind)}>{'█'.repeat(cells)}</Text>
            ))}
          </Box>
          <Text> </Text>
          {inWindow.map(c => {
            const share = c.tokens / Math.max(1, snap.max)
            return (
              <Box>
                <Text color={colorOf(c.name, c.kind)}>■ </Text>
                <Text dimColor={c.kind !== 'used'}>{padEnd(c.name, nameW)} </Text>
                <Text>{padStart(fmtK(c.tokens), 7)} </Text>
                <Text dimColor>{padStart(`${Math.round(share * 100)}%`, 4)} </Text>
                <Text color={colorOf(c.name, c.kind)}>{bar(share, barW)}</Text>
              </Box>
            )
          })}
          {deferred.length > 0 && (
            <Box flexDirection="column" marginTop={1}>
              {deferred.map(c => (
                <Text dimColor>
                  ◌ {padEnd(c.name, nameW)} {padStart(fmtK(c.tokens), 7)} (outside the window, loads on demand)
                </Text>
              ))}
            </Box>
          )}
          <Text dimColor>
            {'\n'}measured {fmtTime(snap.at)} · {fmtK(snap.total)} of {fmtK(snap.max)} ({snap.percent}%)
          </Text>
        </Box>
      )
    }

    // ------------------------------------------------ timeline view
    function drawTimeline() {
      if (list.length === 0) return <Text dimColor>No measurements yet. Points appear after each turn.</Text>
      const maxTokens = Math.max(window, ...list.map(s => s.tokens))
      const sparkWidth = width - 2
      const sparkPoints = list.slice(-sparkWidth)
      const barW = Math.max(8, Math.min(24, Math.floor(width * 0.25)))
      const rowsRoom = Math.max(10, (e.viewport?.rows ?? 40) - 8)
      const shown = list.slice(-rowsRoom)
      const firstIndex = list.length - shown.length
      const labelW = Math.max(10, width - 8 - 1 - barW - 1 - 7 - 1 - 7 - 1 - 4 - 2)
      return (
        <Box flexDirection="column">
          <Text color={fillColor(pctNow)}>{sparkline(sparkPoints.map(s => s.tokens), maxTokens)}</Text>
          <Text dimColor>
            {padEnd('time', 8)} {padEnd('fill', barW)} {padStart('tokens', 7)} {padStart('delta', 7)} {padEnd('turn', 4)}  what happened
          </Text>
          {shown.map((s, i) => {
            const before = list[firstIndex + i - 1]
            const delta = before ? s.tokens - before.tokens : 0
            const dc = deltaColor(delta, window)
            const isSpike = dc === SPIKE_RED
            const parts: string[] = []
            if (s.kind === 'compact') parts.push(`compacted from ${fmtK(s.fromTokens ?? 0)}`)
            if (s.skills.length) parts.push(`/${s.skills.join(' /')}`)
            if (Object.keys(s.tools).length) parts.push(toolsLabel(s.tools))
            if (s.durationMs !== undefined) parts.push(fmtMs(s.durationMs))
            const label = parts.join(' · ')
            return (
              <Box>
                <Text dimColor>{fmtTime(s.t)} </Text>
                <Text color={fillColor(s.percent)}>{bar(s.tokens / Math.max(1, maxTokens), barW)} </Text>
                <Text bold={isSpike}>{padStart(fmtK(s.tokens), 7)} </Text>
                <Text color={dc} dimColor={dc === undefined} bold={isSpike}>
                  {padStart(before ? fmtDelta(delta) : '', 7)}{' '}
                </Text>
                <Text dimColor>{padEnd(s.kind === 'compact' ? 'cmp' : `T${s.turn}`, 4)}  </Text>
                <Text color={s.kind === 'compact' ? COMPACT_BLUE : isSpike ? SPIKE_RED : undefined} wrap="truncate-end">
                  {isSpike ? '▲ ' : ''}{label.slice(0, labelW)}
                </Text>
              </Box>
            )
          })}
        </Box>
      )
    }

    // ------------------------------------------------ skills view
    function drawSkills() {
      if (events.length === 0) return <Text dimColor>No skill invoked yet.</Text>
      type Agg = { skill: string; runs: number; prompt: number; growth: number; max: number; last: number }
      const byName = new Map<string, Agg>()
      for (const ev of events) {
        const growth = ev.tokensAfter === null ? 0 : ev.tokensAfter - ev.tokensBefore
        const a = byName.get(ev.skill) ?? { skill: ev.skill, runs: 0, prompt: 0, growth: 0, max: 0, last: 0 }
        a.runs += 1
        a.prompt += ev.promptTokens
        a.growth += growth
        a.max = Math.max(a.max, growth)
        a.last = Math.max(a.last, ev.t)
        byName.set(ev.skill, a)
      }
      const aggs = [...byName.values()].sort((a, b) => b.growth - a.growth)
      const nameW = Math.min(24, Math.max(6, ...aggs.map(a => a.skill.length)))
      const maxGrowth = Math.max(1, ...aggs.map(a => a.growth))
      const barW = Math.max(8, Math.min(20, width - nameW - 40))
      const recent = events.slice(-Math.max(5, (e.viewport?.rows ?? 40) - aggs.length - 12)).reverse()
      return (
        <Box flexDirection="column">
          <Text dimColor>
            {padEnd('skill', nameW)} {padStart('runs', 4)} {padStart('prompt≈', 8)} {padStart('growth', 8)} {padStart('max', 7)}  share of growth
          </Text>
          {aggs.map(a => (
            <Box>
              <Text bold>{padEnd(a.skill, nameW)} </Text>
              <Text>{padStart(String(a.runs), 4)} </Text>
              <Text dimColor>{padStart(fmtK(a.prompt), 8)} </Text>
              <Text color={deltaColor(a.max, window)}>{padStart(fmtDelta(a.growth), 8)} </Text>
              <Text color={deltaColor(a.max, window)}>{padStart(fmtDelta(a.max), 7)}  </Text>
              <Text color={deltaColor(a.max, window) ?? CALM_GREEN}>{bar(a.growth / maxGrowth, barW)}</Text>
            </Box>
          ))}
          <Text> </Text>
          <Text dimColor>recent invocations (turn growth = context after the turn minus before the skill ran)</Text>
          {recent.map(ev => {
            const growth = ev.tokensAfter === null ? null : ev.tokensAfter - ev.tokensBefore
            const dc = growth === null ? undefined : deltaColor(growth, window)
            return (
              <Box>
                <Text dimColor>{fmtTime(ev.t)} </Text>
                <Text dimColor>T{String(ev.turn).padEnd(3)} </Text>
                <Text bold>{padEnd('/' + ev.skill, nameW + 1)} </Text>
                <Text dimColor>prompt≈{padStart(fmtK(ev.promptTokens), 6)} </Text>
                <Text color={dc} dimColor={dc === undefined}>
                  {growth === null ? 'pending' : `turn ${fmtDelta(growth)}`}
                </Text>
                <Text dimColor> ({fmtK(ev.tokensBefore)} → {ev.tokensAfter === null ? '…' : fmtK(ev.tokensAfter)})</Text>
              </Box>
            )
          })}
        </Box>
      )
    }
  })
}
