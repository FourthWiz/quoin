import type { EngineInterface, Register } from 'claude-code'

// The mod side of the task record and the isolation claim. All bookkeeping
// lives in core/scripts/task_record.py, which the settings hooks also call where
// mods cannot load; this module marks its sessions so those hooks stand aside,
// opens the record on the first edit, journals each main-loop turn, names the
// task with one small-model call and adds /quoin-task and /quoin-skip.
//
// Before an edit lands in a working tree this session has not claimed, it
// claims the tree through core/scripts/run_isolation.py. When another live
// session holds it, the person chooses between a worktree and staying; a
// headless run takes the worktree. Taking the worktree refuses the edit with a
// reason that tells the model to move with EnterWorktree, so the other
// session's tree is never touched. Claims go at session end, and a killed
// session's claim stops counting once its process is gone.

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
    return started
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
