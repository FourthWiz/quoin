import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { TaskRow } from '../types'
import { findProjectRoot, loadRows, relativeAge, sessionContext } from './tasks.ts'
import type { Entry, Fs } from './tasks.ts'

const PANE = 'quoin-tasks'
const TITLE = 'Quoin tasks'

const rows = atom({ plugin: 'workflow-tasks', key: 'rows' } as const, [] as TaskRow[])
const root = atom({ plugin: 'workflow-tasks', key: 'root' } as const, null as string | null)
const selected = atom({ plugin: 'workflow-tasks', key: 'selected' } as const, null as string | null)
const error = atom({ plugin: 'workflow-tasks', key: 'error' } as const, null as string | null)

const storeKey = (projectRoot: string) => `selected:${projectRoot}`

/** Gives the pure logic read access to the project through the engine's file calls. */
const adaptFs = ($: EngineInterface): Fs => ({
  list: async path => (await $.fs.list(path)) as Entry[],
  read: async path => {
    const text = await $.fs.read(path)
    return typeof text === 'string' ? text : ''
  },
  exists: path => $.fs.exists(path),
})

const statusLine = (row: TaskRow | undefined) =>
  row ? `quoin ${row.arg}: ${row.stage.short} -> ${row.next.command ?? 'no command'}` : undefined

/**
 * Looks for the project from the session's directory and lists its tasks. The
 * stored selection is restored when that task still exists, else the first row.
 */
async function scan($: EngineInterface, keep: string | null): Promise<void> {
  const cwd = await $.session.cwd()
  const fs = adaptFs($)
  let found: string | null
  try {
    found = await findProjectRoot(fs, cwd)
  } catch {
    found = null
  }
  if (!found) {
    await update($, root, () => null)
    await update($, rows, () => [])
    await update($, selected, () => null)
    await update($, error, () => `No quoin project found above ${cwd}`)
    $.ui.status(undefined)
    return
  }
  let list: TaskRow[] = []
  let failure: string | null = null
  try {
    list = await loadRows(fs, found)
  } catch (err) {
    failure = `Could not read ${found}/.workflow_artifacts: ${err instanceof Error ? err.message : String(err)}`
  }
  const stored = await $.store.get(storeKey(found))
  const wanted = keep ?? (typeof stored === 'string' ? stored : null)
  const pick = list.find(r => r.arg === wanted)?.arg ?? list[0]?.arg ?? null
  await update($, root, () => found)
  await update($, rows, () => list)
  await update($, selected, () => pick)
  await update($, error, () => failure)
  $.ui.status(statusLine(list.find(r => r.arg === pick)))
}

async function choose($: EngineInterface, value: string): Promise<void> {
  await update($, selected, () => value)
  const [projectRoot, list] = await Promise.all([read($, root), read($, rows)])
  if (projectRoot) await $.store.set(storeKey(projectRoot), value)
  $.ui.status(statusLine(list.find(r => r.arg === value)))
}

/** Puts the row's command in the prompt box. It is never sent; the person presses Enter. */
async function fillPrompt($: EngineInterface, row: TaskRow | undefined): Promise<void> {
  if (!row) return
  const command = row.next.command
  if (!command) {
    $.ui.toast(row.next.note ?? 'No command to fill for this task')
    return
  }
  const result = await $.prompt.fill({ text: command, mode: 'replace' })
  if (result.isFilled) {
    // Close the pane so the keys return to the prompt box.
    await $.ui.close({ id: PANE })
    return
  }
  const why =
    result.refusal === 'dialog'
      ? 'a dialog holds the keys'
      : result.refusal === 'no_composer'
        ? 'no prompt box here'
        : 'the prompt box did not take it'
  $.ui.toast(`Could not fill the prompt (${why}): ${command}`)
}

const padEnd = (s: string, n: number) => (s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length))

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'quoin-tasks',
      description: 'List quoin tasks and fill the next workflow command',
    })
    return next(e)
  })

  on('command.run', { command: 'quoin-tasks' }, async $ => {
    await scan($, null)
    await $.ui.open({ id: PANE, title: TITLE, focus: true, closeOnEscape: true })
    const [projectRoot, list, problem] = await Promise.all([read($, root), read($, rows), read($, error)])
    if (!projectRoot) return { text: problem ?? 'No quoin project found above the working directory' }
    if (problem) return { text: problem }
    return {
      text: `${list.length} active tasks in ${projectRoot}; pick a task, press f to fill its next command`,
    }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Select } = $.ui.resolve(e)
    const [list, projectRoot, current, problem] = await Promise.all([
      read($, rows),
      read($, root),
      read($, selected),
      read($, error),
    ])
    const now = await $.clock.now()
    const row = list.find(r => r.arg === current)

    let lastSession: string | null = null
    if (row && projectRoot) {
      try {
        lastSession = await sessionContext(adaptFs($), projectRoot, row.arg)
      } catch {
        lastSession = null
      }
    }

    const nameWidth = Math.min(40, Math.max(12, ...list.map(r => r.depth * 2 + r.name.length)))
    const options = list.map(r => ({
      value: r.arg,
      label: `${padEnd('  '.repeat(r.depth) + r.name, nameWidth)}  ${padEnd(r.stage.short, 12)} ${relativeAge(r.activity, now)}`,
    }))

    const command = row?.next.command ?? null
    return (
      <Box flexDirection="column">
        <Box>
          <Text bold>Quoin tasks </Text>
          <Text dimColor>{projectRoot ?? ''}</Text>
        </Box>
        {problem && <Text dimColor>{problem}</Text>}
        {!problem && list.length === 0 && <Text dimColor>No active tasks.</Text>}
        {list.length > 0 && (
          <Select
            key="tasks"
            autoFocus
            options={options}
            value={current ?? undefined}
            onSelect={value => choose($, value)}
          />
        )}
        {row && (
          <Box flexDirection="column" marginTop={1}>
            <Text>
              <Text bold>{row.arg}</Text>
              <Text dimColor>  {row.kind === 'program' ? 'program' : 'task'}</Text>
            </Text>
            <Text>stage: {row.stage.label}</Text>
            {row.stage.stage !== null && row.stage.multi && (
              <Text dimColor>
                stage {row.stage.stage}
                {row.stage.stageCount !== null ? ` of ${row.stage.stageCount}` : ''}
              </Text>
            )}
            {row.stage.gate && (
              <Text dimColor>
                gate: {row.stage.gate.name} ({row.stage.gate.verdict})
              </Text>
            )}
            {row.stage.run && (
              <Box flexDirection="column">
                <Text dimColor>
                  run: {row.stage.run.phase}
                  {row.stage.run.subphase ? `/${row.stage.run.subphase}` : ''} {row.stage.run.step}
                </Text>
                {row.stage.run.nextAction !== '' && <Text dimColor>next action: {row.stage.run.nextAction}</Text>}
                <Text dimColor>
                  profile: {row.stage.run.profile || '-'} · updated {row.stage.run.updatedAt || '-'}
                </Text>
                {row.stage.runStale && <Text color="#e0af68">run record is older than the artifacts</Text>}
                {row.stage.artifact && <Text dimColor>artifacts say: {row.stage.artifact.label}</Text>}
              </Box>
            )}
            <Text>next: {command ?? '(none)'}</Text>
            {row.next.then && <Text dimColor>{row.next.then}</Text>}
            {row.next.note && <Text dimColor>{row.next.note}</Text>}
            {lastSession && <Text dimColor>last session: {lastSession}</Text>}
          </Box>
        )}
        <Box gap={2} marginTop={1}>
          <Button
            key="fill"
            hotkey="f"
            variant="primary"
            label="Fill prompt"
            dimColor={command === null}
            onPress={() => fillPrompt($, row)}
          />
          <Button key="refresh" hotkey="r" label="Refresh" onPress={() => scan($, current)} />
        </Box>
      </Box>
    )
  })
}
