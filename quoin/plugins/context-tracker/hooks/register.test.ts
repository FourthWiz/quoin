import { describe, expect, mock, test } from 'claude-code/testing'

const PANE_PROPS = {
  title: 'Context',
  isFocused: false,
  bodyColumns: 100,
  placement: 'inline' as const,
  scroll: { offset: 0, bodyRows: 40 },
  view: {},
}

const usageWith = (tokens: number) => ({
  startedAt: 0,
  rateLimits: [],
  context: {
    tokens,
    window: 200_000,
    percent: Math.round((tokens / 200_000) * 100),
    breakdown: {
      categories: [
        { name: 'System prompt', tokens: 12_000, color: 'promptBorder', isDeferred: false, kind: 'used' as const },
        { name: 'Messages', tokens: tokens - 12_000, color: 'inactive', isDeferred: false, kind: 'used' as const },
        { name: 'Free space', tokens: 200_000 - tokens, color: 'inactive', isDeferred: false, kind: 'free' as const },
      ],
      totalTokens: tokens,
      maxTokens: 200_000,
      rawMaxTokens: 200_000,
      autocompactSource: 'auto' as const,
      percentage: Math.round((tokens / 200_000) * 100),
      gridRows: [],
      model: 'test-model',
      memoryFiles: [],
      mcpTools: [],
      agents: [],
      isAutoCompactEnabled: true,
      apiUsage: null,
    },
  },
})

describe('context-tracker', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`records measurements, skills and draws every view on ${surface}`, async ($, on) => {
      mock.clock(on, { now: 1_700_000_000_000 })
      let tokens = 50_000
      on('session.usage', () => ({ value: usageWith(tokens) }))
      on('session.measure', (_, e) => ({ changed: e.changed }))
      on('skill.prompt', (_, e) => ({ text: e.text }))
      on('turn.complete', (_, e) => ({ text: e.answer }))

      // First measurement: 50k in the window.
      await $.session.measure({
        context: { tokens, window: 200_000, percent: 25 },
        rateLimits: [],
        changed: ['context'],
      })

      // A skill runs, then the turn ends 30k heavier: a spike attributed to it.
      await $.skill.prompt({ skill: 'plan', text: 'x'.repeat(8_000) })
      tokens = 80_000
      await $.turn.complete({
        turnId: 't1',
        answer: 'done',
        durationMs: 12_000,
        isAborted: false,
        reason: 'answer',
        usage: {
          model: 'test-model',
          input_tokens: 1_000,
          output_tokens: 300,
          cache_read_input_tokens: 70_000,
          cache_creation_input_tokens: 9_000,
        },
      })
      // The measurement right after the turn repeats its figure: no duplicate point.
      await $.session.measure({
        context: { tokens, window: 200_000, percent: 40 },
        rateLimits: [],
        changed: ['context'],
      })

      const categories = await $.ui.mount({
        plugin: 'context-tracker',
        surface,
        component: 'Pane',
        requestId: 'context-tracker',
        props: PANE_PROPS,
        viewport: { columns: 100, rows: 40 },
      })
      expect(await categories.find({ type: 'Text', text: /80\.0k/ })).toBeDefined()
      expect(await categories.find({ type: 'Text', text: /2 pts/ })).toBeDefined()
      expect(await categories.find({ type: 'Text', text: /System prompt/ })).toBeDefined()

      await categories.press({ key: 'view-timeline' })
      expect(await categories.find({ type: 'Text', text: /\+30\.0k/ })).toBeDefined()
      expect(await categories.find({ type: 'Text', text: /\/plan/ })).toBeDefined()

      await categories.press({ key: 'view-skills' })
      expect(await categories.find({ type: 'Text', text: /turn \+30\.0k/ })).toBeDefined()
      expect(await categories.find({ type: 'Text', text: /prompt≈/ })).toBeDefined()
    })
  }
})
