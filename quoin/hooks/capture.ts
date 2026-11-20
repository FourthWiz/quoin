import type { EngineInterface, Register } from 'claude-code'

// The mod side of the task record. All bookkeeping lives in
// core/scripts/task_record.py, which the settings hooks also call where mods
// cannot load; this module marks its sessions so those hooks stand aside, opens
// the record on the first edit, journals each main-loop turn, names the task
// with one small-model call and adds /quoin-task and /quoin-skip.

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

  on('session.start', async ($, e, next) => {
    const started = await next(e)
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

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    if (!EDIT_TOOLS.has(String(e.tool)) || 'deny' in result || result.isError) return result
    const input = e as unknown as { file_path?: string; notebook_path?: string }
    const file = input.file_path ?? input.notebook_path
    if (!file) return result
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
