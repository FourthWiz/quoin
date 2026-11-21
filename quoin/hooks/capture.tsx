import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { InboxEntry } from '../types'

// The mod side of the task record, the isolation claim and the harvest. The
// bookkeeping lives in core/scripts (task_record.py, run_isolation.py,
// harvest.py), which the settings hooks also call where mods cannot load; this
// module marks its sessions so those hooks stand aside.
//
// Record: opens the task record on the first edit, journals each main-loop
// turn, names the task with one small-model call, and adds /quoin-task,
// /quoin-skip and /quoin-done.
//
// Claim: before an edit lands in a working tree this session has not claimed,
// it claims the tree. When another live session holds it, the person chooses
// between a worktree and staying; a headless run takes the worktree, and the
// module moves the session there itself and applies the edit to the worktree's
// copy. Claims go at session end and stop counting once their process is gone.
//
// Harvest: one question for lessons and instruction edits, forked over the
// conversation at compaction and after a PR is opened, and asked of the
// transcripts of earlier sessions that ended without it at session start, on a
// small model. Answers land in the inbox; /quoin-inbox accepts or rejects them.

const EDIT_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit'])
const NAME_PROMPT =
  'Name this coding task in three to six plain words, no punctuation, no quotes. Reply with the name only.\n\nTask request:\n'

type TaskState = { kind?: string; named?: boolean; name?: string } | null

function script($: EngineInterface): string {
  return `${$.plugin.root}/core/scripts/task_record.py`
}

// The system interpreter starts several times faster than a pyenv shim.
let python = 'python3'

async function pickPython($: EngineInterface): Promise<void> {
  const found = await $.fs.stat('/usr/bin/python3').catch(() => null)
  if (found && !('deny' in found)) python = '/usr/bin/python3'
}

type Claim = {
  mode: string | null
  reason: string
  tree: string | null
  branch: string | null
  blocking: { task?: string; branch?: string | null } | null
} | null

async function claimTree($: EngineInterface, path: string, shared: boolean): Promise<Claim> {
  const session = await $.session.id()
  const args = [python, `${$.plugin.root}/core/scripts/run_isolation.py`, 'claim-session', '--path', path, '--session-id', session]
  if (shared) args.push('--shared')
  const result = await $.process.run(args, { timeoutMs: 15000 })
  try {
    return JSON.parse(result.stdout) as Claim
  } catch {
    return null
  }
}

async function releaseClaims($: EngineInterface): Promise<void> {
  const session = await $.session.id()
  await $.process.run([python, `${$.plugin.root}/core/scripts/run_isolation.py`, 'release-session', '--session-id', session], {
    timeoutMs: 10000,
  })
}

async function worktreeName($: EngineInterface, claim: Claim): Promise<string> {
  const session = await $.session.id()
  const base = (claim?.branch ?? 'session').replace(/[^A-Za-z0-9._/-]+/g, '-').replace(/^[-/.]+|[-/.]+$/g, '')
  return `${base || 'session'}-${session.slice(0, 6)}`.slice(-64)
}

const INBOX_PANE = 'quoin-inbox'
const inbox = atom({ plugin: 'quoin', key: 'inbox' } as const, [] as InboxEntry[])
const inboxPick = atom({ plugin: 'quoin', key: 'inboxPick' } as const, null as number | null)
const inboxNote = atom({ plugin: 'quoin', key: 'inboxNote' } as const, null as string | null)

async function harvest($: EngineInterface, args: string[], stdin = ''): Promise<unknown> {
  const cwd = await $.session.cwd()
  const result = await $.process.run([python, `${$.plugin.root}/core/scripts/harvest.py`, ...args, '--cwd', cwd], {
    stdin,
    timeoutMs: 30000,
  })
  try {
    return JSON.parse(result.stdout)
  } catch {
    return null
  }
}

// Asked afresh each time: it lists what the inbox and lessons already hold.
async function question($: EngineInterface): Promise<string> {
  const cwd = await $.session.cwd()
  const result = await $.process.run([python, `${$.plugin.root}/core/scripts/harvest.py`, 'question', '--cwd', cwd], {
    timeoutMs: 10000,
  })
  return result.stdout.trim()
}

async function askOfTranscript($: EngineInterface, ask: string, path: string): Promise<string | null> {
  const text = await $.process.run([python, `${$.plugin.root}/core/scripts/harvest.py`, 'transcript-text', '--path', path], {
    timeoutMs: 30000,
  })
  const reply = await $.model.complete({
    model: 'haiku',
    system: 'You read the transcript of a Claude Code session and answer one question about it.',
    prompt: `${ask}\n\nTranscript:\n${text.stdout}`,
    timeoutMs: 90000,
  })
  return reply.isAnswered ? reply.text : null
}

async function forkHarvest($: EngineInterface, source: 'compaction' | 'pr'): Promise<void> {
  const ask = await question($)
  if (!ask) return
  const forked = await $.model.fork({ prompt: ask, effort: 'low' })
  let reply = forked.isAnswered ? forked.text : null
  // A fork needs a request this process already made; a resumed session that
  // compacts first has none, so ask of the transcript on a small model instead.
  if (reply === null) reply = await askOfTranscript($, ask, (await $.session.info()).transcriptPath)
  $.ui.log(`quoin: harvest at ${source}: ${forked.isAnswered ? 'forked' : `fork ${forked.reason}, transcript`}`, { to: 'debug' })
  if (reply === null) return
  const session = await $.session.id()
  const added = (await harvest($, ['add', '--session', session, '--source', source], reply)) as { added?: number[] } | null
  if (added?.added?.length) $.ui.log(`quoin: ${added.added.length} new inbox candidate(s); /quoin-inbox to review`)
}

async function deferredHarvest($: EngineInterface): Promise<void> {
  const info = await $.session.info()
  const dir = info.transcriptPath.slice(0, info.transcriptPath.lastIndexOf('/'))
  const session = await $.session.id()
  const due = (await harvest($, ['pending', '--transcript-dir', dir, '--current', session])) as
    | { session: string; path: string }[]
    | null
  if (!due?.length) return
  const ask = await question($)
  let total = 0
  for (const item of due) {
    const reply = await askOfTranscript($, ask, item.path)
    $.ui.log(`quoin: deferred harvest of ${item.session}: ${reply === null ? 'no answer' : 'answered'}`, { to: 'debug' })
    if (reply === null) continue
    const added = (await harvest($, ['add', '--session', item.session, '--source', 'deferred'], reply)) as {
      added?: number[]
    } | null
    total += added?.added?.length ?? 0
  }
  if (total) $.ui.log(`quoin: ${total} new inbox candidate(s) from earlier sessions; /quoin-inbox to review`)
}

async function loadInbox($: EngineInterface, keep: number | null): Promise<void> {
  const list = ((await harvest($, ['list'])) as InboxEntry[] | null) ?? []
  await update($, inbox, () => list)
  await update($, inboxPick, () => list.find(x => x.id === keep)?.id ?? list[0]?.id ?? null)
}

async function settle($: EngineInterface, action: 'accept' | 'reject'): Promise<void> {
  const pick = await read($, inboxPick)
  if (pick === null) return
  const out = (await harvest($, [action, '--id', String(pick)])) as { ok?: boolean; target?: string; error?: string } | null
  const note = out?.ok
    ? action === 'accept'
      ? `Accepted #${pick} into ${out.target ?? 'its file'}`
      : `Rejected #${pick}`
    : `Could not ${action} #${pick}: ${out?.error ?? 'no answer'}`
  await update($, inboxNote, () => note)
  await loadInbox($, null)
}

async function record($: EngineInterface, args: string[], body?: object): Promise<TaskState> {
  const session = await $.session.id()
  const cwd = await $.session.cwd()
  const result = await $.process.run([python, script($), ...args, '--cwd', cwd, '--session', session], {
    stdin: body ? JSON.stringify(body) : '',
    timeoutMs: 15000,
  })
  try {
    return JSON.parse(result.stdout) as TaskState
  } catch {
    return null
  }
}

async function markSession($: EngineInterface): Promise<void> {
  const session = await $.session.id()
  await $.process.run([python, script($), 'mark-mod', '--session', session], { timeoutMs: 10000 })
}

async function prompts($: EngineInterface): Promise<string[]> {
  const messages = await $.session.messages()
  if ('deny' in messages) return []
  return messages.filter(m => m.role === 'user' && m.text.trim() !== '').map(m => m.text.trim())
}

async function nameTask($: EngineInterface, request: string): Promise<void> {
  if (!request) return
  const reply = await $.model.complete({
    model: 'haiku',
    prompt: NAME_PROMPT + request.slice(0, 4000),
    timeoutMs: 15000,
  })
  if (!reply.isAnswered) return
  const text = (reply.text.split('\n')[0] ?? '').replace(/["'`.]/g, '').trim()
  if (text) await record($, ['rename', '--name', text])
}

export const register: Register = on => {
  let opened = false
  let turnFiles: string[] = []
  let interactive = true
  const claimed: string[] = []

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    interactive = e.isInteractive
    await pickPython($)
    await markSession($)
    await $.command.register({ name: 'quoin-task', description: 'Record this session as a quoin task: /quoin-task <name>' })
    await $.command.register({ name: 'quoin-skip', description: 'Turn off the quoin task record for this session' })
    await $.command.register({ name: 'quoin-done', description: 'Close this session\'s quoin task without a PR' })
    await $.command.register({ name: 'quoin-inbox', description: 'Review harvested lessons and instruction edits' })
    $.clock.after(0, () => {
      void deferredHarvest($).catch(() => undefined)
    })
    return started
  })

  on('command.run', { command: 'quoin-done' }, async $ => {
    const session = await $.session.id()
    const cwd = await $.session.cwd()
    const result = await $.process.run([python, script($), 'done', '--cwd', cwd, '--session', session], { timeoutMs: 15000 })
    let closed: { name?: string } = {}
    try {
      closed = JSON.parse(result.stdout)
    } catch {
      closed = {}
    }
    return { text: closed.name ? `Closed task "${closed.name}".` : 'No task to close for this session.' }
  })

  on('command.run', { command: 'quoin-inbox' }, async $ => {
    await update($, inboxNote, () => null)
    await loadInbox($, null)
    await $.ui.open({ id: INBOX_PANE, title: 'Quoin inbox', focus: true, closeOnEscape: true })
    const list = await read($, inbox)
    return { text: list.length ? `${list.length} pending candidate(s): a accepts, x rejects` : 'The inbox is empty.' }
  })

  on('ui.render', { component: 'Pane', requestId: INBOX_PANE }, async ($, e) => {
    const { Box, Text, Button, Select } = $.ui.resolve(e)
    const [list, pick, note] = await Promise.all([read($, inbox), read($, inboxPick), read($, inboxNote)])
    const entry = list.find(x => x.id === pick)
    const options = list.map(x => ({
      value: String(x.id),
      label: `#${x.id} ${x.kind.padEnd(11)} ${x.text.length > 70 ? x.text.slice(0, 69) + '…' : x.text}`,
    }))
    return (
      <Box flexDirection="column">
        <Text bold>Quoin inbox</Text>
        {note && <Text dimColor>{note}</Text>}
        {list.length === 0 && <Text dimColor>Nothing waiting. Candidates arrive from sessions as they compact, open a PR or end.</Text>}
        {list.length > 0 && (
          <Select
            key="inbox"
            autoFocus
            options={options}
            value={pick === null ? undefined : String(pick)}
            onSelect={value => update($, inboxPick, () => Number(value))}
          />
        )}
        {entry && (
          <Box flexDirection="column" marginTop={1}>
            <Text>{entry.text}</Text>
            {entry.happened !== '' && <Text dimColor>what happened: {entry.happened}</Text>}
            {entry.applies_to !== '' && <Text dimColor>applies to: {entry.applies_to}</Text>}
            <Text dimColor>
              goes to {entry.target} · task {entry.task} · from {entry.source}
            </Text>
          </Box>
        )}
        <Box gap={2} marginTop={1}>
          <Button key="accept" hotkey="a" variant="primary" label="Accept" dimColor={!entry} onPress={() => settle($, 'accept')} />
          <Button key="reject" hotkey="x" label="Reject" dimColor={!entry} onPress={() => settle($, 'reject')} />
          <Button key="refresh" hotkey="r" label="Refresh" onPress={() => loadInbox($, pick)} />
        </Box>
      </Box>
    )
  })

  on('session.compact', async ($, e, next) => {
    if (e.agentId === undefined) await forkHarvest($, 'compaction').catch(() => undefined)
    return next(e)
  })

  on('command.run', { command: 'quoin-task' }, async ($, e) => {
    const all = await prompts($)
    const state = await record($, ['task', '--name', e.args.trim()], { first_request: all[0] ?? '' })
    opened = true
    return { text: state?.name ? `Recording this session as task "${state.name}".` : 'Could not open the task record.' }
  })

  on('command.run', { command: 'quoin-skip' }, async $ => {
    await record($, ['skip'])
    return { text: 'Task record off for this session.' }
  })

  on('session.end', async ($, e, next) => {
    const ended = await next(e)
    await releaseClaims($)
    claimed.length = 0
    return ended
  })

  on('tool.call', async ($, e, next) => {
    if (String(e.tool) === 'Bash') {
      const command = (e as unknown as { command?: string }).command ?? ''
      if (!command.includes('gh pr create')) return next(e)
      const ran = await next(e)
      if (!('deny' in ran) && !ran.isError && /github\.com\/[^\s]+\/pull\/\d+/.test(ran.text ?? '')) {
        $.clock.after(0, () => {
          void forkHarvest($, 'pr').catch(() => undefined)
        })
      }
      return ran
    }
    if (!EDIT_TOOLS.has(String(e.tool))) return next(e)
    const input = e as unknown as { file_path?: string; notebook_path?: string }
    const file = input.file_path ?? input.notebook_path
    if (!file) return next(e)

    if (!claimed.some(tree => file === tree || file.startsWith(tree + '/'))) {
      let claim = await claimTree($, file, false)
      if (claim?.reason === 'conflict') {
        const holder = claim.blocking?.task ?? 'another session'
        let stay = false
        if (interactive) {
          const answer = await $.ui
            .ask(`Another live Claude Code session (${holder}) is editing ${claim.tree}. Where should this session work?`, {
              header: 'Checkout',
              options: ['Move to a worktree', 'Continue here'],
            })
            .catch(() => 'Move to a worktree')
          stay = answer === 'Continue here'
        }
        if (!stay) {
          const name = await worktreeName($, claim)
          const moved = await $.tool
            .call({ tool: 'EnterWorktree', name })
            .catch((err: unknown) => ({ deny: String(err) }))
          if ('deny' in moved || moved.isError) {
            const why = 'deny' in moved ? moved.deny : (moved.text ?? 'EnterWorktree failed')
            return {
              deny:
                `Another live Claude Code session (${holder}) is working in the checkout ${claim.tree}, so this edit ` +
                `was not made, and moving this session to its own worktree failed: ${why}`,
            }
          }
          const root = await $.session.cwd()
          const target = root + file.slice((claim.tree ?? '').length)
          const moveClaim = await claimTree($, target, false)
          if (moveClaim?.tree) claimed.push(moveClaim.tree)
          const rewritten = input.notebook_path !== undefined ? { ...e, notebook_path: target } : { ...e, file_path: target }
          const result = await next(rewritten as typeof e)
          if ('deny' in result) return result
          if (!result.isError && !turnFiles.includes(target)) turnFiles.push(target)
          if (!result.isError && !opened) {
            opened = true
            const all = await prompts($)
            await record($, ['open', '--file', target], { first_request: all[0] ?? '', edit_request: all.at(-1) ?? '' })
          }
          const note =
            `Another live Claude Code session (${holder}) is working in ${claim.tree}, so this session moved into its own ` +
            `git worktree at ${root} and this edit was applied to ${target} instead. Work only under ${root} from now on.`
          return { ...result, context: [...(result.context ?? []), note] }
        }
        claim = await claimTree($, file, true)
      }
      if (claim?.tree) claimed.push(claim.tree)
    }

    const result = await next(e)
    if ('deny' in result || result.isError) return result
    if (!turnFiles.includes(file)) turnFiles.push(file)
    if (!opened) {
      opened = true
      const all = await prompts($)
      await record($, ['open', '--file', file], { first_request: all[0] ?? '', edit_request: all.at(-1) ?? '' })
    }
    return result
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId !== undefined) return done
    const files = turnFiles
    turnFiles = []
    const state = await record($, ['journal'], { answer: e.answer, files })
    if (state?.kind && state.kind !== 'skip' && !state.named) {
      const all = await prompts($)
      await nameTask($, all.at(-1) ?? all[0] ?? '')
    }
    return done
  })
}
