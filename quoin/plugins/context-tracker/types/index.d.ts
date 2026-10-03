export type SampleKind = 'turn' | 'measure' | 'compact'

/** One point on the context timeline. */
export type Sample = {
  /** When it was taken, ms since the epoch. */
  t: number
  /** Context tokens the next request re-sends (input + cache read + cache write). */
  tokens: number
  /** `tokens` over the model's window, whole percent. */
  percent: number
  kind: SampleKind
  /** Main-thread turn number the sample belongs to (0 before the first). */
  turn: number
  /** Tool calls made in that turn, by tool name. */
  tools: Record<string, number>
  /** Skills invoked in that turn. */
  skills: string[]
  /** Wall-clock length of the turn, when known. */
  durationMs?: number
  /** For a compaction: the size it came down from. */
  fromTokens?: number
}

export type Category = {
  name: string
  tokens: number
  kind: 'used' | 'free' | 'buffer' | 'deferred'
}

export type Breakdown = {
  at: number
  total: number
  max: number
  percent: number
  categories: Category[]
}

/** One skill invocation and the growth attributed to it. */
export type SkillEvent = {
  t: number
  skill: string
  turn: number
  /** Estimated tokens of the skill's own prompt text. */
  promptTokens: number
  /** Context size at the last sample before the skill ran. */
  tokensBefore: number
  /** Context size at the first sample after it; null until measured. */
  tokensAfter: number | null
}

export type View = 'categories' | 'timeline' | 'skills'

declare module 'claude-code' {
  interface PluginState {
    'context-tracker': {
      samples: Sample[]
      breakdown: Breakdown | null
      skillEvents: SkillEvent[]
      view: View
    }
  }
}
