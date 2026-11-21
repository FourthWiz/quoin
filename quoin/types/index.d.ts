/** One inbox candidate as the inbox pane shows it. */
export type InboxEntry = {
  id: number
  status: 'pending' | 'accepted' | 'rejected'
  kind: 'lesson' | 'instruction'
  task: string
  session: string
  source: string
  target: string
  happened: string
  applies_to: string
  text: string
}

declare module 'claude-code' {
  interface PluginState {
    quoin: {
      inbox: InboxEntry[]
      inboxPick: number | null
      inboxNote: string | null
    }
  }
}
